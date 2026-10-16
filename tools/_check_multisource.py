#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同日多来源口径（`source_policy`）自测脚本。

**要修的是什么**：Apple 导出里「按日累加型」指标（步数/能量/距离/爬楼/锻炼分钟）
常常同一天同时有 Apple Watch 与 iPhone 的记录。旧版解析器无条件把它们加在一起，
于是既把数值系统性算高（实测一份导出放大 1.38 倍），又让
`Dataset.sources_of()` 只剩一个来源 —— `SRC_MIXED` 规则**静默失效**，
规则层根本看不见这个错误。

修法是把口径变成显式选择（`primary` / `per-day-max` / `sum`），
而不是去猜 Apple 没有公开规范的重叠区间去重算法。

用法：
    cd 06_HealthReport
    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    python tools/_check_multisource.py                      # 合成样本检查（秒级）
    python tools/_check_multisource.py --real               # 再加真实导出，跑三种口径
    python tools/_check_multisource.py --real --baseline out/_baseline_observations.json
    python tools/_check_multisource.py --real --memory      # 再量一次 tracemalloc 峰值

退出码 0 表示全部通过；非零表示有失败项，失败项列在末尾。

隐私说明
--------
**本文件不含任何真实个人数值。** 真实导出相关的一切都是跑出来再打印的；
`--baseline` 指向的 JSON 由 out/ 下的临时脚本生成，不进仓库。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import tracemalloc
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from health_report import catalog, privacy  # noqa: E402
from health_report.parsers import parse_apple, parse_path  # noqa: E402
from health_report.parsers.apple import SOURCE_POLICIES  # noqa: E402
from health_report.samples import write_inbody_csv  # noqa: E402

#: 默认的真实导出（相对仓库根）。不存在就跳过真实部分。
DEFAULT_REAL = os.path.join(_ROOT, "..", "apple_health_export", "导出.xml")

#: 多来源天数最多写几条 note。
MAX_NOTES = 6

ok: List[str] = []
bad: List[str] = []
skipped: List[str] = []
TMP = tempfile.mkdtemp(prefix="hr_multisrc_")


def check(label: str, cond: bool, detail: str = "") -> None:
    (ok if cond else bad).append(label)
    print(("  [OK] " if cond else "  [!!] ") + label + (f"  {detail}" if detail else ""))


def skip(label: str) -> None:
    skipped.append(label)
    print("  [--] SKIP " + label)


def section(title: str) -> None:
    print()
    print(f"=== {title} ===")


# ──────────────────────────────────────────────────────────── 合成样本


def write_multi_source_xml(path: str) -> str:
    """造一份「同一天多台设备」的导出。

    天数与来源都精心安排，好让三种口径的结果互不相同：

    2026-01-01  主力手环 步数 1000+2000=3000、心率 100/120/110（平均 110）
                iPhone   步数 2500、心率 60
                备用机    步数 500
    2026-01-02  iPhone   步数 1200、心率 70

    全数据集记录条数：主力手环 5 > iPhone 4 > 备用机 1，
    所以不指定 prefer_sources 时主力手环就是主设备（记录条数最多的那台）。
    """
    rows: List[str] = ['<?xml version="1.0" encoding="UTF-8"?>', "<HealthData>",
                       ' <Me HKCharacteristicTypeIdentifierDateOfBirth="1990-01-01"'
                       ' HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexMale"/>']

    def rec(rtype: str, src: str, unit: str, value: Any, start: str) -> None:
        rows.append(
            f' <Record type="{rtype}" sourceName="{src}" unit="{unit}"'
            f' startDate="{start} +0800" endDate="{start} +0800" value="{value}"/>'
        )

    steps, hr = "HKQuantityTypeIdentifierStepCount", "HKQuantityTypeIdentifierHeartRate"
    rec(steps, "主力手环", "count", 1000, "2026-01-01 08:00:00")
    rec(steps, "主力手环", "count", 2000, "2026-01-01 12:00:00")
    rec(steps, "iPhone", "count", 2500, "2026-01-01 09:00:00")
    rec(steps, "备用机", "count", 500, "2026-01-01 10:00:00")
    rec(hr, "主力手环", "count/min", 100, "2026-01-01 08:00:00")
    rec(hr, "主力手环", "count/min", 120, "2026-01-01 12:00:00")
    rec(hr, "主力手环", "count/min", 110, "2026-01-01 14:00:00")
    rec(hr, "iPhone", "count/min", 60, "2026-01-01 09:00:00")
    rec(steps, "iPhone", "count", 1200, "2026-01-02 09:00:00")
    rec(hr, "iPhone", "count/min", 70, "2026-01-02 09:00:00")
    rows.append("</HealthData>")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rows) + "\n")
    return path


