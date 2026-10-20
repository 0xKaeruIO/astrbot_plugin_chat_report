"""按群自动审查：触发条件、批量审查、处理与三种通知模式。"""

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
    DEFAULT_AUTO_REVIEW_TEMPLATE,
)

from .utils import (
    _fmt_duration,
    EvidenceDeliveryError,
)


class AutoReviewMixin:
    """按群自动审查：触发条件、批量审查、处理与三种通知模式。"""

    def _violation_notice_ttl(self, conf: dict[str, Any]) -> int:
        """返回违规消息通知去重 TTL（秒），限制在 1 分钟到 24 小时。"""
        try:
            minutes = int(conf.get("auto_review_notify_dedup_minutes", 60))
        except Exception:
            minutes = 60
        return max(60, min(86400, minutes * 60))

    def _prune_violation_notices(self, conf: dict[str, Any]) -> None:
        """清理过期通知记录，并限制内存中最多保留 1000 条。"""
        ttl = self._violation_notice_ttl(conf)
        now = time.time()
        self._auto_review_notified = {
            str(mid): ts
            for mid, ts in self._auto_review_notified.items()
            if now - float(ts) < ttl
        }
        if len(self._auto_review_notified) > 1000:
            newest = sorted(
                self._auto_review_notified.items(),
                key=lambda kv: float(kv[1]),
                reverse=True,
            )[:1000]
            self._auto_review_notified = dict(newest)

    def _was_violation_notified(
        self, message_id: str, conf: dict[str, Any]
    ) -> bool:
        """判断该消息是否已在 TTL 内发过违规通知。"""
        mid = str(message_id or "").strip()
        if not mid:
            return True
        self._prune_violation_notices(conf)
        return mid in self._auto_review_notified

    def _mark_violation_notified(
        self, message_id: str, conf: dict[str, Any]
    ) -> None:
        """记录已通知的违规消息 ID，供 TTL 内去重。"""
        mid = str(message_id or "").strip()
        if not mid:
            return
        self._auto_review_notified[mid] = time.time()
        self._prune_violation_notices(conf)

    async def _auto_review_loop(self) -> None:
        """定时检查：群安静时也能按「时间间隔」触发自动审查。"""
        while True:
            try:
                await asyncio.sleep(30)
                now = time.time()
                for group_id, state in list(self._auto_review_state.items()):
                    if group_id in self._auto_review_running:
                        continue
                    conf = self._group_conf_cached(group_id)
                    if not conf.get("auto_review_enabled"):
                        continue
                    interval = int(
                        conf.get("auto_review_interval_minutes", 0)
                    ) * 60
                    if interval <= 0 or int(state.get("pending", 0) or 0) <= 0:
                        continue
                    if now - float(state.get("last_review", 0) or 0) < interval:
                        continue
                    event = self._last_events.get(group_id)
                    if event is None:
                        continue
                    self._auto_review_running.add(group_id)
                    try:
                        await self._run_auto_review(
                            event, group_id, conf, state
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.error(
                            f"[{PLUGIN_NAME}] 定时自动审查失败：{e}",
                            exc_info=True,
                        )
                    finally:
                        self._auto_review_running.discard(group_id)
                        state["pending"] = 0
                        state["last_review"] = now
                        self._auto_review_dirty = True
                if self._auto_review_dirty:
                    self._save_auto_review_state()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 自动审查循环异常：{e}")

    async def _maybe_auto_review(
        self, event: AstrMessageEvent, group_id: str
    ) -> None:
        """群消息到达时检查自动审查的条数/时间触发条件。"""
        conf = self._group_conf_cached(group_id)
        if not conf.get("auto_review_enabled"):
            return
        state = self._auto_review_state.setdefault(
            str(group_id),
            {
                "pending": 0,
                "last_review": time.time(),
                "last_mid": "",
                "last_ts": 0,
            },
        )
        pending = int(state.get("pending", 0) or 0)
        if pending <= 0 or group_id in self._auto_review_running:
            return
        now = time.time()
        threshold = int(conf.get("auto_review_message_threshold", 0))
        interval = int(conf.get("auto_review_interval_minutes", 0)) * 60
        due_count = threshold > 0 and pending >= threshold
        due_time = (
            interval > 0
            and now - float(state.get("last_review", 0) or 0) >= interval
        )
        if not (due_count or due_time):
            return
        self._auto_review_running.add(group_id)
        try:
            await self._run_auto_review(event, group_id, conf, state)
        finally:
            self._auto_review_running.discard(group_id)
            state["pending"] = 0
            state["last_review"] = now
            self._auto_review_dirty = True
            self._save_auto_review_state()

    async def _run_auto_review(
        self,
        event: AstrMessageEvent,
        group_id: str,
        conf: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        """执行一轮自动审查：取窗口、AI 研判、处理违规并按模式通知。

        Args:
            event: 最近一次群消息事件，用于发送通知/执行处罚。
            group_id: 群号字符串。
            conf: _group_conf 返回的该群最终配置。
            state: 该群自动审查状态（pending/last_review/last_mid/last_ts）。
        """
        scope_count = max(2, int(conf.get("auto_review_scope_count", 50)))
        scope_minutes = max(0, int(conf.get("auto_review_scope_minutes", 60)))
        all_items = [dict(x) for x in (self._group_cache.get(str(group_id)) or [])]
        if not all_items:
            return
        if scope_minutes > 0:
            cutoff = time.time() - scope_minutes * 60
            all_items = [
                x for x in all_items if int(x.get("time") or 0) >= cutoff
            ]
        items = all_items[-scope_count:]
        if not items:
            return

        # 只审查上次审查之后的新消息；旧消息只作为上下文
        last_mid = str(state.get("last_mid") or "")
        boundary = 0
        if last_mid:
            for idx, item in enumerate(items):
                if str(item.get("message_id")) == last_mid:
                    boundary = idx + 1
                    break
        last_ts = int(state.get("last_ts") or 0)
        if boundary > 0:
            candidates = items[boundary:]
        elif last_ts > 0:
            candidates = [
                x for x in items if int(x.get("time") or 0) > last_ts
            ]
        else:
            candidates = list(items)
        bot_id = self._event_self_id(event)
        candidates = [
            x
            for x in candidates
            if not self._was_acted(x.get("message_id"))
            and str(x.get("sender_id")) != bot_id
        ]
        if not candidates:
            state["last_mid"] = str(items[-1].get("message_id") or "")
            state["last_ts"] = int(items[-1].get("time") or 0)
            return

        review = await self._ai_auto_review(event, conf, items, candidates)
        if not review.get("ok"):
            logger.warning(
                f"[{PLUGIN_NAME}] 自动审查失败，等下一轮重试：{review.get('summary')}"
            )
            return

        raw_findings = list(review.get("findings") or [])
        findings = [
            finding
            for finding in raw_findings
            if not self._was_violation_notified(
                str(finding.get("message_id") or ""), conf
            )
        ]
        deduped_all = bool(raw_findings) and not findings
        threshold = float(self._c("ai_confidence_threshold", 0.7))
        banter_guard = bool(conf.get("banter_guard", True))
        rank = {"high": 3, "medium": 2, "low": 1, "none": 0}
        eligible = [
            f
            for f in findings
            if float(f.get("confidence") or 0) >= threshold
            and not (
                banter_guard
                and f.get("banter")
                and str(f.get("severity")) != "high"
            )
        ]
        eligible.sort(
            key=lambda f: (
                -rank.get(str(f.get("severity")), 0),
                -float(f.get("confidence") or 0),
            )
        )
        max_actions = max(0, int(conf.get("auto_review_max_actions", 3)))
        to_act = eligible[:max_actions]

        group_name = await self._get_group_name(event, group_id)
        role = await self._bot_group_role(event, group_id)
        actions: list[str] = []
        errors: list[str] = []
        acted: list[str] = []
        for finding in to_act:
            item = finding["item"]
            message_id = str(finding.get("message_id") or "")
            target_id = str(item.get("sender_id") or "")
            if not message_id or not target_id or self._was_acted(message_id):
                continue
            ctx = self._auto_review_action_ctx(
                group_id, conf, item, finding, review, items, group_name
            )
            try:
                if role == "member" and conf.get(
                    "not_admin_evidence_only", True
                ):
                    await self._handle_evidence_only(
                        event,
                        conf,
                        ctx,
                        message_id,
                        target_id,
                        is_reply=True,
                        context_items=items,
                    )
                    actions.append(f"仅留证并转发：{ctx['target']}")
                else:
                    await self._punish_with_evidence(
                        event,
                        conf,
                        ctx,
                        message_id,
                        target_id,
                        context_items=items,
                    )
                    actions.append(
                        f"撤回并禁言：{ctx['target']}（{ctx['mute_duration']}）"
                    )
                acted.append(message_id)
            except EvidenceDeliveryError as e:
                errors.append(f"{ctx['target']}：{e}")
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[{PLUGIN_NAME}] 自动审查处理失败：{e}", exc_info=True
                )
                errors.append(f"{ctx['target']}：{e}")

        if actions and errors:
            action_plan = "；".join(actions) + f"；{len(errors)} 条处理失败，待人工确认"
        elif actions:
            action_plan = "；".join(actions)
        elif errors:
            action_plan = f"{len(errors)} 条处理失败，待人工确认"
        elif findings:
            action_plan = "发现疑似违规但未自动处理，请管理员人工确认"
        else:
            action_plan = "未发现违规，无需处理"

        mode = str(conf.get("auto_review_notify_mode") or "safe_silent").lower()
        if mode in ("1", "always", "all"):
            should_notify = True
        elif mode in ("3", "after_action", "after"):
            should_notify = bool(actions) or bool(errors)
        else:
            should_notify = bool(findings)
        if deduped_all and not actions and not errors:
            # 本轮识别到的违规消息此前已通知过，且没有新的处理动作
            should_notify = False
        notified: list[str] = []
        if should_notify:
            notified = await self._send_auto_review_notice(
                event,
                conf,
                group_id,
                group_name,
                items,
                findings,
                action_plan,
                review,
            )
        if notified or actions:
            for finding in findings:
                self._mark_violation_notified(
                    str(finding.get("message_id") or ""), conf
                )

        state["last_mid"] = str(items[-1].get("message_id") or "")
        state["last_ts"] = int(items[-1].get("time") or 0)
        self._auto_review_dirty = True

        report_id = f"AR{int(time.time())}{uuid.uuid4().hex[:4]}"
        self._audit(
            "auto_review",
            report={
                "report_id": report_id,
                "group_id": group_id,
                "target_id": "",
                "reporter_id": "",
                "occur_time": self._item_time_text(items[-1]),
                "participants": self._participants_text(items),
                "context_conclusion": review.get("context_conclusion", ""),
                "cause": review.get("summary", ""),
                "ai_judgement": (
                    f"发现 {len(findings)} 条疑似违规"
                    if findings
                    else "未发现违规"
                ),
            },
            extra={
                "review_scope": f"{len(items)} 条",
                "violation_count": len(findings),
                "actions": actions,
                "errors": errors,
                "acted_message_ids": acted,
                "notify_mode": mode,
                "notified": notified,
            },
        )
        self._save_state()

    def _auto_review_action_ctx(
        self,
        group_id: str,
        conf: dict[str, Any],
        item: dict[str, Any],
        finding: dict[str, Any],
        review: dict[str, Any],
        items: list[dict[str, Any]],
        group_name: str,
    ) -> dict[str, Any]:
        """为单条自动审查违规消息构造证据/模板上下文。"""
        target_id = str(item.get("sender_id") or "")
        target_name = str(item.get("sender_name") or target_id)
        recent_times = "、".join(
            self._item_time_text(x, "%H:%M:%S") for x in items[-5:] if x.get("time")
        )
        return {
            "group_name": group_name,
            "group_id": group_id,
            "target_id": target_id,
            "reporter_id": "",
            "sender": target_name,
            "target": f"{target_name}({target_id})",
            "reporter": "自动审查",
            "message": str(item.get("text") or ""),
            "message_id": str(item.get("message_id") or ""),
            "is_auto_review": True,
            "target_report_count": 0,
            "warn_count": self._warn_counts.get(f"{group_id}:{target_id}", 0),
            "report_id": f"AR{int(time.time())}{uuid.uuid4().hex[:4]}",
            "recent_count": len(items),
            "recent_times": recent_times,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mute_duration": _fmt_duration(conf["mute_duration"]),
            "context_log": self._format_context_log(items),
            "context_count": len(items),
            "participants": self._participants_text(items),
            "occur_time": self._item_time_text(item),
            "context_conclusion": review.get("context_conclusion")
            or review.get("summary", ""),
            "cause": finding.get("reason")
            or review.get("summary")
            or "自动审查发现违规",
            "malicious_hint": "",
            "ai_judgement": "违规（自动审查）",
            "ai_confidence": f"{round(float(finding.get('confidence') or 0) * 100)}%",
            "action_plan": "撤回该消息并禁言",
        }

    async def _send_auto_review_notice(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        group_id: str,
        group_name: str,
        items: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        action_plan: str,
        review: dict[str, Any],
    ) -> list[str]:
        """按通知模式发送自动审查结果（合并转发聊天记录）。"""
        receivers = await self._collect_receivers(event, group_id, conf)
        receivers.update(self._qq_list(conf.get("evidence_admin_ids")))
        role = await self._bot_group_role(event, group_id)
        if role == "member":
            receivers.update(
                self._qq_list(await self._group_owner_ids(event, group_id))
            )
        receivers.discard(self._event_self_id(event))
        if not receivers:
            logger.warning(f"[{PLUGIN_NAME}] 自动审查通知无人可发：群{group_id}")
            return []

        first = self._item_time_text(items[0])
        last = self._item_time_text(items[-1])
        occur_time = f"{first} ~ {last}" if first and first != last else last
        scope_minutes = int(conf.get("auto_review_scope_minutes", 60))
        review_scope = f"最近 {len(items)} 条"
        if scope_minutes > 0:
            review_scope += f"（{scope_minutes} 分钟内）"
        max_conf = max(
            [float(f.get("confidence") or 0) for f in findings] or [0.0]
        )
        report_id = f"AR{int(time.time())}{uuid.uuid4().hex[:4]}"
        ctx = {
            "group_name": group_name,
            "group_id": group_id,
            "target": "自动审查",
            "target_id": "",
            "reporter": "系统",
            "reporter_id": "",
            "sender": "自动审查",
            "message": review.get("summary", ""),
            "message_id": "",
            "target_report_count": 0,
            "warn_count": 0,
            "report_id": report_id,
            "recent_count": len(items),
            "recent_times": "、".join(
                self._item_time_text(x, "%H:%M:%S")
                for x in items[-5:]
                if x.get("time")
            ),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mute_duration": _fmt_duration(conf["mute_duration"]),
            "context_log": self._format_context_log(items),
            "context_count": len(items),
            "participants": self._participants_text(items),
            "occur_time": occur_time,
            "context_conclusion": review.get("context_conclusion")
            or review.get("summary")
            or "无",
            "cause": review.get("summary") or "自动审查",
            "malicious_hint": "",
            "ai_judgement": (
                f"发现 {len(findings)} 条疑似违规" if findings else "未发现违规"
            ),
            "ai_confidence": f"{round(max_conf * 100)}%",
            "action_plan": action_plan,
            "review_scope": review_scope,
            "violation_count": len(findings),
        }
        tpl = self._c(
            "auto_review_notify_template", DEFAULT_AUTO_REVIEW_TEMPLATE
        )
        markers = {
            str(finding.get("message_id") or ""): "[违规内容]"
            for finding in findings
            if str(finding.get("message_id") or "")
        }
        failed, sent, text, forward_used = await self._send_notice(
            event,
            conf,
            ctx,
            receivers,
            tpl,
            title_prefix="自动审查",
            context_items=items,
            attach_text=False,
            markers=markers,
        )
        if failed:
            await self._notify_in_group(
                event, group_id, failed, text
            )
        self._audit(
            "auto_review_notified",
            report={
                "report_id": report_id,
                "group_id": group_id,
                "target_id": "",
                "reporter_id": "",
                "occur_time": occur_time,
                "participants": ctx["participants"],
                "context_conclusion": ctx["context_conclusion"],
                "cause": ctx["cause"],
                "ai_judgement": ctx["ai_judgement"],
            },
            extra={
                "notified": sent,
                "failed": failed,
                "forward_records": forward_used,
                "violation_count": len(findings),
            },
        )
        return sent
