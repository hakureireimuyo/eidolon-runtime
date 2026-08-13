#!/usr/bin/env bash
# Eidolon Runtime 一键启动 / 停止脚本
#
# 启动(Web 服务 + UI,单进程;前端无构建步骤):
#   bash scripts/start.sh
#   就绪后提示访问地址;Ctrl+C 停止服务。
#   默认端口 8010(与 Studio 的 8000 错开,两者可同时运行),
#   可用环境变量 EIDOLON_RUNTIME_PORT 覆盖。
#
# 停止(终端直接关闭后残留的孤儿进程也可用此命令清理):
#   bash scripts/start.sh stop
#
# 未配置 API Key 时界面仍可打开、角色卡可加载,发起对话会返回 503 提示(见 README)。
# 日志:workspace/logs/runtime.log(workspace 已 gitignore)
# 开发者工具默认开启(/devtools);如需关闭:EIDOLON_RUNTIME_DEVTOOLS=0 bash scripts/start.sh
#
# 依赖:uv。首次运行自动创建本仓 .venv 并安装依赖(git 源 pin rev,见 README);
#      本脚本在 monorepo 检出与独立 clone 两种形态下均可直接使用。
set -euo pipefail

# 仓库根:优先 git 推导(monorepo 检出与独立 clone 均正确),非 git 环境回退脚本目录
REPO="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO" ] || REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

LOG_DIR="$REPO/workspace/logs"
PORT="${EIDOLON_RUNTIME_PORT:-8010}"
URL="http://127.0.0.1:$PORT"

is_windows() { case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) return 0;; *) return 1;; esac; }

# 监听指定端口的进程 PID(Windows 走 PowerShell,不依赖 netstat 本地化输出)
port_pids() {
    local port=$1
    if is_windows; then
        powershell -NoProfile -Command \
            "(Get-NetTCPConnection -LocalPort $port -State Listen).OwningProcess" \
            2>/dev/null | tr -d '\r'
    else
        command -v lsof >/dev/null 2>&1 && lsof -ti :"$port" 2>/dev/null || true
    fi
}

# 结束单个监听进程(带镜像名校验,避免误杀复用 PID 的无关进程)
kill_pid() {
    local pid=$1 img
    [ -n "$pid" ] || return 0
    if is_windows; then
        img=$(powershell -NoProfile -Command \
            "(Get-Process -Id $pid -ErrorAction SilentlyContinue).ProcessName" \
            2>/dev/null | tr -d '\r' | tr 'A-Z' 'a-z')
        case "$img" in
            node|python|uv|pythonw)
                taskkill //PID "$pid" //F >/dev/null 2>&1 || true ;;
            *)
                echo "  [skip] PID $pid(${img:-不存在})不是 node/python 服务进程,跳过" ;;
        esac
    else
        kill "$pid" 2>/dev/null || true
    fi
}

# 结束监听 runtime 端口的服务进程(start 前台退出与 stop 子命令共用同一逻辑)
stop_services() {
    local pid
    for pid in $(port_pids "$PORT"); do
        kill_pid "$pid"
    done
}

# stop 子命令:带反馈输出与结果验证
cmd_stop() {
    local before=0 after=0
    before=$(port_pids "$PORT" | wc -l)
    echo "[runtime] 停止服务 ..."
    stop_services
    sleep 1
    after=$(port_pids "$PORT" | wc -l)
    if [ "$after" = 0 ]; then
        [ "$before" = 0 ] && echo "[runtime] 未发现运行中的 runtime 服务(端口 $PORT)" || echo "[runtime] 已停止"
    else
        echo "[runtime] 仍有进程占用端口 $PORT,请检查:$LOG_DIR/runtime.log"
    fi
}

# 前台运行退出(Ctrl+C / 失败)时,结束监听 runtime 端口的服务进程
cleanup() {
    stop_services >/dev/null 2>&1
    echo "[runtime] 已停止"
}

start() {
    for tool in uv curl; do
        command -v "$tool" >/dev/null 2>&1 || {
            echo "[runtime] 缺少依赖:$tool(需先安装)"
            exit 1
        }
    done

    # 端口占用检查(可能 runtime 已在运行)
    if curl -s -m 1 -o /dev/null "$URL/" 2>/dev/null \
        && curl -s -m 1 -o /dev/null "$URL/api/registry" 2>/dev/null; then
        echo "[runtime] 端口 $PORT 已有 runtime 运行,直接访问 $URL 即可"
        echo "[runtime] 如需重启,先执行:bash scripts/start.sh stop"
        exit 1
    fi

    mkdir -p "$LOG_DIR"
    : >"$LOG_DIR/runtime.log"

    echo "[runtime] 启动服务:$URL(日志:$LOG_DIR/runtime.log)"
    (cd "$REPO" && EIDOLON_RUNTIME_DEVTOOLS="${EIDOLON_RUNTIME_DEVTOOLS:-1}" \
        exec uv run uvicorn backend.main:app --host 127.0.0.1 --port "$PORT") \
        >>"$LOG_DIR/runtime.log" 2>&1 &
    PID=$!

    local ok=0
    for _ in $(seq 1 60); do
        if curl -sf -m 1 -o /dev/null "$URL/" 2>/dev/null; then ok=1; break; fi
        sleep 0.5
    done
    [ "$ok" = 1 ] || {
        echo "[runtime] 启动失败,日志:$LOG_DIR/runtime.log"
        exit 1
    }

    echo
    echo "[runtime] ✓ Eidolon Runtime 已启动"
    echo "  地址: $URL"
    echo "  日志: $LOG_DIR/runtime.log"
    [ -f "$REPO/config.toml" ] || {
        echo "  提示: 未发现 config.toml——未配置 API Key 时角色卡可加载,"
        echo "        对话会返回 503;启动后在右上角「设置」填写即可。"
    }
    echo "  Ctrl+C 停止服务"
    echo

    # 前台等待;Ctrl+C 时 wait 被中断,EXIT trap 结束服务
    wait "$PID" 2>/dev/null || true
}

trap 'exit 130' INT
trap 'exit 143' TERM
trap cleanup EXIT

case "${1:-start}" in
    start) start ;;
    stop) cmd_stop ;;
    *)
        echo "用法: bash scripts/start.sh [start|stop]"
        exit 1
        ;;
esac