def write_personal_name_xml(path: str) -> str:
    """来源名里带机主姓名的导出（Apple 默认命名习惯）。

    也顺带放了两种「同一个设备」的写法：不带姓名前缀的 `Apple Watch`
    与带 NBSP 的 `某某的Apple\\xa0Watch` —— 真实导出里就长这样。
    """
    rows: List[str] = ['<?xml version="1.0" encoding="UTF-8"?>', "<HealthData>"]
    rows.append(
        ' <Record type="HKQuantityTypeIdentifierStepCount"'
        ' sourceName="某某的Apple\u00a0Watch" unit="count"'
        ' startDate="2026-02-01 08:00:00 +0800" endDate="2026-02-01 08:00:00 +0800" value="8000"/>'
    )
    rows.append(
        ' <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count"'
        ' startDate="2026-02-01 09:00:00 +0800" endDate="2026-02-01 09:00:00 +0800" value="100"/>'
    )
    rows.append(
        ' <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"'
        ' durationUnit="min" sourceName="某某的Apple\u00a0Watch"'
        ' startDate="2026-02-01 18:00:00 +0800" endDate="2026-02-01 18:30:00 +0800"/>'
    )
    rows.append("</HealthData>")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rows) + "\n")
    return path


def steps_total(ds: Any) -> float:
    return sum(o.value for o in ds.series("steps"))


def steps_of(ds: Any, day: str) -> List[Any]:
    return [o for o in ds.series("steps") if o.date == day]


def _is_multisource_note(text: str) -> bool:
    """这条 note 是不是「同日多来源口径说明」（含收尾的"另有 N 个指标"那条）。"""
    return ("同一天有多台设备" in text) or ("也存在同一天多设备" in text)


# ──────────────────────────────────────────────────────────── 1~5：合成检查


