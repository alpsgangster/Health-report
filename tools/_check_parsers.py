#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析器自测脚本（不是 pytest 测试，是可以在无 pytest 环境下直接跑的回归清单）。

用法：
    cd 06_HealthReport
    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    python tools/_check_parsers.py

退出码 0 表示全部通过；非零表示有失败项，失败项会列在末尾。

────────────────────────────────────────────────────────────────────────────
隐私说明（重要，见 docs/RELEASE_CHECKLIST.md 第 2 条）
────────────────────────────────────────────────────────────────────────────
**本文件不含任何真实个人数值。** 需要拿真实样本比对时，脚本是**从样本文件里读出来**
再和解析结果比的（见 `load_inbody_expectations` / `load_apple_expectations`），
所以字面量里不会出现身高 / 体重 / 出生日期这类能定位到个人的数字；
本脚本可以随仓库一同发布，不需要在推送前改写。
（`tools/_check_analysis.py` 是另一回事：它把真实报告里的一组数值写进了文件头，
需要按 RELEASE_CHECKLIST 单独处理。）

────────────────────────────────────────────────────────────────────────────
覆盖范围
────────────────────────────────────────────────────────────────────────────
1. `detect_kind()` 对 zip / 裸 xml / 导出目录（多种文件名布局）/ InBody / 通用表的识别；
2. `parse_generic()`：逗号·制表符·分号三种分隔符、UTF-8 与 GBK 两种编码、
   宽松日期（含 `YYYY年M月D日`）、单位换算与缺失、未知指标、跳过行计数；
3. 错误路径：缺列、不支持的后缀、文件不存在、InBody 文件没有日期列；
4. `units.convert()` / `units.looks_like_ratio_value()` 的单位口径；
5. `parse_reference_text()`：`A~B`、`A-B`、`A至B`、`<10`、`>=7`、多区间、`—`；
6. `parse_inbody()`：profile 的 height_cm/age/sex、判定行进 labels、参考区间挂载、
   身高既进 profile 又产生观测、行名解析不出来时用「分类」列兜底；
7. `parse_apple()`：聚合口径（sum/mean/snapshot）、睡眠阶段与跨天归属、
   Workout 的时长/距离/心率/配速、年龄推算、单位特例（m→cm、m→km、lb→kg、count→步/层）；
8. 接口约束：`Observation.unit == catalog.get(metric).unit`、`Dataset.files` 只放 basename。
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import sys
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from health_report import catalog, units  # noqa: E402
from health_report.parsers import (  # noqa: E402
    detect_kind,
    parse_apple,
    parse_generic,
    parse_inbody,
    parse_path,
)
from health_report.parsers._detect import parse_date_lenient  # noqa: E402
from health_report.parsers.inbody import parse_reference_text  # noqa: E402

# 真实样本（相对仓库根）。缺了就 SKIP，不当失败。
INBODY = os.path.join(_ROOT, "..", "01_InBody体测", "InBody数据对比.csv")
XML = os.path.join(_ROOT, "..", "02_Apple健康数据", "样例_export.xml")

