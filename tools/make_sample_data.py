#!/usr/bin/env python3
"""重新生成 examples/ 下的合成示例文件。

示例文件是**生成出来的**，不是手写的：这样样例数据永远和解析器、catalog 保持一致，
不会出现「样例里的字段名改了但解析器没跟上」这种事。

    python tools/make_sample_data.py            # 写到 examples/
    python tools/make_sample_data.py -o /tmp/x  # 写到别处

注意：改 samples.py 里的公式会改变示例文件内容，也会改变 `health-report demo`
的输出。所以 `examples/expected_report.md` 需要跟着重新生成：

    python -m health_report demo -o examples/expected_report.md
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from health_report.samples import write_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="生成合成示例文件")
    ap.add_argument("-o", "--out", default=None, help="输出目录（默认仓库根的 examples/）")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = args.out or os.path.join(here, "..", "examples")
    outdir = os.path.abspath(outdir)

    for p in write_all(outdir):
        print(f"[√] {p}  ({os.path.getsize(p) / 1024:.1f} KB)")
    print("\n这些文件是合成数据，不含任何真实个人数据，可以安全提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
