"""
Chat Engine HTTP/WebSocket 服务器

端点：
    POST /v1/chat          — 快速回复（辅脑）
    POST /v1/chat/full     — 一站式：快速回复 + 异步评估 + 追答
    POST /v1/evaluate      — 双脑评估（独立调用）
    GET  /v1/chat          — WebSocket 升级（实时交互）
    GET  /v1/sessions/{id} — 获取会话信息
    DELETE /v1/sessions/{id} — 删除会话
    GET  /v1/status        — 引擎状态
"""

import asyncio
import json
import signal
import time

from aiohttp import web, WSMsgType

from config import HTTP_HOST, HTTP_PORT
import engine
from context_manager import estimate_total_tokens

from log_config import get_logger
logger = get_logger("server")


# ==================== REST Handlers ====================

async def handle_chat(request: web.Request) -> web.Response:
    """POST /v1/chat — 辅脑快速回复"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    session_id = body.get("session_id", "")
    user_message = body.get("message", "")
    system_prompt = body.get("system_prompt", "")

    if not session_id or not user_message:
        return web.json_response({"error": "session_id and message required"}, status=400)

    result = await engine.chat(
        session_id=session_id,
        user_message=user_message,
        system_prompt=system_prompt,
        temperature=body.get("temperature", 0.8),
        max_tokens=body.get("max_tokens", 512),
        role=body.get("role", "fast"),
    )
    return web.json_response(result)


async def handle_chat_full(request: web.Request) -> web.Response:
    """POST /v1/chat/full — 快速回复 + 异步评估"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    session_id = body.get("session_id", "")
    user_message = body.get("message", "")
    system_prompt = body.get("system_prompt", "")

    if not session_id or not user_message:
        return web.json_response({"error": "session_id and message required"}, status=400)

    result = await engine.chat_with_evaluate(
        session_id=session_id,
        user_message=user_message,
        system_prompt=system_prompt,
    )
    return web.json_response(result)


async def handle_evaluate(request: web.Request) -> web.Response:
    """POST /v1/evaluate — 双脑评估（独立调用）"""
    import brain

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    session_id = body.get("session_id", "")
    user_message = body.get("user_message", body.get("message", ""))
    fast_reply = body.get("fast_reply", body.get("reply", ""))
    system_prompt = body.get("system_prompt", "")

    if not session_id or not fast_reply:
        return web.json_response({"error": "session_id and fast_reply required"}, status=400)

    decision = await brain.evaluate(
        session_id=session_id,
        user_message=user_message,
        fast_reply=fast_reply,
        system_prompt=system_prompt,
    )
    return web.json_response(decision)


