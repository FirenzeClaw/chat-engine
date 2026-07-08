"""
个性权重模块 — 8 维权重贯通所有决策

Spec 003: 多模态自主 AI — Layer 4

8 维权重 (0-1):
- curiosity: 好奇心
- sociability: 社交性
- playfulness: 幽默感
- empathy: 共情力
- assertiveness: 主见度
- creativity: 创造性
- impulsiveness: 冲动性
- loyalty: 忠诚度
"""

import json
import random
from dataclasses import dataclass, field, asdict
from typing import Optional

from config import PERSONALITY_WEIGHTS as _PERSONALITY_WEIGHTS_DEFAULT

from log_config import get_logger
logger = get_logger("personality")


# ==================== Dataclass ====================

@dataclass
class PersonalityWeights:
    """8 维个性权重"""
    curiosity: float = 0.7
    sociability: float = 0.8
    playfulness: float = 0.6
    empathy: float = 0.5
    assertiveness: float = 0.3
    creativity: float = 0.6
    impulsiveness: float = 0.2
    loyalty: float = 0.75

    def as_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> bool:
        """验证所有权重在 0-1 范围内。"""
        for name in self.__dataclass_fields__:
            val = getattr(self, name)
            if val < 0 or val > 1:
                return False
        return True


# ==================== Personality Class ====================

class Personality:
    """个性管理器 — 决策函数 + 风格调制"""

    def __init__(self, weights: Optional[PersonalityWeights] = None):
        if weights is None:
            weights = self._load_from_env()
        self.weights = weights
        logger.info("个性权重已加载: curiosity=%.1f sociability=%.1f playfulness=%.1f",
                     weights.curiosity, weights.sociability, weights.playfulness)

    @staticmethod
    def _load_from_env() -> PersonalityWeights:
        """从环境变量 PERSONALITY_WEIGHTS JSON 加载权重。"""
        import config  # 延迟引用，确保运行时读取最新值
        try:
            data = json.loads(config.PERSONALITY_WEIGHTS)
            return PersonalityWeights(
                curiosity=float(data.get("curiosity", 0.7)),
                sociability=float(data.get("sociability", 0.8)),
                playfulness=float(data.get("playfulness", 0.6)),
                empathy=float(data.get("empathy", 0.5)),
                assertiveness=float(data.get("assertiveness", 0.3)),
                creativity=float(data.get("creativity", 0.6)),
                impulsiveness=float(data.get("impulsiveness", 0.2)),
                loyalty=float(data.get("loyalty", 0.75)),
            )
        except (json.JSONDecodeError, ValueError, KeyError):
            logger.warning("个性权重 JSON 解析失败，使用默认值")
            return PersonalityWeights()

    def update_weights(self, updates: dict) -> PersonalityWeights:
        """部分更新权重（API PATCH 用）。

        Args:
            updates: 包含要更新字段的 dict

        Returns:
            更新后的权重

        Raises:
            ValueError: 无效的权重值
        """
        new_weights = self.weights.as_dict()
        for key, val in updates.items():
            if key in new_weights:
                fval = float(val)
                if fval < 0 or fval > 1:
                    raise ValueError(f"权重 {key}={fval} 超出范围 [0, 1]")
                new_weights[key] = fval
        self.weights = PersonalityWeights(**new_weights)
        # 持久化到 memory_store（fire-and-forget）
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._persist())
            else:
                loop.run_until_complete(self._persist())
        except Exception:
            pass
        return self.weights

    async def _persist(self):
        """持久化权重到 memory_store。"""
        try:
            from memory_store import set as mem_set
            await mem_set(
                "global/personality/weights",
                "core",
                json.dumps(self.weights.as_dict()),
            )
        except Exception:
            pass

    # ==================== Decision Functions ====================

    def should_reply(self, is_at: bool = False, is_direct: bool = False) -> bool:
        """判断是否应该回复。

        规则 (per R5):
        - 群聊@或私聊C2C: 总是回复
        - 否则: sociability × 0.6 + impulsiveness × 0.4 > 0.3

        Args:
            is_at: 是否被 @
            is_direct: 是否私聊

        Returns:
            True 如果应该回复
        """
        if is_at or is_direct:
            return True

        score = self.weights.sociability * 0.6 + self.weights.impulsiveness * 0.4
        return score > 0.3

    def should_search(self, during_conversation: bool = False) -> bool:
        """判断是否应该搜索。

        规则 (per R5):
        - curiosity > 0.5: 允许自主搜索（受频率限制）
        - curiosity > 0.3: 对话中遇到不确定可搜索
        - 否则: 不搜索

        Args:
            during_conversation: 是否在对话中

        Returns:
            True 如果应该搜索
        """
        if during_conversation:
            return self.weights.curiosity > 0.3
        else:
            return self.weights.curiosity > 0.5

    def reply_style(self) -> dict:
        """计算回复风格参数。

        Returns:
            {
                "temperature": float (0.5-1.0),
                "humor_level": float (0-1),
                "empathy_mode": bool,
                "assertiveness_mode": bool,
            }
        """
        w = self.weights

        return {
            "temperature": 0.5 + w.playfulness * 0.5,
            "humor_level": w.playfulness,
            "empathy_mode": w.empathy > 0.5,
            "assertiveness_mode": w.assertiveness > 0.6,
        }

    def should_be_bored(self) -> bool:
        """判断是否应该触发无聊破冰。

        规则 (per R5):
        boredom_threshold = 0.3 + (1 - impulsiveness) × 0.5
        随机值 < threshold 时允许无聊触发。

        Returns:
            True 如果应该考虑无聊行为
        """
        w = self.weights
        threshold = 0.3 + (1 - w.impulsiveness) * 0.5

        # 使用随机值模拟"心情"波动
        mood = random.random()
        return mood < threshold


# ==================== Singleton ====================

_personality: Optional[Personality] = None


def get_personality() -> Personality:
    """获取全局单例 Personality。"""
    global _personality
    if _personality is None:
        _personality = Personality()
    return _personality
