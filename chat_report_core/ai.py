"""举报研判与自动审查的 AI 调用、结果归一化。"""

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
    DEFAULT_AUTO_REVIEW_PROMPT,
    DEFAULT_SYSTEM_PROMPT,
    LEGACY_PROMPT_SUFFIX,
)

from .utils import (
    _render,
    _extract_json,
)


class AIMixin:
    """举报研判与自动审查的 AI 调用、结果归一化。"""

    async def _ai_judge(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        group_id: str,
        target_id: str,
        is_reply: bool,
        content: str,
        recent: list[dict[str, Any]],
        *,
        context_items: list[dict[str, Any]] | None = None,
        message_id: str = "",
        target_display: str = "",
        reporter_display: str = "",
        reporter_in_context: bool = False,
    ) -> dict[str, Any]:
        """调用 AI 结合上下文研判举报，返回归一化后的 judgement；失败返回 None。"""
        fallback = {
            "decision": "uncertain",
            "confidence": 0.0,
            "reason": "",
            "target_message_id": "",
            "banter": False,
            "severity": "none",
            "reporter_involved": reporter_in_context,
            "participants": "",
            "cause": "",
            "context_summary": "",
        }
        context_items = context_items or []
        try:
            provider = self._get_provider(event)
            if not provider:
                fallback["reason"] = "未配置可用的 AI 提供商"
                return fallback

            system_tpl = str(self._c("ai_system_prompt", DEFAULT_SYSTEM_PROMPT) or "")
            if not system_tpl.strip():
                system_tpl = DEFAULT_SYSTEM_PROMPT
            if "banter" not in system_tpl:
                # 用户自定义的是旧版提示词，补上上下文/玩笑相关字段要求
                system_tpl += LEGACY_PROMPT_SUFFIX
            system_prompt = _render(
                system_tpl, {"group_rules": conf.get("group_rules", "")}
            )

            lines = [
                f"举报方式：{'引用举报' if is_reply else '无引用举报（@被举报人）'}",
                f"被举报人：{target_display or target_id}",
                f"举报人：{reporter_display or '未知'}"
                f"（是否出现在下方上下文对话中：{'是' if reporter_in_context else '否'}）",
            ]
            if is_reply:
                lines.append(f"被举报消息 ID：{message_id or '未知'}")
                lines.append(f"被举报消息内容：{content or '[非文本消息]'}")
            else:
                lines.append(
                    "说明：本次为无引用举报，请在下方群聊上下文中找出被举报人最近的发言进行判断。"
                )
            if context_items:
                lines.append("")
                lines.append(
                    f"【群聊上下文（按时间顺序，共 {len(context_items)} 条，"
                    "← 标记的是被举报消息）】"
                )
                lines.append(self._format_context_log(context_items))
                lines.append("")
            lines.append(f"被举报人最近 {len(recent)} 条发言（机器人缓存）：")
            if recent:
                for item in recent:
                    lines.append(
                        f"- [{item['time_str']}] (id={item['message_id']}) {item['text']}"
                    )
            else:
                lines.append("- （无缓存记录）")
            lines.append(
                "请结合上下文判断双方关系与语气（是否熟人玩笑互怼、对方是否反感、"
                "举报人是否在场），再严格按照要求输出 JSON 判断结果。"
            )
            vision = self._collect_vision_images(
                conf,
                context_items,
                priority_ids={str(message_id)} if message_id else set(),
                target_label="被举报消息",
            )
            vision_block = self._vision_prompt_block(conf, vision)
            if vision_block:
                lines.insert(len(lines) - 1, vision_block)
            prompt = "\n".join(lines)
            image_urls = list(vision.get("images") or [])

            timeout = int(self._c("ai_timeout", 30))
            retry = int(self._c("ai_retry_times", 1))
            last_err: Exception | None = None
            for attempt in range(retry + 1):
                try:
                    resp = await self._text_chat_with_images(
                        provider,
                        system_prompt,
                        prompt,
                        image_urls,
                        timeout,
                    )
                    raw = getattr(resp, "completion_text", "") or ""
                    data = _extract_json(raw)
                    if not data:
                        last_err = RuntimeError(f"AI 返回无法解析：{raw[:200]}")
                        continue
                    return self._normalize_judgement(data)
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if attempt < retry:
                        await asyncio.sleep(1)
            logger.warning(f"[{PLUGIN_NAME}] AI 研判失败：{last_err}")
            fallback["reason"] = f"AI 调用/解析失败：{last_err}"
            return fallback
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] AI 研判异常：{e}")
            fallback["reason"] = f"AI 研判异常：{e}"
            return fallback

    @staticmethod
    def _banter_should_exempt(
        conf: dict[str, Any], decision: str, banter: bool, severity: str
    ) -> bool:
        """上下文是玩笑互动、且严重程度不是 high 时豁免处罚。"""
        return bool(
            conf.get("banter_guard", True)
            and banter
            and decision == "violate"
            and str(severity or "none") != "high"
        )

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        """把配置/JSON 中的多种真假写法统一转成 bool。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value if value is not None else "").strip().lower()
        if text in ("true", "1", "yes", "y", "是"):
            return True
        if text in ("false", "0", "no", "n", "否", ""):
            return False if text else default
        return default

    @classmethod
    def _normalize_judgement(cls, data: dict[str, Any]) -> dict[str, Any]:
        """兼容旧版 AI 输出字段，统一 decision/confidence/banter 等结构。"""
        raw_decision = str(data.get("decision", "")).strip().lower()
        mapping = {
            "violate": "violate",
            "violation": "violate",
            "违规": "violate",
            "safe": "safe",
            "安全": "safe",
            "uncertain": "uncertain",
            "拿不准": "uncertain",
        }
        decision = mapping.get(raw_decision, "uncertain")
        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except Exception:
            confidence = 0.0
        if confidence > 1:
            confidence = confidence / 100.0
        confidence = max(0.0, min(1.0, confidence))

        severity = str(data.get("severity", "") or "").strip().lower()
        if severity not in ("none", "low", "medium", "high"):
            severity = "none" if decision == "safe" else "medium"

        return {
            "decision": decision,
            "confidence": confidence,
            "reason": str(data.get("reason", "")),
            "target_message_id": str(data.get("target_message_id", "") or ""),
            "banter": cls._as_bool(data.get("banter"), False),
            "severity": severity,
            "reporter_involved": cls._as_bool(data.get("reporter_involved"), True),
            "participants": str(data.get("participants", "") or ""),
            "cause": str(data.get("cause", "") or ""),
            "context_summary": str(data.get("context_summary", "") or ""),
        }

    async def _text_chat_with_images(
        self,
        provider: Any,
        system_prompt: str,
        prompt: str,
        image_urls: list[str],
        timeout: int,
    ) -> Any:
        """调用 AI；有图片时传 image_urls，提供方不支持则退回纯文本。"""
        kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "prompt": prompt,
        }
        if image_urls:
            kwargs["image_urls"] = image_urls
        try:
            return await asyncio.wait_for(
                provider.text_chat(**kwargs), timeout=timeout
            )
        except TypeError as e:
            if not image_urls:
                raise
            logger.warning(
                f"[{PLUGIN_NAME}] 当前 AI 提供方不支持 image_urls，已退回纯文本：{e}"
            )
            kwargs.pop("image_urls", None)
            return await asyncio.wait_for(
                provider.text_chat(**kwargs), timeout=timeout
            )

    def _get_provider(self, event: AstrMessageEvent):
        """按配置选择 AI 提供商；未配置时使用当前会话默认提供商。"""
        provider = None
        provider_id = str(self._c("ai_provider_id", "") or "").strip()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
        if not provider:
            try:
                provider = self.context.get_using_provider(
                    umo=getattr(event, "unified_msg_origin", None)
                )
            except Exception:
                provider = None
        return provider

    def _normalize_auto_findings(
        self,
        data: dict[str, Any],
        items: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """校验并归一化自动审查 findings，只保留候选消息且去重。"""
        item_by_id = {
            str(x.get("message_id")): x for x in items if x.get("message_id")
        }
        candidate_ids = {
            str(x.get("message_id")) for x in candidates if x.get("message_id")
        }
        raw = data.get("findings")
        if not isinstance(raw, list):
            raw = data.get("violations")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            message_id = str(entry.get("message_id") or "").strip()
            if message_id not in candidate_ids:
                continue
            item = item_by_id.get(message_id)
            if not item:
                continue
            try:
                confidence = float(entry.get("confidence", 0.0) or 0.0)
            except Exception:
                confidence = 0.0
            if confidence > 1:
                confidence = confidence / 100.0
            confidence = max(0.0, min(1.0, confidence))
            severity = str(entry.get("severity") or "none").strip().lower()
            if severity not in ("none", "low", "medium", "high"):
                severity = "medium"
            out.append(
                {
                    "message_id": message_id,
                    "item": item,
                    "confidence": confidence,
                    "severity": severity,
                    "banter": self._as_bool(entry.get("banter"), False),
                    "reason": str(entry.get("reason") or ""),
                }
            )
        return out

    async def _ai_auto_review(
        self,
        event: AstrMessageEvent,
        conf: dict[str, Any],
        items: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """对一批群消息做自动审查，返回 {ok, findings, summary, context_conclusion}。"""
        fallback = {
            "ok": False,
            "findings": [],
            "summary": "",
            "context_conclusion": "",
        }
        provider = self._get_provider(event)
        if not provider:
            fallback["summary"] = "未配置可用的 AI 提供商"
            return fallback
        try:
            system_tpl = str(
                self._c("auto_review_system_prompt", DEFAULT_AUTO_REVIEW_PROMPT)
                or DEFAULT_AUTO_REVIEW_PROMPT
            )
            if "findings" not in system_tpl:
                system_tpl = DEFAULT_AUTO_REVIEW_PROMPT
            system_prompt = _render(
                system_tpl, {"group_rules": conf.get("group_rules", "")}
            )
            candidate_ids = {
                str(x.get("message_id"))
                for x in candidates
                if x.get("message_id")
            }
            lines = ["【群聊记录（按时间顺序）】"]
            for item in items:
                message_id = str(item.get("message_id") or "")
                tag = "候选" if message_id in candidate_ids else "上下文"
                ts = self._item_time_text(item, "%m-%d %H:%M:%S") or "--"
                sender_id = str(item.get("sender_id") or "")
                sender_name = str(item.get("sender_name") or sender_id)
                text = str(item.get("text") or "").replace("\n", " ")
                if len(text) > 300:
                    text = text[:300] + "…"
                lines.append(
                    f"- [{tag}] id={message_id} [{ts}] "
                    f"{sender_name}({sender_id})：{text}"
                )
            lines.append(
                "请只从标记为「候选」的消息里挑出违规消息，按 JSON 格式输出。"
            )
            vision = self._collect_vision_images(
                conf,
                items,
                priority_ids=candidate_ids,
                target_label="候选违规消息",
            )
            vision_block = self._vision_prompt_block(conf, vision)
            if vision_block:
                lines.insert(len(lines) - 1, vision_block)
            prompt = "\n".join(lines)
            image_urls = list(vision.get("images") or [])
            timeout = int(self._c("ai_timeout", 30))
            retry = int(self._c("ai_retry_times", 1))
            last_err: Exception | None = None
            for attempt in range(retry + 1):
                try:
                    resp = await self._text_chat_with_images(
                        provider,
                        system_prompt,
                        prompt,
                        image_urls,
                        timeout,
                    )
                    raw = getattr(resp, "completion_text", "") or ""
                    data = _extract_json(raw)
                    if not data:
                        last_err = RuntimeError(f"AI 返回无法解析：{raw[:200]}")
                        continue
                    findings = self._normalize_auto_findings(
                        data, items, candidates
                    )
                    return {
                        "ok": True,
                        "findings": findings,
                        "summary": str(data.get("summary") or ""),
                        "context_conclusion": str(
                            data.get("context_conclusion")
                            or data.get("context_summary")
                            or ""
                        ),
                    }
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if attempt < retry:
                        await asyncio.sleep(1)
            logger.warning(f"[{PLUGIN_NAME}] 自动审查 AI 失败：{last_err}")
            fallback["summary"] = f"AI 调用/解析失败：{last_err}"
            return fallback
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 自动审查 AI 异常：{e}")
            fallback["summary"] = f"自动审查 AI 异常：{e}"
            return fallback