TMP = tempfile.mkdtemp(prefix="hr_check_")
ok: List[str] = []
bad: List[str] = []
skipped: List[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    (ok if cond else bad).append(label)
    print(("  [OK] " if cond else "  [!!] ") + label + (f"  {detail}" if detail else ""))


def skip(label: str) -> None:
    skipped.append(label)
    print("  [--] SKIP " + label)


def section(title: str) -> None:
    print()
    print(f"=== {title} ===")


def _find_col(header: List[str], name: str) -> Optional[int]:
    for i, h in enumerate(header):
        if (h or "").strip() == name:
            return i
    return None


def load_inbody_expectations(path: str) -> Dict[str, Any]:
    """从真实 InBody CSV 里读出**期望值**，脚本自身不落地任何个人数值。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.reader(f) if r and any((c or "").strip() for c in r)]
    header, data = rows[0], rows[1:]
    name_col = _find_col(header, "指标")
    unit_col = _find_col(header, "单位")
    ref_col = _find_col(header, "报告参考范围")
    note_col = _find_col(header, "备注")
    assert name_col is not None and unit_col is not None

    date_cols: List[Tuple[int, str]] = []
    for i, h in enumerate(header):
        dt = parse_date_lenient(h)
        if dt is not None:
            date_cols.append((i, dt.strftime("%Y-%m-%d")))
    assert len(date_cols) == 2, f"样本应当有 2 个日期列，实际 {len(date_cols)}"

    exp: Dict[str, Any] = {"dates": [d for _, d in date_cols], "ref": {}, "profile": {},
                           "labels": [], "values": {}, "notes": []}
    for row in data:
        name = (row[name_col] if name_col < len(row) else "").strip()
        if not name:
            continue
        if ref_col is not None:
            ref_text = (row[ref_col] if ref_col < len(row) else "").strip()
            if ref_text not in ("—", "-", ""):
                exp["ref"].setdefault(name, ref_text)
        if name.endswith("判定"):
            for col, _ in date_cols:
                v = (row[col] if col < len(row) else "").strip()
                if v and v != "—":
                    exp["labels"].append(v)
            continue
        if name in ("身高", "年龄", "性别"):
            exp["profile"][name] = [(row[col] if col < len(row) else "").strip() for col, _ in date_cols]
            continue
        exp["values"][name] = [(row[col] if col < len(row) else "").strip() for col, _ in date_cols]
        if note_col is not None:
            n = (row[note_col] if note_col < len(row) else "").strip()
            if n and n != "—":
                exp["notes"].append((name, n))
    return exp


def load_apple_expectations(path: str) -> Dict[str, Any]:
    """从真实 Apple XML 里读出期望值（粗扫，只取少数几个用于比对的量）。"""
    src = open(path, "r", encoding="utf-8").read()
    exp: Dict[str, Any] = {}
    m = re.search(r'HKCharacteristicTypeIdentifierDateOfBirth="([^"]+)"', src)
    if m:
        exp["birth_date"] = m.group(1)[:10]
    m = re.search(r'HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSex(\w+)"', src)
    if m:
        exp["sex"] = {"Male": "男", "Female": "女", "Other": ""}.get(m.group(1), "")
    steps = [float(x) for x in
             re.findall(r'HKQuantityTypeIdentifierStepCount"[^>]*value="([\d.]+)"', src)]
    exp["steps_total"] = sum(steps)
    masses = re.findall(
        r'HKQuantityTypeIdentifierBodyMass" sourceName="([^"]*)"[^>]*startDate="([^"]+)"'
        r'[^>]*value="([\d.]+)"', src)
    if masses:
        last_day = max(t[1] for t in masses)[:10]
        same_day = [t for t in masses if t[1][:10] == last_day]
        exp["mass_date"] = last_day
        exp["mass_value"] = float(same_day[-1][2])
        exp["mass_source"] = same_day[-1][0]
    exp["n_workouts"] = len(re.findall(r"<Workout ", src))
    return exp


# ──────────────────────────────────────────────────────────── 1. 识别

section("1. detect_kind")
if os.path.exists(XML):
    check("xml -> apple", detect_kind(XML) == "apple", detect_kind(XML))
else:
    skip("xml -> apple")
if os.path.exists(INBODY):
    check("csv(inbody) -> inbody", detect_kind(INBODY) == "inbody", detect_kind(INBODY))
else:
    skip("csv(inbody) -> inbody")

if os.path.exists(XML):
    zp = os.path.join(TMP, "export.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.write(XML, "apple_health_export/export.xml")
        z.write(XML, "apple_health_export/cda/export_cda.xml")
    check("zip -> apple", detect_kind(zp) == "apple")
    check("zip 解析出观测", len(parse_apple(zp).observations) > 0)

    d1 = os.path.join(TMP, "d1")
    os.makedirs(os.path.join(d1, "apple_health_export"), exist_ok=True)
    shutil.copy(XML, os.path.join(d1, "apple_health_export", "export.xml"))
    check("目录(apple_health_export/export.xml) -> apple", detect_kind(d1) == "apple")
    check("目录解析出观测", len(parse_apple(d1).observations) > 0)

    d2 = os.path.join(TMP, "d2")
    os.makedirs(d2, exist_ok=True)
    shutil.copy(XML, os.path.join(d2, "导出.xml"))
    check("目录(导出.xml) -> apple", detect_kind(d2) == "apple")

    d3 = os.path.join(TMP, "d3")
    os.makedirs(d3, exist_ok=True)
    shutil.copy(XML, os.path.join(d3, "export_alt.xml"))
    shutil.copy(XML, os.path.join(d3, "export_small.xml"))
    check("目录(export*.xml) -> apple", detect_kind(d3) == "apple")
else:
    for _lbl in ("zip -> apple", "目录 -> apple"):
        skip(_lbl)

# ──────────────────────────────────────────────────────────── 2. 通用表（合成数据）

section("2. parse_generic（合成数据）")
g1 = os.path.join(TMP, "g1.csv")
with open(g1, "w", encoding="utf-8") as f:
    f.write("日期,指标,数值,单位,来源\n")
    f.write("2026-03-01,体重,80.0,kg,家用秤\n")
    f.write("2026/03/02,体重,176.3698,lb,家用秤\n")
    f.write("2026年3月3日,体脂率,20.0,%,家用秤\n")
    f.write("2026-03-04 07:30:00,心率,60,次/分,手环\n")
    f.write("2026-03-04 08:30:00,心率,80,次/分,手环\n")
    f.write("2026-03-05,外星指标,1,kg,家用秤\n")
    f.write("不是日期,体重,1,kg,x\n")
    f.write("2026-03-06,体重,79.0,,\n")
check("generic 识别", detect_kind(g1) == "generic", detect_kind(g1))
gds = parse_generic(g1)
check("generic 体重 3 天", len(gds.series("body_mass")) == 3,
      str([(o.date, o.value) for o in gds.series("body_mass")]))
check("generic lb -> kg 换算", abs(gds.series("body_mass")[1].value - 80.0) < 1e-4,
      str(gds.series("body_mass")[1].value))
check("generic 中文日期 2026年3月3日", any(o.date == "2026-03-03" for o in gds.series("body_fat_pct")))
check("generic 心率取当日平均 70", gds.latest("heart_rate").value == 70.0,
      str(gds.latest("heart_rate").value))
check("generic 未知指标进 notes", any("外星指标" in n for n in gds.notes))
check("generic 跳过行计数", any("跳过" in n for n in gds.notes))
check("generic 来源列生效", gds.series("body_mass")[0].source == "家用秤",
      gds.series("body_mass")[0].source)
check("generic 缺单位记 note", any("单位缺失" in n for n in gds.notes))

g2 = os.path.join(TMP, "g2.tsv")
with open(g2, "w", encoding="utf-8") as f:
    f.write("date\tmetric\tvalue\tunit\n2026-03-01\t步数\t9000\t步\n")
check("tsv -> generic（不被 InBody 抢走）", detect_kind(g2) == "generic", detect_kind(g2))
check("tsv 解析", parse_generic(g2).latest("steps") is not None
      and parse_generic(g2).latest("steps").value == 9000.0)

g3 = os.path.join(TMP, "g3.csv")
with open(g3, "w", encoding="utf-8") as f:
    f.write("date;metric;value\n2026-03-01;体重;70\n")
check("分号分隔", parse_generic(g3).latest("body_mass").value == 70.0)

g4 = os.path.join(TMP, "g4.csv")
with open(g4, "w", encoding="gbk") as f:
    f.write("日期,指标,数值,单位\n2026-03-01,体重,80.0,kg\n")
check("GBK 编码可读", parse_generic(g4).latest("body_mass").value == 80.0)

g5 = os.path.join(TMP, "g5.csv")
with open(g5, "w", encoding="utf-8") as f:
    f.write("date,metric,value\n2026-03-01,体脂率,0.185\n")
check("无单位 0~1 体脂率 -> 18.5", parse_generic(g5).latest("body_fat_pct").value == 18.5,
      str(parse_generic(g5).latest("body_fat_pct").value))
g6 = os.path.join(TMP, "g6.csv")
with open(g6, "w", encoding="utf-8") as f:
    f.write("date,metric,value,unit\n2026-03-01,体脂率,22.5,percent\n")
check("percent 写成百分数时不再乘 100", parse_generic(g6).latest("body_fat_pct").value == 22.5,
      str(parse_generic(g6).latest("body_fat_pct").value))

# ──────────────────────────────────────────────────────────── 3. 错误路径

section("3. 错误路径")
gpoor = os.path.join(TMP, "poor.csv")
with open(gpoor, "w", encoding="utf-8") as f:
    f.write("a,b,c\n1,2,3\n")
try:
    parse_generic(gpoor)
    check("缺列抛 ValueError", False)
except ValueError as e:
    check("缺列抛 ValueError 且列出实际表头", "实际读到的表头" in str(e))
    print("   ->", str(e).replace("\n", " | "))

try:
    detect_kind(os.path.join(TMP, "nope.xlsx"))
    check("不存在先抛 FileNotFoundError", False)
except FileNotFoundError:
    check("不存在先抛 FileNotFoundError", True)
with open(os.path.join(TMP, "nope.xlsx"), "w", encoding="utf-8") as f:
    f.write("x")
try:
    detect_kind(os.path.join(TMP, "nope.xlsx"))
    check("不支持后缀抛 ValueError 且列出支持输入", False)
except ValueError as e:
    check("不支持后缀抛 ValueError 且列出支持输入", "支持的输入" in str(e))

gnodate = os.path.join(TMP, "nodate.csv")
with open(gnodate, "w", encoding="utf-8") as f:
    f.write("分类,指标,单位,变化量,报告参考范围,备注\n人体成分,体重,kg,0,—,—\n")
try:
    parse_inbody(gnodate)
    check("InBody 无日期列抛 ValueError", False)
except ValueError as e:
    check("InBody 无日期列抛 ValueError", "没找到日期列" in str(e))

try:
    parse_apple(os.path.join(TMP, "nope.zip"))
    check("parse_apple 缺文件抛 FileNotFoundError", False)
except FileNotFoundError:
    check("parse_apple 缺文件抛 FileNotFoundError", True)

# ──────────────────────────────────────────────────────────── 4. 单位口径

section("4. units.convert / looks_like_ratio_value")
for v, fu, tu, want in [
    (58, "count/min", "次/分", 58.0),
    (150, "count/min", "次/分", 150.0),
    (4600, "count", "步", 4600),
    (9, "count", "层", 9),
    (12, "counts", "步", 12),
    (12, "count", "分", 12),
    (9000, "steps", "步", 9000),
    (40.0, "mL/min·kg", "ml/kg/min", 40.0),
    (40.0, "ml/min/kg", "ml/kg/min", 40.0),
    # 176.3698 lb 只近似等于 80 kg（换算因子不是有限小数），用相对容差比
    (176.3698, "lb", "kg", 80.0),
    # 刻意取 1.72 m（不是样本里的身高值），避免脚本里出现真实个人数值
    (1.72, "m", "cm", 172.0),
    (900, "m", "km", 0.9),
    (1, "furlong", "kg", None),
    (1, "count", "kg", None),
    (1, "kg", "count", None),
    (1, "furlong", "步", None),
]:
    got = units.convert(v, fu, tu)
    if want is None:
        good = got is None
    else:
        # 相对容差 1e-6：lb -> kg 的因子不是有限小数，且用例里的 lb 值本身是四舍五入过的
        good = got is not None and abs(got - want) <= 1e-6 * max(1.0, abs(want))
    check(f"convert({v}, {fu!r}, {tu!r}) ~= {want}", good, f"got={got}")

for u, want in [("", True), ("%", True), ("％", True), ("percent", True), ("ratio", True),
                ("kg", False), ("count/min", False), ("次/分", False)]:
    check(f"looks_like_ratio_value({u!r}) = {want}", units.looks_like_ratio_value(u) is want)
check("旧名字 looks_like_ratio_unit 已移除（不留兼容别名）",
      not hasattr(units, "looks_like_ratio_unit"))

# ──────────────────────────────────────────────────────────── 5. 参考区间

section("5. parse_reference_text")
for text, expect in [
    ("35.5~43.4", (35.5, 43.4)),
    ("0.80~0.90", (0.80, 0.90)),
    ("1518~1774 / 1484~1731", (1518.0, 1774.0)),
    ("7.3-14.6", (7.3, 14.6)),
    ("18.5至24.9", (18.5, 24.9)),
    ("90～110", (90.0, 110.0)),
    ("<10", (None, 10.0)),
    ("<=10", (None, 10.0)),
    (">7", (7.0, None)),
    (">=7", (7.0, None)),
]:
    r, _ = parse_reference_text(text, "测试")
    got = (r.low, r.high) if r else (None, None)
    check(f"参考区间 {text!r} -> {expect}", got == expect, f"got={got}")
for text in ("—", "-", "", "无"):
    r, _ = parse_reference_text(text, "测试")
    check(f"空参考区间 {text!r} -> None", r is None)
r, _ = parse_reference_text("<10", "内脏脂肪等级")
check("<10 -> high_inclusive=False",
      r is not None and r.high == 10.0 and r.high_inclusive is False)
r, _ = parse_reference_text(">=7", "x")
check(">=7 -> low_inclusive=True", r is not None and r.low == 7.0 and r.low_inclusive is True)
r, _ = parse_reference_text(">7", "x")
check(">7 -> low_inclusive=False", r is not None and r.low_inclusive is False)
r, _ = parse_reference_text("abc", "x")
check("无法解析：保留原文且不给数字区间",
      r is not None and r.low is None and r.high is None and r.text == "abc")

# ──────────────────────────────────────────────────────────── 6. InBody（真实样本）

section("6. parse_inbody（真实样本，期望值从文件读出，不写死在脚本里）")
if os.path.exists(INBODY):
    ide = load_inbody_expectations(INBODY)
    ids = parse_inbody(INBODY)

    hw = ide["profile"].get("身高", [""])[0]
    aw = ide["profile"].get("年龄", [""])[0]
    sw = ide["profile"].get("性别", [""])[0]
    check("身高进 profile", ids.profile.get("height_cm") == float(hw),
          str(ids.profile.get("height_cm")))
    check("年龄进 profile", ids.profile.get("age") == int(float(aw)), str(ids.profile.get("age")))
    check("性别进 profile", ids.profile.get("sex") == sw, str(ids.profile.get("sex")))

    mass = ids.series("body_mass")
    exp_mass = [float(v) for v in ide["values"].get("体重", []) if v not in ("", "—")]
    check("body_mass 两个日期都在", [o.date for o in mass] == ide["dates"],
          str([o.date for o in mass]))
    check("body_mass 数值与文件一致", [o.value for o in mass] == exp_mass,
          str([o.value for o in mass]))
    check("body_mass 参考区间挂上且文字与文件一致",
          all(o.ref is not None and o.ref.label() == ide["ref"].get("体重") for o in mass),
          str([o.ref.label() if o.ref else None for o in mass]))

    check("判定行不产生观测", not any("judgement" in o.metric for o in ids.observations))
    check("labels 条数与文件里判定单元格数一致", len(ids.labels) == len(ide["labels"]),
          f"{len(ids.labels)} vs {len(ide['labels'])}")
    check("labels 值都来自文件", all(v in ide["labels"] for v in ids.labels.values()))
    check("多区间记 note", any("多个参考区间" in n for n in ids.notes))
    check("身高既进 profile 又产生观测", len(ids.series("height_cm")) == len(ide["dates"]))
    check("参考区间合法：low <= high", all(
        (o.ref is None) or (o.ref.low is None) or (o.ref.high is None) or (o.ref.low <= o.ref.high)
        for o in ids.observations))
    check("没给参考范围的行 ref 为 None", all(o.ref is None for o in ids.series("inbody_score")))
    check("所有挂上的参考区间文字都来自文件",
          all((o.ref is None) or (o.ref.text in ("", *ide["ref"].values()))
              for o in ids.observations))
    check("没有无法识别的指标行（分类列兜底生效）",
          not any("没能对应" in n for n in ids.notes),
          str([n for n in ids.notes if "没能对应" in n]))

    # 「分类」列兜底：真实报告里分类=内脏脂肪等级、行名只写「等级」
    vfl = ids.series("visceral_fat_level")
    check("「等级」行靠分类列兜底解析成 visceral_fat_level",
          len(vfl) == len(ide["dates"]), str([(o.date, o.value) for o in vfl]))
    check("内脏脂肪等级区间是 <10",
          bool(vfl) and all(o.ref is not None and o.ref.label() == "<10" for o in vfl),
          str([o.ref.label() if o.ref else None for o in vfl]))
    check("内脏脂肪等级数值与文件一致",
          [o.value for o in vfl] == [float(v) for v in ide["values"].get("等级", []) if v],
          str([o.value for o in vfl]))
    check("分类列不影响正常行（体重仍按行名解析）", len(mass) == len(ide["dates"]))
else:
    skip("parse_inbody 全部检查（缺样本）")

# ──────────────────────────────────────────────────────────── 7. Apple（真实样本）

section("7. parse_apple（真实样本，期望值从文件读出）")
if os.path.exists(XML):
    aex = load_apple_expectations(XML)
    ads = parse_apple(XML)
    check("profile 的 birth_date 来自 Me 元素",
          ads.profile.get("birth_date") == aex["birth_date"], str(ads.profile))
    check("profile 的 sex 中文映射正确", ads.profile.get("sex") == aex["sex"], str(ads.profile))
    check("年龄已推算且非负",
          isinstance(ads.profile.get("age"), int) and ads.profile["age"] >= 0,
          str(ads.profile.get("age")))
    check("步数按日累加（= 文件里各段之和）",
          abs(ads.latest("steps").value - aex["steps_total"]) < 1e-6,
          f"{ads.latest('steps').value} vs {aex['steps_total']}")
    bm = ads.latest("body_mass")
    check("body_mass 取当日最晚一条（数值与来源都对）",
          bm.value == aex["mass_value"] and bm.source == aex["mass_source"],
          f"{bm.value}/{bm.source} vs {aex['mass_value']}/{aex['mass_source']}")
    check("workouts 条数与文件一致", len(ads.workouts) == aex["n_workouts"],
          f"{len(ads.workouts)} vs {aex['n_workouts']}")
    run = [w for w in ads.workouts if w.activity == "跑步"]
    check("运动类型中文化（有跑步）", bool(run))
    if run:
        w = run[0]
        check("跑步的时长/距离/心率/配速都算了",
              bool(w.duration_min and w.distance_km and w.avg_hr and w.max_hr and w.pace_sec_per_km),
              f"dur={w.duration_min} dist={w.distance_km} avg={w.avg_hr} pace={w.pace_sec_per_km}")
        check("配速 = 时长秒 / 距离km",
              abs(w.pace_sec_per_km - (w.duration_min * 60.0) / w.distance_km) < 1e-6)
    check("总览 note 在", any("Apple 健康数据总览" in n for n in ads.notes))
    check("睡眠出现且为小时量级",
          bool(ads.series("sleep_total")) and all(0 < o.value < 14 for o in ads.series("sleep_total")),
          str([o.value for o in ads.series("sleep_total")]))
    check("心率是合理量级（不是把 count/min 当别的单位）",
          bool(ads.series("heart_rate")) and all(30 <= o.value <= 220 for o in ads.series("heart_rate")),
          str([o.value for o in ads.series("heart_rate")]))
    check("一天多条记录已聚合成一条", all(
        len([1 for o in ads.observations if o.metric == m and o.date == d]) == 1
        for m, d in {(o.metric, o.date) for o in ads.observations}))
    check("count/min 不再触发单位无法换算的告警",
          not any("无法换算" in n for n in ads.notes),
          str([n for n in ads.notes if "无法换算" in n]))
else:
    skip("parse_apple 样例检查（缺样本）")

# ──────────────────────────────────────────────────────────── 8. 合成 Apple 特例

section("8. 睡眠阶段映射 / ActivitySummary / 单位特例（合成数据）")
slp = os.path.join(TMP, "sleep.xml")
with open(slp, "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData>\n')
    f.write(' <Me HKCharacteristicTypeIdentifierDateOfBirth="1990-01-01"/>\n')
    for val, s, e in [
        ("HKCategoryValueSleepAnalysisInBed", "2026-03-01 23:00:00", "2026-03-02 07:00:00"),
        ("HKCategoryValueSleepAnalysisAwake", "2026-03-02 03:00:00", "2026-03-02 03:30:00"),
        ("HKCategoryValueSleepAnalysisAsleepCore", "2026-03-02 03:30:00", "2026-03-02 05:30:00"),
        ("HKCategoryValueSleepAnalysisAsleepDeep", "2026-03-02 05:30:00", "2026-03-02 06:30:00"),
        ("HKCategoryValueSleepAnalysisAsleepREM", "2026-03-02 06:30:00", "2026-03-02 07:00:00"),
        ("HKCategoryValueSleepAnalysisAsleepUnspecified", "2026-03-02 02:00:00", "2026-03-02 03:00:00"),
    ]:
        f.write(f' <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple Watch" '
                f'startDate="{s} +0800" endDate="{e} +0800" value="{val}"/>\n')
    f.write('</HealthData>\n')
sds = parse_apple(slp)
got = {o.metric: o.value for o in sds.observations if o.metric.startswith("sleep")}
check("sleep_total = core2+deep1+rem0.5+未分期1 = 4.5", got.get("sleep_total") == 4.5, str(got))
check("sleep_core = 2.0", got.get("sleep_core") == 2.0, str(got))
check("sleep_deep = 1.0", got.get("sleep_deep") == 1.0, str(got))
check("sleep_rem = 0.5", got.get("sleep_rem") == 0.5, str(got))
check("sleep_awake = 0.5（不计入 total）", got.get("sleep_awake") == 0.5, str(got))
check("sleep_inbed = 8.0（不计入 total）", got.get("sleep_inbed") == 8.0, str(got))
check("睡眠全部归到醒来那天 2026-03-02",
      all(o.date == "2026-03-02" for o in sds.observations), str([o.date for o in sds.observations]))

asx = os.path.join(TMP, "as.xml")
with open(asx, "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData>\n')
    f.write(' <Me HKCharacteristicTypeIdentifierDateOfBirth="1990-01-01" '
            'HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexMale"/>\n')
    f.write(' <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" '
            'startDate="2026-03-01 08:00:00 +0800" endDate="2026-03-01 09:00:00 +0800" value="1200"/>\n')
    f.write(' <ActivitySummary dateComponents="2026-03-01" activeEnergyBurned="500" '
            'activeEnergyBurnedUnit="kcal" appleExerciseTime="45" appleExerciseTimeUnit="min" '
            'appleStandHours="12" appleStandHoursUnit="count"/>\n')
    f.write(' <ActivitySummary dateComponents="2026-03-02" activeEnergyBurned="300" '
            'activeEnergyBurnedUnit="kcal" appleExerciseTime="20" appleExerciseTimeUnit="min" '
            'appleStandHours="10" appleStandHoursUnit="count"/>\n')
    f.write('</HealthData>\n')
asds = parse_apple(asx)
check("ActivitySummary 补 active_energy 两天", len(asds.series("active_energy")) == 2,
      str([(o.date, o.value) for o in asds.series("active_energy")]))
check("ActivitySummary 不覆盖当天已有的 steps",
      len(asds.series("steps")) == 1 and asds.latest("steps").value == 1200.0)
check("ActivitySummary 补齐有 note", any("活动汇总" in n for n in asds.notes))
check("appleStandHours(count) -> stand_minutes 720/600",
      [o.value for o in asds.series("stand_minutes")] == [720.0, 600.0],
      str([o.value for o in asds.series("stand_minutes")]))
check("StepCount 的 count 单位被 convert 放行（步数没触发单位告警）",
      not any("步数" in n and "无法换算" in n for n in asds.notes))

hb = os.path.join(TMP, "hb.xml")
with open(hb, "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData>\n')
    f.write(' <Me HKCharacteristicTypeIdentifierDateOfBirth="1990-06-15" '
            'HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexOther"/>\n')
    for t, u, v in [
        ("HKQuantityTypeIdentifierHeight", "m", "1.72"),
        ("HKQuantityTypeIdentifierDistanceWalkingRunning", "m", "900"),
        ("HKQuantityTypeIdentifierBodyMass", "lb", "176.3698"),
        ("HKQuantityTypeIdentifierBodyFatPercentage", "%", "0.162"),
        ("HKQuantityTypeIdentifierFlightsClimbed", "count", "9"),
        ("HKQuantityTypeIdentifierWeirdThing", "zzz", "1"),
    ]:
        f.write(f' <Record type="{t}" sourceName="x" unit="{u}" startDate="2026-03-01 10:00:00 +0800" '
                f'endDate="2026-03-01 10:00:00 +0800" value="{v}"/>\n')
    f.write('</HealthData>\n')
hds = parse_apple(hb)
check("身高 m -> cm", hds.latest("height_cm").value == 172.0, str(hds.latest("height_cm").value))
check("距离 m -> km", hds.latest("distance_walking_running").value == 0.9)
check("体重 lb -> kg", abs(hds.latest("body_mass").value - 80.0) < 1e-4,
      str(hds.latest("body_mass").value))
check("体脂率 0.162 -> 16.2", hds.latest("body_fat_pct").value == 16.2)
check("爬楼层数 count -> 层（9）", hds.latest("flights_climbed").value == 9.0,
      str(hds.latest("flights_climbed").value))
check("HKBiologicalSexOther -> 空串", hds.profile.get("sex") == "", repr(hds.profile.get("sex")))
check("未知 HK 类型去重列进 notes",
      any("WeirdThing" in n and "不认识的 HK 类型" in n for n in hds.notes))
check("年龄按最新日期推算（1990-06-15 -> 2026-03-01 = 35）", hds.profile.get("age") == 35,
      str(hds.profile))
check("时间戳丢掉时区偏移", hds.latest("height_cm").ts == "2026-03-01 10:00:00",
      hds.latest("height_cm").ts)
check("心率类 count/min 不再触发单位告警（本文件没有心率，顺带确认无误报）",
      not any("无法换算" in n for n in hds.notes), str(hds.notes))

# ──────────────────────────────────────────────────────────── 9. 接口约束

section("9. 接口约束（model.py / catalog.py 的约定）")
all_sets: List[Tuple[str, Any]] = [("generic", parse_generic(g1)), ("合成 apple", parse_apple(hb))]
if os.path.exists(INBODY):
    all_sets.append(("inbody", parse_inbody(INBODY)))
if os.path.exists(XML):
    all_sets.append(("apple 样例", parse_apple(XML)))
for nm, dsx in all_sets:
    bad_units = [
        (o.metric, o.unit, catalog.get(o.metric).unit)  # type: ignore[union-attr]
        for o in dsx.observations
        if catalog.get(o.metric) is not None and o.unit != catalog.get(o.metric).unit  # type: ignore[union-attr]
    ]
    check(f"{nm}: Observation.unit == catalog 的 spec.unit", not bad_units, str(bad_units[:5]))
    bad_files = [f for f in dsx.files if os.sep in f or (os.altsep and os.altsep in f)]
    check(f"{nm}: Dataset.files 只放 basename", not bad_files, str(dsx.files))
    unknown_metrics = [o.metric for o in dsx.observations if catalog.get(o.metric) is None]
    check(f"{nm}: 所有 metric 都在 catalog 里", not unknown_metrics, str(set(unknown_metrics)))

check("parsers 模块 re-export 了五个名字",
      all(hasattr(sys.modules["health_report.parsers"], n)
          for n in ("parse_apple", "parse_inbody", "parse_generic", "parse_path", "detect_kind")))
if os.path.exists(INBODY) and os.path.exists(XML):
    check("parse_path 与直接调用结果一致",
          len(parse_path(INBODY).observations) == len(parse_inbody(INBODY).observations)
          and len(parse_path(XML).observations) == len(parse_apple(XML).observations))

# ──────────────────────────────────────────────────────────── 收尾

print()
print("=" * 62)
print(f"通过 {len(ok)} / {len(ok) + len(bad)}" + (f"，跳过 {len(skipped)}" if skipped else ""))
if bad:
    print("失败项：")
    for b in bad:
        print("  -", b)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
