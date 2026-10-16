#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""睡眠时刻指标（入睡 / 醒来 / 睡眠中点）自检脚本。

这不是 pytest 测试，而是可以在无 pytest 环境下直接跑的回归清单。

用法：
    cd 06_HealthReport
    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    python tools/_check_sleep_metrics.py          # 全量（含真实文件比对，约 30~60 秒）
    python tools/_check_sleep_metrics.py --fast   # 跳过真实文件比对

退出码 0 = 全部通过；非零 = 有失败项，失败项列在末尾。

────────────────────────────────────────────────────────────────────────────
这份脚本守的是什么
────────────────────────────────────────────────────────────────────────────
1. **取值口径**：三个指标都是「**醒来日的前一天 18:00** 起算的分钟数」。
   用分钟数而不是钟点，是因为时间在午夜处不连续（23:30 与 00:30 只差 60 分钟，
   按钟点算却会得到 23 的差），而 18:00 这个参考点让取值落进 0~900 的**单调区间**，
   可以直接算均值、标准差与分位数。
2. **只用 `Asleep*` 段**定入睡与醒来：`onset` = 当晚最早 asleep 段的 start，
   `wake` = 最晚 asleep 段的 end。**不含 `Awake` 与 `InBed`** ——
   它们分别是「躺着没睡着」与「在床上」，不是睡眠本身。
3. **跨天防护**：`wake − onset > 16 小时`（当夜与当天小睡被 Apple 按结束日
   合并到了同一天）时**不产出**这三个指标，且提示只记一次、不刷屏。
4. **没有 asleep 段的夜晚**（只有 InBed / Awake）不产出这三个指标。
5. **回归护栏**：拿真实导出文件比对，原有 6 个睡眠时长指标
   （`sleep_total` / `sleep_core` / `sleep_deep` / `sleep_rem` / `sleep_awake` /
   `sleep_inbed`）的数值、来源、时间戳必须**逐条完全一致**，
   新增的观测只允许是这 3 个时刻指标。

真实文件与基线（都只读，不写回）：
    * 真实导出：`..\\apple_health_export\\导出.xml`
    * 基线：优先 `out/_sleep_clock_baseline.json`（用**改动前**的代码 dump 的），
      退化到 `out/_baseline_observations.json`。基线缺失时该节 SKIP。
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from typing import Any, Dict, List, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from health_report import catalog  # noqa: E402
from health_report.parsers import parse_apple  # noqa: E402
from health_report.samples import write_apple_xml  # noqa: E402

#: 真实导出文件（相对仓库根），只读。缺了就当 SKIP，不当失败。
REAL_XML = os.path.join(_ROOT, "..", "apple_health_export", "导出.xml")

#: 改动前的基线快照。第一个是"用当前这版代码在改动前 dump 的"，最可信；
#: 第二个是仓库里原有的，可能早于「同日多来源口径」那次改动。
BASELINE_CANDIDATES: Tuple[str, ...] = (
    os.path.join(_ROOT, "out", "_sleep_clock_baseline.json"),
    os.path.join(_ROOT, "out", "_baseline_observations.json"),
)

#: 原有的 6 个「睡眠时长」指标 —— 数值必须与改动前逐条一致。
SLEEP_DURATION_METRICS: Tuple[str, ...] = (
    "sleep_total", "sleep_core", "sleep_deep", "sleep_rem", "sleep_awake", "sleep_inbed",
)

#: 本次新增的 3 个「睡眠时刻」指标。
SLEEP_CLOCK_METRICS: Tuple[str, ...] = (
    "sleep_onset_min", "sleep_wake_min", "sleep_mid_min",
)

#: 跨天防护提示的识别串（与 apple.py 里的汇总 note 对应）。
SKIP_NOTE_MARK = "睡着段跨度超过"

#: 合成夹具（samples.write_apple_xml）当夜的手算期望值，参考点 = 前一夜 18:00：
#:   asleep 段 23:10→02:00、02:00→03:05、03:05→04:35、04:35→06:45
#:   onset = 23:10 -> 5h10m = 310；wake = 06:45 -> 12h45m = 765（07:05 是 InBed 的结束，不算）
#:   mid   = 537.5；sleep_total = 2h50m+1h05m+1h30m+2h10m = 7h35m
FIXTURE_ONSET = 310.0
FIXTURE_WAKE = 765.0
FIXTURE_MID = 537.5
FIXTURE_SLEEP_TOTAL_HOURS = 7.0 + 35.0 / 60.0
#: 夹具里每晚的段完全固定，所以 6 个时长指标逐日均相同。
FIXTURE_DURATION_HOURS: Dict[str, float] = {
    "sleep_total": FIXTURE_SLEEP_TOTAL_HOURS,
    "sleep_core": 5.0,
    "sleep_deep": 1.0 + 5.0 / 60.0,
    "sleep_rem": 1.5,
    "sleep_awake": 1.0 / 3.0,
    "sleep_inbed": 8.25,
}