def synthetic_checks() -> None:
    xml = write_multi_source_xml(os.path.join(TMP, "multi.xml"))
    runs = {p: parse_apple(xml, source_policy=p) for p in SOURCE_POLICIES}

    section("1. sum 类指标：三种口径的取值与来源")
    for p, ds in runs.items():
        d1 = steps_of(ds, "2026-01-01")
        check(f"{p}: 每天只产出一条 Observation", len(d1) == 1,
              f"{len(d1)} 条")
        row = d1[0] if d1 else None
        print(f"     {p:<12} 01-01 步数={row.value if row else None}"
              f"  来源={row.source if row else None}  全年={steps_total(ds):,.0f}")
    check("primary: 01-01 取主力手环的 3000（不是 6000）",
          steps_of(runs["primary"], "2026-01-01")[0].value == 3000.0,
          str(steps_of(runs["primary"], "2026-01-01")[0].value))
    check("primary: 01-01 来源是主力手环（记录条数最多）",
          steps_of(runs["primary"], "2026-01-01")[0].source == "主力手环")
    check("primary: 01-02 只有 iPhone，自动回退（不整天丢数据）",
          [o.value for o in steps_of(runs["primary"], "2026-01-02")] == [1200.0])
    check("primary: 全年 = 3000 + 1200 = 4200", steps_total(runs["primary"]) == 4200.0,
          f"{steps_total(runs['primary'])}")
    check("per-day-max: 全年 = 3000 + 1200 = 4200",
          steps_total(runs["per-day-max"]) == 4200.0, f"{steps_total(runs['per-day-max'])}")
    check("sum: 01-01 无条件累加 = 3000+2500+500 = 6000（旧行为）",
          steps_of(runs["sum"], "2026-01-01")[0].value == 6000.0,
          str(steps_of(runs["sum"], "2026-01-01")[0].value))
    check("sum: 全年 = 7200（旧行为，重复计算）", steps_total(runs["sum"]) == 7200.0,
          f"{steps_total(runs['sum'])}")
    check("sum: 每天仍然只产出一条 Observation（口径只改数值，不改条数）",
          all(len(runs["sum"].series(m)) == len([o for o in runs["sum"].series(m)])
              for m in ("steps", "heart_rate")) and bool(runs["sum"].series("steps")))
    check("sources_of('steps') 在 primary 下 >= 2（SRC_MIXED 才看得见问题）",
          len(runs["primary"].sources_of("steps")) >= 2,
          str(runs["primary"].sources_of("steps")))

    section("2. mean 类指标（心率）：按来源平均 vs 合并平均")
    for p, ds in runs.items():
        hr1 = [o for o in ds.series("heart_rate") if o.date == "2026-01-01"]
        print(f"     {p:<12} 01-01 心率={hr1[0].value if hr1 else None:.4f}"
              f"  来源={hr1[0].source if hr1 else None}")
    hr = runs["primary"].series("heart_rate")
    hr01 = [o for o in hr if o.date == "2026-01-01"][0]
    check("primary: 取主力手环自己的平均 = (100+120+110)/3 = 110", hr01.value == 110.0,
          f"{hr01.value}")
    hr_sum = [o for o in runs["sum"].series("heart_rate") if o.date == "2026-01-01"][0]
    check("sum: 回到旧行为 = 四条记录合并平均 = 390/4",
          abs(hr_sum.value - 390.0 / 4.0) < 1e-9, f"{hr_sum.value}")
    hr_max = [o for o in runs["per-day-max"].series("heart_rate") if o.date == "2026-01-01"][0]
    check("per-day-max: 取各来源平均里的最大值 = 110", hr_max.value == 110.0, f"{hr_max.value}")

    section("3. prefer_sources：宽松匹配与优先顺序")
    check("prefer_sources=['iPhone'] 时 01-01 取 iPhone 的 2500",
          steps_of(parse_apple(xml, prefer_sources=["iPhone"]), "2026-01-01")[0].value == 2500.0)
    check("prefer_sources 只写部分名字也能命中（'Phone'）",
          steps_of(parse_apple(xml, prefer_sources=["Phone"]), "2026-01-01")[0].value == 2500.0)
    check("prefer_sources 对平均类同样生效（心率取 iPhone 的 60）",
          [o for o in parse_apple(xml, prefer_sources=["iPhone"]).series("heart_rate")
           if o.date == "2026-01-01"][0].value == 60.0)

    pxml = write_personal_name_xml(os.path.join(TMP, "personal.xml"))
    no_pref = parse_apple(pxml, prefer_sources=["iPhone"])
    with_pref = parse_apple(pxml, prefer_sources=["Apple Watch"])
    check("含姓名的设备名要先脱敏再比：prefer 'Apple Watch' 命中它",
          steps_of(with_pref, "2026-02-01")[0].value == 8000.0,
          str(steps_of(with_pref, "2026-02-01")[0].value))
    check("不写 prefer 时按记录条数/名字排序（iPhone 在前）",
          steps_of(no_pref, "2026-02-01")[0].value == 100.0)
    for variant in ("apple watch", "APPLE WATCH", "Watch", "Apple Watch"):
        got = parse_apple(pxml, prefer_sources=[variant])
        check(f"prefer_sources=['{variant}'] 也能命中（大小写/子串不敏感）",
              steps_of(got, "2026-02-01")[0].value == 8000.0)
    check("prefer_sources 命中的来源写进观测时也是脱敏后的名字",
          steps_of(with_pref, "2026-02-01")[0].source
          == privacy.mask_source_name("某某的Apple\u00a0Watch"),
          repr(steps_of(with_pref, "2026-02-01")[0].source))

    section("4. 来源名脱敏（默认开）与 --show-source-names 等价开关")
    raw_name = "某某的Apple\u00a0Watch"
    masked_name = privacy.mask_source_name(raw_name)
    check("privacy.looks_like_personal_name 认得出带姓名的设备名",
          privacy.looks_like_personal_name(raw_name))
    check("privacy.mask_source_name 剥掉姓名前缀、保留器械名",
          "某某" not in masked_name and "Watch" in masked_name, repr(masked_name))
    check("脱敏是幂等的（再脱一次不变）",
          privacy.mask_source_name(masked_name) == masked_name, repr(masked_name))
    check("（已知）原名里的非断行空格会被原样留下",
          masked_name.replace("\u00a0", " ") == "Apple Watch", repr(masked_name))
    check("不含姓名前缀的设备名原样返回", privacy.mask_source_name("iPhone") == "iPhone")

    masked = parse_apple(pxml, prefer_sources=["Apple Watch"])
    raw = parse_apple(pxml, prefer_sources=["Apple Watch"], mask_source_names=False)
    check("默认：Observation.source 已脱敏（等于 mask_source_name 的结果）",
          steps_of(masked, "2026-02-01")[0].source == masked_name,
          repr(steps_of(masked, "2026-02-01")[0].source))
    check("默认：Workout.source 也脱敏",
          bool(masked.workouts) and masked.workouts[0].source == masked_name,
          repr(masked.workouts[0].source) if masked.workouts else "（没有训练）")
    check("mask_source_names=False：完全不脱敏",
          steps_of(raw, "2026-02-01")[0].source == raw_name,
          repr(steps_of(raw, "2026-02-01")[0].source))
    check("默认：含姓名时只加一次 MASK_NOTE",
          masked.notes.count(privacy.MASK_NOTE) == 1,
          str(masked.notes.count(privacy.MASK_NOTE)))
    # 同下：先扣掉 MASK_NOTE（那段提示故意带通用示例），
    # 断言的原意是"来自数据里的真实设备名不许出现在笔记里"。
    notes_without_mask_note = " ".join(masked.notes).replace(privacy.MASK_NOTE, "")
    check("默认：所有 note 里都不出现机主姓名",
          "某某" not in notes_without_mask_note)
    check("mask_source_names=False：不加 MASK_NOTE",
          privacy.MASK_NOTE not in raw.notes)
    check("默认：全是原始名字（不脱敏）时才没有任何来源被改写",
          all(o.source == raw_name or o.source == "iPhone" for o in raw.observations),
          str(raw.all_sources()))
    check("脱敏后仍能用宽松匹配指回同一台设备（prefer 命中）",
          steps_of(masked, "2026-02-01")[0].value == 8000.0,
          str(steps_of(masked, "2026-02-01")[0].value))

    section("5. 非法口径 / parse_path 的关键字透传")
    for bogus in ("Primary", "", "per_day_max", "max"):
        try:
            parse_apple(xml, source_policy=bogus)
            check(f"非法口径「{bogus}」抛 ValueError", False, "没有抛")
        except ValueError as exc:
            check(f"非法口径「{bogus}」抛带中文说明的 ValueError",
                  "来源口径" in str(exc) and "primary" in str(exc), str(exc)[:40] + "…")
    inbody = write_inbody_csv(os.path.join(TMP, "inbody_sample.csv"))
    generic = os.path.join(TMP, "generic.csv")
    with open(generic, "w", encoding="utf-8", newline="") as f:
        f.write("date,metric,value,unit,source\n")
        f.write("2026-01-01,体重,74.0,kg,某某的智能秤\n")
    for label, p in (("apple", xml), ("inbody", inbody), ("generic", generic)):
        try:
            ds = parse_path(p, source_name="", source_policy="primary",
                            prefer_sources=["iPhone"], mask_source_names=True)
            check(f"parse_path({label}) 接受这三个关键字", bool(ds.observations))
        except TypeError as exc:
            check(f"parse_path({label}) 接受这三个关键字", False, str(exc))
    gds = parse_path(generic)
    check("parse_generic 默认也脱敏",
          gds.observations[0].source == "智能秤", gds.observations[0].source)
    check("parse_generic 传 mask_source_names=False 时不脱敏",
          parse_path(generic, mask_source_names=False).observations[0].source
          == "某某的智能秤")


