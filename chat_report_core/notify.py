"""私聊/群内通知、收件人解析、合并转发通知与待处理举报登记。"""

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
from astrbot.core.message.components import At, Plain, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .constants import (
    PLUGIN_NAME,
    DEFAULT_SAFE_TEMPLATE,
    DEFAULT_MANUAL_TEMPLATE,
    DEFAULT_MANUAL_TEMPLATE_NOCONTENT,
    DEFAULT_SAFE_GROUP_TEMPLATE,
    DEFAULT_MALICIOUS_TEMPLATE,
)

from .utils import (
    _render,
)


class NotifyMixin:
    """私聊/群内通知、收件人解析、合并转发通知与待处理举报登记。"""

    async def _announce_safe(
        self, event: AstrMessageEvent, conf: dict[str, Any], ctx: dict[str, Any]
    ) -> None:
        """AI 判定安全时，可选地在群内发一条「已核实安全」提示。"""
        tpl = conf.get("safe_group_template") or DEFAULT_SAFE_GROUP_TEMPLATE
        try:
            await event.send(event.plain_result(_render(tpl, ctx)))
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 群内安全提示发送失败：{e}")

    def _notify_text(
        self,
        tpl: str,
        ctx: dict[str, Any],
        conf: dict[str, Any] | None = None,
        include_context: bool = True,
        respect_toggle: bool = True,
    ) -> str:
        """渲染通知；模板里没有 {context_log} 但开启了「通知附带上下文」时自动补一段。"""
        include = True
        if respect_toggle:
            include = bool(
                (conf or {}).get(
                    "notify_include_context", self._c("notify_include_context", True)
                )
            )
        render_ctx = ctx
        if not include:
            render_ctx = dict(ctx)
            render_ctx["context_log"] = "（已关闭上下文展示）"
        text = _render(tpl, render_ctx)
        if not include_context or not include:
            return text
        if "{context_log}" in str(tpl):
            return text
        if not ctx.get("context_log"):
            return text
        block = (
            "\n──────────\n"
            f"【发生时间】{ctx.get('occur_time', '')}\n"
            f"【参与人员】{ctx.get('participants', '')}\n"
            f"【上下文聊天记录】（共 {ctx.get('context_count', 0)} 条）\n"
            f"{ctx.get('context_log')}\n"
            f"【事情起因】{ctx.get('cause', '')}"
        )
        return text + block

    async def _send_notice(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        ctx: dict[str, Any],
        receivers: Any,
        tpl: str,
        *,
        title_prefix: str,
        context_items: list[dict[str, Any]] | None = None,
        use_forward: bool = True,
        respect_notify_context: bool = True,
    ) -> tuple[list[str], list[str], str, bool]:
        """发送通知文本，并在需要时单独发送聊天记录证据。

        通知和聊天记录是两条独立消息：先发带元数据/上下文/事情起因的通知文本，
        再发一条 QQ 合并转发聊天记录作为证据存档；合并转发失败不影响通知送达。

        Args:
            receivers: 接收人 QQ 集合或列表。
            tpl: 通知模板。
            title_prefix: 聊天记录摘要标题前缀（举报通知/自动审查等）。
            context_items: 上下文条目；为空时只发通知文本。
            use_forward: 是否允许发送独立的聊天记录证据。
            respect_notify_context: 是否受 notify_include_context 开关约束。

        Returns:
            (失败 QQ 列表, 成功 QQ 列表, 纯文本兜底内容, 是否至少成功发送一次聊天记录)
        """
        group_id = str(ctx.get("group_id") or "")
        include_ctx = True
        if respect_notify_context:
            include_ctx = bool(conf.get("notify_include_context", True))
        full_text = self._notify_text(
            tpl, ctx, conf, respect_toggle=respect_notify_context
        )
        nodes: list[dict[str, Any]] = []
        if (
            use_forward
            and include_ctx
            and conf.get("forward_context_records", True)
            and context_items
        ):
            summary_ctx = dict(ctx)
            summary_ctx["context_log"] = "（见本条独立聊天记录）"
            summary = self._notify_text(
                tpl,
                summary_ctx,
                conf,
                include_context=False,
                respect_toggle=respect_notify_context,
            )
            nodes = self._build_forward_nodes(
                context_items,
                title=f"{title_prefix}-{ctx.get('report_id', '')}",
                summary_text=summary,
                self_id=self._event_self_id(event),
            )
        sent: list[str] = []
        failed: list[str] = []
        forward_sent = 0
        for uid in receivers:
            text_ok = await self._send_private(event, uid, full_text, group_id)
            record_ok = False
            if nodes:
                record_ok = await self._send_private_forward_nodes(
                    event, uid, group_id, nodes
                )
            if text_ok or record_ok:
                sent.append(uid)
            else:
                failed.append(uid)
            if record_ok:
                forward_sent += 1
        return failed, sent, full_text, forward_sent > 0

    async def _notify_safe(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        ctx: dict[str, Any],
        context_items: list[dict[str, Any]] | None = None,
    ):
        """向管理员/指定用户发送安全通知，失败时群内艾特兜底。"""
        receivers = await self._collect_receivers(event, ctx["group_id"], conf)
        if not receivers:
            return
        tpl = self._c("safe_notify_template", DEFAULT_SAFE_TEMPLATE)
        group_id = str(ctx.get("group_id") or "")
        failed, sent, text, forward_used = await self._send_notice(
            event,
            conf,
            ctx,
            receivers,
            tpl,
            title_prefix="举报通知",
            context_items=context_items,
        )
        if failed:
            await self._notify_in_group(event, group_id, failed, text)
        self._audit(
            "safe_notified",
            report={
                "report_id": ctx.get("report_id"),
                "group_id": ctx.get("group_id"),
                "target_id": ctx.get("target_id", ""),
                "participants": ctx.get("participants", ""),
                "occur_time": ctx.get("occur_time", ""),
                "context_conclusion": ctx.get("context_conclusion", ""),
                "cause": ctx.get("cause", ""),
            },
            extra={
                "notified": sorted(receivers),
                "delivered": sent,
                "forward_records": forward_used,
            },
        )
        self._save_state()

    async def _notify_malicious(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        ctx: dict[str, Any],
        context_items: list[dict[str, Any]] | None = None,
    ):
        """举报人未参与对话且属玩笑互动 → 提示管理员「疑似第三方恶意举报」。"""
        receivers = await self._collect_receivers(event, ctx["group_id"], conf)
        if not receivers:
            return
        tpl = self._c("malicious_notify_template", DEFAULT_MALICIOUS_TEMPLATE)
        group_id = str(ctx.get("group_id") or "")
        failed, sent, text, forward_used = await self._send_notice(
            event,
            conf,
            ctx,
            receivers,
            tpl,
            title_prefix="疑似恶意举报",
            context_items=context_items,
        )
        if failed:
            await self._notify_in_group(event, group_id, failed, text)
        self._audit(
            "suspected_malicious_notified",
            report={
                "report_id": ctx.get("report_id"),
                "group_id": ctx.get("group_id"),
                "target_id": ctx.get("target_id", ""),
                "participants": ctx.get("participants", ""),
                "occur_time": ctx.get("occur_time", ""),
                "context_conclusion": ctx.get("context_conclusion", ""),
                "cause": ctx.get("cause", ""),
            },
            extra={
                "notified": sorted(receivers),
                "delivered": sent,
                "reporter_id": ctx.get("reporter_id", ""),
                "forward_records": forward_used,
            },
        )
        self._save_state()

    def _make_pending_report(
        self,
        ctx: dict[str, Any],
        *,
        is_reply: bool,
        reason: str,
        notified: list[str],
        admin_ids: list[str],
    ) -> dict[str, Any]:
        """构造待处理举报记录（转人工与「仅留存证据」模式共用）。"""
        return {
            "report_id": ctx["report_id"],
            "group_id": ctx["group_id"],
            "group_name": ctx.get("group_name", ""),
            "target_id": ctx.get("target_id", ""),
            "target": ctx.get("target", ""),
            "reporter_id": ctx.get("reporter_id", ""),
            "reporter": ctx.get("reporter", ""),
            "sender": ctx.get("sender", ""),
            "message_id": ctx.get("message_id", ""),
            "is_reply": is_reply,
            "notified": sorted(notified),
            "admin_ids": admin_ids,
            "receiver_can_handle": bool(self._c("receiver_can_handle", True)),
            "allowed_reply_content": [
                str(self._c("reply_content", "1") or "1"),
                "1",
            ],
            "handled": False,
            "expired": False,
            "created": int(time.time()),
            "expire_minutes": int(self._c("report_expire_minutes", 60)),
            "ai_judgement": ctx.get("ai_judgement", ""),
            "ai_confidence": ctx.get("ai_confidence", ""),
            "target_report_count": ctx.get("target_report_count", 0),
            "warn_count": ctx.get("warn_count", 0),
            "mute_duration": ctx.get("mute_duration", ""),
            "message": ctx.get("message", ""),
            "recent_times": ctx.get("recent_times", ""),
            "recent_count": ctx.get("recent_count", 0),
            # 上下文与起因（处理完成通知、页面调阅都要用）
            "context_log": ctx.get("context_log", ""),
            "context_count": ctx.get("context_count", 0),
            "participants": ctx.get("participants", ""),
            "occur_time": ctx.get("occur_time", ""),
            "context_conclusion": ctx.get("context_conclusion", ""),
            "cause": ctx.get("cause", ""),
            "malicious_hint": ctx.get("malicious_hint", ""),
            "reason": reason,
        }

    async def _notify_manual(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        ctx: dict[str, Any],
        *,
        is_reply: bool,
        reason: str,
        admin_ids: list[str],
        context_items: list[dict[str, Any]] | None = None,
    ):
        """登记待处理举报并私聊通知管理员/指定用户，支持合并转发。"""
        receivers = await self._collect_receivers(event, ctx["group_id"], conf)
        # 机器人没有管理权限时，群主也该收到转人工通知
        if conf.get("not_admin_evidence_only", True):
            role = await self._bot_group_role(event, ctx["group_id"])
            if role == "member":
                receivers.update(
                    self._qq_list(await self._group_owner_ids(event, ctx["group_id"]))
                )
        if not receivers:
            logger.warning(f"[{PLUGIN_NAME}] 举报 {ctx['report_id']} 无人可通知")
            return

        if is_reply:
            tpl = self._c("manual_notify_template", DEFAULT_MANUAL_TEMPLATE)
        else:
            tpl = self._c(
                "manual_notify_template_nocontent", DEFAULT_MANUAL_TEMPLATE_NOCONTENT
            )

        report = self._make_pending_report(
            ctx,
            is_reply=is_reply,
            reason=reason,
            notified=sorted(receivers),
            admin_ids=admin_ids,
        )
        self._pending[report["report_id"]] = report
        self._save_state()

        group_id = str(ctx.get("group_id") or "")
        failed, sent, text, forward_used = await self._send_notice(
            event,
            conf,
            ctx,
            receivers,
            tpl,
            title_prefix="举报待处理",
            context_items=context_items,
        )
        if failed:
            await self._notify_in_group(event, group_id, failed, text)

        self._audit(
            "manual_notified",
            report=report,
            extra={
                "reason": reason,
                "delivered": sent,
                "forward_records": forward_used,
            },
        )

    async def _collect_receivers(
        self, event: AstrMessageEvent, group_id: str, conf: dict[str, Any]
    ) -> set[str]:
        """汇总 bot 管理员、指定用户与（可选）群管理员作为通知对象。"""
        receivers: set[str] = set()
        # 只保留纯数字 QQ 号：AstrBot 全局 admins_id 里可能混有 "astrbot" 这类非 QQ 项
        receivers.update(
            self._qq_list(self._bot_admins(getattr(event, "unified_msg_origin", None)))
        )
        receivers.update(self._qq_list(conf.get("receiver_ids")))
        if conf.get("auto_include_group_admins"):
            receivers.update(self._qq_list(await self._group_admin_ids(event, group_id)))
        receivers.discard(str(event.get_self_id()))
        return receivers

    async def _collect_admins(
        self, event: AstrMessageEvent, group_id: str, conf: dict[str, Any]
    ) -> list[str]:
        """汇总可执行处理的管理员 QQ。"""
        admins = set(self._qq_list(self._bot_admins(None)))
        if conf.get("auto_include_group_admins"):
            admins.update(self._qq_list(await self._group_admin_ids(event, group_id)))
        return sorted(admins)

    @staticmethod
    def _qq_list(values: Any) -> list[str]:
        """过滤出合法 QQ 号（纯数字），避免把非 QQ 项当成收件人。"""
        out: list[str] = []
        for value in values or []:
            text = str(value).strip()
            if text.isdigit():
                out.append(text)
        return out

    def _bot_admins(self, umo: str | None = None) -> list[str]:
        """读取 AstrBot 全局管理员列表。"""
        for kwargs in ({"umo": umo}, {}):
            try:
                cfg = self.context.get_config(**kwargs)
                admins = cfg.get("admins_id", []) or []
                return [str(x) for x in admins]
            except Exception:
                continue
        return []

    async def _send_private(
        self,
        event: AstrMessageEvent,
        user_id: str,
        message: str,
        group_id: str = "",
    ) -> bool:
        """私聊发送。带 group_id 时优先走「群临时会话」，对方未加好友也能送达。"""
        bot = getattr(event, "bot", None)
        if not bot or not str(user_id).strip():
            return False
        if not str(user_id).strip().isdigit():
            logger.debug(f"[{PLUGIN_NAME}] 忽略非 QQ 号的收件人：{user_id}")
            return False
        uid = int(user_id)
        gid = int(group_id) if str(group_id).strip() else 0
        api = getattr(bot, "api", None)

        async def via_api(**kwargs) -> None:
            """通过 bot.api.call_action 发送私聊消息。"""
            if api is None:
                raise RuntimeError("bot.api 不可用")
            await api.call_action("send_private_msg", **kwargs)

        async def via_bot() -> None:
            """通过 bot.send_private_msg 直连发送私聊消息。"""
            await bot.send_private_msg(user_id=uid, message=message)

        attempts: list[tuple[str, Any]] = []
        if gid:
            attempts.append(
                (
                    "群临时会话",
                    lambda: via_api(user_id=uid, message=message, group_id=gid),
                )
            )
        attempts.append(("私聊", via_bot))
        attempts.append(("私聊(call_action)", lambda: via_api(user_id=uid, message=message)))

        for label, fn in attempts:
            try:
                await fn()
                return True
            except Exception as e:
                logger.debug(f"[{PLUGIN_NAME}] {label} 发送给 {user_id} 失败：{e}")
        logger.warning(f"[{PLUGIN_NAME}] 私聊 {user_id} 所有方式均失败")
        return False

    async def _notify_in_group(
        self, event: AstrMessageEvent, group_id: str, at_ids: list[str], text: str
    ) -> None:
        """私聊不可达（未与机器人建立会话）时，退化为群内艾特提醒，保证通知不丢。"""
        ids = self._qq_list(at_ids)
        if not str(group_id).strip() or not ids:
            return
        bot = getattr(event, "bot", None)
        if not bot:
            return
        hint = "【以下人员未与机器人建立私聊，改为群内提醒】\n"
        segments: list[dict[str, Any]] = []
        for uid in ids:
            segments.append({"type": "at", "data": {"qq": uid}})
            segments.append({"type": "text", "data": {"text": " "}})
        segments.append({"type": "text", "data": {"text": "\n" + hint + text}})
        try:
            await bot.api.call_action(
                "send_group_msg", group_id=int(group_id), message=segments
            )
            logger.info(
                f"[{PLUGIN_NAME}] 私聊不可达，已在群 {group_id} 内艾特提醒：{ids}"
            )
            return
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 群内艾特提醒失败，退化为普通群消息：{e}")
        await self._send_group(event, group_id, hint + text)

    async def _send_group(self, event: AstrMessageEvent, group_id: str, message: str):
        """向群聊发送纯文本消息，失败只记录日志。"""
        bot = getattr(event, "bot", None)
        if not bot or not str(group_id).strip():
            return
        try:
            await bot.send_group_msg(group_id=int(group_id), message=message)
            return
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] send_group_msg 失败，尝试 call_action：{e}")
        try:
            await bot.api.call_action(
                "send_group_msg", group_id=int(group_id), message=message
            )
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 群 {group_id} 发送失败：{e}")