TMP = tempfile.mkdtemp(prefix="hr_sleep_clock_")
ok: List[str] = []
bad: List[str] = []
skipped: List[str] = []


# ──────────────────────────────────────────────────────────── 输出小工具


def check(label: str, cond: bool, detail: str = "") -> None:
    (ok if cond else bad).append(label)
    print(("  [OK] " if cond else "  [!!] ") + label + (f"  {detail}" if detail else ""))


def skip(label: str) -> None:
    skipped.append(label)
    print("  [--] SKIP " + label)


def info(label: str, detail: str = "") -> None:
    """只打印、不参与通过/失败判定（用于说明性的观察）。"""
    print("  [i]  " + label + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print(f"=== {title} ===")


# ──────────────────────────────────────────────────────────── 小工具


def _write_sleep_xml(path: str, segments: Sequence[Tuple[str, str, str]],
                     source: str = "Apple Watch") -> str:
    """把 (stage_value, startDate, endDate) 列表写成一份最小的 Apple 导出 XML。"""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<HealthData>",
             ' <Me HKCharacteristicTypeIdentifierDateOfBirth="1990-01-01"'
             ' HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexMale"/>']
    for value, start, end in segments:
        lines.append(
            f' <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="{source}"'
            f' creationDate="{end} +0800" startDate="{start} +0800" endDate="{end} +0800"'
            f' value="{value}"/>'
        )
    lines.append("</HealthData>")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _clock_map(ds: Any, metric: str) -> Dict[str, float]:
    return {o.date: o.value for o in ds.series(metric)}


def _check_clock_shape(prefix: str, ds: Any) -> None:
    """三个时刻指标的通用不变量：自洽、date 集合一致、单位与来源口径一致。"""
    onset = _clock_map(ds, "sleep_onset_min")
    wake = _clock_map(ds, "sleep_wake_min")
    mid = _clock_map(ds, "sleep_mid_min")
    check(f"{prefix}：三个时刻指标的日期集合一致",
          set(onset) == set(wake) == set(mid),
          f"onset={len(onset)} wake={len(wake)} mid={len(mid)}")
    if not onset:
        return
    bad_mid = [d for d in onset
               if abs(mid.get(d, 1e9) - (onset[d] + wake.get(d, 0.0)) / 2.0) > 0.5]
    check(f"{prefix}：mid == (onset + wake) / 2（允许 0.5 取整误差）", not bad_mid,
          str(bad_mid[:3]))
    bad_order = [d for d in onset if not wake.get(d, -1e9) > onset[d]]
    check(f"{prefix}：wake > onset（逐夜）", not bad_order, str(bad_order[:3]))
    bad_unit = [(o.metric, o.unit) for o in ds.observations
                if o.metric in SLEEP_CLOCK_METRICS
                and catalog.get(o.metric) is not None
                and o.unit != catalog.get(o.metric).unit]
    check(f"{prefix}：Observation.unit == catalog 的 spec.unit（分钟）", not bad_unit,
          str(bad_unit[:3]))
    total = {o.date: o for o in ds.series("sleep_total")}
    bad_src = [(o.metric, o.date, o.source) for o in ds.observations
               if o.metric in SLEEP_CLOCK_METRICS
               and o.date in total and o.source != total[o.date].source]
    check(f"{prefix}：来源与同一天的 sleep_total 一致", not bad_src, str(bad_src[:3]))
    # 时刻跨度必须 >= 睡着总时长（两者之差是夜间清醒与中间的空档）
    bad_span = [(d, wake[d] - onset[d], total[d].value * 60.0) for d in onset
                if d in total and wake[d] - onset[d] < total[d].value * 60.0 - 1e-6]
    check(f"{prefix}：醒来 − 入睡 >= sleep_total（差值即夜间清醒）", not bad_span,
          str(bad_span[:3]))


def _obs_key(o: Dict[str, Any]) -> Tuple[str, str, float, str, str]:
    """比对用的观测指纹：指标 / 日期 / 数值 / 来源 / 时间戳（基线 JSON 里的字典）。"""
    return (str(o.get("metric", "")), str(o.get("date", "")),
            round(float(o.get("value") or 0.0), 9),
            str(o.get("source", "")), str(o.get("ts", "")))


