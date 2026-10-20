"""群聊冲突观察：主动发现两人/多人争执，通知管理员并可中性劝停。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .constants import (
    DEFAULT_CONFLICT_MEDIATE_TEMPLATE,
    DEFAULT_CONFLICT_NOTIFY_TEMPLATE,
    DEFAULT_CONFLICT_PROMPT,
    PLUGIN_NAME,
)
from .utils import _extract_json, _render


CONFLICT_ROLE_LABELS = {
    "initiator": "发起",
    "responder": "回应",
    "mediator": "劝架",
    "bystander": "旁观",
}
CONFLICT_TYPE_LABELS = {
    "two_party": "两人争执",
    "multi_party": "多人争执",
}
CONFLICT_TREND_LABELS = {
    "escalating": "正在升级",
    "stable": "持续对峙",
    "cooling": "正在降温",
}
CONFLICT_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}


class ConflictMixin:
    """主动扫描群聊冲突，发送管理员预警与一条中性劝停消息。"""

    async def _conflict_loop(self) -> None:
        """每 20 秒扫描一次有最近消息的群，按新消息数与冷却触发。"""
        while True:
            try:
                await asyncio.sleep(20)
                for group_id, event in list(self._last_events.items()):
                    if group_id in self._conflict_running:
                        continue
                    conf = self._group_conf_cached(group_id)
                    if not conf.get("conflict_detection_enabled"):
                        continue
                    await self._maybe_scan_conflict(event, group_id, conf)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{PLUGIN_NAME}] 冲突观察循环异常：{e}")

    def _conflict_window(
        self, group_id: str, conf: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """取该群最近窗口内的消息，按时间过滤并限制条数。"""
        items = list(self._group_cache.get(str(group_id)) or [])
        try:
            minutes = max(1, min(120, int(conf.get("conflict_window_minutes", 10))))
        except Exception:
            minutes = 10
        try:
            limit = max(5, min(60, int(conf.get("conflict_window_messages", 20))))
        except Exception:
            limit = 20
        cutoff = time.time() - minutes * 60
        filtered = [
            item
            for item in items
            if not int(item.get("time") or 0)
            or int(item.get("time") or 0) >= cutoff
        ]
        return filtered[-limit:]

    def _conflict_new_count(
        self, items: list[dict[str, Any]], state: dict[str, Any]
    ) -> int:
        """计算上次扫描位置之后的新消息数；找不到边界时按整窗计算。"""
        last_mid = str(state.get("last_scan_mid") or "")
        if not last_mid:
            return len(items)
        for index, item in enumerate(items):
            if str(item.get("message_id") or "") == last_mid:
                return max(0, len(items) - index - 1)
        return len(items)

    def _format_conflict_participants(
        self, participants: list[dict[str, Any]]
    ) -> str:
        """格式化为「A（发起）、B（回应）、C（劝架）」。"""
        parts: list[str] = []
        for person in participants:
            name = str(person.get("name") or person.get("id") or "群成员")
            role = CONFLICT_ROLE_LABELS.get(
                str(person.get("role") or ""), "参与"
            )
            parts.append(f"{name}（{role}）")
        return "、".join(parts) or "未识别"

    def _format_conflict_recent(
        self, items: list[dict[str, Any]], limit: int = 8
    ) -> str:
        """把最近消息压成通知里的多行摘要。"""
        lines: list[str] = []
        for item in items[-limit:]:
            name = str(item.get("sender_name") or item.get("sender_id") or "群成员")
            text = str(item.get("text") or "").replace("\n", " ").strip()
            if len(text) > 100:
                text = text[:100] + "…"
            if text:
                lines.append(f"{name}：{text}")
        return "\n".join(lines) or "（无可用消息）"

    def _normalize_conflict(
        self,
        data: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """校验 AI 冲突结果：必须有两个以上有效冲突方，否则视为无冲突。"""
        item_by_id = {
            str(item.get("sender_id") or ""): item
            for item in items
            if item.get("sender_id")
        }
        item_by_name = {
            str(item.get("sender_name") or ""): item
            for item in items
            if item.get("sender_name")
        }
        raw_parts = data.get("participants")
        if not isinstance(raw_parts, list):
            raw_parts = []
        participants: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_parts:
            if isinstance(raw, str):
                raw = {"name": raw}
            if not isinstance(raw, dict):
                continue
            uid = str(raw.get("id") or raw.get("user_id") or "").strip()
            name = str(raw.get("name") or raw.get("nickname") or "").strip()
            if not uid and name:
                item = item_by_name.get(name)
                if item:
                    uid = str(item.get("sender_id") or "")
            if uid and uid not in item_by_id:
                item = item_by_name.get(name)
                if item:
                    uid = str(item.get("sender_id") or "")
            if not uid or uid in seen:
                continue
            item = item_by_id.get(uid) or {}
            role = str(raw.get("role") or "bystander").strip().lower()
            if role not in CONFLICT_ROLE_LABELS:
                role = "bystander"
            seen.add(uid)
            participants.append(
                {
                    "id": uid,
                    "name": name or str(item.get("sender_name") or uid),
                    "role": role,
                }
            )

        active_ids = {
            p["id"]
            for p in participants
            if p["role"] in ("initiator", "responder")
        }
        conflict = bool(data.get("conflict")) and not bool(data.get("banter"))
        if len(active_ids) < 2:
            conflict = False

        conflict_type = str(data.get("conflict_type") or "").strip().lower()
        if conflict_type not in CONFLICT_TYPE_LABELS:
            conflict_type = "two_party" if len(active_ids) == 2 else "multi_party"
        severity = str(data.get("severity") or "").strip().lower()
        if severity not in CONFLICT_SEVERITY_RANK:
            severity = "medium" if conflict else "none"
        trend = str(data.get("trend") or "").strip().lower()
        if trend not in CONFLICT_TREND_LABELS:
            trend = "stable"
        summary = str(data.get("summary") or "").strip()
        if conflict and not summary:
            summary = "群聊出现持续争执"
        suggestion = str(data.get("suggestion") or "").strip()
        if conflict and not suggestion:
            suggestion = "先劝停，必要时撤回/禁言"
        return {
            "conflict": conflict,
            "conflict_type": conflict_type if conflict else "none",
            "severity": severity,
            "participants": participants if conflict else [],
            "summary": summary,
            "trend": trend,
            "suggestion": suggestion,
            "recent_messages": self._format_conflict_recent(items),
        }

    async def _ai_conflict_scan(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        group_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """调用 AI 判断窗口内是否存在真实冲突。"""
        fallback = {
            "conflict": False,
            "conflict_type": "none",
            "severity": "none",
            "participants": [],
            "summary": "",
            "trend": "stable",
            "suggestion": "",
            "recent_messages": self._format_conflict_recent(items),
        }
        provider = self._get_provider(event)
        if not provider:
            return fallback
        try:
            system_prompt = _render(
                DEFAULT_CONFLICT_PROMPT,
                {"group_rules": conf.get("group_rules", "")},
            )
            extra = str(conf.get("conflict_extra_prompt") or "").strip()
            if extra:
                system_prompt += f"\n【补充要求】\n{extra}"
            lines = ["【群聊记录（按时间顺序）】"]
            for item in items:
                message_id = str(item.get("message_id") or "")
                ts = self._item_time_text(item, "%m-%d %H:%M:%S") or "--"
                sender_id = str(item.get("sender_id") or "")
                sender_name = str(item.get("sender_name") or sender_id)
                text = str(item.get("text") or "").replace("\n", " ").strip()
                if len(text) > 200:
                    text = text[:200] + "…"
                lines.append(
                    f"- id={message_id} [{ts}] {sender_name}({sender_id})：{text}"
                )
            prompt = "\n".join(lines)
            timeout = int(self._c("ai_timeout", 30))
            retry = int(self._c("ai_retry_times", 1))
            last_err: Exception | None = None
            for attempt in range(retry + 1):
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(
                            system_prompt=system_prompt, prompt=prompt
                        ),
                        timeout=timeout,
                    )
                    raw = getattr(resp, "completion_text", "") or ""
                    data = _extract_json(raw)
                    if not data:
                        last_err = RuntimeError(f"AI 返回无法解析：{raw[:200]}")
                        continue
                    return self._normalize_conflict(data, items)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if attempt < retry:
                        await asyncio.sleep(1)
            logger.warning(f"[{PLUGIN_NAME}] 冲突研判失败：{last_err}")
            return fallback
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 冲突研判异常：{e}")
            return fallback

    async def _maybe_scan_conflict(
        self,
        event: AstrMessageEvent,
        group_id: str,
        conf: dict[str, Any],
    ) -> None:
        """判断是否满足窗口/人数/新消息/冷却条件，然后执行一次冲突研判。"""
        state = self._conflict_state.setdefault(
            str(group_id),
            {"last_scan_mid": "", "last_alert_ts": 0.0, "last_alert_mid": ""},
        )
        items = self._conflict_window(group_id, conf)
        try:
            min_participants = max(
                2, min(10, int(conf.get("conflict_min_participants", 2)))
            )
        except Exception:
            min_participants = 2
        active_ids = {
            str(item.get("sender_id") or "")
            for item in items
            if item.get("sender_id")
        }
        if len(active_ids) < min_participants:
            return
        try:
            min_new = max(1, int(conf.get("conflict_min_new_messages", 6)))
        except Exception:
            min_new = 6
        if self._conflict_new_count(items, state) < min_new:
            return
        now = time.time()
        try:
            cooldown = max(1, int(conf.get("conflict_cooldown_minutes", 10))) * 60
        except Exception:
            cooldown = 600
        if now - float(state.get("last_alert_ts") or 0) < cooldown:
            return
        if str(group_id) in self._conflict_running:
            return
        self._conflict_running.add(str(group_id))
        try:
            result = await self._ai_conflict_scan(event, conf, group_id, items)
            last_mid = str(items[-1].get("message_id") or "") if items else ""
            state["last_scan_mid"] = last_mid
            if not result.get("conflict"):
                return
            severity = str(result.get("severity") or "medium")
            try:
                min_severity = str(
                    conf.get("conflict_min_severity", "medium") or "medium"
                )
            except Exception:
                min_severity = "medium"
            if CONFLICT_SEVERITY_RANK.get(severity, 0) < CONFLICT_SEVERITY_RANK.get(
                min_severity, 2
            ):
                return
            alerted = await self._handle_conflict_alert(
                event, group_id, conf, result, items
            )
            if alerted:
                state["last_alert_ts"] = now
                state["last_alert_mid"] = last_mid
        finally:
            self._conflict_running.discard(str(group_id))

    async def _handle_conflict_alert(
        self,
        event: AstrMessageEvent,
        group_id: str,
        conf: dict[str, Any],
        result: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> bool:
        """发送管理员预警，并按开关发送一条中性群消息；不撤回、不禁言。"""
        group_name = await self._get_group_name(event, group_id)
        occur_time = (
            self._item_time_text(items[-1], "%Y-%m-%d %H:%M:%S")
            if items
            else time.strftime("%Y-%m-%d %H:%M:%S")
        )
        participants = list(result.get("participants") or [])
        ctx = {
            "group_name": group_name,
            "group_id": group_id,
            "occur_time": occur_time or time.strftime("%Y-%m-%d %H:%M:%S"),
            "conflict_type_label": CONFLICT_TYPE_LABELS.get(
                str(result.get("conflict_type") or ""), "群聊争执"
            ),
            "participants": self._format_conflict_participants(participants),
            "summary": str(result.get("summary") or ""),
            "trend_label": CONFLICT_TREND_LABELS.get(
                str(result.get("trend") or ""), "持续对峙"
            ),
            "recent_messages": self._format_conflict_recent(items),
            "suggestion": str(result.get("suggestion") or "先劝停，必要时撤回/禁言"),
        }
        tpl = self._c(
            "conflict_notify_template", DEFAULT_CONFLICT_NOTIFY_TEMPLATE
        )
        text = _render(tpl, ctx)
        notified: list[str] = []
        if bool(conf.get("conflict_notify_admin", True)):
            receivers = await self._collect_receivers(event, group_id, conf)
            for uid in receivers:
                if await self._send_private(event, uid, text, group_id):
                    notified.append(uid)
        mediated = False
        if bool(conf.get("conflict_mediate", True)):
            mediated = await self._send_conflict_mediation(
                event, group_id, participants, conf
            )
        self._audit(
            "conflict_alert",
            report={
                "report_id": "",
                "group_id": group_id,
                "target_id": "",
                "participants": ctx["participants"],
                "occur_time": ctx["occur_time"],
                "context_conclusion": ctx["summary"],
                "cause": ctx["summary"],
            },
            extra={
                "conflict_type": result.get("conflict_type"),
                "severity": result.get("severity"),
                "trend": result.get("trend"),
                "notified": notified,
                "mediated": mediated,
            },
        )
        return bool(notified or mediated)

    async def _send_conflict_mediation(
        self,
        event: AstrMessageEvent,
        group_id: str,
        participants: list[dict[str, Any]],
        conf: dict[str, Any],
    ) -> bool:
        """发送一条中性劝停消息，只艾特冲突双方，不处罚。"""
        parties = [
            p
            for p in participants
            if p.get("role") in ("initiator", "responder") and p.get("id")
        ]
        if not parties:
            return False
        ids = []
        for person in parties[:5]:
            uid = str(person.get("id") or "")
            if uid.isdigit() and uid not in ids:
                ids.append(uid)
        if not ids:
            return False
        tpl = self._c(
            "conflict_mediate_template", DEFAULT_CONFLICT_MEDIATE_TEMPLATE
        )
        text = _render(
            tpl,
            {
                "participants": self._format_conflict_participants(participants),
                "group_id": group_id,
            },
        ).strip()
        if not text:
            return False
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None) if bot else None
        if not bot or not str(group_id).strip().isdigit():
            return False
        segments: list[dict[str, Any]] = []
        for uid in ids:
            segments.append({"type": "at", "data": {"qq": uid}})
            segments.append({"type": "text", "data": {"text": " "}})
        segments.append({"type": "text", "data": {"text": text}})
        try:
            if api is not None:
                await api.call_action(
                    "send_group_msg", group_id=int(group_id), message=segments
                )
            else:
                fallback = " ".join(f"@{uid}" for uid in ids) + " " + text
                await bot.send_group_msg(group_id=int(group_id), message=fallback)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 群 {group_id} 劝停消息发送失败：{e}")
            return False
