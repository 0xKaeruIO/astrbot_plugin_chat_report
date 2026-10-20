"""群消息缓存、消息链解析、上下文回溯、群成员与昵称解析。"""

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
    MAX_CACHE_PER_USER,
    MAX_GROUP_CACHE,
    _SEG_LABELS,
)


class ContextMixin:
    """群消息缓存、消息链解析、上下文回溯、群成员与昵称解析。"""

    def _cache_message(
        self, event: AstrMessageEvent, group_id: str, sender_id: str, self_id: str
    ):
        """缓存一条群消息到用户队列和整群队列，并累计自动审查待处理条数。"""
        if sender_id == self_id:
            return
        text = (event.message_str or "").strip()
        if not text:
            # 纯图片/语音等消息没有文本，转成 [图片] 这类占位，避免被整条丢弃
            try:
                text = self._chain_to_text(event.get_messages())
            except Exception:
                text = ""
        if not text:
            return
        try:
            message_id = str(event.message_obj.message_id)
        except Exception:
            message_id = ""

        sender_name = ""
        try:
            sender_name = str(event.get_sender_name() or "").strip()
        except Exception:
            sender_name = ""
        # 未加好友时私聊昵称会变成「临时会话(123)」，这种不缓存
        if sender_name and "临时会话" not in sender_name and sender_id not in sender_name:
            self._user_names[sender_id] = sender_name
        sender_name = sender_name or self._user_names.get(sender_id, sender_id)

        entry = {
            "text": text,
            "message_id": message_id,
            "time": int(time.time()),
            "sender_id": str(sender_id),
            "sender_name": str(sender_name),
        }
        # 只有含图片/语音的消息才保留原始消息段，供合并转发恢复媒体
        try:
            raw_chain = list(event.get_messages())
        except Exception:
            raw_chain = []
        if any(isinstance(seg, (Image, Record)) for seg in raw_chain):
            entry["components"] = raw_chain[:20]
        key = f"{group_id}:{sender_id}"
        queue = self._msg_cache.setdefault(key, deque(maxlen=MAX_CACHE_PER_USER))
        queue.append(dict(entry))
        # 整群上下文缓存（包含所有发言人，用于还原一段完整对话）
        group_queue = self._group_cache.setdefault(
            str(group_id), deque(maxlen=MAX_GROUP_CACHE)
        )
        group_queue.append(entry)
        state = self._auto_review_state.setdefault(
            str(group_id),
            {
                "pending": 0,
                "last_review": time.time(),
                "last_mid": "",
                "last_ts": 0,
            },
        )
        state["pending"] = int(state.get("pending", 0) or 0) + 1
        self._auto_review_dirty = True
        # 保存最近事件，供定时审查在群安静时复用
        self._last_events[str(group_id)] = event

    @staticmethod
    def _seg_key(seg: Any) -> str:
        """取消息段的类型名，如 Image / Face（兼容枚举与类名两种情况）。"""
        seg_type = getattr(seg, "type", None)
        return str(getattr(seg_type, "name", "") or "") or type(seg).__name__

    @classmethod
    def _chain_to_text(cls, chain: list) -> str:
        """把消息段链转成可读文本：图片→[图片]、表情→[表情] 等。"""
        parts = []
        for seg in chain or []:
            if isinstance(seg, Plain):
                parts.append(str(getattr(seg, "text", "")))
            elif isinstance(seg, At):
                qq = str(getattr(seg, "qq", ""))
                if qq == "all":
                    parts.append("@全体成员")
                else:
                    parts.append(f"@{getattr(seg, 'name', '') or qq}")
            elif isinstance(seg, Reply):
                continue
            else:
                parts.append(_SEG_LABELS.get(cls._seg_key(seg), "[消息]"))
        return "".join(parts).strip()

    @staticmethod
    def _chain_text_only(chain: list) -> str:
        """只取纯文本部分，用于判断被举报的消息是否有可分析的文字。"""
        return "".join(
            str(getattr(seg, "text", ""))
            for seg in chain or []
            if isinstance(seg, Plain)
        ).strip()

    @staticmethod
    def _event_message_id(event: AstrMessageEvent) -> str:
        """读取事件的消息 ID；失败返回空串。"""
        try:
            return str(event.message_obj.message_id or "")
        except Exception:
            return ""

    @staticmethod
    def _item_time_text(item: dict[str, Any] | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """把缓存消息的时间戳格式化为指定格式；无时间返回空串。"""
        if not item:
            return ""
        ts = int(item.get("time") or 0)
        if ts <= 0:
            return ""
        return time.strftime(fmt, time.localtime(ts))

    @classmethod
    def _raw_chain_to_text(cls, segments: list) -> str:
        """把 get_group_msg_history 返回的原始消息段转成可读文本。"""
        parts: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if seg_type == "text":
                parts.append(str(data.get("text") or ""))
            elif seg_type == "at":
                qq = str(data.get("qq") or "")
                parts.append(
                    "@全体成员" if qq == "all" else f"@{data.get('name') or qq}"
                )
            elif seg_type == "reply":
                continue
            elif seg_type == "face":
                parts.append("[表情]")
            elif seg_type == "image":
                parts.append("[图片]")
            elif seg_type == "record":
                parts.append("[语音]")
            elif seg_type == "video":
                parts.append("[视频]")
            elif seg_type == "file":
                parts.append("[文件]")
            elif seg_type in ("json", "xml"):
                parts.append("[卡片消息]")
            else:
                parts.append("[消息]")
        return "".join(parts).strip()

    @classmethod
    def _parse_history_message(cls, raw: Any) -> dict[str, Any]:
        """把 get_group_msg_history 的单条原始消息转成缓存条目。"""
        if not isinstance(raw, dict):
            return {}
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        sender_id = str(sender.get("user_id") or raw.get("user_id") or "")
        sender_name = str(sender.get("card") or sender.get("nickname") or "").strip()
        message = raw.get("message")
        if isinstance(message, list):
            text = cls._raw_chain_to_text(message)
        else:
            text = str(raw.get("raw_message") or message or "").strip()
        entry = {
            "message_id": str(raw.get("message_id") or raw.get("id") or ""),
            "sender_id": sender_id,
            "sender_name": sender_name or sender_id,
            "text": text,
            "time": int(raw.get("time") or 0),
            "is_target": False,
        }
        if isinstance(message, list) and any(
            isinstance(seg, dict) and str(seg.get("type")) in ("image", "record")
            for seg in message
        ):
            entry["raw_segments"] = message[:20]
        return entry

    async def _fetch_group_history(
        self, event: AstrMessageEvent, group_id: str, count: int
    ) -> list[dict[str, Any]]:
        """调用 get_group_msg_history 补齐本地缓存里没有的群消息。失败返回空列表。"""
        bot = getattr(event, "bot", None)
        if not bot or not str(group_id).strip() or count <= 0:
            return []
        data: Any = None
        api = getattr(bot, "api", None)

        async def _call_history(**kwargs: Any) -> Any:
            """调用 OneBot 的 get_group_msg_history，兼容 bot.api 与 bot 直连。"""
            if api is not None:
                return await api.call_action("get_group_msg_history", **kwargs)
            return await bot.call_action("get_group_msg_history", **kwargs)

        try:
            try:
                data = await asyncio.wait_for(
                    _call_history(
                        group_id=int(group_id),
                        count=int(count),
                        reverseOrder=False,
                    ),
                    timeout=8,
                )
            except asyncio.TimeoutError:
                raise
            except Exception:
                # 部分 OneBot 实现不认识 reverseOrder，去掉该参数重试
                data = await asyncio.wait_for(
                    _call_history(group_id=int(group_id), count=int(count)),
                    timeout=8,
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 拉取群 {group_id} 历史消息失败：{e}")
            return []
        messages = data.get("messages") if isinstance(data, dict) else data
        if not isinstance(messages, list):
            return []
        items = []
        for raw in messages:
            item = self._parse_history_message(raw)
            if item and item.get("text"):
                if item.get("sender_id") and item.get("sender_name"):
                    self._user_names.setdefault(
                        str(item["sender_id"]), str(item["sender_name"])
                    )
                items.append(item)
        items.sort(key=lambda x: (int(x.get("time") or 0), self._mid_sort_key(x)))
        return items

    async def _fetch_message(
        self, event: AstrMessageEvent, message_id: str
    ) -> dict[str, Any]:
        """用 get_msg 取单条消息详情（被举报消息不在缓存里时补原文与发生时间）。"""
        bot = getattr(event, "bot", None)
        if not bot or not str(message_id).strip().isdigit():
            return {}
        api = getattr(bot, "api", None)
        try:
            if api is not None:
                raw = await asyncio.wait_for(
                    api.call_action("get_msg", message_id=int(message_id)), timeout=5
                )
            else:
                raw = await asyncio.wait_for(
                    bot.call_action("get_msg", message_id=int(message_id)), timeout=5
                )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] get_msg({message_id}) 失败：{e}")
            return {}
        return self._parse_history_message(raw)

    @staticmethod
    def _mid_sort_key(item: dict[str, Any]) -> int:
        """消息 ID 排序键：纯数字按数值，否则按字符串。"""
        mid = str(item.get("message_id") or "")
        return int(mid) if mid.isdigit() else 0

    @classmethod
    def _merge_timelines(cls, *timelines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按消息ID合并多条时间线（后传入的覆盖先传入的同ID消息）。"""
        merged: dict[str, dict[str, Any]] = {}
        for items in timelines:
            for item in items or []:
                mid = str(item.get("message_id") or "")
                key = mid or (
                    f"{item.get('sender_id')}::{item.get('time')}::{item.get('text')}"
                )
                merged[key] = dict(item)
        return sorted(
            merged.values(),
            key=lambda x: (int(x.get("time") or 0), cls._mid_sort_key(x)),
        )

    @classmethod
    def _format_context_line(cls, item: dict[str, Any], max_text: int = 180) -> str:
        """把一条上下文消息格式化为「昵称(QQ)：内容」。"""
        ts = cls._item_time_text(item, "%m-%d %H:%M:%S") or "--"
        sender_id = str(item.get("sender_id") or "")
        sender_name = str(item.get("sender_name") or sender_id or "?")
        label = (
            f"{sender_name}({sender_id})"
            if sender_id and sender_id not in sender_name
            else sender_name
        )
        text = str(item.get("text") or "").replace("\n", " ").strip()
        if len(text) > max_text:
            text = text[:max_text] + "…"
        mark = "  ← 被举报消息" if item.get("is_target") else ""
        return f"[{ts}] {label}：{text}{mark}"

    @classmethod
    def _format_context_log(cls, items: list[dict[str, Any]]) -> str:
        """把上下文消息列表拼成多行聊天记录。"""
        if not items:
            return "（未能获取到上下文记录）"
        return "\n".join(cls._format_context_line(item) for item in items)

    @classmethod
    def _participants_text(
        cls, items: list[dict[str, Any]], limit: int = 8
    ) -> str:
        """按出现顺序汇总上下文参与者「昵称(QQ)」，去重。"""
        seen: set[str] = set()
        names: list[str] = []
        for item in items or []:
            sender_id = str(item.get("sender_id") or "")
            sender_name = str(item.get("sender_name") or sender_id).strip()
            key = sender_id or sender_name
            if not key or key in seen:
                continue
            seen.add(key)
            names.append(
                f"{sender_name}({sender_id})"
                if sender_id and sender_id not in sender_name
                else sender_name
            )
            if len(names) >= limit:
                break
        return "、".join(names) if names else "（未知）"

    @classmethod
    def _trim_context(
        cls, items: list[dict[str, Any]], max_chars: int
    ) -> list[dict[str, Any]]:
        """上下文文本超长时，从最早的记录开始裁剪（被举报消息始终保留）。"""
        if max_chars <= 0 or not items:
            return items
        window = list(items)
        while len(window) > 1:
            rendered = cls._format_context_log(window)
            if len(rendered) <= max_chars:
                break
            if window[0].get("is_target"):
                window.pop()
            else:
                window.pop(0)
        return window

    async def _enrich_context_media(
        self,
        event: AstrMessageEvent,
        items: list[dict[str, Any]],
        priority_id: str = "",
    ) -> None:
        """给历史里缺媒体段的消息补 raw_segments，避免后续只剩 [图片] 占位。"""
        candidates = [
            item
            for item in items
            if str(item.get("message_id") or "")
            and not item.get("components")
            and not item.get("raw_segments")
            and any(
                marker in str(item.get("text") or "")
                for marker in ("[图片]", "[语音]")
            )
        ]
        if not candidates:
            return
        candidates.sort(
            key=lambda item: 0
            if str(item.get("message_id") or "") == str(priority_id)
            else 1
        )
        for item in candidates[:6]:
            detail = await self._fetch_message(
                event, str(item.get("message_id") or "")
            )
            segments = detail.get("raw_segments") if detail else None
            if segments:
                item["raw_segments"] = segments

    async def _collect_context(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        group_id: str,
        target_id: str,
        message_id: str,
        fallback_text: str = "",
        target_display: str = "",
        exclude_message_id: str = "",
    ) -> list[dict[str, Any]]:
        """取被举报消息前 N 条 + 后 M 条，组成整群上下文记录。

        Args:
            event: 群消息事件，必要时用于拉取群历史。
            conf: 该群最终配置（context_before_count / context_after_count）。
            group_id: 群号字符串。
            target_id: 被举报人 QQ。
            message_id: 被举报消息 ID；为空表示无引用举报。
            fallback_text: 消息原文兜底（缓存/接口都取不到时使用）。
            target_display: 被举报人展示名，用于无引用举报的占位条目。
            exclude_message_id: 生成日志时需要排除的消息 ID（通常是举报命令本身）。

        Returns:
            按时间升序排列的上下文条目列表。
        """
        before = max(0, int(conf.get("context_before_count", 30)))
        after = max(0, int(conf.get("context_after_count", 5)))

        cache_items = [
            dict(x) for x in (self._group_cache.get(str(group_id)) or [])
        ]
        timeline: list[dict[str, Any]] = [dict(x) for x in cache_items]
        if conf.get("context_fetch_history", True):
            history = await self._fetch_group_history(
                event, group_id, before + after + 20
            )
            if history:
                timeline = self._merge_timelines(history, cache_items)

        if exclude_message_id:
            timeline = [
                x
                for x in timeline
                if str(x.get("message_id")) != str(exclude_message_id)
            ]
        await self._enrich_context_media(event, timeline, message_id)

        target_index: int | None = None
        if message_id:
            for index, item in enumerate(timeline):
                if str(item.get("message_id")) == str(message_id):
                    target_index = index
                    break
        if target_index is None:
            # 缓存/历史里找不到被举报消息（消息较旧或机器人刚重启）时补一条
            if message_id or fallback_text:
                fetched = (
                    await self._fetch_message(event, message_id) if message_id else {}
                )
                timeline.append(
                    {
                        "message_id": str(message_id),
                        "sender_id": str(fetched.get("sender_id") or target_id),
                        "sender_name": str(
                            fetched.get("sender_name") or target_display or target_id
                        ),
                        "text": str(
                            fetched.get("text") or fallback_text or "[非文本消息]"
                        ),
                        "time": int(fetched.get("time") or 0),
                        "is_target": True,
                    }
                )
                target_index = len(timeline) - 1
            elif timeline:
                target_index = len(timeline) - 1

        if target_index is None:
            return []

        start = max(0, target_index - before)
        end = min(len(timeline), target_index + after + 1)
        window = timeline[start:end]
        for item in window:
            item["is_target"] = bool(message_id) and str(
                item.get("message_id")
            ) == str(message_id)

        return self._trim_context(window, int(conf.get("context_max_chars", 3000)))

    def _message_text_by_id(self, group_id: str, message_id: str) -> str:
        """从群缓存按消息 ID 查文本；未命中返回空串。"""
        for item in self._group_cache.get(str(group_id)) or []:
            if str(item.get("message_id")) == str(message_id):
                return str(item.get("text") or "")
        return ""

    def _message_sender_by_id(self, group_id: str, message_id: str) -> str:
        """从群缓存按消息 ID 查发送者 QQ；未命中返回空串。"""
        for item in self._group_cache.get(str(group_id)) or []:
            if str(item.get("message_id")) == str(message_id):
                return str(item.get("sender_id") or "")
        return ""

    async def _group_member_roles(
        self, event: AstrMessageEvent, group_id: str
    ) -> dict[str, str]:
        """群成员角色表 {user_id: owner/admin/member}，带 5 分钟缓存。"""
        key = str(group_id)
        now = time.time()
        cached = self._group_roles_cache.get(key)
        if cached and now - cached[0] < 300:
            return cached[1]
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None) if bot else None
        if not bot or not key.strip():
            return {}
        roles: dict[str, str] = {}
        try:
            if api is not None:
                members = await asyncio.wait_for(
                    api.call_action("get_group_member_list", group_id=int(group_id)),
                    timeout=8,
                )
            else:
                members = await asyncio.wait_for(
                    bot.call_action("get_group_member_list", group_id=int(group_id)),
                    timeout=8,
                )
            for member in members or []:
                uid = str(member.get("user_id") or "")
                if not uid:
                    continue
                roles[uid] = str(member.get("role") or "member")
                name = str(member.get("card") or member.get("nickname") or "").strip()
                if name:
                    self._user_names[uid] = name
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 获取群成员角色失败：{e}")
        if roles:
            self._group_roles_cache[key] = (now, roles)
        return roles

    async def _group_admin_ids(
        self, event: AstrMessageEvent, group_id: str
    ) -> set[str]:
        """获取本群管理员 QQ 列表（含群主）。"""
        roles = await self._group_member_roles(event, group_id)
        ids = {uid for uid, role in roles.items() if role in ("owner", "admin")}
        if ids:
            return ids
        # 成员列表拿不到时退回逐个查询的旧接口
        try:
            members = await event.bot.api.call_action(
                "get_group_member_list", group_id=int(group_id)
            )
            for member in members or []:
                uid = str(member.get("user_id") or "")
                name = str(member.get("card") or member.get("nickname") or "").strip()
                if uid and name:
                    self._user_names[uid] = name
                if str(member.get("role")) in ("owner", "admin"):
                    ids.add(uid)
        except Exception as e:
            logger.debug(f"[{PLUGIN_NAME}] 获取群管理员失败：{e}")
        return ids

    async def _group_owner_ids(
        self, event: AstrMessageEvent, group_id: str
    ) -> list[str]:
        """获取本群群主 QQ 列表。"""
        roles = await self._group_member_roles(event, group_id)
        return [uid for uid, role in roles.items() if role == "owner"]

    async def _bot_group_role(
        self, event: AstrMessageEvent, group_id: str
    ) -> str | None:
        """机器人本人的群角色：owner/admin/member；查不到返回 None。"""
        try:
            bot_id = str(event.get_self_id())
        except Exception:
            return None
        if not bot_id:
            return None
        roles = await self._group_member_roles(event, group_id)
        role = roles.get(bot_id)
        if role:
            return role
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None) if bot else None
        if api is not None:
            try:
                info = await asyncio.wait_for(
                    api.call_action(
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(bot_id),
                    ),
                    timeout=5,
                )
                role = str((info or {}).get("role") or "").strip()
                if role:
                    return role
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] 查询机器人群角色失败：{e}")
        return None

    async def _resolve_name(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> str:
        """解析群成员展示名：群名片 → 昵称 → QQ，并写入昵称缓存。"""
        user_id = str(user_id)
        if not user_id:
            return ""
        cached = self._user_names.get(user_id)
        if cached:
            return cached
        bot = getattr(event, "bot", None)
        if bot and group_id:
            try:
                info = await bot.api.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    no_cache=False,
                )
                name = str(info.get("card") or info.get("nickname") or "").strip()
                if name:
                    self._user_names[user_id] = name
                    return name
            except Exception:
                pass
        return user_id

    async def _stranger_name(self, event: AstrMessageEvent, user_id: str) -> str:
        """取陌生人（未加好友）的 QQ 昵称，避免显示成「临时会话(xxx)」。"""
        bot = getattr(event, "bot", None)
        if not bot:
            return ""
        try:
            info = await bot.api.call_action("get_stranger_info", user_id=int(user_id))
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        return str(info.get("nickname") or info.get("nick") or "").strip()

    async def _resolve_private_name(self, event: AstrMessageEvent, user_id: str) -> str:
        """私聊发送者的展示名：优先群名片缓存，其次 QQ 昵称，最后才用 QQ 号兜底。"""
        user_id = str(user_id)
        cached = self._user_names.get(user_id)
        if cached:
            return cached
        name = ""
        try:
            name = str(event.get_sender_name() or "").strip()
        except Exception:
            name = ""
        # 未加好友时 get_sender_name 会返回「临时会话(123456)」这类占位名
        if not name or "临时会话" in name or user_id in name:
            nick = await self._stranger_name(event, user_id)
            if nick:
                name = nick
        if not name or "临时会话" in name:
            name = user_id
        return name

    async def _get_group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        """获取群名称，带缓存与失败兜底。"""
        group_id = str(group_id)
        if group_id in self._group_names:
            return self._group_names[group_id]
        bot = getattr(event, "bot", None)
        name = group_id
        if bot:
            try:
                info = await bot.api.call_action(
                    "get_group_info", group_id=int(group_id)
                )
                name = str(info.get("group_name") or group_id)
            except Exception:
                pass
        self._group_names[group_id] = name
        return name
