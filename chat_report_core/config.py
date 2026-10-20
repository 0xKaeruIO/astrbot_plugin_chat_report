"""配置读取、分群覆盖与 WebUI 配置接口。"""

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

try:  # 兼容不同版本的 AstrBot
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except ImportError:  # pragma: no cover
    get_astrbot_plugin_data_path = None

try:  # 独立配置页面（pages/config）读写配置用的插件 Web API
    from astrbot.api.web import error_response, json_response, request
except ImportError:  # pragma: no cover
    error_response = json_response = None
    request = None

from .constants import (
    PLUGIN_NAME,
    DEFAULT_WARN_TEMPLATE,
    DEFAULT_SAFE_GROUP_TEMPLATE,
    DEFAULT_GROUP_RULES,
    MAX_AUTO_REVIEW_PENDING,
    MAX_AUTO_REVIEW_STATE_GROUPS,
)

# 本文件位于 <插件目录>/chat_report_core/config.py；向上两级才是插件目录。
PLUGIN_DIR = Path(__file__).resolve().parents[1]


class ConfigMixin:
    """配置读取、分群覆盖与 WebUI 配置接口。"""

    CONFIG_GROUP_KEYS = ("ai", "groups", "behavior", "templates")

    @staticmethod
    def _resolve_data_dir() -> Path:
        """返回插件数据目录；优先使用 AstrBot 提供的路径，否则退回插件目录。"""
        try:
            if get_astrbot_plugin_data_path:
                base = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
            else:
                base = PLUGIN_DIR.parents[1] / PLUGIN_NAME
        except Exception:
            base = PLUGIN_DIR / "data"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _c(self, key: str, default: Any) -> Any:
        """读取配置项：兼容扁平结构与分组（嵌套）结构。"""
        value: Any = None
        try:
            value = self.config.get(key)
            if value is None:
                for group in self.CONFIG_GROUP_KEYS:
                    sub = self.config.get(group)
                    if isinstance(sub, dict) and sub.get(key) is not None:
                        value = sub.get(key)
                        break
        except Exception:
            value = None
        return default if value is None else value

    def _parse_overrides(self) -> dict[str, dict]:
        """解析 group_overrides JSON，返回 {群号: 覆盖配置}。"""
        raw = self._c("group_overrides", "")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] 分群配置 JSON 解析失败：{e}")
        return {}

    def _group_conf(self, group_id: str) -> dict[str, Any]:
        """合并全局默认值与群级覆盖，返回该群最终配置。"""
        conf: dict[str, Any] = {
            "group_rules": self._c("default_group_rules", DEFAULT_GROUP_RULES),
            "receiver_ids": list(self._c("default_receiver_ids", []) or []),
            "mute_duration": int(self._c("default_mute_duration", 600)),
            "recent_msg_count": int(self._c("recent_msg_count", 10)),
            "report_cooldown": int(self._c("report_cooldown", 60)),
            "daily_limit": int(self._c("daily_report_limit", 20)),
            "notify_admin_when_safe": bool(self._c("notify_admin_when_safe", False)),
            "announce_safe_in_group": bool(
                self._c("announce_safe_in_group", False)
            ),
            "auto_include_group_admins": bool(
                self._c("auto_include_group_admins", False)
            ),
            "reply_content": str(self._c("reply_content", "1") or "1"),
            "warn_template": self._c("warn_template", DEFAULT_WARN_TEMPLATE),
            "safe_group_template": self._c(
                "safe_group_template", DEFAULT_SAFE_GROUP_TEMPLATE
            ),
            # 上下文研判
            "context_enabled": bool(self._c("context_enabled", True)),
            "context_before_count": int(self._c("context_before_count", 30)),
            "context_after_count": int(self._c("context_after_count", 5)),
            "context_fetch_history": bool(self._c("context_fetch_history", True)),
            "context_max_chars": int(self._c("context_max_chars", 3000)),
            "banter_guard": bool(self._c("banter_guard", True)),
            # 图片理解（需要视觉模型）
            "ai_vision_enabled": bool(self._c("ai_vision_enabled", False)),
            "ai_vision_max_images": int(self._c("ai_vision_max_images", 4)),
            "ai_vision_target_images": bool(
                self._c("ai_vision_target_images", True)
            ),
            "ai_vision_context_images": bool(
                self._c("ai_vision_context_images", True)
            ),
            "ai_vision_detect_violation": bool(
                self._c("ai_vision_detect_violation", True)
            ),
            "ai_vision_detect_qr": bool(self._c("ai_vision_detect_qr", True)),
            "ai_vision_detect_diversion": bool(
                self._c("ai_vision_detect_diversion", True)
            ),
            "ai_vision_read_screenshots": bool(
                self._c("ai_vision_read_screenshots", True)
            ),
            "ai_vision_extra_prompt": str(
                self._c("ai_vision_extra_prompt", "") or ""
            ),
            # 证据留存
            "evidence_admin_ids": list(self._c("evidence_admin_ids", []) or []),
            "evidence_required": bool(self._c("evidence_required", True)),
            "evidence_use_forward": bool(self._c("evidence_use_forward", True)),
            "evidence_keep_files": bool(self._c("evidence_keep_files", True)),
            # 机器人不是群管理员时的兜底：只留证转发，不撤回不禁言
            "not_admin_evidence_only": bool(
                self._c("not_admin_evidence_only", True)
            ),
            # 通知附加内容
            "notify_include_context": bool(self._c("notify_include_context", True)),
            "forward_context_records": bool(
                self._c("forward_context_records", True)
            ),
            "notify_admin_on_suspected_malicious": bool(
                self._c("notify_admin_on_suspected_malicious", True)
            ),
            # 自动审查
            "auto_review_enabled": bool(self._c("auto_review_enabled", False)),
            "auto_review_message_threshold": int(
                self._c("auto_review_message_threshold", 0)
            ),
            "auto_review_interval_minutes": int(
                self._c("auto_review_interval_minutes", 0)
            ),
            "auto_review_scope_count": int(self._c("auto_review_scope_count", 50)),
            "auto_review_scope_minutes": int(
                self._c("auto_review_scope_minutes", 60)
            ),
            "auto_review_notify_mode": str(
                self._c("auto_review_notify_mode", "safe_silent") or "safe_silent"
            ),
            "auto_review_max_actions": int(
                self._c("auto_review_max_actions", 3)
            ),
            "auto_review_state_max_groups": int(
                self._c("auto_review_state_max_groups", MAX_AUTO_REVIEW_STATE_GROUPS)
            ),
            "auto_review_pending_cap": int(
                self._c("auto_review_pending_cap", MAX_AUTO_REVIEW_PENDING)
            ),
        }
        override = self._parse_overrides().get(str(group_id))
        if isinstance(override, dict):
            conf.update(override)
        return conf

    def _group_conf_cached(self, group_id: str) -> dict[str, Any]:
        """10 秒缓存的分群配置，避免每条群消息都解析一次 JSON。"""
        key = str(group_id)
        now = time.time()
        cached = self._group_conf_cache.get(key)
        if cached and now - cached[0] < 10:
            return cached[1]
        conf = self._group_conf(key)
        self._group_conf_cache[key] = (now, conf)
        return conf

    @staticmethod
    def _schema_file() -> Path:
        """返回插件根目录下的 _conf_schema.json 路径。"""
        return PLUGIN_DIR / "_conf_schema.json"

    def _schema_defaults(self) -> dict[str, Any]:
        """把 _conf_schema.json 解析为 {分组: {键: 默认值}}。"""
        try:
            schema = json.loads(self._schema_file().read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取 _conf_schema.json 失败：{e}")
            return {}

        def walk(items: Any) -> dict[str, Any]:
            """递归展开 schema 的 object 类型，得到 {配置键: 默认值}。"""
            out: dict[str, Any] = {}
            if not isinstance(items, dict):
                return out
            for key, item in items.items():
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "object":
                    out[key] = walk(item.get("items", {}))
                else:
                    out[key] = item.get("default")
            return out

        return walk(schema)

    def _current_group_conf(self, group: str, defaults: dict[str, Any]) -> dict[str, Any]:
        """读取当前会话所在群的配置；私聊或无群时退回全局默认。"""
        raw = self.config.get(group)
        raw = raw if isinstance(raw, dict) else {}
        conf: dict[str, Any] = {}
        for key, default in defaults.items():
            if key in raw and raw[key] is not None:
                conf[key] = raw[key]
            else:
                fallback = self.config.get(key)
                conf[key] = fallback if fallback is not None else default
        return conf

    def _collect_config(self) -> dict[str, Any]:
        """按 schema 分组收集当前配置，供配置页回填。"""
        defaults = self._schema_defaults()
        return {
            group: self._current_group_conf(group, items)
            for group, items in defaults.items()
            if isinstance(items, dict)
        }

    @staticmethod
    def _coerce_value(value: Any, ref: Any) -> Any:
        """按 schema 默认值的类型把前端提交的值转成正确类型。"""
        if isinstance(ref, bool):
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "是")
            return bool(value)
        if isinstance(ref, int):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return ref
        if isinstance(ref, float):
            try:
                return float(value)
            except (TypeError, ValueError):
                return ref
        if isinstance(ref, list):
            if isinstance(value, list):
                return [str(v).strip() for v in value if str(v).strip()]
            if isinstance(value, str):
                parts = re.split(r"[\n,，;；\s]+", value)
                return [p for p in (x.strip() for x in parts) if p]
            return ref
        if value is None:
            return ref if ref is not None else ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    def _provider_options(self) -> list[dict[str, str]]:
        """列出可用 AI 提供商，供配置页下拉框使用。"""
        options: list[dict[str, str]] = []
        try:
            providers = self.context.get_all_providers()
        except Exception:
            providers = []
        for provider in providers or []:
            try:
                meta = provider.meta()
            except Exception:
                meta = None
            pid = str(getattr(meta, "id", "") or "").strip()
            model = str(getattr(meta, "model", "") or "").strip()
            if not pid:
                continue
            options.append({"id": pid, "label": f"{pid}（{model}）" if model else pid})
        return options

    def _known_groups(self) -> list[dict[str, str]]:
        """运行时见过的群（由群消息事件累积），仅用于页面里的群号联想。"""
        out: list[dict[str, str]] = []
        try:
            for gid, gname in self._group_names.items():
                out.append({"id": str(gid), "name": str(gname or "")})
        except Exception:
            pass
        return out

    def _register_web_apis(self) -> None:
        """向 AstrBot 注册配置页的 GET/POST 接口。"""
        if json_response is None or request is None:
            logger.warning(f"[{PLUGIN_NAME}] 当前 AstrBot 无 astrbot.api.web，跳过配置页接口注册")
            return
        p = PLUGIN_NAME
        self.context.register_web_api(f"/{p}/config", self.api_get_config, ["GET"], "读取插件配置")
        self.context.register_web_api(f"/{p}/config", self.api_save_config, ["POST"], "保存插件配置")
        logger.info(f"[{PLUGIN_NAME}] 配置页接口已注册：/{p}/config (GET/POST)")

    async def api_get_config(self):
        """配置页 GET：返回当前配置、默认值、提供商与已知群。"""
        return json_response(
            {
                "plugin": PLUGIN_NAME,
                "config": self._collect_config(),
                "defaults": self._schema_defaults(),
                "providers": self._provider_options(),
                "known_groups": self._known_groups(),
            }
        )

    async def api_save_config(self):
        """配置页 POST：校验并按 schema 写回分组配置。"""
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体需为 JSON 对象")
        incoming = payload.get("config", payload)
        if not isinstance(incoming, dict):
            return error_response("config 字段需为对象")

        defaults = self._schema_defaults()
        new_conf: dict[str, Any] = {}
        for group, items in defaults.items():
            if not isinstance(items, dict):
                continue
            src = incoming.get(group)
            src = src if isinstance(src, dict) else {}
            stored = self.config.get(group)
            stored = stored if isinstance(stored, dict) else {}
            clean: dict[str, Any] = {}
            for key, default in items.items():
                base = stored.get(key)
                if base is None:
                    base = self._c(key, default)
                ref = default if default is not None else base
                clean[key] = self._coerce_value(src[key], ref) if key in src else base
            new_conf[group] = clean

        try:
            self.config.update(new_conf)
            save_async = getattr(self.config, "save_config_async", None)
            if save_async is not None:
                await save_async(new_conf)
            else:
                self.config.save_config(new_conf)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 保存配置失败：{e}", exc_info=True)
            return error_response(f"保存失败：{e}", status_code=500)

        logger.info(f"[{PLUGIN_NAME}] 通过独立配置页保存了插件配置")
        return json_response({"ok": True, "config": self._collect_config()})
