"""
多脑评估引擎 — 从 qq-bot orchestrator 完整移植

双脑评估流程：
1. 规则预过滤（_fuse）— 快速判断是否候选追答
2. 理性脑评估 — 分析逻辑完整性和信息遗漏
3. 感性脑评估 — 分析情感基调和共情机会
4. 融合决策（_merge）— 加权平均 + 规则 boost
5. 追答生成 — 主脑生成补充回复文本
"""

import json
import logging
import re
import time
from typing import Optional

from openai import AsyncOpenAI

from config import (
    LLM_API_KEY, LLM_BASE_URL, LLM_STRONG_MODEL,
    FOLLOW_UP_ENABLED, FOLLOW_UP_MAX_PER_HOUR,
)

logger = logging.getLogger("brain")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[brain] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# 追答频率控制
_follow_up_counter: dict[str, list[float]] = {}
_follow_up_lock = __import__('asyncio').Lock()


# ==================== Rule Filter ====================

def _fuse(fast_reply: str) -> dict:
    """规则预过滤：判断是否需要主脑评估。

    Returns:
        {candidate: bool, score_boost: int, reason: str}
    """
    reply = fast_reply.strip()

    # 错误回复不追答
    if reply.startswith("[错误]") or reply.startswith("[超时]"):
        return {"candidate": False, "score_boost": 0, "reason": ""}

    # "不知道"类回复 → 高概率追答
    unsure = ["我不知道", "不确定", "不清楚", "不了解", "没法", "抱歉"]
    for kw in unsure:
        if kw in reply:
            return {"candidate": True, "score_boost": 5, "reason": "不确定回复"}

    # 太短的回复
    if len(reply) < 10:
        return {"candidate": True, "score_boost": 3, "reason": "回复过短"}

    # 默认候选
    return {"candidate": True, "score_boost": 0, "reason": ""}


# ==================== Evaluation ====================

def _build_eval_prompt(brain_type: str, user_msg: str, reply: str) -> str:
    """构建评估 prompt。"""
    persona_hint = "（Bot 性格：友好、幽默、自然，2-4句话）"

    if brain_type == "rational":
        return f"""你是理性评估者。分析此对话是否需要追加回复。

用户: {user_msg}
回复: {reply}

输出 JSON:
{{"score": 0-10, "should_follow_up": bool, "reason": "原因", "memory_update": null}}

标准：回复是否不完整/有误解/遗漏信息？是否需纠正？{persona_hint}"""
    else:
        return f"""你是感性评估者。从情感角度分析此对话。

用户: {user_msg}
回复: {reply}

输出 JSON:
{{"score": 0-10, "should_follow_up": bool, "reason": "原因", "memory_update": null}}

标准：情感基调是否合适？是否需要更多共情？{persona_hint}"""


def _parse_eval(raw: str) -> dict:
    """解析 LLM 输出的 JSON。"""
    try:
        if "```json" in raw:
            raw = raw[raw.index("```json") + 7 : raw.index("```", raw.index("```json") + 7)]
        elif "```" in raw:
            raw = raw[raw.index("```") + 3 : raw.index("```", raw.index("```") + 3)]
        return json.loads(raw.strip())
    except (json.JSONDecodeError, ValueError, KeyError):
        return {"score": 0, "should_follow_up": False, "reason": "", "memory_update": None}


def _merge(rational: dict, emotional: dict, rule: dict) -> dict:
    """融合理性+感性评估结果。

    理性权重 0.6，感性 0.4，加上规则 boost。
    threshold > 0.5 → 触发追答。
    """
    r_score = rational.get("score", 0)
    e_score = emotional.get("score", 0)
    boost = rule.get("score_boost", 0)

    combined = (r_score * 0.6 + e_score * 0.4 + boost) / 10.0

    memory_updates = []
    for ev in [rational, emotional]:
        mu = ev.get("memory_update")
        if mu and isinstance(mu, dict) and mu.get("action"):
            memory_updates.append(mu)

    return {
        "should_follow_up": combined > 0.5,
        "combined_score": round(combined, 3),
        "rational_score": r_score,
        "emotional_score": e_score,
        "salience_score": round((r_score + e_score) / 2.0, 1),  # T035: 综合重要性 0-10
        "reason": rule.get("reason") or rational.get("reason", "") or emotional.get("reason", ""),
        "memory_updates": memory_updates,
    }


