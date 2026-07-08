"""
chat-engine 配置模块 — 集中管理所有环境变量
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

# --- LLM API ---
# 通用认证和基础 URL（辅脑和主脑共用）
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")

# 辅脑：快速、便宜，用于即时回复
LLM_FAST_MODEL = os.getenv("LLM_FAST_MODEL", os.getenv("LLM_MODEL", "deepseek-chat"))
# 主脑：强推理，用于评估/追答生成
LLM_STRONG_MODEL = os.getenv("LLM_STRONG_MODEL", os.getenv("LLM_MODEL", "deepseek-chat"))
LLM_MODEL = LLM_FAST_MODEL

# --- QQ Bot ---
QQ_BOT_APPID = os.getenv("QQ_BOT_APPID", "")
QQ_BOT_SECRET = os.getenv("QQ_BOT_SECRET", "")
QQ_WS_URL = os.getenv("QQ_WS_URL", "wss://api.sgroup.qq.com/websocket")

# --- Memory ---
DB_PATH = os.getenv("DB_PATH", "botuser/memory.db")

# --- HTTP Server (web UI) ---
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "18090"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "10"))

# --- Session ---
SESSION_TTL = int(os.getenv("SESSION_TTL", "3600"))  # 会话过期时间（秒）
MAX_CONTEXT_ROUNDS = int(os.getenv("MAX_CONTEXT_ROUNDS", "20"))  # 最大上下文轮数（回退用）
MAX_CONTEXT_TOKENS = int(os.getenv("MAX_CONTEXT_TOKENS", "4096"))  # 上下文窗口 token 上限
CONTEXT_SATURATION_PCT = float(os.getenv("CONTEXT_SATURATION_PCT", "0.80"))  # 饱和度告警阈值
MEMORY_INJECT_MODE = os.getenv("MEMORY_INJECT_MODE", "auto")  # auto | full | light | off
ACCESS_BOOST_DAYS = int(os.getenv("ACCESS_BOOST_DAYS", "7"))  # 高频访问 boost 判定窗口
ACCESS_BOOST_MIN = int(os.getenv("ACCESS_BOOST_MIN", "3"))  # 高频访问最低次数

# --- Phase 1: 记忆引擎衰减 ---
DECAY_GIST_DAYS = int(os.getenv("DECAY_GIST_DAYS", "90"))   # 模糊层过期天数
DECAY_DETAIL_DAYS = int(os.getenv("DECAY_DETAIL_DAYS", "30"))  # 精确层半衰天数
AUTO_MIGRATE_DAYS = int(os.getenv("AUTO_MIGRATE_DAYS", "60"))  # 精确层自动模糊化天数

# --- Phase 1: 检索 ---
MAX_RETRIEVAL_CANDIDATES = int(os.getenv("MAX_RETRIEVAL_CANDIDATES", "20"))
MAX_RETRIEVAL_RESULTS = int(os.getenv("MAX_RETRIEVAL_RESULTS", "5"))

# --- Phase 1: 集群 ---
CLUSTER_TRIGGER_DAYS = int(os.getenv("CLUSTER_TRIGGER_DAYS", "14"))
CLUSTER_TRIGGER_MIN_ACCESS = int(os.getenv("CLUSTER_TRIGGER_MIN_ACCESS", "3"))

# --- Follow-up ---
FOLLOW_UP_ENABLED = os.getenv("FOLLOW_UP_ENABLED", "true").lower() == "true"
FOLLOW_UP_MAX_PER_HOUR = int(os.getenv("FOLLOW_UP_MAX_PER_HOUR", "5"))  # 每小时最多追答数

# --- System Prompt ---
DEFAULT_SYSTEM_PROMPT = os.getenv("DEFAULT_SYSTEM_PROMPT", (
    "你是一个友好的聊天助手。\n"
    "回复自然简洁，2-4 句话为宜。\n"
    "可以适度使用表情符号，但不要过度。"
))
