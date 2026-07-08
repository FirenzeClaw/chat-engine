"""
QQ Bot WebSocket 协议模块

处理 QQ Bot API 的所有协议细节：连接、鉴权、心跳、Resume、消息接收。
通过 on_message 回调将业务消息传递给调用方。

接口: async def run_qq_loop(on_message: Callable[[dict], Awaitable[None]]) -> None
"""

import asyncio
import json
import logging
import time
from typing import Callable, Awaitable, Set

import aiohttp

from config import (
    QQ_BOT_APPID, QQ_BOT_SECRET, QQ_WS_URL, HEARTBEAT_INTERVAL,
)

logger = logging.getLogger("qq")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[QQ] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# Intents 位图
# PUBLIC_GUILD_MESSAGES (1<<30): 频道 @ (AT_MESSAGE_CREATE)
# GROUP_AND_C2C_EVENT  (1<<25): 群聊 @ + 单聊 (GROUP_AT_MESSAGE_CREATE, C2C_MESSAGE_CREATE)
# DIRECT_MESSAGE       (1<<12): 频道私信 (DIRECT_MESSAGE_CREATE) — 需特殊权限申请
INTENTS = (1 << 30) | (1 << 25) | (1 << 12)

# 消息去重
_seen_msg_ids: Set[str] = set()
MAX_SEEN_IDS = 10000

# Token 缓存
_access_token: str = ""
_token_expires: float = 0

# Session 状态（Resume 恢复用）
_session_id: str = ""
_latest_s: int | None = None


async def _get_access_token() -> str:
    """获取/刷新 QQ Bot access_token（7200s 有效期，提前 5 分钟刷新）"""
    global _access_token, _token_expires
    if _access_token and time.time() < _token_expires:
        return _access_token

    url = "https://bots.qq.com/app/getAppAccessToken"
    payload = {"appId": QQ_BOT_APPID, "clientSecret": QQ_BOT_SECRET}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            _access_token = data.get("access_token", "")
            expires = int(data.get("expires_in", 7200))
            _token_expires = time.time() + expires - 300
            logger.info("access_token 已获取，有效期 %ds", expires)
            return _access_token


def _is_duplicate(msg_id: str) -> bool:
    """检查消息是否重复，并自动限制 set 大小"""
    if msg_id in _seen_msg_ids:
        return True
    _seen_msg_ids.add(msg_id)
    if len(_seen_msg_ids) > MAX_SEEN_IDS:
        _seen_msg_ids.clear()
    return False


def _build_unified_msg(event_type: str, event_data: dict) -> dict:
    """从 QQ 事件数据构造统一消息格式"""
    author = event_data.get("author", {})
    msg_id = event_data.get("id", "")
    return {
        "type": event_type,
        "id": msg_id,
        "user_id": author.get("id", "unknown"),
        "username": author.get("username", ""),
        "content": event_data.get("content", ""),
        "group_id": event_data.get("group_openid", ""),
        "channel_id": event_data.get("channel_id", ""),
        "guild_id": event_data.get("guild_id", ""),
        "ref_msg_id": msg_id,
        "timestamp": time.time(),
    }


async def _heartbeat(ws, interval: float):
    """心跳协程：按 Hello 下发的 interval 发送，携带最新 s 值"""
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send_json({"op": 1, "d": _latest_s})
        except Exception:
            break


async def run_qq_loop(on_message: Callable[[dict], Awaitable[None]]):
    """QQ Bot WebSocket 主循环

    状态机: hello → identify/resume → running
    通过 on_message 回调将每条业务消息传递给调用方。
    """
    global _session_id, _latest_s
    session = aiohttp.ClientSession()

    while True:
        try:
            async with session.ws_connect(QQ_WS_URL) as ws:
                logger.info("WebSocket 已连接: %s", QQ_WS_URL)

                state = "hello"
                hello_interval = HEARTBEAT_INTERVAL
                hb_task = None

                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    data = json.loads(msg.data)
                    op = data.get("op", 0)

                    if "s" in data and data["s"] is not None:
                        _latest_s = data["s"]

                    # Phase 1: Hello
                    if state == "hello":
                        if op == 10:
                            hello_interval = data.get("d", {}).get("heartbeat_interval", 45000) / 1000
                            logger.info("Hello received, heartbeat_interval=%.2fs", hello_interval)

                            token = await _get_access_token()
                            if _session_id and _latest_s is not None:
                                await ws.send_json({
                                    "op": 6, "d": {
                                        "token": f"QQBot {token}",
                                        "session_id": _session_id,
                                        "seq": _latest_s,
                                    }
                                })
                                logger.info("发送 Resume (session=%s..., seq=%s)", _session_id[:8], _latest_s)
                                state = "resume"
                            else:
                                await ws.send_json({
                                    "op": 2, "d": {
                                        "token": f"QQBot {token}",
                                        "intents": INTENTS,
                                        "shard": [0, 1],
                                        "properties": {}
                                    }
                                })
                                logger.info("发送 Identify (intents=%d)", INTENTS)
                                state = "identify"

                    # Phase 2: Ready / Resumed
                    elif state in ("identify", "resume"):
                        if op == 0 and data.get("t") == "READY":
                            _session_id = data.get("d", {}).get("session_id", "")
                            logger.info("Ready, session_id=%s...", _session_id[:16])
                            state = "running"
                            hb_task = asyncio.create_task(_heartbeat(ws, hello_interval))

                        elif op == 0 and data.get("t") == "RESUMED":
                            logger.info("Resumed, 开始补发遗漏事件")
                            state = "running"
                            hb_task = asyncio.create_task(_heartbeat(ws, hello_interval))

                        elif op == 9:
                            logger.warning("Invalid Session，重置状态后重连")
                            _session_id = ""
                            _latest_s = None
                            break

                        elif op == 7:
                            logger.info("服务端要求重连")
                            break

                    # Phase 3: Running
                    elif state == "running":
                        if op == 11:
                            pass
                        elif op == 7:
                            logger.info("服务端要求重连")
                            break
                        elif op == 9:
                            logger.warning("Session 失效，重置后重连")
                            _session_id = ""
                            _latest_s = None
                            break
                        elif op == 0:
                            event_type = data.get("t", "")
                            event_data = data.get("d", {})

                            if event_type in ("AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE",
                                              "C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                                msg_id = event_data.get("id", "")
                                if _is_duplicate(msg_id):
                                    continue

                                unified_msg = _build_unified_msg(event_type, event_data)
                                logger.info("收到消息: %s -> %s", unified_msg['user_id'], unified_msg['content'][:50])
                                await on_message(unified_msg)

                if hb_task:
                    hb_task.cancel()
                    try:
                        await hb_task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            logger.warning("WebSocket 断开: %s，5秒后重连", e)
            await asyncio.sleep(5)


# 导出 token 获取（供 send_qq_message 使用）
get_access_token = _get_access_token