# ==================== Rate Limiting ====================

async def _check_rate(session_id: str, window_s: int = 3600, limit: int = 5) -> bool:
    from asyncio import Lock
    global _follow_up_lock
    async with _follow_up_lock:
        now = time.time()
        ts = _follow_up_counter.get(session_id, [])
        ts = [t for t in ts if now - t < window_s]
        _follow_up_counter[session_id] = ts
        return len(ts) < limit


async def _record(session_id: str):
    global _follow_up_lock
    async with _follow_up_lock:
        ts = _follow_up_counter.get(session_id, [])
        ts.append(time.time())
        _follow_up_counter[session_id] = ts


# ==================== Main Evaluate API ====================

async def evaluate(
    session_id: str,
    user_message: str,
    fast_reply: str,
    system_prompt: str = "",
) -> dict:
    """多脑评估 + 追答生成。

    Args:
        session_id: 会话 ID
        user_message: 用户原始消息
        fast_reply: 辅脑的快速回复
        system_prompt: 评估用 system prompt

    Returns:
        {
            should_follow_up: bool,
            follow_up_text: str,           # 仅当 should_follow_up=True
            combined_score: float,
            rational_score: int,
            emotional_score: int,
            reason: str,
            memory_updates: list,
        }
    """
    if not FOLLOW_UP_ENABLED:
        return {"should_follow_up": False, "reason": "follow-up disabled", "combined_score": 0}

    # 1. 规则过滤
    rule = _fuse(fast_reply)
    if not rule["candidate"]:
        logger.debug("规则过滤: 不需追答 — %s", fast_reply[:40])
        return {"should_follow_up": False, "reason": "rule-filter", "combined_score": 0}

    client = AsyncOpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)

    async def call_eval(brain_type: str) -> dict:
        prompt = _build_eval_prompt(brain_type, user_message, fast_reply)
        try:
            resp = await client.chat.completions.create(
                model=LLM_STRONG_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt or "你是严谨的评估者。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=256,
                temperature=0.3,
            )
            return _parse_eval(resp.choices[0].message.content or "")
        except Exception as e:
            logger.exception("%s 评估失败", brain_type)
            return {"score": 0, "should_follow_up": False, "reason": str(e), "memory_update": None}

    # 2. 双脑并行评估
    import asyncio as _asyncio
    rational, emotional = await _asyncio.gather(
        call_eval("rational"),
        call_eval("emotional"),
    )

    # 3. 融合决策
    decision = _merge(rational, emotional, rule)
    logger.info(
        "评估: rational=%d emotional=%d combined=%.2f → follow_up=%s",
        decision["rational_score"], decision["emotional_score"],
        decision["combined_score"], decision["should_follow_up"],
    )

    # 4. 追答生成
    if decision["should_follow_up"]:
        if await _check_rate(session_id, limit=FOLLOW_UP_MAX_PER_HOUR):
            follow_up_text = await _generate_follow_up(
                client, user_message, fast_reply, decision["reason"]
            )
            if follow_up_text:
                await _record(session_id)
                decision["follow_up_text"] = follow_up_text
                logger.info("追答已生成: %s", follow_up_text[:40])
        else:
            decision["should_follow_up"] = False
            decision["reason"] = "rate-limited"
            logger.info("追答限速跳过: session=%s (已达 %d/h 上限)",
                         session_id[:12], FOLLOW_UP_MAX_PER_HOUR)

    return decision


async def _generate_follow_up(
    client: AsyncOpenAI, user_msg: str, reply: str, reason: str
) -> str:
    """生成追答文本。"""
    prompt = f"""生成追加回复（1-3句话，以"对了，"开头）：

用户: {user_msg}
回复: {reply}
原因: {reason}

不要重复回复。"""
    try:
        resp = await client.chat.completions.create(
            model=LLM_STRONG_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.7,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""