def _live_key(o: Any) -> Tuple[str, str, float, str, str]:
    """与 `_obs_key` 同一套指纹，只是输入换成解析出来的 `Observation` 对象。"""
    return (str(o.metric), str(o.date), round(float(o.value), 9),
            str(o.source), str(o.ts))


def _load_baseline() -> Tuple[str, Dict[str, Any]]:
    """按优先级找一份可用的基线 JSON。找不到返回 ("", {})。"""
    for path in BASELINE_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            info(f"基线 {os.path.basename(path)} 读不出来，跳过它", str(exc))
            continue
        if payload.get("observations"):
            return path, payload
    return "", {}


# ──────────────────────────────────────────────────────────── 1. catalog

section("1. catalog：3 条 MetricSpec 的设置")
for mid, cn, unit in (("sleep_onset_min", "入睡时刻", "分钟"),
                      ("sleep_wake_min", "醒来时刻", "分钟"),
                      ("sleep_mid_min", "睡眠中点", "分钟")):
    spec = catalog.get(mid)
    check(f"catalog 有 {mid}（{cn}）", spec is not None)
    if spec is None:
        continue
    check(f"{mid}：中文名 {cn}", spec.cn == cn, spec.cn)
    check(f"{mid}：单位 {unit}", spec.unit == unit, spec.unit)
    check(f"{mid}：category=睡眠", spec.category == "睡眠", spec.category)
    check(f"{mid}：agg=mean", spec.agg == "mean", spec.agg)
    check(f"{mid}：verdict=False（没有公认参考范围）", spec.verdict is False)
    check(f"{mid}：precision=0", spec.precision == 0, str(spec.precision))
    check(f"{mid}：noise > 0（小值，例如 15 分钟）", spec.noise > 0, str(spec.noise))
    check(f"{mid}：没有参考区间", spec.ref_low is None and spec.ref_high is None
          and not spec.ref_fn and not spec.ref_text)
    check(f"{mid}：hint 写清了参考点 18:00", "18:00" in spec.hint, spec.hint[:40])
    check(f"{mid}：aliases 里有中文名与英文 id",
          cn in spec.aliases and mid in spec.aliases, str(spec.aliases))
    check(f"{mid}：resolve 能认出来（中文名与英文 id）",
          catalog.resolve(cn) == mid and catalog.resolve(mid) == mid)
check("三条 spec 的 id 不重复（catalog.BY_ID 覆盖全部 SPECS）",
      len(catalog.BY_ID) == len(catalog.SPECS))

# ──────────────────────────────────────────────────────────── 2. 合成 XML

section("2. 合成 XML（samples.write_apple_xml）：期望值 + 原有指标不受影响")
fixture_xml = write_apple_xml(os.path.join(TMP, "apple_export_sample.xml"))
fds = parse_apple(fixture_xml)

onset = fds.series("sleep_onset_min")
wake = fds.series("sleep_wake_min")
mid = fds.series("sleep_mid_min")
check("合成文件解析出 3 个时刻指标", bool(onset and wake and mid),
      f"{len(onset)}/{len(wake)}/{len(mid)}")
if onset and wake and mid:
    check("第一天入睡时刻 = 310（23:10 相对前一天 18:00）",
          abs(onset[0].value - FIXTURE_ONSET) < 1e-9, str(onset[0].value))
    check("第一天醒来时刻 = 765（06:45，不是 InBed 的 07:05=785）",
          abs(wake[0].value - FIXTURE_WAKE) < 1e-9, str(wake[0].value))
    check("第一天睡眠中点 = 537.5", abs(mid[0].value - FIXTURE_MID) < 1e-9, str(mid[0].value))
    check("ts 取该夜醒来的时间戳", onset[0].ts == wake[0].ts == mid[0].ts == "2026-05-01 06:45:00",
          f"{onset[0].ts} / {wake[0].ts} / {mid[0].ts}")
    check("睡眠时刻落进 0~900 的单调区间",
          all(0 < o.value < 900 for o in onset + wake + mid),
          f"onset {min(o.value for o in onset)}~{max(o.value for o in onset)}，"
          f"wake {min(o.value for o in wake)}~{max(o.value for o in wake)}")
    check("每一天都有这 3 个指标（与 sleep_total 同样多）",
          len(onset) == len(wake) == len(mid) == len(fds.series("sleep_total")),
          f"{len(onset)} vs {len(fds.series('sleep_total'))}")
