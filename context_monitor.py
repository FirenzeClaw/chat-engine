"""
上下文监测模块

提供 per-session 和全局的上下文健康监测：
- Token 估算（中文 1 字≈1 token，英文 1 词≈1.3 token）
- 上下文饱和度检测
- 会话空闲检测
- 全局监测摘要

不依赖 engine 内部状态，通过 session_manager 暴露的接口读取数据。
"""

import logging
import time
import re
from typing import Optional

from config import MAX_CONTEXT_TOKENS, CONTEXT_SATURATION_PCT

logger = logging.getLogger("context_monitor")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[monitor] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def estimate_tokens(text: str) -> int:
    """粗略 token 估算。

    中文/日文/韩文: 1 字符 ≈ 1 token
    英文: 1 词 ≈ 1.3 token
    标点/符号: 忽略
    """
    cjk = len(re.findall(
        r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text
    ))
    rest = re.sub(
        r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', ' ', text
    )
    words = len(rest.split())
    return cjk + int(words * 1.3)


def _session_health(session) -> dict:
    """计算单个 session 的健康指标。"""
    messages = session.messages
    total_tokens = sum(estimate_tokens(m["content"]) for m in messages)
    usage_pct = round(total_tokens / MAX_CONTEXT_TOKENS, 3) if MAX_CONTEXT_TOKENS > 0 else 0
    idle_s = time.time() - session.last_active

    if session.is_expired():
        status = "expired"
    elif usage_pct > 0.95:
        status = "critical"
    elif usage_pct > CONTEXT_SATURATION_PCT:
        status = "saturated"
    elif idle_s > 1800:  # 30 分钟无活动
        status = "idle"
    else:
        status = "healthy"

    return {
        "session_id": session.session_id,
        "message_count": len(messages),
        "estimated_tokens": total_tokens,
        "max_tokens": MAX_CONTEXT_TOKENS,
        "context_usage_pct": usage_pct,
        "saturated": usage_pct > CONTEXT_SATURATION_PCT,
        "idle_seconds": round(idle_s, 1),
        "status": status,
    }


def check_session(session) -> dict:
    """检查单个 session 健康状态。

    当饱和度超过阈值时自动打 warning 日志。
    """
    report = _session_health(session)

    if report["status"] == "critical":
        logger.warning(
            "上下文严重饱和: session=%s tokens=%d/%d (%.0f%%)",
            session.session_id[:12], report["estimated_tokens"],
            report["max_tokens"], report["context_usage_pct"] * 100,
        )
    elif report["status"] == "saturated":
        logger.info(
            "上下文饱和: session=%s tokens=%d/%d (%.0f%%)",
            session.session_id[:12], report["estimated_tokens"],
            report["max_tokens"], report["context_usage_pct"] * 100,
        )

    return report


async def global_monitor() -> dict:
    """全局监测摘要：所有 session 的健康分布。

    返回：
        {
            total_sessions: int,
            healthy: int,
            saturated: int,
            critical: int,
            idle: int,
            expired: int,
            avg_tokens: float,
            sessions: [per-session health report, ...],
        }
    """
    from engine import session_manager

    sessions = list(session_manager._sessions.values())
    if not sessions:
        return {
            "total_sessions": 0,
            "healthy": 0, "saturated": 0, "critical": 0,
            "idle": 0, "expired": 0,
            "avg_tokens": 0,
            "sessions": [],
        }

    reports = [_session_health(s) for s in sessions]
    counts = {"healthy": 0, "saturated": 0, "critical": 0, "idle": 0, "expired": 0}
    for r in reports:
        status = r["status"]
        if status in counts:
            counts[status] += 1

    avg_tokens = sum(r["estimated_tokens"] for r in reports) / len(reports) if reports else 0

    return {
        "total_sessions": len(sessions),
        **counts,
        "avg_tokens": round(avg_tokens, 1),
        "sessions": reports,
    }
