"""
用户数据管理（平台无关）

为每个 QQ 用户维护独立的会话目录：
    botuser/{user_id}/
        ├── config.json        # 用户配置
        ├── messages.json      # 消息历史
        └── kimi_chat_history.json  # AI 对话历史
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent / "botuser"


def get_user_dir(user_id: str) -> Path:
    """获取用户数据目录，不存在则创建"""
    user_dir = BASE_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def save_message(user_id: str, msg: dict):
    """保存消息到用户历史"""
    user_dir = get_user_dir(user_id)
    msg_file = user_dir / "messages.json"
    messages = []
    if msg_file.exists():
        try:
            messages = json.loads(msg_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    messages.append(msg)
    if len(messages) > 200:
        messages = messages[-200:]
    msg_file.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")


def load_messages(user_id: str) -> list:
    """加载用户历史消息"""
    msg_file = get_user_dir(user_id) / "messages.json"
    if msg_file.exists():
        return json.loads(msg_file.read_text(encoding="utf-8"))
    return []


def get_chat_history(user_id: str, max_rounds: int = 200) -> list:
    """获取 AI 对话历史（用于上下文注入）"""
    history_file = get_user_dir(user_id) / "kimi_chat_history.json"
    if history_file.exists():
        history = json.loads(history_file.read_text(encoding="utf-8"))
        return history[-max_rounds:]
    return []


def save_chat_history(user_id: str, history: list):
    """保存 AI 对话历史"""
    history_file = get_user_dir(user_id) / "kimi_chat_history.json"
    history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def get_config(user_id: str) -> dict:
    """获取用户配置"""
    config_file = get_user_dir(user_id) / "config.json"
    if config_file.exists():
        return json.loads(config_file.read_text(encoding="utf-8"))
    return {"user_id": user_id, "created_at": str(Path(config_file).stat().st_ctime) if config_file.exists() else ""}


# ==================== MemoryStore 委托 ====================

async def save_profile(user_id: str, profile: dict) -> int:
    """保存用户资料到 memory_store（异步）。"""
    from memory_store import set as mem_set
    return await mem_set(
        f"user/{user_id}/profile", "profile",
        json.dumps(profile, ensure_ascii=False)
    )


async def save_conversation_summary(user_id: str, summary_data: dict) -> int:
    """保存对话摘要到 memory_store（异步）。"""
    from memory_store import set as mem_set
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{date_str}-{summary_data.get('message_count', 0)}"
    return await mem_set(
        f"user/{user_id}/conversations", key,
        json.dumps(summary_data, ensure_ascii=False)
    )


async def save_fact(user_id: str, fact_key: str, fact_data: dict) -> int:
    """保存用户事实到 memory_store（异步）。"""
    from memory_store import set as mem_set
    return await mem_set(
        f"user/{user_id}/facts", fact_key,
        json.dumps(fact_data, ensure_ascii=False)
    )


async def get_profile(user_id: str) -> Optional[dict]:
    """从 memory_store 读取用户资料（异步）。"""
    from memory_store import get as mem_get
    result = await mem_get(f"user/{user_id}/profile", "profile")
    if result:
        return json.loads(result["value"])
    return None
