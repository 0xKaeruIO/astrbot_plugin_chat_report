"""图片理解：从聊天上下文中收集图片，并生成视觉模型提示词。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.core.message.components import Image

from .constants import DEFAULT_VISION_PROMPT, MAX_VISION_IMAGES


class VisionMixin:
    """为举报研判和自动审查提供图片引用与视觉检查提示。"""

    @staticmethod
    def _vision_flag(conf: dict[str, Any], key: str, default: bool = True) -> bool:
        """读取图片开关，兼容 bool 与字符串形式的配置值。"""
        value = conf.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "开启")
        return bool(value)

    def _vision_limit(self, conf: dict[str, Any]) -> int:
        """返回单次最多送图的张数，限制在 1..MAX_VISION_IMAGES。"""
        try:
            value = int(conf.get("ai_vision_max_images", 4) or 4)
        except Exception:
            value = 4
        return max(1, min(MAX_VISION_IMAGES, value))

    @staticmethod
    def _normalize_vision_ref(ref: Any) -> str:
        """把图片引用规范成视觉模型可读取的 URL、base64 或绝对路径。"""
        if not isinstance(ref, str):
            return ""
        ref = ref.strip()
        if not ref:
            return ""
        if ref.startswith(("http://", "https://", "base64://", "file://")):
            return ref
        try:
            path = Path(ref).expanduser()
            if path.exists():
                return str(path.resolve())
        except OSError:
            pass
        return ""

    def _item_image_refs(self, item: dict[str, Any]) -> list[str]:
        """从缓存条目的 components 或历史 raw_segments 中提取图片引用。"""
        refs: list[str] = []
        for comp in item.get("components") or []:
            if not isinstance(comp, Image):
                continue
            ref = (
                getattr(comp, "url", None)
                or getattr(comp, "file", None)
                or getattr(comp, "path", None)
            )
            normalized = self._normalize_vision_ref(ref)
            if normalized:
                refs.append(normalized)
        for seg in item.get("raw_segments") or []:
            if not isinstance(seg, dict) or str(seg.get("type")) != "image":
                continue
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            ref = data.get("url") or data.get("file") or data.get("path")
            normalized = self._normalize_vision_ref(ref)
            if normalized:
                refs.append(normalized)
        return refs

    def _collect_vision_images(
        self,
        conf: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        priority_ids: set[str] | None = None,
        target_label: str = "被举报消息",
    ) -> dict[str, Any]:
        """按上限收集图片：优先目标/候选消息，再收集上下文图片，去重保序。"""
        result: dict[str, Any] = {
            "images": [],
            "target_count": 0,
            "context_count": 0,
            "target_label": target_label,
        }
        if not self._vision_flag(conf, "ai_vision_enabled", False):
            return result

        priority_ids = {str(x) for x in (priority_ids or set()) if str(x)}

        def is_priority(item: dict[str, Any]) -> bool:
            message_id = str(item.get("message_id") or "")
            return bool(item.get("is_target")) or (
                bool(message_id) and message_id in priority_ids
            )

        limit = self._vision_limit(conf)
        seen: set[str] = set()
        images: list[str] = []
        target_count = 0
        context_count = 0

        if self._vision_flag(conf, "ai_vision_target_images", True):
            for item in items:
                if not isinstance(item, dict) or not is_priority(item):
                    continue
                for ref in self._item_image_refs(item):
                    if ref in seen:
                        continue
                    if len(images) >= limit:
                        break
                    seen.add(ref)
                    images.append(ref)
                    target_count += 1
                if len(images) >= limit:
                    break

        if self._vision_flag(conf, "ai_vision_context_images", True):
            for item in items:
                if not isinstance(item, dict) or is_priority(item):
                    continue
                for ref in self._item_image_refs(item):
                    if ref in seen:
                        continue
                    if len(images) >= limit:
                        break
                    seen.add(ref)
                    images.append(ref)
                    context_count += 1
                if len(images) >= limit:
                    break

        result["images"] = images
        result["target_count"] = target_count
        result["context_count"] = context_count
        return result

    def _vision_prompt_block(
        self, conf: dict[str, Any], vision: dict[str, Any]
    ) -> str:
        """根据开关生成图片检查要求；没有图片或未开启时返回空串。"""
        images = vision.get("images") or []
        if not images:
            return ""
        checks: list[str] = []
        if self._vision_flag(conf, "ai_vision_detect_violation", True):
            checks.append(
                "- 违规图片：色情、暴力、血腥、违法、辱骂、恐吓、诈骗等画面或文字。"
            )
        if self._vision_flag(conf, "ai_vision_detect_qr", True):
            checks.append(
                "- 二维码/小程序码/群二维码：是否出现在图片中，是否用于拉群、加好友、导流或收款。"
            )
        if self._vision_flag(conf, "ai_vision_detect_diversion", True):
            checks.append(
                "- 引流图：联系方式、广告、拉群、站外链接、账号导流、刷单、诈骗话术。"
            )
        if self._vision_flag(conf, "ai_vision_read_screenshots", True):
            checks.append(
                "- 聊天记录截图：读取截图中的对话、昵称、时间和上下文，与文字记录对照。"
            )
        if not checks:
            return ""
        extra = str(conf.get("ai_vision_extra_prompt") or "").strip()
        if extra:
            extra = f"补充要求：{extra}"
        return DEFAULT_VISION_PROMPT.format(
            image_count=len(images),
            target_label=str(vision.get("target_label") or "被举报消息"),
            target_count=int(vision.get("target_count") or 0),
            context_count=int(vision.get("context_count") or 0),
            checks="\n".join(checks),
            extra=extra,
        ).strip()
