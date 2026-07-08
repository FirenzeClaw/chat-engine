"""
Chat Engine + QQ Bot 统一入口

单进程运行：
- HTTP/WS 服务器（前端 Web UI + API）
- QQ WebSocket 协议（收发消息）
- LLM 引擎（Chat + 多脑评估）
- 记忆系统（SQLite）
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Set

import aiohttp
from aiohttp import web, WSMsgType

from config import (
    HTTP_HOST, HTTP_PORT, HEARTBEAT_INTERVAL,
    QQ_BOT_APPID, QQ_BOT_SECRET, QQ_WS_URL,
)
from qq_protocol import run_qq_loop, get_access_token

logger = logging.getLogger("main")
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[main] %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)

# WebSocket 客户端管理
ws_clients: Set[web.WebSocketResponse] = set()
message_queue = asyncio.Queue()


# ==================== QQ 消息回调 ====================

async def on_qq_message(unified_msg: dict):
    """QQ 消息到达 → 分发处理"""
    user_id = unified_msg.get("user_id", "unknown")

    # 持久化
    try:
        import botuser
        botuser.save_message(user_id, unified_msg)
    except Exception:
        pass

    # 社交信息采集
    try:
        import social
        asyncio.create_task(social.fetch_user_profile(user_id))
        gid = unified_msg.get("group_id", "")
        if gid:
            asyncio.create_task(social.fetch_group_info(gid))
    except Exception:
        pass

    # 广播给前端
    for client in list(ws_clients):
        try:
            await client.send_json(unified_msg)
        except Exception:
            ws_clients.discard(client)

    # 协调器处理 → AI 回复
    try:
        from orchestrator import process_qq_message
        asyncio.create_task(process_qq_message(
            user_id=user_id,
            content=unified_msg.get("content", ""),
            msg_metadata=unified_msg,
            send_reply=send_qq_message,
        ))
    except Exception as e:
        logger.exception("消息处理失败")


# ==================== QQ 消息发送 ====================

async def send_qq_message(data: dict):
    """通过 QQ REST API 发送消息"""
    user_id = data.get("user_id", "")
    content = data.get("content", "")
    group_id = data.get("group_id", "")
    channel_id = data.get("channel_id", "")
    guild_id = data.get("guild_id", "")
    msg_type = data.get("msg_type", "")
    ref_msg_id = data.get("ref_msg_id", "")

    if msg_type == "AT_MESSAGE_CREATE":
        if not channel_id:
            return
        url = f"https://api.sgroup.qq.com/v2/channels/{channel_id}/messages"
    elif msg_type == "DIRECT_MESSAGE_CREATE":
        if not guild_id:
            return
        url = f"https://api.sgroup.qq.com/v2/dms/{guild_id}/messages"
    elif "GROUP" in msg_type:
        if not group_id:
            return
        url = f"https://api.sgroup.qq.com/v2/groups/{group_id}/messages"
    else:
        url = f"https://api.sgroup.qq.com/v2/users/{user_id}/messages"

    msg_id = ref_msg_id if ref_msg_id else uuid.uuid4().hex
    payload = {"content": content, "msg_type": 0, "msg_id": msg_id, "msg_seq": 1}

    headers = {
        "Authorization": f"QQBot {await get_access_token()}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    logger.info("回复已发送 → %s", user_id[:12])
                else:
                    logger.warning("发送失败 %d: %s", resp.status, await resp.text())
        except Exception as e:
            logger.exception("发送异常")


# ==================== HTTP 处理器 ====================

async def handle_http_index(request):
    index_path = Path(__file__).parent / "index.html"
    if index_path.exists():
        return web.FileResponse(index_path)
    return web.Response(text="Chat Engine + QQ Bot Running", content_type="text/html")


async def handle_ws_frontend(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "manual_reply":
                    await message_queue.put(data)
    finally:
        ws_clients.discard(ws)
    return ws


async def manual_reply_consumer():
    while True:
        data = await message_queue.get()
        if data.get("user_id") and data.get("content"):
            await send_qq_message({
                "user_id": data["user_id"],
                "content": data["content"],
                "msg_type": "C2C_MESSAGE_CREATE",
                "group_id": "", "channel_id": "", "guild_id": "", "ref_msg_id": "",
            })
            logger.info("手动回复已发送 → %s", data["user_id"][:12])


# ==================== API 端点（从 server.py 挂载）====================

# 导入 chat-engine 的 HTTP API，让前端也能直接调用
from server import handle_chat, handle_chat_full, handle_evaluate, handle_ws_chat
from server import handle_get_session, handle_get_evaluation, handle_delete_session, handle_status, handle_health
from server import handle_session_health, handle_monitor


# ==================== Main ====================

async def main():
    # 初始化记忆
    from orchestrator import init_memory
    await init_memory()

    # 初始化引擎
    from engine import startup
    await startup()

    # 初始化回复调度器
    from reply_scheduler import get_scheduler
    scheduler = get_scheduler()
    await scheduler.start()

    # 创建 HTTP 应用
    app = web.Application()

    # Web UI
    app.router.add_get("/", handle_http_index)
    app.router.add_get("/ws", handle_ws_frontend)

    # Chat Engine API
    app.router.add_post("/v1/chat", handle_chat)
    app.router.add_post("/v1/chat/full", handle_chat_full)
    app.router.add_post("/v1/evaluate", handle_evaluate)
    app.router.add_get("/v1/chat", handle_ws_chat)
    app.router.add_get("/v1/sessions/{session_id}", handle_get_session)
    app.router.add_get("/v1/sessions/{session_id}/evaluation", handle_get_evaluation)
    app.router.add_delete("/v1/sessions/{session_id}", handle_delete_session)
    app.router.add_get("/v1/sessions/{session_id}/health", handle_session_health)
    app.router.add_get("/v1/status", handle_status)
    app.router.add_get("/v1/monitor", handle_monitor)
    app.router.add_get("/v1/health", handle_health)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.router.add_static("/static", static_dir)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()

    logger.info("=" * 50)
    logger.info("Chat Engine + QQ Bot 已启动")
    logger.info("  HTTP:   http://%s:%s", HTTP_HOST, HTTP_PORT)
    logger.info("  API:    http://%s:%s/v1/chat", HTTP_HOST, HTTP_PORT)
    logger.info("  QQ:     %s", QQ_WS_URL)
    logger.info("  LLM:    %s", __import__('engine').LLM_FAST_MODEL)
    logger.info("=" * 50)

    # 手动回复消费者
    asyncio.create_task(manual_reply_consumer())

    # 每日记忆衰减
    async def daily_decay():
        await asyncio.sleep(60)
        while True:
            try:
                from memory_store import apply_decay
                r = await apply_decay()
                logger.info("记忆衰减: expired=%d boosted=%d", r["expired_count"], r["boosted_count"])
            except Exception:
                pass
            await asyncio.sleep(86400)
    asyncio.create_task(daily_decay())

    # 每日关联扫描（偏移 1h，避免与衰减冲突）
    async def daily_link_scan():
        await asyncio.sleep(3600)  # 延迟 1h
        while True:
            try:
                from memory_store import _daily_link_scan
                r = await _daily_link_scan()
                logger.info("每日关联扫描: links=%d", r["links_created"])
            except Exception:
                pass
            await asyncio.sleep(86400)
    asyncio.create_task(daily_link_scan())

    # 每日批量标注（偏移 2h）
    async def daily_batch_tag():
        await asyncio.sleep(7200)  # 延迟 2h
        while True:
            try:
                from memory_store import _daily_batch_tag
                r = await _daily_batch_tag()
                logger.info("每日批量标注: tagged=%d rel=%d", r["tagged"], r["relationships"])
            except Exception:
                pass
            await asyncio.sleep(86400)
    asyncio.create_task(daily_batch_tag())

    # 每日集群检查（偏移 3h，在标注之后运行）
    async def daily_cluster_check():
        await asyncio.sleep(10800)  # 延迟 3h
        while True:
            try:
                from memory_store import _check_cluster_trigger
                r = await _check_cluster_trigger()
                logger.info("每日集群检查: clusters=%d", r["clusters_created"])
            except Exception:
                pass
            await asyncio.sleep(86400)
    asyncio.create_task(daily_cluster_check())

    # QQ 协议循环
    await run_qq_loop(on_qq_message)


if __name__ == "__main__":
    asyncio.run(main())
