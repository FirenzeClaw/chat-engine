"""
JSON 解析工具 — 从 LLM 输出中提取 JSON 的通用函数。

消除 brain、context_manager、engine、image_handler、boredom 中的重复代码。
"""

import json

from log_config import get_logger

logger = get_logger("json_utils")


def parse_json_block(raw: str) -> dict:
    """从 LLM 原始输出中提取并解析 JSON。

    处理常见的 LLM 输出格式：
    - ```json ... ```
    - ``` ... ```
    - 裸 { ... }

    Returns:
        解析后的 dict，解析失败返回空 dict。
    """
    try:
        text = raw.strip()

        if "```json" in text:
            text = text[text.index("```json") + 7:]
            if "```" in text:
                text = text[:text.index("```")]
        elif "```" in text:
            text = text[text.index("```") + 3:]
            if "```" in text:
                text = text[:text.index("```")]
        elif "{" in text and "}" in text:
            start = text.index("{")
            end = text.rindex("}") + 1
            text = text[start:end]

        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug("JSON 解析失败: %s, raw=%s", e, raw[:200])
        return {}
