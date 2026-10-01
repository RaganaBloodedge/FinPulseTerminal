"""测试运行器 —— 与 C++ 侧 tests/ 的输出风格保持一致。

用法::

    python python/tests/run_tests.py
    python python/tests/run_tests.py -v          # 逐条列出用例
"""

from __future__ import annotations

import argparse
import sys
import time
import unittest
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="FinPulse 引擎单元测试")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # 让 `import finpulse_engine` 生效：tests/ 的父目录是 python/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    here = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(here), pattern="test_*.py")

    verbosity = 2 if args.verbose else 1
    if not args.verbose:
        # 非 verbose 模式下 unittest 的进度点会混进我们的摘要里，改用静默收集
        runner = unittest.TextTestRunner(verbosity=0, stream=_Null(), buffer=True)
    else:
        runner = unittest.TextTestRunner(verbosity=2)

    print("┌─ FinPulse 引擎单元测试 ─────────────────────────")
    started = time.perf_counter()
    result = runner.run(suite)
    elapsed = (time.perf_counter() - started) * 1000.0

    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    skipped = len(result.skipped)

    print("└─────────────────────────────────────────────────")
    if result.wasSuccessful():
        print(f"全部通过: {total} 通过 / 0 失败 / 共 {total} 个用例"
              f"（耗时 {elapsed:.0f} ms）")
        return 0

    print(f"失败: {total - failed - skipped} 通过 / {failed} 失败 / 共 {total} 个用例\n")
    for kind, entries in (("FAIL", result.failures), ("ERROR", result.errors)):
        for test, tb in entries:
            print(f"  [{kind}] {test.id()}")
            for line in tb.strip().splitlines()[-4:]:
                print(f"         {line}")
    return 1


class _Null:
    """吞掉 unittest 的进度输出，只留我们自己的摘要。"""

    def write(self, *_args):
        pass

    def flush(self):
        pass


if __name__ == "__main__":
    sys.exit(main())
