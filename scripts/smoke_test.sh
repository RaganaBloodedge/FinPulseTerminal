#!/usr/bin/env bash
# FinPulse Terminal 端到端冒烟测试。
#
# 一条命令验证"真的能跑"：构建 → C++ 测试 → Python 测试 → CLI 全链路 →
# 智能体研判（含反向工具通道）→ CSV 源 → GUI。任何一步失败立即退出。
#
# 用法（Linux / WSL）:
#   bash scripts/smoke_test.sh
#
# 可选环境变量:
#   BUILD_DIR   构建目录（默认 ~/finpulse-smoke-$$）
#   QT_PREFIX   Qt6 安装路径（给了就一并构建并自检 GUI）

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$HOME/finpulse-smoke-$$}"
QT_PREFIX="${QT_PREFIX:-}"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m   OK\033[0m %s\n' "$*"; }

cd "$ROOT"

# ── 1. 构建 ──────────────────────────────────────────────
step "1/7 构建（含 C++ 测试与工具）"
CMAKE_ARGS=(-S . -B "$BUILD_DIR" -G Ninja -DCMAKE_BUILD_TYPE=Release)
[ -n "$QT_PREFIX" ] && CMAKE_ARGS+=(-DCMAKE_PREFIX_PATH="$QT_PREFIX")
cmake "${CMAKE_ARGS[@]}" > /tmp/finpulse-cfg.log 2>&1
cmake --build "$BUILD_DIR" -j"$(nproc)" > /tmp/finpulse-build.log 2>&1
if grep -qE 'warning:|error:' /tmp/finpulse-build.log; then
    echo "构建产生了 warning/error（本项目要求零告警）："
    grep -E 'warning:|error:' /tmp/finpulse-build.log | head
    exit 1
fi
ok "零 error / 零 warning"

# ── 2. C++ 单元测试 ──────────────────────────────────────
step "2/7 C++ 单元测试"
"$BUILD_DIR/tests/finpulse-tests" | tail -1
ok "C++ 测试通过"

# ── 3. Python 单元测试 ───────────────────────────────────
step "3/7 Python 单元测试"
PYTHONPATH=python python3 python/tests/run_tests.py | tail -1
ok "Python 测试通过"

# ── 4. CLI 全链路（synthetic 源）─────────────────────────
step "4/7 CLI 全链路"
OUT="$("$BUILD_DIR/finpulse-cli" --bars 250 --seed 42 2>/dev/null)"
echo "$OUT" | grep -q "^\[6/6\]" || { echo "CLI 未跑完 6 节报告"; exit 1; }
RPC_LINE="$(echo "$OUT" | grep 'RPC 调用')"
echo "  $RPC_LINE"
echo "$RPC_LINE" | grep -q "成功 6" || { echo "RPC 有失败"; exit 1; }
ok "6 节报告完整，RPC 全部成功"

# ── 5. 智能体研判 + 反向工具通道 ─────────────────────────
#
# 这是本项目最核心的一条主张："C++ 与 Python 双向联动"。它必须用
# **实测出来的调用次数**来验证 —— 报告里少几个数字很容易被当成正常输出
# 放过去，而通道没通就是没通，没有中间状态。
step "5/7 CLI 智能体研判（含反向工具通道）"
OUT="$("$BUILD_DIR/finpulse-cli" --agent --brief --bars 260 --seed 42 2>/dev/null)"
echo "$OUT" | grep -q "^\[7/7\]" || { echo "CLI 未跑出智能体研判一节"; exit 1; }
echo "$OUT" | grep -qE '^  决议:' || { echo "投委会没有给出决议"; exit 1; }
# 反向通道：Python 必须真的回调了 C++ 的终端工具。次数为 0 就是没通。
CALLS="$(echo "$OUT" | grep '工具调用' | head -1 | grep -oE '[0-9]+' | head -1)"
if [ -z "$CALLS" ] || [ "$CALLS" -eq 0 ]; then
    echo "反向工具通道没有发生真实调用（工具调用 = 0）"
    exit 1
fi
echo "  Python 反向回调 C++ 终端工具 $CALLS 次"
ok "投委会出决议，反向工具通道实测可用"

# ── 6. CSV 源 ────────────────────────────────────────────
step "6/7 CSV 数据源"
[ -f data/DEMO-A.csv ] || { echo "缺少 data/DEMO-A.csv"; exit 1; }
"$BUILD_DIR/finpulse-cli" --source csv --symbol DEMO-A --bars 300 2>/dev/null \
    | grep -q "^\[6/6\]" || { echo "CSV 源链路失败"; exit 1; }
ok "CSV 源端到端可用"

# ── 7. GUI（可选）────────────────────────────────────────
step "7/7 GUI 自检（含 AI 研判页）"
GUI="$BUILD_DIR/src/gui/finpulse-gui"
if [ -x "$GUI" ]; then
    "$GUI" --version
    SHOT="$(mktemp -u /tmp/finpulse-gui-XXXX.png)"
    # 用 --screenshot 模式自检：它跑完首轮分析、真渲染了一帧后自行退出，
    # 比"起进程再被 timeout 杀掉"更能证明窗口真的画出来了
    if QT_QPA_PLATFORM=offscreen timeout 60 "$GUI" --screenshot "$SHOT" \
           > /tmp/finpulse-gui.log 2>&1 && [ -s "$SHOT" ]; then
        ok "GUI 渲染一帧成功（$(du -h "$SHOT" | cut -f1)）"
        rm -f "$SHOT"
    else
        echo "GUI 自检失败，输出："
        tail -5 /tmp/finpulse-gui.log
        exit 1
    fi

    # AI 研判页：走 --debate 钩子，它做的事和用户点击完全一样
    # （切页签 → 点「跑投委会研判」），所以测的是真代码路径。
    SHOT2="$(mktemp -u /tmp/finpulse-gui-agent-XXXX.png)"
    if QT_QPA_PLATFORM=offscreen timeout 90 "$GUI" --screenshot "$SHOT2" --debate 5000 \
           > /tmp/finpulse-gui-agent.log 2>&1 && [ -s "$SHOT2" ]; then
        ok "GUI AI 研判页跑完一场投委会（$(du -h "$SHOT2" | cut -f1)）"
        rm -f "$SHOT2"
    else
        echo "GUI AI 研判页自检失败，输出："
        tail -5 /tmp/finpulse-gui-agent.log
        exit 1
    fi
else
    echo "   （未构建 GUI —— 未安装 Qt6 时属正常，CLI 不受影响）"
fi

printf '\n\033[1;32m冒烟测试全部通过。\033[0m 构建目录: %s\n' "$BUILD_DIR"
