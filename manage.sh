#!/bin/bash
# Chat Engine + QQ Bot 管理脚本

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="$SCRIPT_DIR/server.pid"
LOG_FILE="$SCRIPT_DIR/logs/server.log"

start() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "[manage] 已在运行 (PID: $(cat $PID_FILE))"
        return
    fi
    echo "[manage] 启动 Chat Engine + QQ Bot..."
    mkdir -p logs botuser data
    nohup python -u main.py > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    if kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "[manage] 已启动 (PID: $(cat $PID_FILE))"
    else
        echo "[manage] 启动失败，查看日志: $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop() {
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            echo "[manage] chat-engine 已停止 (PID: $pid)"
        fi
        rm -f "$PID_FILE"
    else
        echo "[manage] chat-engine 未运行"
    fi
}

restart() {
    stop
    sleep 1
    start
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
        echo "[manage] 运行中 (PID: $(cat $PID_FILE))"
        if command -v curl &>/dev/null; then
            curl -s http://127.0.0.1:18090/v1/health 2>/dev/null && echo ""
        fi
    else
        echo "[manage] 未运行"
    fi
}

logs() {
    tail -f "$LOG_FILE"
}

case "${1:-start}" in
    start)    start ;;
    stop)     stop ;;
    restart)  restart ;;
    status)   status ;;
    logs)     logs ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
