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
# 推理强度（step-3.7-flash 等推理模型支持: low/medium/high）
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "")

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

# --- Spec 002: 回复调度器 ---
# 私聊防抖窗口（秒）
REPLY_WAIT_PRIVATE_MIN = int(os.getenv("REPLY_WAIT_PRIVATE_MIN", "3"))
REPLY_WAIT_PRIVATE_MAX = int(os.getenv("REPLY_WAIT_PRIVATE_MAX", "8"))
# 群聊等待窗口（秒）
REPLY_WAIT_GROUP_MIN = int(os.getenv("REPLY_WAIT_GROUP_MIN", "15"))
REPLY_WAIT_GROUP_MAX = int(os.getenv("REPLY_WAIT_GROUP_MAX", "60"))
# 群聊随机插话窗口（秒）
REPLY_CHIME_IN_MIN = int(os.getenv("REPLY_CHIME_IN_MIN", "120"))
REPLY_CHIME_IN_MAX = int(os.getenv("REPLY_CHIME_IN_MAX", "360"))
REPLY_CHIME_IN_SPEAKERS = int(os.getenv("REPLY_CHIME_IN_SPEAKERS", "2"))
# 冷却时间（秒）
REPLY_COOLDOWN_PRIVATE = int(os.getenv("REPLY_COOLDOWN_PRIVATE", "5"))
REPLY_COOLDOWN_GROUP = int(os.getenv("REPLY_COOLDOWN_GROUP", "30"))
# Actor/缓冲上限
REPLY_MAX_BUFFER = int(os.getenv("REPLY_MAX_BUFFER", "20"))
REPLY_MAX_ACTORS = int(os.getenv("REPLY_MAX_ACTORS", "50"))
# 焦虑词触发列表（逗号分隔）
REPLY_ANXIETY_TRIGGERS = os.getenv("REPLY_ANXIETY_TRIGGERS", "在吗,在不在,在在在,？？？,人呢,哈喽,hello")
# ThinkingGate 并发与速率限制
THINKING_MAX_CONCURRENT = int(os.getenv("THINKING_MAX_CONCURRENT", "3"))
THINKING_RATE_LIMIT = int(os.getenv("THINKING_RATE_LIMIT", "20"))
THINKING_QUEUE_TIMEOUT_P3 = int(os.getenv("THINKING_QUEUE_TIMEOUT_P3", "5"))
THINKING_QUEUE_TIMEOUT_P4 = int(os.getenv("THINKING_QUEUE_TIMEOUT_P4", "10"))

# --- Follow-up ---
FOLLOW_UP_ENABLED = os.getenv("FOLLOW_UP_ENABLED", "true").lower() == "true"
FOLLOW_UP_MAX_PER_HOUR = int(os.getenv("FOLLOW_UP_MAX_PER_HOUR", "5"))  # 每小时最多追答数

# --- System Prompt ---
DEFAULT_SYSTEM_PROMPT = os.getenv("DEFAULT_SYSTEM_PROMPT", (
    "你是「小夏」，永远18岁的少女，温暖细腻有主见。"
    "回复自然口语化，2-4句话为宜，适度使用 emoji。"
    "诚实不刻薄、关心不越界、有趣不低俗。"
))