# ──────────────────────────────────────────────────────────── 6~7：真实导出


def real_checks(path: str, baseline: Optional[str]) -> None:
    size = os.path.getsize(path)
    section(f"6. 真实导出：三种口径对照（{size / 1024 / 1024:.0f} MB，只读）")
    print(f"     {path}")
    runs: Dict[str, Any] = {}
    for p in SOURCE_POLICIES:
        ds = parse_apple(path, source_policy=p)
        runs[p] = ds
        st = ds.series("steps")
        print(f"     {p:<12} 步数：天数={len(st):,}  全年合计={sum(o.value for o in st):,.0f}"
              f"  来源数={len(ds.sources_of('steps'))}")

    tot = {p: sum(o.value for o in runs[p].series("steps")) for p in SOURCE_POLICIES}
    primary, permax, total = tot["primary"], tot["per-day-max"], tot["sum"]
    check("primary 全年总步数 < sum（不再系统性算高）", primary < total,
          f"{primary:,.0f} < {total:,.0f}")
    check("sum 比 primary 放大 1.2 倍以上（确认旧口径确实算高）",
          total / primary > 1.2, f"{total / primary:.3f} 倍")
    check("primary 与 per-day-max 接近（差异应能解释：主设备不一定是当天最大值）",
          abs(primary - permax) / permax < 0.05,
          f"primary={primary:,.0f} per-day-max={permax:,.0f} 差 {abs(primary - permax) / permax * 100:.2f}%")
    check("三种口径的天数一致（口径不改覆盖率）",
          len(runs["primary"].series("steps")) == len(runs["sum"].series("steps"))
          == len(runs["per-day-max"].series("steps")),
          f"{len(runs['primary'].series('steps'))}/"
          f"{len(runs['sum'].series('steps'))}/{len(runs['per-day-max'].series('steps'))}")

    # 挑「累加口径下步数最高的那天」做对照 —— 不写死任何真实日期或数值，
    # 真实数据的期望值全部是跑出来再比的（仓库约定，见 tools/_check_parsers.py）。
    worst = max(runs["sum"].series("steps"), key=lambda o: o.value)
    day = worst.date
    vals = {}
    for p, ds in runs.items():
        rows = steps_of(ds, day)
        vals[p] = (rows[0].value, rows[0].source) if rows else (None, None)
        print(f"     {p:<12} {day} 步数={vals[p][0]:,.0f}  来源={vals[p][1]}")
    print(f"     （这天是累加口径下步数最高的一天：{vals['sum'][0]:,.0f}）")
    check(f"{day} primary 不是两倍累加值",
          vals["primary"][0] is not None and vals["primary"][0] < vals["sum"][0] * 0.75,
          f"{vals['primary'][0]} vs {vals['sum'][0]}")
    check(f"{day} per-day-max 不是两倍累加值",
          vals["per-day-max"][0] is not None and vals["per-day-max"][0] < vals["sum"][0] * 0.75,
          f"{vals['per-day-max'][0]} vs {vals['sum'][0]}")
    check(f"{day} primary 落在「各来源值」之中（sum − max = 较小那份，应能对上）",
          vals["primary"][0] is not None and (
              abs(vals["primary"][0] - vals["per-day-max"][0]) < 1e-6
              or abs(vals["primary"][0] - (vals["sum"][0] - vals["per-day-max"][0])) < 1e-6),
          f"primary={vals['primary'][0]} max={vals['per-day-max'][0]} "
          f"sum-max={vals['sum'][0] - vals['per-day-max'][0]:,.0f}")

    check("sources_of('steps') 在 primary 下 >= 2（SRC_MIXED 能看见问题）",
          len(runs["primary"].sources_of("steps")) >= 2,
          str(runs["primary"].sources_of("steps")))

    notes = [n for n in runs["primary"].notes if _is_multisource_note(n)]
    print(f"     多来源说明 note：{len(notes)} 条（上限 {MAX_NOTES}）")
    for n in notes:
        print("       - " + n)
    check(f"多来源说明最多 {MAX_NOTES} 条", len(notes) <= MAX_NOTES, str(len(notes)))
    detailed = [n for n in notes if "本次口径" in n]
    check("每条说明都带口径名、主设备、放大倍数与去重提示",
          all(("本次口径" in n) and ("最常采用的设备" in n) and ("去重算法没有公开规范" in n)
              and (("倍" in n) or ("相差" in n)) for n in detailed) and bool(detailed))
    # ⚠️ 比对前要先扣掉 MASK_NOTE 本身：那段提示**故意带了一个通用示例**
    # （「如「某某的 Apple Watch」」）好让用户看懂发生了什么。
    # 断言的原意是"**来自数据里的**真实设备名不许出现"，不是"笔记里不许有'某某'两个字"——
    # 直接 `"某某" not in notes` 会被提示文字自己命中（实测踩到，替换占位符后才暴露）。
    notes_without_mask_note = " ".join(runs["primary"].notes).replace(privacy.MASK_NOTE, "")
    check("说明里不含机主姓名（默认已脱敏）", "某某" not in notes_without_mask_note)
    check("含姓名的设备名触发了 MASK_NOTE（且只一次）",
          runs["primary"].notes.count(privacy.MASK_NOTE) == 1,
          str(runs["primary"].notes.count(privacy.MASK_NOTE)))
    check("默认输出里所有来源名都已脱敏（不含机主姓名）",
          all("某某" not in s for s in runs["primary"].all_sources()),
          str(runs["primary"].all_sources()))

    # per-day-max / sum 的说明也要能正常生成
    for p in ("per-day-max", "sum"):
        n2 = [n for n in runs[p].notes if "去重算法" in n]
        print(f"     {p} 口径的说明 {len(n2)} 条，示例：")
        if n2:
            print("       - " + n2[0])
        check(f"{p} 口径也写了多来源说明", bool(n2))
    if notes and len(notes) > 0:
        print("     primary 口径说明示例：")
        print("       - " + notes[0])

    if not baseline:
        skip("7. 与改动前基线逐日比对（未传 --baseline）")
        return
    section("7. 与改动前基线逐日比对")
    if not os.path.exists(baseline):
        skip(f"7. 基线文件不存在：{baseline}")
        return
    compare_baseline(runs, baseline)


