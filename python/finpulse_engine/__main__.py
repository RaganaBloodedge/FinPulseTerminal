"""引擎主入口。

启动方式（由 C++ 壳拉起，也可以手工跑来做调试）::

    PYTHONPATH=python python3 -u -m finpulse_engine

手工调试时可以直接往 stdin 敲帧（见 tools/ 里的 shell_probe.py），
或者干脆用仓库根目录的 ``scripts/smoke_test.sh``。

三条硬性约束：

1. **stdout 只走协议。** 所有日志一律写 stderr。这条在 ``protocol`` 和
   ``Log.h`` 里都强调过 —— 它是最容易在后期改坏的一条。
2. **读取必须用 os.read。** ``sys.stdin.buffer.read(n)`` 会一直阻塞到读满
   n 个字节才返回，而请求帧大多比 n 短，结果就是引擎"卡住不动"。
3. **任何解析/计算异常都不能让进程退出。** 帧坏了要退出（流已不可信），
   但单个请求算错了只回一个错误响应。
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
import time
from typing import Optional, Sequence

from . import __version__
from .datasource import registry as ds_registry
from .forecast import registry as fc_registry
from .protocol import FrameError, FrameReader, encode_frame
from .rpc import Dispatcher
from . import service

#: 单次读取的字节数。取 64 KiB 是因为它比任何常规响应都大，
#: 又不至于让一次系统调用占用过多内存。
_READ_CHUNK = 64 * 1024

log = logging.getLogger("finpulse")


def setup_logging() -> None:
    level_name = os.environ.get("FINPULSE_LOG", "warn").strip().upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,  # 绝不能是 stdout：那是协议通道
        force=True,
    )


def build_dispatcher(out: Optional[object] = None,
                     write_lock: Optional[threading.Lock] = None) -> Dispatcher:
    """发现插件并注册 RPC 方法。

    ``out`` / ``write_lock`` 会一路传到智能体的事件通道上。响应帧和事件帧
    共用 stdout，所以两边必须共用同一把锁 —— 详见 ``stream.FrameSink``。
    """
    ds_registry.discover()
    fc_registry.discover()

    dispatcher = Dispatcher()
    service.build(dispatcher, event_out=out, write_lock=write_lock)

    log.info(
        "finpulse-engine %s 就绪 (python %s) | 数据源=%d 预测器=%d 方法=%d",
        __version__,
        platform.python_version(),
        len(ds_registry.names()),
        len(fc_registry.names()),
        len(dispatcher.methods),
    )
    log.debug("数据源: %s", ds_registry.names())
    log.debug("预测器: %s", fc_registry.names())
    return dispatcher


def main(argv: Optional[Sequence[str]] = None) -> int:
    setup_logging()

    stdout = sys.stdout.buffer
    # 这把锁保护的是**帧边界**，不是数据：智能体研判时会从多个线程推送
    # 进度事件，而主循环同时可能在写响应。两处各持一把锁挡不住交错。
    write_lock = threading.Lock()

    try:
        stdin_fd = sys.stdin.fileno()
    except (AttributeError, OSError):
        log.error("stdin 不可用，引擎必须以管道方式启动")
        return 4

    # 事件只写给"正在等事件的调用方"：stdin 是管道就说明壳在对面，
    # 这时推事件是有意义的；交互式 tty 下推事件只会把终端刷满。
    event_out = stdout if not sys.stdin.isatty() else None
    dispatcher = build_dispatcher(out=event_out, write_lock=write_lock)
    reader = FrameReader()

    handled = 0

    while True:
        try:
            chunk = os.read(stdin_fd, _READ_CHUNK)
        except InterruptedError:
            continue
        except OSError as exc:
            log.error("读取 stdin 失败: %s", exc)
            return 3

        if not chunk:
            log.info("stdin 已关闭，引擎正常退出（累计处理 %d 个请求）", handled)
            return 0

        try:
            for msg in reader.feed(chunk):
                method = msg.get("method") if isinstance(msg, dict) else "?"
                started = time.perf_counter()

                response = dispatcher.handle(msg)
                if response is None:
                    continue

                # 用同一把锁写响应：一次响应必须整帧连续落进管道。
                with write_lock:
                    stdout.write(encode_frame(response))
                    stdout.flush()  # 必须立刻刷：对面在同步等这个响应
                handled += 1

                log.debug(
                    "%s 用时 %.2f ms",
                    method,
                    (time.perf_counter() - started) * 1000.0,
                )
        except FrameError as exc:
            # 帧边界已经不可信，继续解只会产出一串垃圾。退出，让壳重建。
            log.error("帧同步丢失: %s", exc)
            return 2
        except BrokenPipeError:
            log.warning("stdout 已断开，引擎退出")
            return 0
        except KeyboardInterrupt:
            log.info("收到中断，引擎退出")
            return 0


if __name__ == "__main__":
    sys.exit(main())
