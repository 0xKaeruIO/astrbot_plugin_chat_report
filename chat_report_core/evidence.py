"""证据组装、落盘、合并转发，以及「先留证再处罚」流程。"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, Image, Plain, Record, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .constants import (
    PLUGIN_NAME,
    MAX_FORWARD_NODES,
    MAX_FORWARD_MEDIA_ITEMS,
    MAX_FORWARD_MEDIA_CHARS,
    MEDIA_CONVERT_TIMEOUT,
    DEFAULT_WARN_TEMPLATE,
    DEFAULT_EVIDENCE_TEMPLATE,
    _SEG_LABELS,
)

from .utils import (
    _render,
    _fmt_duration,
    EvidenceDeliveryError,
)


class EvidenceMixin:
    """证据组装、落盘、合并转发，以及「先留证再处罚」流程。"""

    def _mark_acted(self, message_id: str) -> None:
        """记录已处理消息 ID，供自动审查去重。"""
        mid = str(message_id or "").strip()
        if mid and mid not in self._acted_message_ids:
            self._acted_message_ids.append(mid)

    def _was_acted(self, message_id: str) -> bool:
        """判断消息 ID 是否已被举报/自动审查处理过。"""
        return str(message_id or "").strip() in self._acted_message_ids

    async def _evidence_targets(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        group_id: str,
        *,
        include_owner: bool = False,
        include_receivers: bool = False,
    ) -> list[str]:
        """「指定的管理员」：evidence_admin_ids → 默认指定用户 → bot 管理员 → 群管理员。

        include_owner / include_receivers 用于「机器人不是群管理员」时的兜底：
        证据同时发给群主与常规通知对象，由人工处理。
        """
        try:
            bot_id = str(event.get_self_id())
        except Exception:
            bot_id = ""
        ordered: list[str] = []

        def add(values: Any) -> None:
            """把合法 QQ 追加到有序证据接收列表，跳过机器人与重复项。"""
            for uid in self._qq_list(values):
                if uid and uid != bot_id and uid not in ordered:
                    ordered.append(uid)

        add(conf.get("evidence_admin_ids"))
        if include_owner:
            add(await self._group_owner_ids(event, group_id))
        if include_receivers:
            add(conf.get("receiver_ids"))
            add(self._bot_admins(getattr(event, "unified_msg_origin", None)))
            if conf.get("auto_include_group_admins"):
                add(sorted(await self._group_admin_ids(event, group_id)))
        if not ordered:
            add(conf.get("receiver_ids"))
        if not ordered:
            add(self._bot_admins(getattr(event, "unified_msg_origin", None)))
        if not ordered and conf.get("auto_include_group_admins"):
            add(sorted(await self._group_admin_ids(event, group_id)))
        return ordered

    def _build_evidence(
        self, ctx: dict[str, Any], target_id: str, message_id: str
    ) -> dict[str, Any]:
        """组装证据 JSON：消息、上下文、AI 结论与后续动作。"""
        return {
            "report_id": ctx.get("report_id", ""),
            "group_id": ctx.get("group_id", ""),
            "group_name": ctx.get("group_name", ""),
            "target": ctx.get("target", ""),
            "target_id": target_id,
            "reporter": ctx.get("reporter", ""),
            "reporter_id": ctx.get("reporter_id", ""),
            "message": ctx.get("message", ""),
            "message_id": message_id,
            "occur_time": ctx.get("occur_time", ""),
            "participants": ctx.get("participants", ""),
            "ai_judgement": ctx.get("ai_judgement", ""),
            "ai_confidence": ctx.get("ai_confidence", ""),
            "context_conclusion": ctx.get("context_conclusion", ""),
            "cause": ctx.get("cause", ""),
            "context_count": ctx.get("context_count", 0),
            "context_log": ctx.get("context_log", ""),
            "mute_duration": ctx.get("mute_duration", ""),
            "action_plan": ctx.get("action_plan", ""),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _save_evidence_file(self, evidence: dict[str, Any]) -> str:
        """把证据写到插件数据目录 evidence/<举报ID>.json，返回文件路径。"""
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            path = self.evidence_dir / f"{evidence.get('report_id') or 'unknown'}.json"
            self._atomic_write(path, evidence)
            return str(path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 证据文件写入失败：{e}")
            return ""

    @staticmethod
    def _event_self_id(event: AstrMessageEvent) -> str:
        """安全读取机器人自身 QQ；失败返回空串。"""
        try:
            return str(event.get_self_id())
        except Exception:
            return ""

    async def _build_forward_nodes(
        self,
        context_items: list[dict[str, Any]],
        *,
        title: str,
        summary_text: str,
        self_id: str = "",
    ) -> list[dict[str, Any]]:
        """构造 QQ「聊天记录」合并转发节点：1 条摘要 + 每条上下文消息 1 条。

        含图片/语音的消息会把原始媒体转换成 base64 放入节点；受
        MAX_FORWARD_MEDIA_ITEMS / MAX_FORWARD_MEDIA_CHARS 限制，超限或转换失败时
        退回 [图片]/[语音] 占位文本。

        Args:
            context_items: 上下文条目（含 text/sender_id/sender_name/is_target，
                可能含 components/raw_segments）。
            title: 摘要节点标题，例如「举报证据-xxx」「自动审查-xxx」。
            summary_text: 摘要正文。
            self_id: 机器人 QQ，用作摘要节点 uin；缺失时用 0。
        """
        default_uin: Any = int(self_id) if str(self_id).isdigit() else 0
        default_user = str(self_id or default_uin)
        nodes: list[dict[str, Any]] = [
            {
                "type": "node",
                "data": {
                    "name": (title or "举报记录")[:32],
                    "nickname": (title or "举报记录")[:32],
                    "uin": default_uin,
                    "user_id": default_user,
                    "content": [
                        {
                            "type": "text",
                            "data": {"text": summary_text or "举报记录"},
                        }
                    ],
                },
            }
        ]
        items = list(context_items or [])[-(MAX_FORWARD_NODES - 1):]
        media_budget = {
            "items": MAX_FORWARD_MEDIA_ITEMS,
            "chars": MAX_FORWARD_MEDIA_CHARS,
        }
        for item in items:
            content = await self._forward_item_content(item, media_budget)
            if not content:
                continue
            if item.get("is_target"):
                content.insert(
                    0, {"type": "text", "data": {"text": "【被举报】"}}
                )
            sender_id = str(item.get("sender_id") or "")
            name = str(item.get("sender_name") or sender_id or "群成员")
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": name[:32],
                        "nickname": name[:32],
                        "uin": int(sender_id)
                        if sender_id.isdigit()
                        else default_uin,
                        "user_id": sender_id or default_user,
                        "content": content,
                    },
                }
            )
        return nodes

    async def _forward_item_content(
        self, item: dict[str, Any], media_budget: dict[str, int]
    ) -> list[dict[str, Any]]:
        """把一条缓存消息转成合并转发节点内容，尽量保留原始图片/语音。"""
        components = item.get("components") or []
        raw_segments = item.get("raw_segments") or []
        parts: list[dict[str, Any]] = []
        if components:
            for comp in components:
                if isinstance(comp, Plain):
                    text = str(getattr(comp, "text", "") or "")
                    if text:
                        parts.append({"type": "text", "data": {"text": text}})
                elif isinstance(comp, At):
                    qq = str(getattr(comp, "qq", "") or "")
                    label = "@全体成员" if qq == "all" else f"@{getattr(comp, 'name', '') or qq}"
                    parts.append({"type": "text", "data": {"text": label}})
                elif isinstance(comp, Reply):
                    continue
                elif isinstance(comp, (Image, Record)):
                    parts.append(
                        await self._media_forward_segment(comp, media_budget)
                    )
                else:
                    label = _SEG_LABELS.get(self._seg_key(comp), "[消息]")
                    parts.append({"type": "text", "data": {"text": label}})
        elif raw_segments:
            for seg in raw_segments:
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type") or "")
                data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
                if seg_type == "text":
                    text = str(data.get("text") or "")
                    if text:
                        parts.append({"type": "text", "data": {"text": text}})
                elif seg_type == "at":
                    qq = str(data.get("qq") or "")
                    label = "@全体成员" if qq == "all" else f"@{data.get('name') or qq}"
                    parts.append({"type": "text", "data": {"text": label}})
                elif seg_type == "reply":
                    continue
                elif seg_type in ("image", "record"):
                    source = (
                        data.get("url") or data.get("file") or data.get("path") or ""
                    )
                    if source:
                        comp = (
                            Image(file=str(source))
                            if seg_type == "image"
                            else Record(file=str(source))
                        )
                        parts.append(
                            await self._media_forward_segment(comp, media_budget)
                        )
                    else:
                        parts.append(
                            {
                                "type": "text",
                                "data": {
                                    "text": "[图片]" if seg_type == "image" else "[语音]"
                                },
                            }
                        )
                else:
                    parts.append(
                        {
                            "type": "text",
                            "data": {"text": _SEG_LABELS.get(seg_type, "[消息]")},
                        }
                    )
        else:
            text = str(item.get("text") or "").strip()
            if text:
                parts.append({"type": "text", "data": {"text": text}})
        if not parts:
            text = str(item.get("text") or "").strip()
            if text:
                parts.append({"type": "text", "data": {"text": text}})
        return parts

    async def _media_forward_segment(
        self, comp: Any, media_budget: dict[str, int]
    ) -> dict[str, Any]:
        """把一个 Image/Record 转成 base64 消息段；超限时退回 URL 或占位文本。"""
        kind = "image" if isinstance(comp, Image) else "record"
        label = "[图片]" if kind == "image" else "[语音]"
        if media_budget["items"] <= 0:
            return {"type": "text", "data": {"text": label}}
        media_budget["items"] -= 1
        b64 = ""
        try:
            b64 = await asyncio.wait_for(
                comp.convert_to_base64(), timeout=MEDIA_CONVERT_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 合并转发媒体转换失败：{e}")
        if b64 and len(b64) <= media_budget["chars"]:
            media_budget["chars"] -= len(b64)
            return {"type": kind, "data": {"file": f"base64://{b64}"}}
        source = (
            getattr(comp, "url", None)
            or getattr(comp, "file", None)
            or getattr(comp, "path", None)
        )
        if isinstance(source, str) and source.startswith(
            ("http://", "https://", "file://")
        ):
            return {"type": kind, "data": {"file": source}}
        return {"type": "text", "data": {"text": label}}

    async def _send_private_forward_nodes(
        self,
        event: AstrMessageEvent,
        user_id: str,
        group_id: str,
        nodes: list[dict[str, Any]],
    ) -> bool:
        """私聊发送 QQ 合并转发聊天记录。

        依次尝试「带来源群的临时会话 → 普通私聊 → 通用 send_forward_msg」，
        全部失败返回 False，由调用方退回纯文本。

        Args:
            user_id: 接收人 QQ。
            group_id: 来源群号，仅用于临时会话；空串表示普通私聊。
            nodes: _build_forward_nodes 生成的节点列表。
        """
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None) if bot else None
        if not bot or api is None or not str(user_id).strip().isdigit() or not nodes:
            return False
        uid = int(user_id)
        gid = int(group_id) if str(group_id).strip().isdigit() else 0
        attempts: list[tuple[str, Any]] = []
        if gid:
            attempts.append(
                (
                    "群临时会话合并转发",
                    lambda: api.call_action(
                        "send_private_forward_msg",
                        user_id=uid,
                        group_id=gid,
                        messages=nodes,
                    ),
                )
            )
        attempts.append(
            (
                "私聊合并转发",
                lambda: api.call_action(
                    "send_private_forward_msg", user_id=uid, messages=nodes
                ),
            )
        )
        attempts.append(
            (
                "通用合并转发",
                lambda: api.call_action(
                    "send_forward_msg",
                    message_type="private",
                    user_id=uid,
                    messages=nodes,
                ),
            )
        )
        for label, fn in attempts:
            try:
                await asyncio.wait_for(fn(), timeout=8)
                return True
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] {label}给 {user_id} 失败：{e}")
        return False

    async def _deliver_evidence(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        ctx: dict[str, Any],
        targets: list[str] | None = None,
        context_items: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """把证据作为独立聊天记录私聊给指定管理员，返回成功送达的 QQ 列表。"""
        group_id = str(ctx.get("group_id") or "")
        if targets is None:
            targets = await self._evidence_targets(event, conf, group_id)
        if not targets:
            logger.warning(f"[{PLUGIN_NAME}] 未配置任何证据接收管理员，无法留存证据")
            return []
        tpl = self._c("evidence_template", DEFAULT_EVIDENCE_TEMPLATE)
        text = _render(tpl, ctx)
        nodes: list[dict[str, Any]] = []
        if context_items and bool(conf.get("evidence_use_forward", True)):
            summary_ctx = dict(ctx)
            summary_ctx["context_log"] = "（见本条聊天记录中的上下文消息）"
            nodes = await self._build_forward_nodes(
                context_items,
                title=f"举报证据-{ctx.get('report_id', '')}",
                summary_text=_render(tpl, summary_ctx),
                self_id=self._event_self_id(event),
            )
        sent: list[str] = []
        for uid in targets:
            if nodes and await self._send_private_forward_nodes(
                event, uid, group_id, nodes
            ):
                sent.append(uid)
                continue
            if await self._send_private(event, uid, text, group_id):
                sent.append(uid)
        logger.info(
            f"[{PLUGIN_NAME}] 证据 {ctx.get('report_id', '')} 送达管理员：{sent}（目标：{targets}）"
        )
        return sent

    async def _punish_with_evidence(
        self,
        event: AiocqhttpMessageEvent,
        conf: dict[str, Any],
        ctx: dict[str, Any],
        message_id: str,
        target_id: str,
        *,
        context_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """先留存并私聊转发证据，送达后才撤回 + 禁言 + 警告。"""
        group_id = str(ctx.get("group_id") or "")
        mute_duration = int(conf.get("mute_duration", 600))

        # 1. 证据：组装（补全消息原文）+ 落盘 + 私聊转发给指定管理员
        evidence_ctx = dict(ctx)
        evidence_ctx["action_plan"] = f"撤回该消息 + 禁言 {_fmt_duration(mute_duration)}"
        message_text = str(evidence_ctx.get("message") or "").strip()
        if not message_text:
            message_text = self._message_text_by_id(group_id, message_id)
        if not message_text:
            fetched = await self._fetch_message(event, message_id)
            message_text = str(fetched.get("text") or "")
        evidence_ctx["message"] = message_text or "[消息内容未获取到]"
        evidence_ctx["message_id"] = str(message_id)
        evidence = self._build_evidence(evidence_ctx, target_id, message_id)
        evidence_file = ""
        if conf.get("evidence_keep_files", True):
            evidence_file = self._save_evidence_file(evidence)
        sent_to = await self._deliver_evidence(
            event, conf, evidence_ctx, context_items=context_items
        )
        if not sent_to and conf.get("evidence_required", True):
            raise EvidenceDeliveryError("证据未能私聊送达任何指定管理员")

        # 2. 撤回（失败不阻断禁言，但要记审计）
        recall_ok = True
        try:
            await event.bot.delete_msg(message_id=int(message_id))
        except Exception as e:  # noqa: BLE001
            recall_ok = False
            logger.warning(f"[{PLUGIN_NAME}] 撤回消息 {message_id} 失败：{e}")

        # 3. 禁言
        await event.bot.set_group_ban(
            group_id=int(group_id), user_id=int(target_id), duration=mute_duration
        )
        self._mark_acted(message_id)

        # 4. 警告计数 +1
        new_warn_count = self._incr_warn_count(group_id, target_id)

        # 5. 群内艾特警告
        warn_tpl = conf.get("warn_template", DEFAULT_WARN_TEMPLATE)
        warn_ctx = {
            "target": target_id,
            "warn_count": new_warn_count,
            "mute_duration": _fmt_duration(mute_duration),
            "group_id": group_id,
        }
        warn_text = _render(warn_tpl, warn_ctx)
        try:
            await event.send(
                event.chain_result([At(qq=str(target_id)), Plain(" " + warn_text)])
            )
        except Exception:
            # 艾特失败则退化为纯文本
            await self._send_group(event, group_id, warn_text)

        logger.info(
            f"[{PLUGIN_NAME}] 已自动处理：群{group_id} 用户{target_id} "
            f"消息{message_id} 警告{new_warn_count}次 禁言{mute_duration}秒 "
            f"撤回={recall_ok} 证据={evidence_file or '-'} 送达={sent_to}"
        )
        return {
            "warn_count": new_warn_count,
            "recall_ok": recall_ok,
            "evidence_file": evidence_file,
            "evidence_sent_to": sent_to,
            "mute_duration": mute_duration,
        }

    async def _handle_evidence_only(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        ctx: dict[str, Any],
        message_id: str,
        target_id: str,
        *,
        is_reply: bool,
        context_items: list[dict[str, Any]] | None = None,
    ) -> None:
        """机器人不是本群管理员：只留存证据并私聊转发给指定管理员/群主，不撤回、不禁言。"""
        group_id = str(ctx.get("group_id") or "")
        evidence_ctx = dict(ctx)
        evidence_ctx["action_plan"] = (
            "机器人不是本群管理员，未撤回、未禁言；已留存证据并转发，请管理员手动处理"
        )

        message_text = str(evidence_ctx.get("message") or "").strip()
        if not message_text:
            message_text = self._message_text_by_id(group_id, message_id)
        if not message_text:
            fetched = await self._fetch_message(event, message_id)
            message_text = str(fetched.get("text") or "")
        evidence_ctx["message"] = message_text or "[消息内容未获取到]"
        evidence_ctx["message_id"] = str(message_id)

        evidence = self._build_evidence(evidence_ctx, target_id, message_id)
        evidence_file = ""
        if conf.get("evidence_keep_files", True):
            evidence_file = self._save_evidence_file(evidence)

        targets = await self._evidence_targets(
            event, conf, group_id, include_owner=True, include_receivers=True
        )
        sent_to = await self._deliver_evidence(
            event,
            conf,
            evidence_ctx,
            targets=targets,
            context_items=context_items,
        )
        self._mark_acted(message_id)

        # 登记为待处理：收到证据的管理员/群主回复 1 即可结案
        if sent_to:
            report = self._make_pending_report(
                ctx,
                is_reply=is_reply,
                reason="机器人不是本群管理员，已仅留存证据并转发，等待管理员手动处理",
                notified=sent_to,
                admin_ids=targets,
            )
            self._pending[report["report_id"]] = report
            self._save_state()

        logger.info(
            f"[{PLUGIN_NAME}] 机器人非管理员，仅留存证据：群{group_id} "
            f"消息{message_id} 证据={evidence_file or '-'} 送达={sent_to}"
        )
        self._audit(
            "evidence_only",
            report={
                "report_id": ctx.get("report_id", ""),
                "group_id": group_id,
                "target_id": target_id,
                "reporter_id": ctx.get("reporter_id", ""),
                "occur_time": ctx.get("occur_time", ""),
                "participants": ctx.get("participants", ""),
                "context_conclusion": ctx.get("context_conclusion", ""),
                "cause": ctx.get("cause", ""),
                "ai_judgement": ctx.get("ai_judgement", ""),
            },
            extra={
                "evidence_file": evidence_file,
                "evidence_sent_to": sent_to,
                "evidence_targets": targets,
                "reason": "bot_not_group_admin",
            },
        )