_check_clock_shape("合成文件", fds)

for metric, want in FIXTURE_DURATION_HOURS.items():
    series = fds.series(metric)
    wrong = [o for o in series if abs(o.value - want) > 1e-9]
    check(f"原有指标 {metric} 逐日不变（{want:.6f} 小时）", bool(series) and not wrong,
          f"{len(series)} 天，异常 {len(wrong)} 条")

# ──────────────────────────────────────────────────────────── 3. 跨天防护

section("3. 跨天防护：当夜 + 当天小睡被合并到同一天（跨度 > 16 小时）")
# 睡得早（21:00）的人 + 下午午睡（12:30~14:00）：两段都按**结束日**归到 2026-03-02，
# 于是"当夜"被算成 21:00 → 14:00 = 17 小时 —— 这正是要防的那种合并。
nap_xml = _write_sleep_xml(os.path.join(TMP, "night_plus_nap.xml"), [
    ("HKCategoryValueSleepAnalysisInBed", "2026-03-01 20:50:00", "2026-03-02 07:00:00"),
    ("HKCategoryValueSleepAnalysisAsleepCore", "2026-03-01 21:00:00", "2026-03-02 06:30:00"),
    ("HKCategoryValueSleepAnalysisAsleepREM", "2026-03-02 06:30:00", "2026-03-02 06:45:00"),
    ("HKCategoryValueSleepAnalysisAwake", "2026-03-02 06:45:00", "2026-03-02 06:55:00"),
    ("HKCategoryValueSleepAnalysisAsleepUnspecified", "2026-03-02 12:30:00", "2026-03-02 14:00:00"),
])
nds = parse_apple(nap_xml)
clock_n = [o for o in nds.observations if o.metric in SLEEP_CLOCK_METRICS]
check("跨度 17 小时的夜晚不产出 3 个时刻指标", not clock_n,
      str([(o.metric, o.date, o.value) for o in clock_n]))
skip_notes = [n for n in nds.notes if SKIP_NOTE_MARK in n]
check("往 notes 记了一条汇总提示", len(skip_notes) == 1, f"{len(skip_notes)} 条")
if skip_notes:
    info("提示原文", skip_notes[0])
    check("提示里写明了被跳过的夜晚数（1）", "1 个夜晚" in skip_notes[0], skip_notes[0][:30])
# 原有睡眠指标必须照常产出（防护不能连时长一起吞掉）
nap_total = nds.series("sleep_total")
check("原有 sleep_total 照常产出 = 9.5 + 0.25 + 1.5 = 11.25",
      len(nap_total) == 1 and abs(nap_total[0].value - 11.25) < 1e-9,
      str([(o.date, o.value) for o in nap_total]))
check("原有 sleep_awake / sleep_inbed 照常产出（10 分钟 / 10 小时 10 分）",
      abs(nds.latest("sleep_awake").value - 10.0 / 60.0) < 1e-9
      and abs(nds.latest("sleep_inbed").value - (10 * 60 + 10) / 60.0) < 1e-9,
      f"{nds.latest('sleep_awake').value} / {nds.latest('sleep_inbed').value}")

# 多个同类夜晚只记一条提示（不刷屏）：连续三夜都是「前一夜 + 当天傍晚小睡」，
# 每夜跨度都是 21 小时，都会被跳过。
many_xml = _write_sleep_xml(os.path.join(TMP, "many_long.xml"), [
    (value, start, end)
    for day in (10, 11, 12)
    for value, start, end in (
        ("HKCategoryValueSleepAnalysisAsleepCore",
         f"2026-04-{day - 1:02d} 23:00:00", f"2026-04-{day:02d} 06:00:00"),
        ("HKCategoryValueSleepAnalysisAsleepUnspecified",
         f"2026-04-{day:02d} 19:00:00", f"2026-04-{day:02d} 20:00:00"),
    )
])
mds = parse_apple(many_xml)
long_notes = [n for n in mds.notes if SKIP_NOTE_MARK in n]
check("三个同类夜晚只记一条提示（不刷屏）", len(long_notes) == 1, f"{len(long_notes)} 条")
if long_notes:
    check("提示里的数量是 3", "3 个夜晚" in long_notes[0], long_notes[0][:30])
check("这三个夜晚也不产出时刻指标",
      not [o for o in mds.observations if o.metric in SLEEP_CLOCK_METRICS])

