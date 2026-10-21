"""AstrBot 聊天举报插件入口。

职责：注册插件、初始化共享状态、接收群消息/私聊事件，并委托给 chat_report_core 下的
配置、上下文、AI、证据、通知、举报与自动审查模块处理。
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .chat_report_core import (
    AIMixin,
    AutoReviewMixin,
    ConfigMixin,
    ConflictMixin,
    ContextMixin,
    EvidenceMixin,
    NotifyMixin,
    ReportMixin,
    StateMixin,
    VisionMixin,
    constants,
)
from .chat_report_core.constants import (
    DEFAULT_BROADCAST_TEMPLATE,
    DEFAULT_RESOLVED_TEMPLATE,
    PLUGIN_NAME,
)
from .chat_report_core.utils import _render


@register(
    "astrbot_plugin_chat_report",
    "yosers",
    "聊天消息举报：结合上下文研判，证据先私聊转发指定管理员再撤回禁言，拿不准转人工处理，支持按群自动审查",
    "1.0.11",
)
class ChatReportPlugin(
    ConfigMixin,
    StateMixin,
    ContextMixin,
    AIMixin,
    VisionMixin,
    EvidenceMixin,
    NotifyMixin,
    ReportMixin,
    AutoReviewMixin,
    ConflictMixin,
    Star,
):
    """聊天举报插件：组合各职责 Mixin，自身只保留生命周期与事件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        """初始化插件配置、数据目录、缓存、待处理举报与后台任务状态。"""
        super().__init__(context)
        self.context = context
        self.config = config

        self.data_dir = self._resolve_data_dir()
        self.reports_file = self.data_dir / "reports.json"
        self.counters_file = self.data_dir / "counters.json"
        self.auto_review_state_file = self.data_dir / "auto_review_state.json"
        self.audit_file = self.data_dir / "audit.jsonl"
        # 处理前留存的证据（违规发言 + 上下文 + AI 结论）
        self.evidence_dir = self.data_dir / "evidence"

        # group:user -> deque[{text, message_id, time, sender_id, sender_name}]
        self._msg_cache: dict[str, deque] = {}
        # group -> deque[{text, message_id, time, sender_id, sender_name}]（整段对话，用于上下文）
        self._group_cache: dict[str, deque] = {}
        # (group:user) -> 被举报次数
        self._report_counts: dict[str, int] = {}
        # (group:user) -> 警告次数
        self._warn_counts: dict[str, int] = {}
        # report_id -> pending report
        self._pending: dict[str, dict[str, Any]] = {}
        # 举报冷却/频率: group:reporter -> {"last": ts, "day": "2026-10-03", "count": n}
        self._rate: dict[str, dict[str, Any]] = {}
        # group_id -> group_name
        self._group_names: dict[str, str] = {}
        # group_id -> (缓存时间, {user_id: role})，用于判断机器人是否本群管理员/找群主
        self._group_roles_cache: dict[str, tuple[float, dict[str, str]]] = {}
        # group_id -> 分群配置短缓存
        self._group_conf_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        # 自动审查状态：group_id -> {pending, last_review, last_mid, last_ts}
        self._auto_review_state: dict[str, dict[str, Any]] = {}
        self._auto_review_running: set[str] = set()
        self._auto_review_dirty = False
        # 已发送过违规通知的消息 ID -> 时间戳，TTL 内不重复标记/通知
        self._auto_review_notified: dict[str, float] = {}
        # 每个群最近一次事件，供定时自动审查在群安静时也能触发
        self._last_events: dict[str, Any] = {}
        self._auto_review_task: asyncio.Task | None = None
        # group_id -> {last_scan_mid, last_alert_ts, last_alert_mid}
        self._conflict_state: dict[str, dict[str, Any]] = {}
        self._conflict_running: set[str] = set()
        self._conflict_task: asyncio.Task | None = None
        # 已处理过的消息 ID（避免自动审查重复处罚同一条消息）
        self._acted_message_ids: deque = deque(maxlen=500)
        # user_id -> 展示名（群里解析到的名片/昵称），私聊通知时用来避免显示成“临时会话(xxx)”
        self._user_names: dict[str, str] = {}
        self._expire_task: asyncio.Task | None = None

        self._load_state()
        self._load_auto_review_state()
        self._register_web_apis()

    async def initialize(self):
        """插件加载时启动举报超时清理与自动审查定时任务。"""
        self._expire_task = asyncio.create_task(self._expire_loop())
        self._auto_review_task = asyncio.create_task(self._auto_review_loop())
        self._conflict_task = asyncio.create_task(self._conflict_loop())
        logger.info(f"[{PLUGIN_NAME}] 举报插件已加载，数据目录：{self.data_dir}")

    async def terminate(self):
        """插件卸载时取消后台任务并保存持久化状态。"""
        if self._expire_task:
            self._expire_task.cancel()
        if self._auto_review_task:
            self._auto_review_task.cancel()
        if self._conflict_task:
            self._conflict_task.cancel()
        self._save_state()
        self._save_auto_review_state()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AiocqhttpMessageEvent):
        """群消息入口：缓存消息，命中举报则处理，否则检查自动审查触发条件。"""
        try:
            group_id = str(event.get_group_id())
            sender_id = str(event.get_sender_id())
            self_id = str(event.get_self_id())
        except Exception:
            return

        if not self._is_monitored_group(group_id):
            return

        self._cache_message(event, group_id, sender_id, self_id)

        if sender_id == self_id:
            return

        parsed = self._parse_report(event, self_id)
        if not parsed:
            # 普通群消息：检查自动审查的「条数 / 时间」触发条件
            try:
                await self._maybe_auto_review(event, group_id)
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 自动审查异常：{e}", exc_info=True)
            return

        # @ 了机器人发「举报」但没说举报谁 → 回一条使用提示
        if parsed.get("no_target"):
            event.stop_event()
            try:
                await event.send(
                    event.plain_result(
                        "请艾特被举报人，或引用他发送的消息，再艾特我发送「举报」。"
                    )
                )
            except Exception as e:
                logger.error(f"[{PLUGIN_NAME}] 举报提示发送失败：{e}")
            return

        # 命中举报：阻止后续 LLM 等处理器
        event.stop_event()
        try:
            await self._handle_report(event, group_id, sender_id, self_id, parsed)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 处理举报异常：{e}", exc_info=True)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊入口：管理员/指定用户回复处理口令时，把对应举报标记为完成。"""
        text = (event.message_str or "").strip()
        if not text:
            return
        sender_id = str(event.get_sender_id())

        # 找到该用户可处理、且尚未处理的举报
        report = None
        for item in self._pending.values():
            if item.get("handled") or item.get("expired"):
                continue
            if sender_id not in item.get("notified", []):
                continue
            allowed = item.get("allowed_reply_content", ["1"])
            if text not in allowed:
                continue
            # 指定用户是否和管理员同等处理
            if not item.get("receiver_can_handle", True) and sender_id not in item.get(
                "admin_ids", []
            ):
                continue
            report = item
            break

        if not report:
            return

        # 阻止这条「1」继续走 AstrBot 的 LLM 流程（否则会同时触发一次主动回复）
        event.stop_event()

        report["handled"] = True
        report["handler"] = sender_id
        handler_name = await self._resolve_private_name(event, sender_id)
        handler_display = f"{handler_name}({sender_id})"
        report["handler_display"] = handler_display

        group_id = str(report.get("group_id") or "")
        ctx = self._build_ctx(report, handler=handler_display)
        resolved_tpl = self._c("resolved_notify_template", DEFAULT_RESOLVED_TEMPLATE)
        resolved_text = _render(resolved_tpl, ctx)

        # 通知其他已通知对象（不含处理人）
        failed: list[str] = []
        if bool(self._c("notify_on_resolved", True)):
            for uid in report.get("notified", []):
                if uid == sender_id:
                    continue
                if not await self._send_private(event, uid, resolved_text, group_id):
                    failed.append(uid)
        if failed:
            await self._notify_in_group(event, group_id, failed, resolved_text)

        # 回复处理人，确认已记录
        await self._send_private(
            event, sender_id, f"已记录，本次举报（{report['report_id']}）由你处理完成。"
        )

        # 可选群内播报
        if bool(self._c("broadcast_result_in_group", False)):
            broadcast_tpl = self._c(
                "group_broadcast_template", DEFAULT_BROADCAST_TEMPLATE
            )
            await self._send_group(
                event, group_id, _render(broadcast_tpl, ctx)
            )

        self._audit("resolved", report=report, extra={"handler": sender_id})
        self._save_state()
