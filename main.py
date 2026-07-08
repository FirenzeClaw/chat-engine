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
import uuid
from pathlib import Path
from typing import Set

import aiohttp
from aiohttp import web, WSMsgType

from config import (
    HTTP_HOST, HTTP_PORT, HEARTBEAT_INTERVAL,
    QQ_BOT_APPID, QQ_BOT_SECRET, QQ_WS_URL,
)
from qq_protocol import run_qq_loop, get_access_token, MessageContext, send_message

from log_config import get_logger
logger = get_logger("main")

# WebSocket 客户端管理
ws_clients: Set[web.WebSocketResponse] = set()
message_queue = asyncio.Queue()


# ==================== QQ 消息回调 ====================

async def on_qq_message(ctx: MessageContext):
    """QQ 消息到达 → 分发处理"""
    user_id = ctx.user_id

    # 持久化（botuser 仍用旧格式兼容）
    try:
        import botuser
        botuser.save_message(user_id, {
            "user_id": ctx.user_id, "username": ctx.username,
            "content": ctx.content, "group_id": ctx.group_id,
            "msg_type": ctx.msg_type, "timestamp": ctx.timestamp,
        })
    except Exception:
        pass

    # 社交信息采集
    try:
        import social
        asyncio.create_task(social.fetch_user_profile(user_id))
        if ctx.group_id:
            asyncio.create_task(social.fetch_group_info(ctx.group_id))
    except Exception:
        pass

    # 广播给前端
    broadcast = {
        "user_id": ctx.user_id, "username": ctx.username,
        "content": ctx.content, "group_id": ctx.group_id,
        "msg_type": ctx.msg_type, "timestamp": ctx.timestamp,
    }
    for client in list(ws_clients):
        try:
            await client.send_json(broadcast)
        except Exception:
            ws_clients.discard(client)

    # 协调器处理 → AI 回复
    async def _safe_process():
        try:
            # send_reply 回调：通过 qq_protocol 发送回复
            async def _reply(content: str):
                await send_message(ctx, content)

            await process_qq_message(ctx, _reply)
        except Exception:
            logger.exception("消息处理任务异常: user=%s", user_id[:16])

    try:
        from orchestrator import process_qq_message
        asyncio.create_task(_safe_process())
    except Exception as e:
        logger.exception("消息处理任务创建失败")


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
            from qq_protocol import MessageContext, send_message
            ctx = MessageContext(
                user_id=data["user_id"],
                username="",
                content=data["content"],
                msg_type="C2C_MESSAGE_CREATE",
            )
            await send_message(ctx, data["content"])
            logger.info("手动回复已发送 → %s", data["user_id"][:12])


# ==================== API 端点（从 server.py 挂载）====================

# 导入 chat-engine 的 HTTP API，让前端也能直接调用
from server import handle_chat, handle_chat_full, handle_evaluate, handle_ws_chat
from server import handle_get_session, handle_get_evaluation, handle_delete_session, handle_status, handle_health
from server import handle_session_health, handle_monitor
from server import handle_context_health, handle_get_personality, handle_patch_personality


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

    # Spec 003: 初始化新模块单例
    from context_manager import get_context_manager
    ctx_mgr = get_context_manager()
    logger.info("ContextManager 已初始化")

    from image_handler import retrieve_relevant_images
    logger.info("ImageHandler 已就绪")

    from web_search import get_web_manager
    wm = get_web_manager()
    logger.info("WebSearchManager 已初始化")

    from boredom import get_boredom_detector
    bd = get_boredom_detector()
    logger.info("BoredomDetector 已初始化")

    from personality import get_personality
    p = get_personality()
    logger.info("Personality 已加载: curiosity=%.1f sociability=%.1f",
                 p.weights.curiosity, p.weights.sociability)

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
    app.router.add_get("/v1/context/health", handle_context_health)
    app.router.add_get("/v1/personality", handle_get_personality)
    app.router.add_patch("/v1/personality", handle_patch_personality)

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