# 边界：跨度刚好 16 小时不跳过（阈值是 > 16 小时）
edge_xml = _write_sleep_xml(os.path.join(TMP, "edge16.xml"), [
    ("HKCategoryValueSleepAnalysisAsleepCore", "2026-05-01 22:00:00", "2026-05-02 06:00:00"),
    ("HKCategoryValueSleepAnalysisAsleepUnspecified",
     "2026-05-02 12:00:00", "2026-05-02 14:00:00"),
])
eds = parse_apple(edge_xml)
edge_onset = eds.series("sleep_onset_min")
check("跨度恰好 16 小时不跳过（阈值是 > 16 小时）",
      len(edge_onset) == 1 and abs(edge_onset[0].value - 240.0) < 1e-9,
      str([(o.metric, o.value) for o in eds.observations if o.metric in SLEEP_CLOCK_METRICS]))
check("该夜晚的 wake 是 14:00（小睡结束）—— 这是 16 小时阈值的已知边界",
      bool(eds.series("sleep_wake_min"))
      and abs(eds.series("sleep_wake_min")[0].value - (20 * 60)) < 1e-9,
      str([o.value for o in eds.series("sleep_wake_min")]))
info("说明", "跨度在 16 小时以内的小睡合并不会被这个阈值挡住（阈值由需求指定为 > 16 小时）；"
             "下游若看到 wake 落在下午，多半就是这种情况。")

# ──────────────────────────────────────────────────────────── 4. 没有 asleep 段的夜晚

section("4. 没有 asleep 段的夜晚（只有 InBed / Awake）：不产出时刻指标")
noasleep_xml = _write_sleep_xml(os.path.join(TMP, "no_asleep.xml"), [
    ("HKCategoryValueSleepAnalysisInBed", "2026-04-01 23:00:00", "2026-04-02 07:00:00"),
    ("HKCategoryValueSleepAnalysisAwake", "2026-04-02 03:00:00", "2026-04-02 03:30:00"),
])
ads = parse_apple(noasleep_xml)
check("没有 asleep 段就不产出 3 个时刻指标",
      not [o for o in ads.observations if o.metric in SLEEP_CLOCK_METRICS],
      str([(o.metric, o.value) for o in ads.observations]))
check("也不记跨天防护的提示", not [n for n in ads.notes if SKIP_NOTE_MARK in n])
check("原有 sleep_inbed 照常产出 8.0",
      abs(ads.latest("sleep_inbed").value - 8.0) < 1e-9,
      str(ads.latest("sleep_inbed").value))
check("原有 sleep_awake 照常产出 0.5",
      abs(ads.latest("sleep_awake").value - 0.5) < 1e-9, str(ads.latest("sleep_awake").value))
check("没有 asleep 段时 sleep_total 本来就不产出",
      not ads.series("sleep_total"), str([(o.date, o.value) for o in ads.series("sleep_total")]))

# 认不出的阶段写法按"睡着（未分期）"处理，仍然产出时刻指标（与时长口径一致）
unknown_xml = _write_sleep_xml(os.path.join(TMP, "unknown_stage.xml"), [
    ("HKCategoryValueSleepAnalysisAsleepCore", "2026-05-01 23:10:00", "2026-05-02 02:00:00"),
    ("HKCategoryValueSleepAnalysisFutureKind", "2026-05-02 02:00:00", "2026-05-02 06:45:00"),
])
uds = parse_apple(unknown_xml)
check("认不出的写法按睡着处理，时刻指标照常产出（onset 310 / wake 765）",
      bool(uds.series("sleep_onset_min")) and bool(uds.series("sleep_wake_min"))
      and abs(uds.latest("sleep_onset_min").value - FIXTURE_ONSET) < 1e-9
      and abs(uds.latest("sleep_wake_min").value - FIXTURE_WAKE) < 1e-9,
      str([(o.metric, o.value) for o in uds.observations]))

# ──────────────────────────────────────────────────────────── 5. 真实文件

section("5. 真实文件：原有 6 个睡眠指标逐条未变（只读，不写回）")
fast = "--fast" in sys.argv[1:]
if fast:
    skip("真实文件比对（--fast）")
elif not os.path.exists(REAL_XML):
    skip(f"真实文件比对（找不到 {REAL_XML}）")
