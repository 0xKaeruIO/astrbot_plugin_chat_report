"""无状态工具函数与共享异常。"""

from __future__ import annotations

import json
import re
from typing import Any

_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _render(template: str, ctx: dict[str, Any]) -> str:
    """只替换已知的 {变量}，保留 JSON 等字面量花括号，未提供的变量原样保留。"""
    text = str(template).replace("{{", "{").replace("}}", "}")

    def repl(match: re.Match) -> str:
        """把已知模板变量替换为 ctx 中的值；未知变量保持原样。"""
        key = match.group(1)
        return str(ctx[key]) if key in ctx else match.group(0)

    return _VAR_RE.sub(repl, text)


def _fmt_duration(seconds: int) -> str:
    """把秒数格式化为「N分钟」或「N秒」的中文短文本。"""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0分钟"
    if seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def _extract_json(text: str) -> dict[str, Any] | None:
    """从 AI 回复中提取第一个 JSON 对象；解析失败返回 None。"""
    if not text:
        return None
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class EvidenceDeliveryError(RuntimeError):
    """证据未能私聊送达任何指定管理员，此时不允许执行撤回/禁言。"""
