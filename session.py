"""
会话管理模块

每个会话维护独立的：
- 对话历史（上下文窗口内）
- system prompt
- 元数据（创建时间、最后活跃时间）
- 最新评估结果（供轮询）
- JSON 文件持久化（防重启丢失）
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import SESSION_TTL, MAX_CONTEXT_ROUNDS, DEFAULT_SYSTEM_PROMPT

from log_config import get_logger
logger = get_logger("session")

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "sessions.json"


@dataclass
class Session:
    session_id: str
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    evaluation: Optional[dict] = None  # 最新评估结果

    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        self.last_active = time.time()

    def get_context(self) -> list[dict]:
        """返回修剪后的对话上下文"""
        max_msgs = MAX_CONTEXT_ROUNDS * 2
        return self.messages[-max_msgs:]

    def is_expired(self) -> bool:
        return time.time() - self.last_active > SESSION_TTL

    def set_evaluation(self, result: dict):
        self.evaluation = result
        self.last_active = time.time()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "system_prompt": self.system_prompt,
            "messages": self.messages[-MAX_CONTEXT_ROUNDS * 2:],
            "created_at": self.created_at,
            "last_active": self.last_active,
            "evaluation": self.evaluation,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            session_id=d["session_id"],
            system_prompt=d.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
            messages=d.get("messages", []),
            created_at=d.get("created_at", time.time()),
            last_active=d.get("last_active", time.time()),
            evaluation=d.get("evaluation"),
        )


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._dirty = False

    def get_or_create(self, session_id: str, system_prompt: str = "") -> Session:
        if session_id not in self._sessions or self._sessions[session_id].is_expired():
            sp = system_prompt or DEFAULT_SYSTEM_PROMPT
            self._sessions[session_id] = Session(
                session_id=session_id, system_prompt=sp
            )
        return self._sessions[session_id]

    def delete(self, session_id: str):
        self._sessions.pop(session_id, None)
        self._dirty = True

    def get_evaluation(self, session_id: str) -> Optional[dict]:
        s = self._sessions.get(session_id)
        return s.evaluation if s else None

    def set_evaluation(self, session_id: str, result: dict):
        s = self._sessions.get(session_id)
        if s:
            s.set_evaluation(result)
            self._dirty = True

    def cleanup_expired(self) -> int:
        expired = [k for k, s in self._sessions.items() if s.is_expired()]
        for k in expired:
            del self._sessions[k]
        if expired:
            self._dirty = True
        return len(expired)

    def status(self) -> dict:
        return {
            "active_sessions": len(self._sessions),
            "session_ids": list(self._sessions.keys()),
        }

    # ==================== Persistence ====================

    def save(self) -> int:
        """保存所有未过期会话到 JSON 文件。返回保存的会话数。"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        active = {k: s.to_dict() for k, s in self._sessions.items() if not s.is_expired()}
        try:
            DATA_FILE.write_text(
                json.dumps(active, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._dirty = False
            return len(active)
        except Exception as e:
            logger.exception("保存会话失败")
            return 0

    def load(self) -> int:
        """从 JSON 文件加载会话。返回加载的会话数。"""
        if not DATA_FILE.exists():
            return 0
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            count = 0
            for session_id, sd in data.items():
                s = Session.from_dict(sd)
                if not s.is_expired():
                    self._sessions[session_id] = s
                    count += 1
            logger.info("已加载 %d 个会话", count)
            return count
        except Exception as e:
            logger.exception("加载会话失败")
            return 0
