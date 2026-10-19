"""持久化状态、计数、限流、审计与已处理消息记录。"""

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
)


class StateMixin:
    """持久化状态、计数、限流、审计与已处理消息记录。"""

    async def _expire_loop(self):
        """每分钟检查一次待处理举报，超时未处理则标记 expired 并写审计。"""
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                changed = False
                for report in list(self._pending.values()):
                    if report.get("handled") or report.get("expired"):
                        continue
                    expire_at = report.get("created", 0) + int(
                        report.get("expire_minutes", 60)
                    ) * 60
                    if now >= expire_at:
                        report["expired"] = True
                        changed = True
                        logger.info(
                            f"[{PLUGIN_NAME}] 举报 {report['report_id']} 已超时未处理"
                        )
                        self._audit("expired", report=report)
                if changed:
                    self._save_state()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 超时清理异常：{e}")

    def _load_state(self):
        """从 reports.json / counters.json 恢复待处理举报与计数。"""
        try:
            if self.counters_file.exists():
                data = json.loads(self.counters_file.read_text("utf-8"))
                self._report_counts = data.get("report_counts", {}) or {}
                self._warn_counts = data.get("warn_counts", {}) or {}
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取计数失败：{e}")
        try:
            if self.reports_file.exists():
                data = json.loads(self.reports_file.read_text("utf-8"))
                self._pending = data.get("pending", {}) or {}
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取举报记录失败：{e}")

    def _save_state(self):
        """把待处理举报、计数与限流状态原子写回磁盘。"""
        try:
            self._atomic_write(
                self.counters_file,
                {
                    "report_counts": self._report_counts,
                    "warn_counts": self._warn_counts,
                },
            )
            # 只保留未处理的举报，避免文件无限增长
            pending = {
                rid: r
                for rid, r in self._pending.items()
                if not r.get("handled") and not r.get("expired")
            }
            self._pending = pending
            self._atomic_write(self.reports_file, {"pending": pending})
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 保存状态失败：{e}")

    @staticmethod
    def _atomic_write(path: Path, data: Any):
        """先写临时文件再替换目标文件，避免写入中断损坏 JSON。"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)

    def _audit(self, action: str, *, report: dict[str, Any], extra: dict | None = None):
        """追加一条 JSONL 审计记录；写入失败只告警，不影响主流程。"""
        try:
            record = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "action": action,
                "report_id": report.get("report_id", ""),
                "group_id": report.get("group_id", ""),
                "target_id": report.get("target_id", ""),
                "reporter_id": report.get("reporter_id", ""),
                "message_id": report.get("message_id", ""),
                "ai_judgement": report.get("ai_judgement", ""),
                "ai_confidence": report.get("ai_confidence", ""),
                "target_report_count": report.get("target_report_count", 0),
                "reason": report.get("reason", ""),
                # 上下文研判留痕
                "occur_time": report.get("occur_time", ""),
                "participants": report.get("participants", ""),
                "context_conclusion": report.get("context_conclusion", ""),
                "cause": report.get("cause", ""),
            }
            if extra:
                record.update(extra)
            with self.audit_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 审计日志写入失败：{e}")

    def _incr_report_count(self, group_id: str, target_id: str) -> int:
        """被举报人累计被举报次数 +1，返回新值。"""
        key = f"{group_id}:{target_id}"
        self._report_counts[key] = self._report_counts.get(key, 0) + 1
        return self._report_counts[key]

    def _incr_warn_count(self, group_id: str, target_id: str) -> int:
        """被举报人累计警告次数 +1，返回新值。"""
        key = f"{group_id}:{target_id}"
        self._warn_counts[key] = self._warn_counts.get(key, 0) + 1
        return self._warn_counts[key]

    def _can_report(self, sender_id: str, conf: dict[str, Any]) -> bool:
        """判断举报人是否在允许举报的白名单内。"""
        if bool(self._c("allow_everyone_report", True)):
            return True
        whitelist = [str(x) for x in (self._c("reporters_whitelist", []) or [])]
        return sender_id in whitelist

    def _check_rate(
        self, group_id: str, reporter_id: str, conf: dict[str, Any]
    ) -> tuple[bool, str]:
        """检查同群同人的举报冷却与每日上限，返回 (是否允许, 原因)。"""
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        key = f"{group_id}:{reporter_id}"
        state = self._rate.setdefault(key, {"last": 0, "day": today, "count": 0})
        if state.get("day") != today:
            state["day"] = today
            state["count"] = 0

        cooldown = int(conf.get("report_cooldown", 60))
        if cooldown > 0 and now - state.get("last", 0) < cooldown:
            return False, "冷却中"

        daily_limit = int(conf.get("daily_limit", 20))
        if daily_limit > 0 and state.get("count", 0) >= daily_limit:
            return False, "已达每日上限"

        state["last"] = now
        state["count"] = state.get("count", 0) + 1
        return True, ""

    def _recent_messages(
        self, group_id: str, target_id: str, count: int
    ) -> list[dict[str, Any]]:
        """取某群某用户最近 N 条缓存消息，用于无引用举报。"""
        queue = self._msg_cache.get(f"{group_id}:{target_id}")
        if not queue:
            return []
        items = list(queue)[-max(1, int(count)):]
        result = []
        for item in items:
            result.append(
                {
                    "text": item["text"],
                    "message_id": item["message_id"],
                    "time_str": time.strftime("%H:%M:%S", time.localtime(item["time"])),
                }
            )
        return result

    @staticmethod
    def _recent_times(recent: list[dict[str, Any]]) -> str:
        """把最近消息的时间格式化为「HH:MM:SS」并用顿号连接。"""
        if not recent:
            return "（无记录）"
        return "、".join(item["time_str"] for item in recent)

    @staticmethod
    def _build_ctx(report: dict[str, Any], handler: str = "") -> dict[str, Any]:
        """把待处理举报记录转成模板渲染上下文。"""
        return {
            "group_name": report.get("group_name", report.get("group_id", "")),
            "group_id": report.get("group_id", ""),
            "sender": report.get("sender", ""),
            "target": report.get("target", ""),
            "reporter": report.get("reporter", ""),
            "message": report.get("message", ""),
            "message_id": report.get("message_id", ""),
            "target_report_count": report.get("target_report_count", 0),
            "warn_count": report.get("warn_count", 0),
            "ai_judgement": report.get("ai_judgement", ""),
            "ai_confidence": report.get("ai_confidence", ""),
            "report_id": report.get("report_id", ""),
            "recent_times": report.get("recent_times", ""),
            "recent_count": report.get("recent_count", 0),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "handler": handler,
            "mute_duration": report.get("mute_duration", ""),
            # 上下文与起因（已处理的举报再次渲染模板时仍可用）
            "context_log": report.get("context_log", ""),
            "context_count": report.get("context_count", 0),
            "participants": report.get("participants", ""),
            "occur_time": report.get("occur_time", ""),
            "context_conclusion": report.get("context_conclusion", ""),
            "cause": report.get("cause", ""),
            "malicious_hint": report.get("malicious_hint", ""),
        }
