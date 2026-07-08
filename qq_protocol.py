"""
QQ Bot WebSocket 协议模块

处理 QQ Bot API 的所有协议细节：连接、鉴权、心跳、Resume、消息接收。
通过 on_message 回调将业务消息传递给调用方。

接口: async def run_qq_loop(on_message: Callable[[dict], Awaitable[None]]) -> None
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Set

import aiohttp

from config import (
    QQ_BOT_APPID, QQ_BOT_SECRET, QQ_WS_URL, HEARTBEAT_INTERVAL,
)

from log_config import get_logger
logger = get_logger("qq")

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


# ==================== Typed Message Seam ====================

ReplyCallback = Callable[[str], Awaitable[None]]


@dataclass
class MessageContext:
    """类型化消息上下文 — QQ 协议层与业务层之间的缝线。

    替代原来的 untyped dict，所有 QQ 特定字段在此集中定义。
    """
    user_id: str
    username: str
    content: str
    msg_type: str  # AT_MESSAGE_CREATE, C2C_MESSAGE_CREATE, GROUP_AT_MESSAGE_CREATE, etc.
    msg_id: str = ""
    group_id: str = ""
    channel_id: str = ""
    guild_id: str = ""
    ref_msg_id: str = ""
    timestamp: float = 0.0
    image_urls: list[str] = field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return "GROUP" in self.msg_type

    @property
    def is_at(self) -> bool:
        return "AT_MESSAGE" in self.msg_type

    @property
    def is_direct(self) -> bool:
        return "DIRECT" in self.msg_type or "C2C" in self.msg_type

    @property
    def session_key(self) -> str:
        """Actor session_key：私聊为 user_{uid}，群聊为 group_{gid}。"""
        if self.is_group:
            return f"group_{self.group_id or self.user_id}"
        return f"user_{self.user_id}"

    @property
    def raw_uid(self) -> str:
        """去掉 session_key 前缀的原始 ID。"""
        return self.group_id or self.user_id if self.is_group else self.user_id


def _build_unified_msg(event_type: str, event_data: dict) -> MessageContext:
    """从 QQ 事件数据构造类型化消息上下文。"""
    author = event_data.get("author", {})
    msg_id = event_data.get("id", "")
    attachments = event_data.get("attachments", [])
    image_urls = [
        att.get("url", "")
        for att in attachments
        if att.get("content_type", "").startswith("image/")
    ]
    return MessageContext(
        user_id=author.get("id", "unknown"),
        username=author.get("username", ""),
        content=event_data.get("content", ""),
        msg_type=event_type,
        msg_id=msg_id,
        group_id=event_data.get("group_openid", ""),
        channel_id=event_data.get("channel_id", ""),
        guild_id=event_data.get("guild_id", ""),
        ref_msg_id=msg_id,
        timestamp=time.time(),
        image_urls=image_urls,
    )


async def send_message(ctx: MessageContext, content: str) -> None:
    """通过 QQ REST API 发送回复消息。从 main.py 迁入。"""
    import uuid
    import aiohttp as _aiohttp

    if ctx.msg_type == "AT_MESSAGE_CREATE":
        if not ctx.channel_id:
            return
        url = f"https://api.sgroup.qq.com/v2/channels/{ctx.channel_id}/messages"
    elif ctx.msg_type == "DIRECT_MESSAGE_CREATE":
        if not ctx.guild_id:
            return
        url = f"https://api.sgroup.qq.com/v2/dms/{ctx.guild_id}/messages"
    elif ctx.is_group:
        if not ctx.group_id:
            return
        url = f"https://api.sgroup.qq.com/v2/groups/{ctx.group_id}/messages"
    else:
        url = f"https://api.sgroup.qq.com/v2/users/{ctx.user_id}/messages"

    msg_id = ctx.ref_msg_id if ctx.ref_msg_id else uuid.uuid4().hex
    if not hasattr(send_message, '_seq_counter'):
        send_message._seq_counter = {}
    seq = send_message._seq_counter.get(ctx.user_id, 0) + 1
    send_message._seq_counter[ctx.user_id] = seq
    payload = {"content": content, "msg_type": 0, "msg_id": msg_id, "msg_seq": seq}

    logger.info("发送 QQ 消息: type=%s, content=%s, url=%s",
                 ctx.msg_type, content[:60], url.split("/v2/")[-1])

    headers = {
        "Authorization": f"QQBot {await _get_access_token()}",
        "Content-Type": "application/json",
    }
    async with _aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    logger.info("回复已发送 → %s", ctx.user_id[:12])
                else:
                    logger.warning("发送失败 %d: %s", resp.status, await resp.text())
        except Exception:
            logger.exception("发送异常")


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
                                logger.info("收到消息: %s -> %s", unified_msg.user_id, unified_msg.content[:50])
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


# 导出 token 获取
get_access_token = _get_access_token
