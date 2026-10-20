"""举报触发解析与举报主流程（研判、豁免、处罚/转人工分支）。"""

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
    DEFAULT_BROADCAST_TEMPLATE,
)

from .utils import (
    _render,
    _fmt_duration,
    EvidenceDeliveryError,
)


class ReportMixin:
    """举报触发解析与举报主流程（研判、豁免、处罚/转人工分支）。"""

    def _parse_report(
        self, event: AstrMessageEvent, self_id: str
    ) -> dict[str, Any] | None:
        """识别举报消息，返回 {is_reply, target_id, message_id, content}。"""
        try:
            chain = list(event.get_messages())
        except Exception:
            chain = []

        # 触发条件一：整条消息只能是「@ + 举报」，不允许夹带任何其它内容
        # （多一个字、一张图、一个表情都不算，避免日常聊天误触发）
        if any(not isinstance(seg, (At, Plain, Reply)) for seg in chain):
            return None
        if self._chain_text_only(chain).strip().lower() not in ("举报", "report"):
            return None

        # 触发条件二：必须 @ 了机器人本人。只 @ 别人 +「举报」不触发
        bot_id = str(self_id or "").strip()
        self_mentioned = bool(bot_id) and any(
            isinstance(seg, At) and str(seg.qq).strip() == bot_id for seg in chain
        )
        if not self_mentioned:
            return None

        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        # 被举报人候选：排除机器人自己与 @全体成员
        at_ids = [
            str(seg.qq).strip()
            for seg in chain
            if isinstance(seg, At) and str(seg.qq).strip() not in (bot_id, "all")
        ]

        if reply_seg is not None:
            target_id = str(reply_seg.sender_id or "")
            message_id = str(reply_seg.id or "")
            rchain = list(reply_seg.chain or [])
            # 纯文本部分（用于判断能否交给 AI 分析）
            plain_text = self._chain_text_only(rchain)
            if not plain_text:
                plain_text = (reply_seg.message_str or "").strip()
            # 展示用内容：优先按消息段渲染成中文（图片→[图片]），避免出现 ComponentType.Image
            content = self._chain_to_text(rchain) if rchain else plain_text
            return {
                "is_reply": True,
                "target_id": target_id or (at_ids[0] if at_ids else ""),
                "message_id": message_id,
                "content": content,
                "has_text": bool(plain_text),
            }

        if at_ids:
            return {
                "is_reply": False,
                "target_id": at_ids[0],
                "message_id": "",
                "content": "",
            }

        # @ 了机器人 + 「举报」，但没指明举报谁 → 交由上层回一条使用提示
        return {"no_target": True}

    async def _handle_report(
        self,
        event: AiocqhttpMessageEvent,
        group_id: str,
        sender_id: str,
        self_id: str,
        parsed: dict[str, Any],
    ):
        """举报主流程：限流 → 上下文 → AI 研判 → 玩笑豁免 → 证据/处罚/转人工分支。

        Args:
            event: 触发举报的群消息事件（携带 bot 与群/发送者信息）。
            group_id: 群号字符串。
            sender_id: 举报人 QQ。
            self_id: 机器人自身 QQ。
            parsed: _parse_report 的解析结果，含 is_reply/target_id/message_id/content。
        """
        conf = self._group_conf(group_id)

        # 1. 举报权限
        if not self._can_report(sender_id, conf):
            logger.debug(f"[{PLUGIN_NAME}] 用户 {sender_id} 无举报权限，忽略")
            return

        # 2. 冷却与每日上限
        ok, reason = self._check_rate(group_id, sender_id, conf)
        if not ok:
            logger.debug(f"[{PLUGIN_NAME}] 举报被限流：{reason}")
            return

        # 3. 目标
        target_id = str(parsed.get("target_id") or "").strip()
        if not target_id or target_id == self_id:
            return
        is_reply = bool(parsed.get("is_reply"))
        message_id = str(parsed.get("message_id") or "")
        content = str(parsed.get("content") or "")
        has_text = bool(parsed.get("has_text", bool(content)))

        # 4. 计数
        enable_count = bool(self._c("enable_target_report_count", True))
        target_report_count = 0
        if enable_count:
            target_report_count = self._incr_report_count(group_id, target_id)
        warn_count = self._warn_counts.get(f"{group_id}:{target_id}", 0)

        # 5. 被举报人自己的最近发言（判断是否惯犯/刷屏，作为上下文补充）
        recent = self._recent_messages(group_id, target_id, conf["recent_msg_count"])

        # 6. 展示用信息
        group_name = await self._get_group_name(event, group_id)
        target_name = await self._resolve_name(event, group_id, target_id)
        reporter_name = await self._resolve_name(event, group_id, sender_id)
        target_display = f"{target_name}({target_id})"
        reporter_display = f"{reporter_name}({sender_id})"

        # 7. 上下文：被举报消息前后整段群聊记录（含其他成员），用于识别玩笑互怼
        context_items: list[dict[str, Any]] = []
        if conf.get("context_enabled", True):
            context_items = await self._collect_context(
                event,
                conf,
                group_id,
                target_id,
                message_id,
                fallback_text=str(parsed.get("content") or ""),
                target_display=target_display,
                exclude_message_id=self._event_message_id(event),
            )
        target_item = next((x for x in context_items if x.get("is_target")), None)
        if not content and target_item:
            # 引用的消息没解析出文本时，用上下文中同一条消息兜底
            content = str(target_item.get("text") or "")
            has_text = bool(content)
            if not message_id:
                message_id = str(target_item.get("message_id") or "")

        context_log = self._format_context_log(context_items)
        occur_time = self._item_time_text(target_item)
        if not occur_time:
            # 定位不到被举报消息时（例如 @ 举报），只能给出举报时间
            occur_time = time.strftime("%Y-%m-%d %H:%M:%S") + "（举报时间）"
        reporter_in_context = bool(sender_id) and any(
            str(item.get("sender_id")) == sender_id for item in context_items
        )

        report_id = f"R{int(time.time())}{uuid.uuid4().hex[:4]}"
        base_ctx = {
            "group_name": group_name,
            "group_id": group_id,
            "target_id": target_id,
            "reporter_id": sender_id,
            "sender": target_name,
            "target": target_display,
            "reporter": reporter_display,
            "message": content if is_reply else "",
            "message_id": message_id,
            "target_report_count": target_report_count,
            "warn_count": warn_count,
            "report_id": report_id,
            "recent_count": len(recent),
            "recent_times": self._recent_times(recent),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mute_duration": _fmt_duration(conf["mute_duration"]),
            # 上下文相关
            "context_log": context_log,
            "context_count": len(context_items),
            "participants": self._participants_text(context_items),
            "occur_time": occur_time,
            "context_conclusion": "",
            "cause": "",
            "malicious_hint": "",
        }

        # 8. AI 研判（带上上下文）
        target_vision = self._collect_vision_images(
            conf,
            context_items,
            priority_ids={message_id} if message_id else set(),
            target_label="被举报消息",
        )
        if is_reply and not has_text and not target_vision.get("images"):
            # 引用非文本消息，且没有可交给视觉模型的图片，交给人工
            judgement = {
                "decision": "uncertain",
                "confidence": 0.0,
                "reason": "被举报消息为非文本内容，未启用图片理解或没有可读取图片",
                "target_message_id": "",
                "banter": False,
                "severity": "none",
                "reporter_involved": reporter_in_context,
                "participants": "",
                "cause": "",
                "context_summary": "",
            }
        else:
            judgement = await self._ai_judge(
                event,
                conf,
                group_id,
                target_id,
                is_reply,
                content,
                recent,
                context_items=context_items,
                message_id=message_id,
                target_display=target_display,
                reporter_display=reporter_display,
                reporter_in_context=reporter_in_context,
            )

        decision = judgement.get("decision", "uncertain")
        confidence = float(judgement.get("confidence", 0.0) or 0.0)
        threshold = float(self._c("ai_confidence_threshold", 0.7))
        ai_msg_id = str(judgement.get("target_message_id") or "").strip()
        banter = bool(judgement.get("banter"))
        severity = str(judgement.get("severity") or "none")
        ai_reporter_involved = judgement.get("reporter_involved")
        reporter_involved = (
            bool(ai_reporter_involved) and reporter_in_context
            if isinstance(ai_reporter_involved, bool)
            else reporter_in_context
        )

        base_ctx["ai_confidence"] = f"{round(confidence * 100)}%"
        base_ctx["context_conclusion"] = str(
            judgement.get("context_summary") or "（AI 未给出上下文结论）"
        )
        base_ctx["cause"] = str(
            judgement.get("cause") or judgement.get("reason") or "（未获取到明确起因）"
        )
        if banter and not reporter_involved:
            base_ctx["malicious_hint"] = "（疑似第三方恶意举报：举报人未参与该段对话）"

        # 9. 玩笑豁免：上下文是双方互开玩笑时不处罚（severity=high 仍照常处理）
        banter_exempt = self._banter_should_exempt(conf, decision, banter, severity)
        if banter_exempt:
            logger.info(
                f"[{PLUGIN_NAME}] 举报 {report_id} 命中玩笑豁免：banter=True "
                f"severity={severity}，原判 violate -> safe"
            )
            decision = "safe"

        logger.info(
            f"[{PLUGIN_NAME}] 举报 {report_id} 群{group_id} 目标{target_id} "
            f"AI={decision} conf={confidence} banter={banter} severity={severity} "
            f"context={len(context_items)}条 banter_exempt={banter_exempt}"
        )

        audit_report = {
            "report_id": report_id,
            "group_id": group_id,
            "target_id": target_id,
            "reporter_id": sender_id,
            "participants": base_ctx["participants"],
            "occur_time": occur_time,
            "context_conclusion": base_ctx["context_conclusion"],
            "cause": base_ctx["cause"],
        }

        # 10. 分支处理
        if decision == "safe":
            base_ctx["ai_judgement"] = (
                "安全（上下文为玩笑互动，已豁免）" if banter_exempt else "安全"
            )
            if conf.get("announce_safe_in_group"):
                await self._announce_safe(event, conf, base_ctx)
            suspected_malicious = banter and not reporter_involved
            if suspected_malicious and conf.get(
                "notify_admin_on_suspected_malicious", True
            ):
                await self._notify_malicious(
                    event, conf, base_ctx, context_items=context_items
                )
            elif conf.get("notify_admin_when_safe"):
                await self._notify_safe(
                    event, conf, base_ctx, context_items=context_items
                )
            self._audit(
                "safe" if not suspected_malicious else "suspected_malicious",
                report=audit_report,
                extra={
                    "confidence": confidence,
                    "reason": judgement.get("reason", ""),
                    "banter": banter,
                    "severity": severity,
                    "reporter_involved": reporter_involved,
                    "banter_exempt": banter_exempt,
                },
            )
            self._save_state()
            return

        if decision == "violate" and confidence >= threshold:
            # AI 在无引用举报里挑中的消息必须属于被举报人，防止误撤回他人消息
            if not message_id and ai_msg_id:
                owner = self._message_sender_by_id(group_id, ai_msg_id)
                if not owner:
                    fetched = await self._fetch_message(event, ai_msg_id)
                    owner = str(fetched.get("sender_id") or "")
                if owner and owner != target_id:
                    logger.warning(
                        f"[{PLUGIN_NAME}] AI 指向的消息 {ai_msg_id} 属于 {owner}，"
                        f"与被举报人 {target_id} 不一致，放弃自动处理"
                    )
                    ai_msg_id = ""
            # 需要能定位到具体消息才好撤回
            punish_msg_id = message_id or ai_msg_id
            # 机器人不是本群管理员：撤回/禁言必然失败，默认只留存证据并转发
            role = await self._bot_group_role(event, group_id)
            if role == "member" and conf.get("not_admin_evidence_only", True):
                base_ctx["ai_judgement"] = "违规（机器人非本群管理员，仅留存证据）"
                await self._handle_evidence_only(
                    event,
                    conf,
                    base_ctx,
                    punish_msg_id,
                    target_id,
                    is_reply=is_reply,
                    context_items=context_items,
                )
                return
            if not punish_msg_id:
                base_ctx["ai_judgement"] = "违规但无法处理"
                await self._notify_manual(
                    event,
                    conf,
                    base_ctx,
                    is_reply=is_reply,
                    reason="AI 判定违规，但无法定位具体消息ID，无法自动撤回",
                    admin_ids=await self._collect_admins(event, group_id, conf),
                    context_items=context_items,
                )
                return
            try:
                result = await self._punish_with_evidence(
                    event,
                    conf,
                    base_ctx,
                    punish_msg_id,
                    target_id,
                    context_items=context_items,
                )
                new_warn_count = int(result.get("warn_count") or 0)
                base_ctx["warn_count"] = new_warn_count
                self._audit(
                    "violate_auto",
                    report=audit_report,
                    extra={
                        "confidence": confidence,
                        "reason": judgement.get("reason", ""),
                        "banter": banter,
                        "severity": severity,
                        "message_id": punish_msg_id,
                        "warn_count": new_warn_count,
                        "mute_duration": conf["mute_duration"],
                        "evidence_file": result.get("evidence_file", ""),
                        "evidence_sent_to": result.get("evidence_sent_to", []),
                        "recall_ok": result.get("recall_ok", True),
                    },
                )
                if bool(self._c("broadcast_result_in_group", False)):
                    bcast = self._c(
                        "group_broadcast_template", DEFAULT_BROADCAST_TEMPLATE
                    )
                    ctx = dict(base_ctx)
                    ctx["handler"] = "AI自动处理"
                    await self._send_group(event, group_id, _render(bcast, ctx))
                self._save_state()
                return
            except EvidenceDeliveryError as e:
                # 证据没能私聊送达指定管理员 → 不允许撤回/禁言，转人工
                logger.warning(f"[{PLUGIN_NAME}] 举报 {report_id} 证据转发失败：{e}")
                base_ctx["ai_judgement"] = "违规但未处理（证据未送达）"
                await self._notify_manual(
                    event,
                    conf,
                    base_ctx,
                    is_reply=is_reply,
                    reason=f"{e}，已跳过自动撤回/禁言，请人工确认",
                    admin_ids=await self._collect_admins(event, group_id, conf),
                    context_items=context_items,
                )
                return
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 自动处理失败：{e}", exc_info=True)
                base_ctx["ai_judgement"] = "违规但无法处理"
                await self._notify_manual(
                    event,
                    conf,
                    base_ctx,
                    is_reply=is_reply,
                    reason=f"自动撤回/禁言失败：{e}",
                    admin_ids=await self._collect_admins(event, group_id, conf),
                    context_items=context_items,
                )
                return

        # uncertain 或 置信度不足
        base_ctx["ai_judgement"] = "拿不准"
        await self._notify_manual(
            event,
            conf,
            base_ctx,
            is_reply=is_reply,
            reason=judgement.get("reason", ""),
            admin_ids=await self._collect_admins(event, group_id, conf),
            context_items=context_items,
        )