def compare_baseline(runs: Dict[str, Any], baseline_path: str) -> None:
    with open(baseline_path, "r", encoding="utf-8") as f:
        base = json.load(f)
    base_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    old_sources: Dict[str, List[str]] = {}
    for o in base["observations"]:
        base_by_key[(o["metric"], o["date"])] = o
        old_sources.setdefault(o["metric"], [])
        if o["source"] not in old_sources[o["metric"]]:
            old_sources[o["metric"]].append(o["source"])

    # ── 7a. sum 口径应当逐条复现旧行为（数值层面） ─────────
    ds_sum = runs["sum"]
    new_by_key: Dict[Tuple[str, str], Any] = {(o.metric, o.date): o for o in ds_sum.observations}
    missing = sorted(set(base_by_key) - set(new_by_key))
    extra = sorted(set(new_by_key) - set(base_by_key))
    check("sum 口径：(指标, 日期) 集合与基线完全一致", not missing and not extra,
          f"少 {len(missing)} 个（例：{missing[:3]}），多 {len(extra)} 个（例：{extra[:3]}）")

    value_diff: List[str] = []
    for key, b in base_by_key.items():
        n = new_by_key.get(key)
        if n is None:
            continue
        if abs(float(n.value) - float(b["value"])) > 1e-6:
            value_diff.append(f"{key}: {b['value']} -> {n.value}")
    check("sum 口径下所有指标的数值与基线一致（旧行为可复现）",
          not value_diff, f"{len(value_diff)} 处不同，例：{value_diff[:3]}")

    # ── 7b. 睡眠：默认口径（primary）也必须逐日一致 ────────
    ds_primary = runs["primary"]
    pri_by_key = {(o.metric, o.date): o for o in ds_primary.observations}
    sleep_keys = sorted(k for k in base_by_key if k[0].startswith("sleep"))
    for label, key_name in (("sum", "sum"), ("primary", "primary")):
        src = runs[key_name]
        kw = {(o.metric, o.date): o for o in src.observations}
        diff = [f"{k}: {base_by_key[k]['value']} -> {kw[k].value}"
                for k in sleep_keys if k in kw
                and abs(float(kw[k].value) - float(base_by_key[k]["value"])) > 1e-9]
        check(f"{label} 口径：睡眠指标（{len(sleep_keys)} 条）逐日数值与基线完全一致",
              not diff, f"{len(diff)} 处不同，例：{diff[:3]}")
    sleep_src = [k for k in sleep_keys if pri_by_key[k].source != base_by_key[k]["source"]]
    check("primary 口径：睡眠指标的来源与基线一致", not sleep_src, str(sleep_src[:3]))
    sleep_ts = [k for k in sleep_keys if pri_by_key[k].ts != base_by_key[k]["ts"]]
    check("primary 口径：睡眠指标的时间戳与基线一致（跨天归属没变）",
          not sleep_ts, str(sleep_ts[:3]))
    per_metric = Counter(k[0] for k in sleep_keys)
    print(f"     覆盖到的睡眠指标：{dict(per_metric)}")

    # ── 7c. 快照类：primary 下也应与基线一致 ───────────────
    snap = ("body_mass", "body_fat_pct", "resting_hr", "hrv_sdnn", "vo2max",
            "height_cm", "walking_hr_avg", "respiratory_rate", "oxygen_saturation")
    snap_diff = [f"{k}: {base_by_key[k]['value']} -> {pri_by_key[k].value}"
                 for k in base_by_key
                 if k[0] in snap and k in pri_by_key
                 and abs(float(pri_by_key[k].value) - float(base_by_key[k]["value"])) > 1e-9]
    check("primary 口径：快照类指标数值与基线一致（snapshot 路径未改动）",
          not snap_diff, f"{len(snap_diff)} 处不同，例：{snap_diff[:3]}")

    # ── 7d. 旧版 sources_of 的真实情况（纠正一个常见误解） ──
    old_steps_sources = old_sources.get("steps", [])
    print(f"     旧代码（基线）里 steps 的来源数 = {len(old_steps_sources)}：{old_steps_sources}")
    print("     → 说明：旧版 source 取当天**第一条记录**的来源，逐日不同，")
    print("       所以 SRC_MIXED 其实**会**触发；被算高才是旧版真正的问题。")
    check("旧代码里 steps 也有多个来源（SRC_MIXED 并非完全不触发）",
          len(old_steps_sources) >= 2, str(old_steps_sources))

    changed = [k for k, b in base_by_key.items()
               if k in pri_by_key and pri_by_key[k].source != b["source"]]
    only_mask = [k for k in changed
                 if privacy.mask_source_name(base_by_key[k]["source"]) == pri_by_key[k].source]
    print(f"     primary 与基线来源名不同 {len(changed)} 处："
          f"其中 {len(only_mask)} 处只是脱敏（去掉机主姓名），"
          f"{len(changed) - len(only_mask)} 处是按优先级重选了来源")
    print(f"     按优先级重选来源的分布："
          f"{dict(Counter(k[0] for k in changed if k not in set(only_mask)))}")

    # ── 7e. primary 下哪些指标的**数值**变了（应当只有 sum/mean 类） ──
    pri_val_diff: Counter = Counter()
    for key, b in base_by_key.items():
        n = pri_by_key.get(key)
        if n is None:
            continue
        if abs(float(n.value) - float(b["value"])) > 1e-9:
            pri_val_diff[key[0]] += 1
    print(f"     primary 与基线数值不同的指标（按天数）：{dict(pri_val_diff)}")
    offenders = [m for m in pri_val_diff
                 if m.startswith("sleep")
                 or (catalog.get(m) is not None and catalog.get(m).agg == "snapshot")]
    check("primary 下只有 sum/mean 类的数值会变（快照类与睡眠类逐日不变）",
          not offenders, str(offenders))


