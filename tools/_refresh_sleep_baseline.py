#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刷新 `_check_sleep_metrics.py` 用的改动前基线（放在 out/ 下，不进仓库）。

为什么需要刷新、以及为什么刷新是安全的：基线原本 dump 于两处**刻意改动之前**——

1. 血氧饱和度的比值换算修复（`apple.py`：判据从写死 `body_fat_pct` 改为
   「规范单位是 % 且值 ≤ 1」）。真实数据上 379 条 `oxygen_saturation` 从
   0.94 这类比值变成 94 这类百分数，**恰好 ×100**。
2. 新增 3 个睡眠时刻指标（3369 条新观测）。

刷新前已逐条核对：差异 379 处**全部**是 `oxygen_saturation`、**全部恰好 ×100**、
0 处非 ×100、0 个缺失键。所以刷新不会掩盖任何回归。

用法：`python tools\_refresh_sleep_baseline.py`（约 30 秒，只读真实导出文件）
"""
from __future__ import annotations

import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))

BASELINE = os.path.join(_ROOT, "out", "_sleep_clock_baseline.json")


def main() -> int:
    from health_report.parsers import parse_path

    real = os.path.join(_ROOT, "..", "apple_health_export", "导出.xml")
    if not os.path.isfile(real):
        print(f"[×] 找不到真实导出文件：{os.path.abspath(real)}")
        return 1

    print(f"[i] 解析 {os.path.abspath(real)}（只读）……")
    started = time.time()
    ds = parse_path(real)
    seconds = time.time() - started

    old = {}
    if os.path.isfile(BASELINE):
        try:
            old = json.load(open(BASELINE, encoding="utf-8"))
        except (OSError, ValueError):
            old = {}

    payload = {
        "seconds": seconds,
        "notes": list(getattr(ds, "notes", None) or []),
        "profile": dict(getattr(ds, "profile", None) or {}),
        "files": [os.path.basename(f) for f in (getattr(ds, "files", None) or [])],
        "n_workouts": len(getattr(ds, "workouts", None) or []),
        "observations": [
            {"metric": o.metric, "date": o.date, "value": o.value,
             "source": o.source, "ts": o.ts}
            for o in ds.observations
        ],
    }

    os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print(f"[√] 已写入 {os.path.relpath(BASELINE, _ROOT)}"
          f"（{len(payload['observations'])} 条观测，{seconds:.1f} 秒）")
    if old.get("observations"):
        print(f"[i] 原基线有 {len(old['observations'])} 条，现在 {len(payload['observations'])} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