async def handle_ws_chat(request: web.Request) -> web.WebSocketResponse:
    """GET /v1/chat — WebSocket 聊天

    客户端发送 JSON:
        {"type":"chat", "session_id":"...", "message":"...", "system_prompt":"..."}
    服务端回复:
        {"type":"reply", "reply":"...", "latency_ms":123, "session_id":"..."}
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "invalid JSON"})
                    continue

                msg_type = data.get("type", "")

                if msg_type == "chat":
                    result = await engine.chat(
                        session_id=data.get("session_id", "default"),
                        user_message=data.get("message", ""),
                        system_prompt=data.get("system_prompt", ""),
                    )
                    await ws.send_json({"type": "reply", **result})

                elif msg_type == "get_session":
                    info = await engine.get_session_info(data.get("session_id", ""))
                    await ws.send_json({"type": "session_info", **info})

                elif msg_type == "delete_session":
                    await engine.delete_session(data.get("session_id", ""))
                    await ws.send_json({"type": "deleted", "session_id": data.get("session_id", "")})

                elif msg_type == "status":
                    status = await engine.engine_status()
                    await ws.send_json({"type": "status", **status})

                elif msg_type == "ping":
                    await ws.send_json({"type": "pong"})
    finally:
        pass

    return ws


async def handle_get_session(request: web.Request) -> web.Response:
    """GET /v1/sessions/{session_id}"""
    session_id = request.match_info.get("session_id", "")
    info = await engine.get_session_info(session_id)
    return web.json_response(info)


async def handle_get_evaluation(request: web.Request) -> web.Response:
    """GET /v1/sessions/{session_id}/evaluation — 轮询评估结果"""
    session_id = request.match_info.get("session_id", "")
    result = await engine.get_evaluation(session_id)
    return web.json_response({
        "session_id": session_id,
        "evaluation": result,
        "ready": result is not None,
    })


async def handle_health(request: web.Request) -> web.Response:
    """GET /v1/health — 健康检查"""
    return web.json_response({"status": "ok", "uptime": time.time() - _start_time})


async def handle_delete_session(request: web.Request) -> web.Response:
    """DELETE /v1/sessions/{session_id}"""
    session_id = request.match_info.get("session_id", "")
    await engine.delete_session(session_id)
    return web.json_response({"deleted": True, "session_id": session_id})


async def handle_session_health(request: web.Request) -> web.Response:
    """GET /v1/sessions/{session_id}/health — 单 session 健康报告"""
    session_id = request.match_info.get("session_id", "")
    from context_manager import check_session
    if session_id in engine.session_manager._sessions:
        s = engine.session_manager._sessions[session_id]
        return web.json_response(check_session(s))
    return web.json_response(
        {"session_id": session_id, "exists": False, "status": "not_found"},
        status=404,
    )


async def handle_monitor(request: web.Request) -> web.Response:
    """GET /v1/monitor — 全局监测摘要"""
    from context_manager import global_monitor
    summary = await global_monitor()
    return web.json_response(summary)


async def handle_status(request: web.Request) -> web.Response:
    """GET /v1/status"""
    status = await engine.engine_status()
    return web.json_response(status)


async def handle_context_health(request: web.Request) -> web.Response:
    """GET /v1/context/health — 上下文健康报告"""
    total_sessions = len(engine.session_manager._sessions)
    sessions_detail = []
    total_tokens_sum = 0
    compressed_count = 0
    retired_count = 0

    for sid, s in engine.session_manager._sessions.items():
        msgs = [{"role": "system", "content": s.system_prompt}] + s.get_context()
        tokens = estimate_total_tokens(msgs, [], [])
        total_tokens_sum += tokens

        usage_pct = tokens / engine.MAX_CONTEXT_TOKENS * 100 if engine.MAX_CONTEXT_TOKENS > 0 else 0
        status = "normal"
        if usage_pct >= engine.CONTEXT_RETIRE_PCT * 100:
            status = "retired"
        elif usage_pct >= engine.CONTEXT_COMPRESS_PCT * 100:
            status = "compressed"

        if status == "compressed":
            compressed_count += 1
        elif status == "retired":
            retired_count += 1

        sessions_detail.append({
            "session_id": sid[:12],
            "tokens": tokens,
            "usage_pct": round(usage_pct, 1),
            "status": status,
            "message_count": len(s.messages),
        })

    return web.json_response({
        "max_tokens": engine.MAX_CONTEXT_TOKENS,
        "total_sessions": total_sessions,
        "avg_tokens": total_tokens_sum // max(total_sessions, 1),
        "normal_count": total_sessions - compressed_count - retired_count,
        "compressed_count": compressed_count,
        "retired_count": retired_count,
        "sessions": sessions_detail,
    })


async def handle_get_personality(request: web.Request) -> web.Response:
    """GET /v1/personality — 获取当前个性权重"""
    try:
        from personality import get_personality
        p = get_personality()
        return web.json_response(p.weights.as_dict())
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_patch_personality(request: web.Request) -> web.Response:
    """PATCH /v1/personality — 更新个性权重"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    try:
        from personality import get_personality
        p = get_personality()
        new_weights = p.update_weights(body)
        return web.json_response(new_weights.as_dict())
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ==================== Main ====================

_start_time = time.time()


async def main():
    global _start_time
    _start_time = time.time()

    # 启动引擎（加载持久化会话）
    await engine.startup()

    app = web.Application()

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

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()

    logger.info("Chat Engine 启动: http://%s:%s", HTTP_HOST, HTTP_PORT)
    logger.info("  POST /v1/chat              (fast reply)")
    logger.info("  POST /v1/chat/full         (fast + evaluate)")
    logger.info("  POST /v1/evaluate          (dual-brain eval)")
    logger.info("  GET  /v1/sessions/{id}         (session info)")
    logger.info("  GET  /v1/sessions/{id}/evaluation (poll eval)")
    logger.info("  GET  /v1/sessions/{id}/health    (session health)")
    logger.info("  GET  /v1/monitor                (global monitor)")
    logger.info("  GET  /v1/health             (health check)")
    logger.info("  GET  /v1/context/health     (context health)")
    logger.info("  GET  /v1/personality        (personality weights)")
    logger.info("  PATCH /v1/personality       (update weights)")
    logger.info("  辅脑: %s", engine.LLM_FAST_MODEL)
    logger.info("  主脑: %s @ %s", engine.LLM_STRONG_MODEL, engine.LLM_BASE_URL)

    # 信号处理 — 优雅关闭
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(_graceful_shutdown()))
        except NotImplementedError:
            pass  # Windows 不支持 add_signal_handler

    # 保持运行
    await asyncio.Event().wait()


async def _graceful_shutdown():
    logger.info("正在关闭...")
    await engine.shutdown()
    logger.info("已关闭")


if __name__ == "__main__":
    asyncio.run(main())
