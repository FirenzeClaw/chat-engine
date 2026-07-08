"""
统一日志配置 — 所有模块通过 get_logger() 获取 logger。

集中管理格式、级别、输出目标，替代 14 个模块各自独立的 logging 配置。
"""

import logging
import sys

from config import LOG_LEVEL

# 模块级单例 handler（所有 logger 共享，避免重复输出）
_handler: logging.Handler | None = None


def _ensure_handler() -> logging.Handler:
    global _handler
    if _handler is None:
        _handler = logging.StreamHandler(sys.stderr)
        _handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        level = getattr(logging, LOG_LEVEL.upper(), logging.INFO) if LOG_LEVEL else logging.INFO
        _handler.setLevel(level)
    return _handler


def get_logger(name: str) -> logging.Logger:
    """获取统一配置的 logger。

    与 logging.getLogger(name) 相同，但自动附加共享的 handler，
    确保所有模块的日志格式一致，级别由 config.LOG_LEVEL 统一控制。
    """
    logger = logging.getLogger(name)
    handler = _ensure_handler()
    if handler not in logger.handlers:
        logger.addHandler(handler)
    logger.setLevel(handler.level)
    # 禁止传播到 root logger（避免重复输出）
    logger.propagate = False
    return logger