else:
    size_mb = os.path.getsize(REAL_XML) / (1024.0 * 1024.0)
    info(f"解析 {REAL_XML}", f"{size_mb:.0f} MB，约 30 秒")
    t0 = time.time()
    rds = parse_apple(REAL_XML)
    info("解析完成", f"{time.time() - t0:.1f}s，观测 {len(rds.observations)} 条")

    live_onset = rds.series("sleep_onset_min")
    live_wake = rds.series("sleep_wake_min")
    live_mid = rds.series("sleep_mid_min")
    live_total = rds.series("sleep_total")
    info("真实数据上的时刻指标",
         f"入睡 {len(live_onset)} 夜 / 醒来 {len(live_wake)} 夜 / 中点 {len(live_mid)} 夜，"
         f"sleep_total {len(live_total)} 夜")
    if live_onset:
        info("中位数（分钟，参考点 = 前一夜 18:00）",
             f"入睡 {statistics.median([o.value for o in live_onset]):.0f}，"
             f"醒来 {statistics.median([o.value for o in live_wake]):.0f}，"
             f"中点 {statistics.median([o.value for o in live_mid]):.0f}")
    skip_notes_real = [n for n in rds.notes if SKIP_NOTE_MARK in n]
    n_skipped = len(live_total) - len(live_onset)
    check("跨天防护提示只记一条", len(skip_notes_real) <= 1, f"{len(skip_notes_real)} 条")
    if skip_notes_real:
        info("跨天防护提示", skip_notes_real[0])
    check("被跳过的夜晚数 = sleep_total 夜数 − 时刻夜数（且提示里数字一致）",
          (n_skipped == 0 and not skip_notes_real)
          or (bool(skip_notes_real) and f"{n_skipped} 个夜晚" in skip_notes_real[0]),
          f"差值 {n_skipped}")
    _check_clock_shape("真实文件", rds)

    baseline_path, baseline = _load_baseline()
    if not baseline:
        skip("真实文件比对（out/ 下没有可用的基线 JSON；基线必须在改动前 dump）")
    else:
        info("使用的基线", f"{os.path.relpath(baseline_path, _ROOT)}"
                           f"（{len(baseline['observations'])} 条观测，"
                           f"文件时间 {time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(baseline_path)))}）")
        base_keys = {_obs_key(o) for o in baseline["observations"]}
        live_keys = {_live_key(o) for o in rds.observations}

        base_sleep = {k for k in base_keys if k[0] in SLEEP_DURATION_METRICS}
        live_sleep = {k for k in live_keys if k[0] in SLEEP_DURATION_METRICS}
        missing_sleep = sorted(base_sleep - live_sleep)
        check(f"原有 6 个睡眠时长指标逐条一致：{len(base_sleep)} 条中 0 处不同",
              not missing_sleep,
              f"{len(missing_sleep)} 处不同，例如 {missing_sleep[:3]}")
        extra_sleep = sorted(live_sleep - base_sleep)
        check("原有 6 个睡眠指标没有多出观测", not extra_sleep, str(extra_sleep[:3]))

        extra = {k for k in live_keys - base_keys}
        bad_extra = sorted(k for k in extra if k[0] not in SLEEP_CLOCK_METRICS)
        check(f"新增观测只有 {len(SLEEP_CLOCK_METRICS)} 个睡眠时刻指标",
              not bad_extra, f"{len(bad_extra)} 条其他指标的新增：{bad_extra[:3]}")
        info("新增观测条数", str(len(extra)))

        # 非睡眠指标：本次改动不碰它们，理论上也应当一条不差。
        base_other = base_keys - base_sleep
        missing_other = sorted(k for k in base_other if k not in live_keys)
        baseline_has_multisource = any("同一天有多台设备" in n for n in baseline.get("notes") or [])
        stale = (not baseline_has_multisource
                 and any("同一天有多台设备" in n for n in rds.notes))
        if stale:
            info("基线疑似来自旧版本",
                 "它没有「同一天多台设备」的口径说明，说明 dump 于「同日多来源口径」那次改动之前；"
                 f"因此非睡眠指标的 {len(missing_other)} 处差异与本次改动无关，不作失败判定。"
                 "要严格比对请在改动前用 out/_baseline_dump.py 重新 dump 一份。")
        else:
            check(f"非睡眠指标也一条不差（{len(base_other)} 条中 0 处不同）",
                  not missing_other,
                  f"{len(missing_other)} 处不同，例如 {missing_other[:3]}")

# ──────────────────────────────────────────────────────────── 收尾

print()
print("=" * 66)
print(f"通过 {len(ok)} / {len(ok) + len(bad)}" + (f"，跳过 {len(skipped)}" if skipped else ""))
if bad:
    print("失败项：")
    for b in bad:
        print("  -", b)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
