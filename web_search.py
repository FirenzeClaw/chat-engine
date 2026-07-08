"""
网页搜索模块 — DuckDuckGo 搜索 + 页面抓取 + 摘要

Spec 003: 多模态自主 AI — Layer 2

特性:
- 对话中触发搜索 ([SEARCH: query])
- 自主好奇搜索扩充记忆
- 速率限制 (≤5/hr conversation, ≤10/day auto)
"""

import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from config import (
    WEB_SEARCH_MAX_PER_HOUR,
    WEB_SEARCH_AUTO_MAX_PER_DAY,
    WEB_FETCH_TIMEOUT,
)

from log_config import get_logger
logger = get_logger("web_search")


# ==================== Search ====================

async def search(query: str, max_results: int = 3) -> list[dict]:
    """执行 DuckDuckGo 搜索，在线程池中运行同步 API。

    Args:
        query: 搜索关键词
        max_results: 最大返回结果数

    Returns:
        [{title, url, snippet}] 列表
    """
    try:
        # 在线程池中运行同步 API
        results = await asyncio.to_thread(_sync_search, query, max_results)
        return results
    except Exception:
        logger.exception("DuckDuckGo 搜索失败")
        return []


def _sync_search(query: str, max_results: int = 3) -> list[dict]:
    """同步 DuckDuckGo 搜索实现。"""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "")[:500],
                }
                for r in raw
            ]
    except ImportError:
        logger.warning("duckduckgo-search 未安装，搜索不可用")
        return []
    except Exception as e:
        logger.warning("DuckDuckGo 搜索异常: %s", e)
        return []


# ==================== Page Fetch ====================

async def fetch_page_content(url: str, max_chars: int = 2000) -> str:
    """获取网页内容并提取文本。

    Args:
        url: 页面 URL
        max_chars: 最大返回字符数

    Returns:
        提取的文本内容
    """
    try:
        timeout = aiohttp.ClientTimeout(total=WEB_FETCH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; ChatEngine/1.0)"
            }
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return ""
                # 先尝试 text()，编码异常时降级为 binary + charset 检测
                try:
                    html = await resp.text()
                except (UnicodeDecodeError, LookupError):
                    raw_bytes = await resp.read()
                    # 简单尝试常见编码
                    for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
                        try:
                            html = raw_bytes.decode(enc)
                            break
                        except (UnicodeDecodeError, LookupError):
                            continue
                    else:
                        html = raw_bytes.decode("utf-8", errors="replace")
                text = _strip_html(html)
                return text[:max_chars]
    except asyncio.TimeoutError:
        logger.debug("页面抓取超时: %s", url[:60])
        return ""
    except Exception:
        logger.debug("页面抓取失败: %s", url[:60])
        return ""


def _strip_html(html: str) -> str:
    """简单去除 HTML 标签，提取纯文本。"""
    # 去除 script/style
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # 去除标签
    text = re.sub(r'<[^>]+>', ' ', html)
    # 合并空白
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ==================== Search + Summarize ====================

async def search_and_summarize(query: str, check_limit: bool = False) -> str:
    """搜索 + 抓取页面 + 拼接摘要文本，直接供 LLM 消费。

    Args:
        query: 搜索关键词
        check_limit: 是否检查对话搜索速率限制（>5/hr 返回 ""）

    Returns:
        拼接的摘要文本，无结果或超限时返回空字符串
    """
    if check_limit:
        try:
            from web_search import get_web_manager
            wm = get_web_manager()
            if not wm.can_search_conversation():
                logger.info("搜索频率限制 (>5/hr)，跳过: %s", query[:30])
                return ""
            wm.record_search(is_conversation=True)
        except Exception:
            pass
    results = await search(query, max_results=3)
    if not results:
        return ""

    lines = []
    for i, r in enumerate(results):
        lines.append(f"[{i+1}] 标题: {r['title']}")
        lines.append(f"链接: {r['url']}")
        snippet = r.get("snippet", "")
        if snippet:
            lines.append(f"摘要: {snippet}")
        lines.append("")

    return "\n".join(lines).strip()


# ==================== WebSearchManager (Rate Limiting) ====================

class WebSearchManager:
    """网页搜索管理器 — 速率限制 + 自主搜索"""

    def __init__(self):
        self._conv_timestamps: list[float] = []     # 对话搜索时间戳（滑动窗口 1h）
        self._auto_timestamps: list[float] = []     # 自主搜索时间戳（滑动窗口 24h）
        self._auto_cooldown_until: float = 0        # 自主搜索冷却截止时间

    def _prune_conv(self, now: float):
        """清理 1 小时前的对话搜索记录"""
        cutoff = now - 3600
        self._conv_timestamps = [t for t in self._conv_timestamps if t >= cutoff]

    def _prune_auto(self, now: float):
        """清理 24 小时前的自主搜索记录"""
        cutoff = now - 86400
        self._auto_timestamps = [t for t in self._auto_timestamps if t >= cutoff]

    def can_search_conversation(self) -> bool:
        """检查是否可以执行对话搜索（≤5/hr）。"""
        now = time.monotonic()
        self._prune_conv(now)
        return len(self._conv_timestamps) < WEB_SEARCH_MAX_PER_HOUR

    def can_search_auto(self) -> bool:
        """检查是否可以执行自主搜索（≤10/day + cooldown）。"""
        now = time.monotonic()
        self._prune_auto(now)
        if now < self._auto_cooldown_until:
            return False
        return len(self._auto_timestamps) < WEB_SEARCH_AUTO_MAX_PER_DAY

    def record_search(self, is_conversation: bool = True):
        """记录一次搜索。"""
        now = time.monotonic()
        if is_conversation:
            self._conv_timestamps.append(now)
        else:
            self._auto_timestamps.append(now)

    def set_auto_cooldown(self, seconds: float = 3600):
        """设置自主搜索冷却（默认 1h）。"""
        self._auto_cooldown_until = time.monotonic() + seconds


# ==================== Auto Curious Search ====================

async def _auto_curious_search(manager: WebSearchManager) -> Optional[str]:
    """自主好奇搜索：从记忆系统抽取高热度话题，执行搜索并存储结果。

    仅在 can_search_auto() 返回 True 时执行。

    Returns:
        搜索结果摘要文本，失败或无结果返回 None
    """
    if not manager.can_search_auto():
        return None

    try:
        from memory_store import get_top_tags
        tags = await get_top_tags(limit=10)
        if not tags:
            logger.debug("自主搜索: 无话题标签可用")
            return None

        # 随机挑选一个话题
        import random
        topic = random.choice(tags)
        logger.info("自主搜索: 话题=%s", topic)

        summary = await search_and_summarize(topic)
        if not summary:
            manager.record_search(is_conversation=False)
            return None

        # 存储到 global/knowledge
        from memory_store import set as mem_set
        now = datetime.now(timezone.utc)
        knowledge = {
            "topic": topic,
            "summary": summary,
            "fetched_at": now.isoformat(),
        }
        await mem_set(
            "global/knowledge",
            now.strftime("%Y%m%d-%H%M%S"),
            json.dumps(knowledge, ensure_ascii=False),
        )

        manager.record_search(is_conversation=False)
        logger.info("自主搜索已存储: topic=%s", topic)
        return summary
    except Exception:
        logger.exception("自主搜索失败")
        return None


# 模块级单例
_web_manager: Optional[WebSearchManager] = None


def get_web_manager() -> WebSearchManager:
    """获取全局单例 WebSearchManager。"""
    global _web_manager
    if _web_manager is None:
        _web_manager = WebSearchManager()
    return _web_manager