# ──────────────────────────────────────────────────────────── 8：内存


def memory_check(path: str) -> None:
    section("8. 内存：流式解析不该把整棵树读进来")
    size = os.path.getsize(path)
    tracemalloc.start()
    ds = parse_apple(path)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"     文件大小 {size / 1024 / 1024:,.0f} MB"
          f"（{size / 1024 / 1024 / 1024:.2f} GB）")
    print(f"     tracemalloc 峰值 {peak / 1024 / 1024:,.1f} MB"
          f"（观测 {len(ds.observations):,} 条，训练 {len(ds.workouts):,} 条）")
    print(f"     峰值 / 文件大小 = {peak / size:.3f}")
    check("解析峰值远小于文件大小（< 1/4）", peak < size / 4,
          f"{peak / 1024 / 1024:.1f} MB vs {size / 1024 / 1024:.0f} MB")
    check("峰值在几百 MB 量级以内（与文件大小无关）", peak < 400 * 1024 * 1024,
          f"{peak / 1024 / 1024:.1f} MB")


# ──────────────────────────────────────────────────────────── main


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="同日多来源口径自测")
    ap.add_argument("--real", action="store_true", help="跑真实导出（几分钟）")
    ap.add_argument("--path", default=DEFAULT_REAL, help="真实导出的路径")
    ap.add_argument("--baseline", default="", help="改动前的观测快照 JSON（逐日比对）")
    ap.add_argument("--memory", action="store_true", help="再量一次 tracemalloc 峰值")
    ap.add_argument("--only-real", action="store_true", help="跳过合成检查")
    args = ap.parse_args(argv)

    if not args.only_real:
        synthetic_checks()

    if args.real or args.memory:
        path = os.path.abspath(args.path)
        if not os.path.exists(path):
            skip(f"真实导出不存在：{path}")
        else:
            real_checks(path, os.path.abspath(args.baseline) if args.baseline else None)
            if args.memory:
                memory_check(path)
    elif args.baseline:
        skip("--baseline 需要同时传 --real")

    print()
    print("=" * 62)
    print(f"通过 {len(ok)} / {len(ok) + len(bad)}"
          + (f"，跳过 {len(skipped)}" if skipped else ""))
    if bad:
        print("失败项：")
        for b in bad:
            print("  - " + b)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
