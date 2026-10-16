# -*- coding: utf-8 -*-
"""report.py / llm.py 的自检脚本（临时工具，可留可删）。

⚠️ 本脚本**只用合成数值**：日期、画像与来源名直接取自 `samples.py` 那套虚构数据，
体测数值是同一量级另编的。这里不允许出现任何真实体测读数，`verify_privacy()`
会在每次运行时守住这条底线（见该函数的注释）。

用法：
    cd 06_HealthReport
    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    python tools\\_check_report.py

它做这些事：
1. 空数据也能出一份带免责声明的完整报告；
2. 手工构造 11 个 MetricTrend 与 3 个 Finding，跑 build_report，
   把 Markdown 存成 tools/_check_report_sample.md，并逐条检查关键措辞：
   免责声明顺序、判定措辞区分 range/goal/screen、跨来源指标的显眼提示、
   「只做趋势」小节、第五节的确定性文字、第六节的开启说明；
3. build_facts 可以 json.dumps，且里面没有逐条原始观测；
4. LLMConfig.from_env / describe / narrate 的行为（含各类失败静默降级）；
5. build_messages 里确实包含 8 条安全约束；
6. 附加：隐私自检 —— 本文件与两个产出的 .md 里只允许出现合成日期，
   且不允许出现受测者 ID 那种形状的长数字。

analysis.py / pitfalls.py 由同事并行开发：本脚本在它们不存在时注入接口等价的桩模块，
只用于验证报告层，不会写入 src/。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
import types
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import timedelta as _timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import health_report  # noqa: E402
from health_report.model import Dataset, Observation, RefRange, Workout  # noqa: E402

SCRIPT_PATH = Path(__file__).resolve()
SAMPLE_PATH = ROOT / "tools" / "_check_report_sample.md"
EMPTY_PATH = ROOT / "tools" / "_check_report_empty.md"


# ────────────────────────────────────────────────── 1. 同事模块的桩

_STUBBED: List[str] = []


def _install_stubs() -> None:
    """analysis.py / pitfalls.py 缺失时，注入与约定接口等价的桩模块。"""
    try:
        importlib.import_module("health_report.analysis")
    except Exception:
        _STUBBED.append("analysis")

        @dataclass
        class MetricTrend:  # type: ignore[no-redef]
            metric: str = ""
            cn: str = ""
            unit: str = ""
            category: str = ""
            agg: str = "snapshot"
            n: int = 0
            days: int = 0
            first: Optional[Observation] = None
            last: Optional[Observation] = None
            delta: Optional[float] = None
            pct: Optional[float] = None
            per_30d: Optional[float] = None
            mean: Optional[float] = None
            median: Optional[float] = None
            min_value: Optional[float] = None
            max_value: Optional[float] = None
            ref: Optional[RefRange] = None
            ref_origin: str = "none"
            ref_kind: str = ""
            verdict: str = "na"
            verdict_note: str = ""
            range_available: Optional[bool] = None
            sources: List[str] = field(default_factory=list)
            span_days: int = 0
            coverage: Optional[float] = None
            insufficient: bool = False
            daily_mean: Optional[float] = None
            kind: str = "single"

        @dataclass
        class Analysis:  # type: ignore[no-redef]
            trends: Dict[str, MetricTrend] = field(default_factory=dict)
            profile: Dict[str, Any] = field(default_factory=dict)
            span: Optional[tuple] = None
            span_days: int = 0
            sources: List[str] = field(default_factory=list)
            notes: List[str] = field(default_factory=list)
            workout_summary: Dict[str, Any] = field(default_factory=dict)

            def ordered_metrics(self) -> List[str]:
                order = {c: i for i, c in enumerate(catalog.CATEGORY_ORDER)}
                return sorted(self.trends.keys(),
                              key=lambda m: (order.get(self.trends[m].category, 99), m))

            def by_category(self) -> Dict[str, List[str]]:
                groups: Dict[str, List[str]] = {}
                for m in self.ordered_metrics():
                    groups.setdefault(self.trends[m].category, []).append(m)
                return groups

            def out_of_ref(self) -> List[MetricTrend]:
                return [t for t in self.trends.values() if t.verdict in ("low", "high")]

            def enough_for_trend(self) -> List[MetricTrend]:
                return [t for t in self.trends.values() if not t.insufficient]

        analysis_mod = types.ModuleType("health_report.analysis")
        analysis_mod.MetricTrend = MetricTrend
        analysis_mod.Analysis = Analysis
        analysis_mod.NOTE_NO_REF = "无可用参考区间"
        analysis_mod.NOTE_MIXED_SOURCES = "来源不一致，不做判定"
        analysis_mod.NOTE_NOT_JUDGED = "该指标不参与判定"
        sys.modules["health_report.analysis"] = analysis_mod
        setattr(health_report, "analysis", analysis_mod)

    try:
        importlib.import_module("health_report.pitfalls")
    except Exception:
        _STUBBED.append("pitfalls")

        @dataclass
        class Finding:  # type: ignore[no-redef]
            code: str = ""
            severity: str = "info"
            title: str = ""
            detail: str = ""
            metrics: List[str] = field(default_factory=list)
            evidence: Dict[str, Any] = field(default_factory=dict)

        pitfalls_mod = types.ModuleType("health_report.pitfalls")
        pitfalls_mod.Finding = Finding
        sys.modules["health_report.pitfalls"] = pitfalls_mod
        setattr(health_report, "pitfalls", pitfalls_mod)


from health_report import catalog  # noqa: E402
from health_report import samples  # noqa: E402
from health_report.llm import LLMConfig, build_messages, narrate  # noqa: E402
from health_report.report import (  # noqa: E402
    DISCLAIMER,
    _fmt,
    build_facts,
    build_report,
    render_metric_table,
)

_install_stubs()
from health_report.analysis import Analysis, MetricTrend  # noqa: E402
from health_report.pitfalls import Finding  # noqa: E402

try:
    from health_report.analysis import (  # noqa: E402
        NOTE_MIXED_SOURCES,
        NOTE_NO_REF,
        NOTE_NOT_JUDGED,
    )
except ImportError:  # 桩模块或旧版分析层：退回约定字面量
    NOTE_NO_REF = "无可用参考区间"
    NOTE_MIXED_SOURCES = "来源不一致，不做判定"
    NOTE_NOT_JUDGED = "该指标不参与判定"


# ────────────────────────────────────────────────── 2. 断言小工具

_RESULTS: List[Tuple[str, bool]] = []


def check(name: str, ok: bool, extra: str = "") -> bool:
    _RESULTS.append((name, bool(ok)))
    print(("{0} {1}" + ("　→ {2}" if (extra and not ok) else "")).format(
        "✅" if ok else "❌", name, extra))
    return bool(ok)


def contains_all(text: str, needles: List[str], label: str) -> None:
    missing = [n for n in needles if n not in text]
    check(label, not missing, "缺少：" + "、".join(missing) if missing else "")


def section(text: str, start: str, end: Optional[str] = None) -> str:
    """截取一段小节，便于把断言限定在正确的区域里。"""
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start)) if end else -1
    return text[i:] if j < 0 else text[i:j]


def has_line(text: str, prefix: str) -> bool:
    return any(line.startswith(prefix) for line in text.splitlines())


# ────────────────────────────────────────────────── 3. 合成夹具
#
# 全部虚构。日期、画像、来源名直接复用 samples.py 那套合成数据；
# 体测数值用同一量级另编；参考区间优先取 catalog 的内置表，
# 设备自带的那几个另给一组虚构数值。

PROFILE: Dict[str, Any] = dict(samples.PROFILE)          # 身高 172 / 年龄 28 / 男
INBODY_A = samples.INBODY_A_DATE                          # 合成体测 A
INBODY_B = samples.INBODY_B_DATE                          # 合成体测 B
DAILY_0 = samples.DAILY_START.isoformat()                 # 每日数据的起点
DAILY_N = (samples.DAILY_START + _timedelta(days=samples.DAILY_DAYS - 1)).isoformat()
DAILY_4 = (samples.DAILY_START + _timedelta(days=4)).isoformat()

INBODY, SCALE, WATCH = samples.INBODY_SOURCE, samples.SCALE_SOURCE, samples.WATCH_SOURCE

REF_MASS = RefRange(low=55.0, high=75.0, source="虚构设备参考范围")
REF_WATER = RefRange(low=35.0, high=43.0, source="虚构设备参考范围")
REF_PROTEIN = RefRange(low=9.0, high=11.4, source="虚构设备参考范围")
REF_BMI = RefRange(low=18.5, high=23.0, kind="screen", source="虚构设备参考范围")

# 合成日期白名单（含报告里的「生成时间」，那是当天日期）。
ALLOWED_DATES = {INBODY_A, INBODY_B, DAILY_0, DAILY_N, DAILY_4, _date.today().isoformat()}


def _ob(metric: str, day: str, value: float, source: str) -> Observation:
    return Observation(metric, day, value, "", source)


def make_dataset() -> Dataset:
    ds = Dataset(
        files=["(合成夹具) 两次虚构体测", "(合成夹具) 虚构每日数据"],
        profile=dict(PROFILE),
        labels={"bmi_judgement": "超重"},
    )
    ds.observations = [
        Observation("body_mass", INBODY_A, 74.0, "kg", INBODY),
        Observation("body_mass", INBODY_B, 71.2, "kg", INBODY),
        Observation("steps", DAILY_0, 9100.0, "步", WATCH),
        Observation("steps", DAILY_4, 7200.0, "步", WATCH),
    ]
    ds.workouts = [
        Workout(DAILY_0, "跑步", duration_min=45.0, distance_km=7.2, avg_hr=152.0,
                energy_kcal=520.0),
        Workout(DAILY_4, "骑行", duration_min=60.0, distance_km=22.5, avg_hr=131.0,
                energy_kcal=610.0),
    ]
    return ds


def make_analysis() -> Analysis:
    """11 个指标，覆盖 in/low/high/na × range/goal/screen × snapshot/sum/mean。"""
    trends: Dict[str, MetricTrend] = {}
    fat_ref = catalog.builtin_ref("body_fat_pct", PROFILE)   # 内置人群表：体脂率
    goal_ref = catalog.builtin_ref("steps", PROFILE)         # 内置目标：步数
    hr_ref = catalog.builtin_ref("resting_hr", PROFILE)      # 内置筛查阈值：静息心率

    # 体成分 + 设备区间 + 落在区间内（首末两点，kind=trend）
    trends["body_mass"] = MetricTrend(
        metric="body_mass", cn="体重", unit="kg", category="体成分", agg="snapshot",
        n=2, days=2,
        first=_ob("body_mass", INBODY_A, 74.0, INBODY),
        last=_ob("body_mass", INBODY_B, 71.2, INBODY),
        delta=-2.8, pct=-3.78, per_30d=-0.6, mean=72.6, median=72.6,
        min_value=71.2, max_value=74.0,
        ref=REF_MASS, ref_origin="device", ref_kind="range", verdict="in",
        verdict_note="", range_available=True,
        sources=[INBODY], span_days=130, coverage=1.0, insufficient=False, kind="trend",
    )
    # 体成分 + 两个来源：verdict 强制 na，note 是「来源不一致」
    trends["body_fat_pct"] = MetricTrend(
        metric="body_fat_pct", cn="体脂率", unit="%", category="体成分", agg="snapshot",
        n=63, days=62,
        first=_ob("body_fat_pct", INBODY_A, 22.3, INBODY),
        last=_ob("body_fat_pct", INBODY_B, 16.9, SCALE),
        delta=-5.4, pct=-24.22, mean=19.6,
        ref=fat_ref, ref_origin="builtin", ref_kind="", verdict="na",
        verdict_note=NOTE_MIXED_SOURCES, range_available=True,
        sources=[INBODY, SCALE], span_days=130, insufficient=False, kind="trend",
    )
    # 体成分 + 设备区间 + 高于上限
    trends["total_body_water"] = MetricTrend(
        metric="total_body_water", cn="身体总水分", unit="L", category="体成分", agg="snapshot",
        n=2, days=2,
        first=_ob("total_body_water", INBODY_A, 42.5, INBODY),
        last=_ob("total_body_water", INBODY_B, 44.0, INBODY),
        delta=1.5, pct=3.53, mean=43.3,
        ref=REF_WATER, ref_origin="device", ref_kind="range", verdict="high",
        verdict_note="", range_available=True,
        sources=[INBODY], span_days=130, insufficient=False, kind="trend",
    )
    # 变化量小于 noise（蛋白质 noise=0.2）：应显示「噪声内」
    trends["protein_mass"] = MetricTrend(
        metric="protein_mass", cn="蛋白质", unit="kg", category="体成分", agg="snapshot",
        n=2, days=2,
        first=_ob("protein_mass", INBODY_A, 11.5, INBODY),
        last=_ob("protein_mass", INBODY_B, 11.6, INBODY),
        delta=0.1, pct=0.87, mean=11.55,
        ref=REF_PROTEIN, ref_origin="device", ref_kind="range", verdict="high",
        verdict_note="", range_available=True,
        sources=[INBODY], span_days=130, insufficient=False, kind="trend",
    )
    # 肥胖评估 + 筛查阈值 + 高于上限
    trends["bmi"] = MetricTrend(
        metric="bmi", cn="BMI", unit="kg/m2", category="肥胖评估", agg="snapshot",
        n=2, days=2,
        first=_ob("bmi", INBODY_A, 25.0, INBODY),
        last=_ob("bmi", INBODY_B, 24.1, INBODY),
        delta=-0.9, pct=-3.6, mean=24.55,
        ref=REF_BMI, ref_origin="device", ref_kind="screen", verdict="high",
        verdict_note="", range_available=True,
        sources=[INBODY], span_days=130, insufficient=False, kind="trend",
    )
    # 活动量 + 目标 + 未达目标（sum 类：变化列给日均）
    trends["steps"] = MetricTrend(
        metric="steps", cn="步数", unit="步", category="活动量", agg="sum",
        n=30, days=30,
        first=_ob("steps", DAILY_0, 9100.0, WATCH),
        last=_ob("steps", DAILY_4, 7200.0, WATCH),
        delta=None, pct=None, daily_mean=8860.0, mean=8860.0,
        ref=goal_ref, ref_origin="builtin", ref_kind="goal", verdict="low",
        verdict_note="", range_available=True,
        sources=[WATCH], span_days=30, coverage=0.7, insufficient=False, kind="trend",
    )
    # 没有任何可用区间：note = 无可用参考区间 -> 只做趋势
    trends["hrv_sdnn"] = MetricTrend(
        metric="hrv_sdnn", cn="心率变异性 HRV(SDNN)", unit="ms", category="心肺与恢复",
        agg="snapshot", n=8, days=8,
        first=_ob("hrv_sdnn", DAILY_0, 48.0, WATCH),
        last=_ob("hrv_sdnn", DAILY_N, 55.2, WATCH),
        delta=7.2, pct=15.0, mean=51.0,
        ref=None, ref_origin="none", ref_kind="", verdict="na",
        verdict_note=NOTE_NO_REF, range_available=False,
        sources=[WATCH], span_days=60, insufficient=False, kind="sparse",
    )
    # mean 类 + 数据点与天数不等 + 筛查阈值低于下限
    trends["resting_hr"] = MetricTrend(
        metric="resting_hr", cn="静息心率", unit="次/分", category="心肺与恢复", agg="mean",
        n=12, days=5,
        first=_ob("resting_hr", DAILY_0, 61.0, WATCH),
        last=_ob("resting_hr", DAILY_4, 58.0, WATCH),
        daily_mean=58.4,
        ref=hr_ref, ref_origin="builtin", ref_kind="screen", verdict="low",
        verdict_note="", range_available=True,
        sources=[WATCH], span_days=4, insufficient=False, kind="trend",
    )
    # 数据点不足：kind=single + insufficient
    trends["lean_left_leg"] = MetricTrend(
        metric="lean_left_leg", cn="左腿肌肉量", unit="kg", category="肌肉均衡", agg="snapshot",
        n=1, days=1,
        first=_ob("lean_left_leg", INBODY_B, 8.40, INBODY),
        last=_ob("lean_left_leg", INBODY_B, 8.40, INBODY),
        ref=None, ref_origin="none", ref_kind="", verdict="na",
        verdict_note=NOTE_NO_REF, range_available=False,
        sources=[INBODY], span_days=0, insufficient=True, kind="single",
    )
    # 隐藏类别 1：生物电阻抗（设备原始读数，不参与判定）
    trends["z_right_leg_20k"] = MetricTrend(
        metric="z_right_leg_20k", cn="右下肢阻抗 20kHz", unit="Ω", category="生物电阻抗",
        agg="snapshot", n=2, days=2,
        first=_ob("z_right_leg_20k", INBODY_A, 190.0, INBODY),
        last=_ob("z_right_leg_20k", INBODY_B, 250.0, INBODY),
        delta=60.0, pct=31.58, mean=220.0,
        ref=None, ref_origin="none", ref_kind="", verdict="na",
        verdict_note=NOTE_NOT_JUDGED, range_available=False,
        sources=[INBODY], span_days=130, insufficient=False, kind="trend",
    )
    # 隐藏类别 2：设备建议与评分
    trends["inbody_score"] = MetricTrend(
        metric="inbody_score", cn="体成分评分", unit="分", category="设备建议与评分",
        agg="snapshot", n=2, days=2,
        first=_ob("inbody_score", INBODY_A, 78.0, INBODY),
        last=_ob("inbody_score", INBODY_B, 80.0, INBODY),
        delta=2.0, pct=2.56, mean=79.0,
        ref=None, ref_origin="none", ref_kind="", verdict="na",
        verdict_note=NOTE_NOT_JUDGED, range_available=False,
        sources=[INBODY], span_days=130, insufficient=False, kind="sparse",
    )

    return Analysis(
        trends=trends,
        profile=dict(PROFILE),
        span=(INBODY_A, DAILY_N),
        span_days=171,
        sources=[INBODY, SCALE, WATCH],
        notes=["本夹具是合成的，不含任何真实个人数据。"],
        workout_summary={
            "has_data": True, "total": 26, "span": (INBODY_A, INBODY_B),
            "avg_hr_overall": 142.4,
            "by_activity": {
                "跑步": {"count": 12, "minutes": 540.0, "km": 84.2, "kcal": 6240.0,
                         "avg_hr": 152.0},
                "骑行": {"count": 14, "minutes": 900.0, "km": 320.4, "kcal": 8120.0,
                         "avg_hr": 131.5},
            },
        },
    )


def make_findings() -> List[Finding]:
    """三条 Finding：alert / warn / info，措辞里只用合成数值。"""
    return [
        Finding(
            code="WATER_OUT_OF_RANGE", severity="warn",
            title="虚构样例：身体总水分那一次超出设备上限，会放大体重变化",
            detail="合成数据里总水分 44 L 高于虚构上限 43 L，同一份报告里的体重与去脂体重"
                   "会被一起抬高，首末差值因此不能全部算作脂肪变化。",
            metrics=["total_body_water", "body_mass"], evidence={"high_by": 1.0},
        ),
        Finding(
            code="PCT_BASELINE_NOT_COMPARABLE", severity="info",
            title="虚构样例：节段百分比两次不可比",
            detail="设备的节段脂肪/肌肉百分比基准在两次之间被调整过，跨时间只看 kg。",
            metrics=["lean_left_leg"], evidence={},
        ),
        Finding(
            code="MULTI_SOURCE_DEVICE", severity="alert",
            title="虚构样例：有指标来自两套测量系统",
            detail="体脂率同时来自虚构体成分仪与虚构智能秤，来源列标了 ⚠️，"
                   "两者差值不能直接当作变化。",
            metrics=["body_fat_pct"], evidence={"sources": [INBODY, SCALE]},
        ),
    ]


# ────────────────────────────────────────────────── 4. 各项验证


def verify_privacy() -> None:
    """隐私自检：本文件与产出的报告里只允许出现合成数值。

    真实数值的黑名单不进仓库（扫描规则本身会把真实数值写回来），所以这里只守住
    两条能在仓库内自证的规则：
      1. 出现的日期必须是夹具用到的合成日期（或报告生成时的当天日期）；
      2. 不出现 6 位以上的连续数字（真实体测报告里的受测者 ID 就是这种形状）。
    """
    print("\n── 附加 A：隐私自检（只用合成数值）")
    allowed = set(ALLOWED_DATES) | {_date.today().isoformat()}
    for path in (SCRIPT_PATH, SAMPLE_PATH, EMPTY_PATH):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", text))
        stray = sorted(dates - allowed)
        check("{0}：日期只有合成样例的日期".format(path.name), not stray,
              "发现：" + "、".join(stray))
        longs = sorted({m for m in re.findall(r"\d{6,}", text)})
        check("{0}：没有 6 位以上连续数字（受测者 ID 那种形状）".format(path.name),
              not longs, "发现：" + "、".join(longs))


def verify_formatting() -> None:
    print("\n── 数值格式化（_fmt）")
    check("74.00 → 74（去尾零）", _fmt(74.00, 1, "kg") == "74 kg", _fmt(74.00, 1, "kg"))
    check("2.00 → 2（去小数点）", _fmt(2.00, 2, "") == "2", _fmt(2.00, 2, ""))
    check("precision 为 0 时不出现小数点", _fmt(8860.0, 0, "步") == "8860 步",
          _fmt(8860.0, 0, "步"))
    check("unit 为 — 时不拼单位", _fmt(0.86, 2, "—") == "0.86", _fmt(0.86, 2, "—"))
    check("None → —", _fmt(None, 1, "kg") == "—")
    check("非有限值 → —", _fmt(float("nan"), 1, "kg") == "—")
    check("带符号：正数补 +", _fmt(0.3, 1, "kg", signed=True) == "+0.3 kg")
    check("带符号：负数保留 -", _fmt(-2.8, 1, "kg", signed=True) == "-2.8 kg")
    check("带符号：零不补 +（+0 读起来像变化）",
          _fmt(0.0, 0, "次/分", signed=True) == "0 次/分", _fmt(0.0, 0, "次/分", signed=True))


def verify_empty() -> None:
    print("\n── 验证 1：空数据")
    ds = Dataset()
    an = Analysis(trends={}, profile={}, span=None, span_days=0, sources=[], notes=[],
                  workout_summary={"has_data": False, "total": 0, "span": None,
                                   "avg_hr_overall": None, "by_activity": {}})
    try:
        text = build_report(ds, an, [])
        ok, err = True, ""
    except Exception as exc:  # noqa: BLE001
        text, ok, err = "", False, "{0}: {1}".format(exc.__class__.__name__, exc)
    check("空数据不崩", ok, err)
    if not ok:
        return
    contains_all(text, ["# 健康数据报告", "**这不是医学诊断。**", DISCLAIMER,
                        "## 一、数据可信度提示", "## 二、指标总览", "## 三、分类详情",
                        "## 五、规则层结论", "## 六、AI 解读", "## 附录 A：方法与口径",
                        "未发现影响结论可信度的问题。",
                        "只做趋势、不做判定的指标",
                        "以上结论全部来自确定性计算，可以逐条核对第二节的数值。"],
                 "空数据报告包含全部固定小节与免责声明")
    check("空数据：没有训练数据时不出现第四节", "## 四、训练汇总" not in text)
    check("空数据：附表 B 说明本次没有设备类指标", "本次没有设备原始读数或设备建议类指标" in text)
    check("空数据：免责声明在第一节之前",
          text.find(DISCLAIMER) < text.find("## 一、数据可信度提示"))
    EMPTY_PATH.write_text(text, encoding="utf-8")


def verify_full() -> str:
    print("\n── 验证 2：有数据（11 个指标 + 3 个 Finding，全部合成）")
    ds = make_dataset()
    an = make_analysis()
    fs = make_findings()
    try:
        text = build_report(ds, an, fs, title="健康数据报告（合成夹具）")
    except Exception as exc:  # noqa: BLE001
        check("有数据不崩", False, "{0}: {1}".format(exc.__class__.__name__, exc))
        return ""
    check("有数据不崩", True)
    SAMPLE_PATH.write_text(text, encoding="utf-8")

    # ── 免责声明位置
    i_disc = text.find(DISCLAIMER)
    i_sec1 = text.find("## 一、数据可信度提示")
    i_sec2 = text.find("## 二、指标总览")
    i_sec5 = text.find("## 五、规则层结论")
    check("免责声明在标题之后、第一节之前",
          0 <= text.find("# 健康数据报告（合成夹具）") < i_disc < i_sec1,
          "disc={0} sec1={1}".format(i_disc, i_sec1))
    check("免责声明在任何结论之前（第二节、第五节都在其后）", i_disc < i_sec2 and i_disc < i_sec5)
    check("免责声明没有出现在附录 B 之后", DISCLAIMER not in section(text, "## 附录 B"))

    # ── 第一节：可信度
    sec1 = section(text, "## 一、数据可信度提示", "## 二、指标总览")
    check("第一节按 severity 排序（alert → warn → info）",
          sec1.find("🔴") < sec1.find("🟠") < sec1.find("🔵") >= 0, sec1[:200])
    contains_all(sec1, ["### 🔴 虚构样例：有指标来自两套测量系统",
                        "### 🟠 虚构样例：身体总水分那一次超出设备上限",
                        "### 🔵 虚构样例：节段百分比两次不可比"],
                 "第一节列出 alert/warn/info 三类 Finding")
    check("第一节有「只做趋势、不做判定的指标」小节", "### 只做趋势、不做判定的指标" in sec1)
    trend_only = section(sec1, "### 只做趋势、不做判定的指标")
    check("该小节列出 range_available=False 的 HRV 指标", "心率变异性 HRV(SDNN)" in trend_only,
          trend_only)
    check("该小节用了 catalog.ref_note 里的原因",
          "没有公认的通用正常范围" in trend_only, trend_only)
    check("该小节还列出 range_available=False 的左腿肌肉量", "左腿肌肉量" in trend_only)
    check("有区间但来源不一致的指标不被列进「只做趋势」小节（range_available=True）",
          "体脂率" not in trend_only, trend_only)
    check("隐藏类别只在附录提示，不刷屏第一节", "另有 2 项设备原始读数" in trend_only, trend_only)

    # ── 判定措辞
    check("判定列出现「未达目标」（goal + low）", "未达目标" in text)
    check("判定列出现「超出筛查参考」（screen + high/low）", "超出筛查参考" in text)
    check("判定列出现「只做趋势」（na + 无可用参考区间）", "只做趋势" in text)
    check("判定列出现「高于参考上限」（range + high）", "高于参考上限" in text)
    check("判定列出现「在参考区间内」（range + in）", "在参考区间内" in text)
    check("判定列出现「来源不一致，不做判定」（na + 来源不一致，原样显示）",
          "来源不一致，不做判定" in text)
    check("判定列出现「不参与判定」（na + 该指标不参与判定，附录 B）",
          "不参与判定" in section(text, "## 附录 B"))
    fat_row = section(text, "| **体脂率** |", "\n")
    check("跨来源指标没有被写成「只做趋势」",
          "来源不一致，不做判定" in fat_row and "只做趋势" not in fat_row, fat_row)
    step_row = section(text, "| **步数** |", "\n")
    check("goal 类未达目标没有被写成「低于参考下限」",
          "未达目标" in step_row and "低于参考下限" not in step_row, step_row)
    check("参考区间后面标注性质：（目标）", "（目标）" in text)
    check("参考区间后面标注性质：（筛查）", "（筛查）" in text)
    check("sum 类显示日均而不是首末差", "日均 8860 步" in text)
    check("mean 类显示日均（按 precision=0 格式化）", "日均 58 次/分" in text)
    check("数据点与天数不等时显示「N 点/M 天」", "12 点/5 天" in text)
    check("多来源加 ⚠️ 提示不可直接比", "{0} / {1} ⚠️".format(INBODY, SCALE) in text)
    check("变化量小于 noise 阈值时标「噪声内」（蛋白质 0.1，noise 0.2）", "（噪声内）" in text)
    check("变化列带符号与百分比", "（-3.8%）" in text)
    check("主表不显示生物电阻抗/设备建议与评分",
          "右下肢阻抗 20kHz" not in section(text, "## 二、指标总览", "## 三、分类详情"))

    # ── 第三节
    sec3 = section(text, "## 三、分类详情", "## 四、训练汇总")
    contains_all(sec3, ["### 体成分", "### 肥胖评估", "### 活动量", "### 心肺与恢复",
                        "### 肌肉均衡"], "第三节按类别建小节")
    check("第三节用了 CATEGORY_HINT 的口径说明", "这里给的是目标，不是正常范围" in sec3, sec3[:300])
    check("第三节给出 spec.hint 说明行", "生物电阻抗法受水合状态影响" in sec3)
    check("单指标类别不用表格，直接一行文字", "**左腿肌肉量**：最新 8.4 kg" in sec3, sec3[-600:])

    # ── 第四节
    sec4 = section(text, "## 四、训练汇总", "## 五、规则层结论")
    contains_all(sec4, ["**总次数**：26", "{0} → {1}".format(INBODY_A, INBODY_B),
                        "**总体平均心率**：142 次/分",
                        "| 运动类型 | 次数 | 总时长 | 总距离 | 总热量 | 平均心率 |",
                        "9h 0m", "84.2 km"],
                 "第四节给出训练汇总与按活动类型的表格")

    # ── 第五节
    sec5 = section(text, "## 五、规则层结论", "## 六、AI 解读")
    contains_all(sec5, ["### 体成分与体重", "### 超出参考区间的指标", "### 不足以判断的部分",
                        "74 kg → 71.2 kg", "-2.8 kg", "-3.8%",
                        "以上结论全部来自确定性计算，可以逐条核对第二节的数值。"],
                 "第五节是带数字的确定性文字")
    check("第五节超额指标带数值与区间性质",
          "身体总水分" in sec5 and "高于参考上限" in sec5 and "正常参考区间" in sec5, sec5)
    check("第五节列出数据不足的指标", "只有 1 个数据点" in sec5, sec5)
    check("第五节的判定与第二节一致（未达目标）", "未达目标" in sec5)
    check("第五节跳过跨来源的体成分指标并说明原因",
          "没有列出" in sec5 and "体脂率" in sec5 and "固定用同一台设备" in sec5, sec5)
    check("第五节没有把跨来源的首末差值当变化写出来",
          not has_line(sec5, "- **体脂率**："), sec5)
    check("第五节的体重趋势仍在（单来源）", has_line(sec5, "- **体重**："), sec5)

    # ── 第六节（ai_text 为空）
    sec6 = section(text, "## 六、AI 解读", "## 附录 A")
    contains_all(sec6, ["HEALTH_REPORT_API_KEY", "HEALTH_REPORT_BASE_URL", "HEALTH_REPORT_MODEL",
                        "不开启也是完整的"],
                 "第六节在未开启 AI 时给出开启方法与「不开启也是完整的」")

    # ── 附录
    check("附录 A 讲了口径（单位/优先级/三种性质/噪声/日均）",
          all(k in text for k in ["单位归一", "参考区间优先级", "三种区间性质", "噪声阈值",
                                  "汇总类指标"]))
    check("附录 A 重申不是医学诊断", DISCLAIMER in section(text, "## 附录 A"))
    secB = section(text, "## 附录 B：全部指标明细")
    contains_all(secB, ["| 指标 | 最新值 | 参考区间 | 判定 | 变化 | 数据点 | 来源 |",
                        "右下肢阻抗 20kHz", "体成分评分"],
                 "附录 B 含被主表排除的两类指标")

    # ── 禁用措辞与表格完整性
    banned = ["你患有", "说明你有", "建议服用", "建议剂量", "诊断为", "确诊", "处方"]
    hit = [b for b in banned if b in text]
    check("报告没有出现诊断性措辞", not hit, "命中：" + "、".join(hit))
    bad_rows = [l for l in text.splitlines() if l.startswith("| **") and l.count("|") != 8]
    check("表格竖线没有被字段内容破坏（指标表每行 7 个单元格）", not bad_rows,
          "\n".join(bad_rows))

    # ── ai_text 非空 / 关闭附录
    text_ai = build_report(ds, an, fs, ai_text="## 一句话\n这批数据的变化方向都很温和。",
                           ai_model="gpt-4o-mini")
    check("ai_text 非空时输出解读并注明模型",
          "gpt-4o-mini" in text_ai and "仅解释上面的数值，不参与计算" in text_ai
          and "这批数据的变化方向都很温和。" in text_ai)
    check("include_appendix=False 时不输出附录 B",
          "## 附录 B" not in build_report(ds, an, fs, include_appendix=False))

    # ── 容错：字段缺失 / None / 半成品对象
    rough = Analysis(trends={"body_mass": types.SimpleNamespace()}, profile={},
                     span=None, span_days=0, sources=[], notes=[],
                     workout_summary={"has_data": False})
    try:
        build_report(Dataset(), rough, [None, types.SimpleNamespace()])
        check("缺失字段 / Finding 为 None 也不崩", True)
    except Exception as exc:  # noqa: BLE001
        check("缺失字段 / Finding 为 None 也不崩", False, repr(exc))
    bare = types.SimpleNamespace(trends=an.trends, span=(INBODY_A, INBODY_B), span_days=130,
                                 sources=[INBODY])
    try:
        t2 = build_report(ds, bare, [])
        check("analysis 没有 ordered_metrics/by_category 时仍能排版",
              "## 二、指标总览" in t2 and "体重" in t2)
    except Exception as exc:  # noqa: BLE001
        check("analysis 没有 ordered_metrics/by_category 时仍能排版", False, repr(exc))
    try:
        t3 = build_report(ds, an, fs, lang="en")
        check("lang=en 也能生成且保留免责声明与元信息",
              DISCLAIMER in t3 and "| Metric | Latest |" in t3
              and "1. Data credibility notes" in t3)
    except Exception as exc:  # noqa: BLE001
        check("lang=en 也能生成", False, repr(exc))

    check("render_metric_table 可直接用",
          render_metric_table([an.trends["body_mass"]]).startswith("| 指标 | 最新值 |"))
    return text


def verify_facts() -> None:
    print("\n── 验证 3：build_facts")
    ds = make_dataset()
    an = make_analysis()
    fs = make_findings()
    facts = build_facts(ds, an, fs)
    try:
        raw = json.dumps(facts, ensure_ascii=False)
        check("json.dumps(facts, ensure_ascii=False) 成功", True)
    except Exception as exc:  # noqa: BLE001
        raw = ""
        check("json.dumps(facts, ensure_ascii=False) 成功", False, repr(exc))
    check("facts 含 profile / data / metrics / out_of_ref / insufficient / findings / workouts",
          all(k in facts for k in ["profile", "data", "metrics", "out_of_ref",
                                   "insufficient", "findings", "workouts"]))
    check("facts.metrics 排除了隐藏类别",
          all(m["category"] not in ("生物电阻抗", "设备建议与评分") for m in facts["metrics"]))
    check("facts 里没有逐条原始观测（不含 observations 键）", "observations" not in raw.lower())
    check("facts 里没有原始时间戳字段（ts）", '"ts"' not in raw)
    check("facts.metrics 给出了算好的结论级数字",
          all(k in facts["metrics"][0] for k in ["latest", "delta", "pct", "daily_mean",
                                                 "ref_label", "ref_kind", "ref_source",
                                                 "verdict", "n", "days", "sources"]))
    mixed = [m for m in facts["metrics"] if m["metric"] == "body_fat_pct"]
    check("facts 带出了 verdict_note（跨来源指标）",
          bool(mixed) and mixed[0]["verdict_note"] == NOTE_MIXED_SOURCES, str(mixed)[:200])
    check("facts 的 verdict_text 用的是「来源不一致，不做判定」",
          bool(mixed) and mixed[0]["verdict_text"] == "来源不一致，不做判定")
    check("facts 的 out_of_ref / insufficient 非空且带中文名",
          bool(facts["out_of_ref"]) and bool(facts["insufficient"])
          and facts["out_of_ref"][0]["cn"])
    check("facts.findings 只带 code/severity/title/detail/metrics（不携带 evidence 原文）",
          all(set(f.keys()) == {"code", "severity", "title", "detail", "metrics"}
              for f in facts["findings"]))
    check("facts.workouts 保留了训练汇总", facts["workouts"].get("has_data") is True)
    empty = build_facts(Dataset(), Analysis(trends={}, profile={}, span=None, span_days=0,
                                            sources=[], notes=[], workout_summary={}), [])
    try:
        json.dumps(empty, ensure_ascii=False)
        check("空数据的 facts 同样可序列化", True)
    except Exception as exc:  # noqa: BLE001
        check("空数据的 facts 同样可序列化", False, repr(exc))
    check("facts 与报告口径一致（体重 delta = -2.8）",
          any(m["metric"] == "body_mass" and m["delta"] == -2.8 for m in facts["metrics"]))


def verify_llm(facts: Dict[str, Any]) -> None:
    print("\n── 验证 4：LLMConfig / narrate")
    key = "sk-synthetic-abcdef"
    saved = {k: os.environ.pop(k, None) for k in
             ("HEALTH_REPORT_API_KEY", "OPENAI_API_KEY", "HEALTH_REPORT_BASE_URL",
              "OPENAI_BASE_URL", "HEALTH_REPORT_MODEL", "OPENAI_MODEL", "HEALTH_REPORT_TIMEOUT")}
    try:
        cfg = LLMConfig.from_env()
        check("没设环境变量时 available() 为 False", cfg.available() is False)
        desc = cfg.describe()
        check("describe() 在未配置密钥时说明「未配置」", "未配置" in desc, desc)
        check("describe() 不含密钥", "sk-" not in desc)

        os.environ["HEALTH_REPORT_API_KEY"] = key
        cfg2 = LLMConfig.from_env()
        check("设了密钥后 available() 为 True", cfg2.available() is True)
        d2 = cfg2.describe()
        check("describe() 只显示前 4 位 + ***", key[:4] + "***" in d2, d2)
        check("describe() 不含完整密钥", key not in d2)
        check("describe() 不泄露密钥尾部", "abcdef" not in d2, d2)

        os.environ["HEALTH_REPORT_BASE_URL"] = "https://api.deepseek.com/"
        cfg3 = LLMConfig.from_env()
        check("base_url 只去掉末尾斜杠，不擅自补 /v1",
              cfg3.base_url == "https://api.deepseek.com", cfg3.base_url)
        os.environ["HEALTH_REPORT_BASE_URL"] = "https://api.openai.com/v1/"
        check("已有 /v1 的地址被保留",
              LLMConfig.from_env().base_url == "https://api.openai.com/v1")
        os.environ["HEALTH_REPORT_MODEL"] = "deepseek-chat"
        os.environ["HEALTH_REPORT_TIMEOUT"] = "abc"
        cfg4 = LLMConfig.from_env()
        check("HEALTH_REPORT_MODEL 生效", cfg4.model == "deepseek-chat")
        check("timeout 解析失败回落到默认", cfg4.timeout == 60.0, str(cfg4.timeout))
        os.environ["HEALTH_REPORT_TIMEOUT"] = "2.5"
        check("timeout 正常解析", LLMConfig.from_env().timeout == 2.5)

        check("未配置时 narrate 返回空串", narrate(facts, LLMConfig()) == "")
        # 明显不通的地址 + 2 秒超时：必须优雅降级
        os.environ["HEALTH_REPORT_BASE_URL"] = "http://127.0.0.1:9/v1"
        os.environ["HEALTH_REPORT_TIMEOUT"] = "2"
        dead = LLMConfig.from_env()
        try:
            got = narrate(facts, dead)
            check("连不通的接口：narrate 静默返回空串且不抛异常", got == "", repr(got))
        except Exception as exc:  # noqa: BLE001
            check("连不通的接口：narrate 静默返回空串且不抛异常", False, repr(exc))

        # 用假响应体覆盖各种返回结构
        import io
        import urllib.error
        import urllib.request as _u

        class _Resp:
            def __init__(self, payload: bytes) -> None:
                self._p = payload

            def read(self) -> bytes:
                return self._p

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *a: Any) -> bool:
                return False

        def _body(obj: Any) -> bytes:
            return json.dumps(obj, ensure_ascii=False).encode("utf-8")

        records: List[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        logger = logging.getLogger("health_report.llm")
        handler = _Handler()
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        orig = _u.urlopen
        try:
            _u.urlopen = lambda *a, **k: _Resp(_body({"choices": [{"text": "来自 text 字段"}]}))
            check("choices[0].text 兜底可用", narrate(facts, dead) == "来自 text 字段")
            _u.urlopen = lambda *a, **k: _Resp(b'{"ok":true}')
            check("结构不符时返回空串", narrate(facts, dead) == "")
            _u.urlopen = lambda *a, **k: _Resp(b"not json at all")
            check("返回体不是 JSON 时返回空串", narrate(facts, dead) == "")
            _u.urlopen = lambda *a, **k: _Resp(_body(
                {"choices": [{"message": {"content": "  ## 结论\nok  "}}]}))
            check("正常结构能取到 message.content", narrate(facts, dead).startswith("## "))

            def _boom(*a: Any, **k: Any) -> Any:
                raise urllib.error.HTTPError(
                    "https://example.invalid/chat/completions", 401, "Unauthorized", {},
                    io.BytesIO(_body({"error": "invalid api key " + key})))

            _u.urlopen = _boom
            records.clear()
            check("HTTPError 静默降级为空串", narrate(facts, dead) == "")
            joined = " ".join(records)
            check("HTTPError 的日志读出了 body 里的错误信息", "invalid api key" in joined, joined)
            check("HTTPError 的日志里没有密钥", key not in joined, joined)
        finally:
            _u.urlopen = orig
            logger.removeHandler(handler)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def verify_messages(facts: Dict[str, Any]) -> None:
    print("\n── 验证 5：build_messages")
    msgs = build_messages(facts, "zh")
    check("返回 system + user 两条", len(msgs) == 2 and msgs[0]["role"] == "system"
          and msgs[1]["role"] == "user")
    system = msgs[0]["content"]
    constraints = [
        "你不是医生",                    # 1 不许诊断
        "只能引用 facts 里出现过的数字",  # 2 不许自己算
        "数据里没有",                    # 2 缺数字时的说法
        "verdict 为 na",                 # 3 只谈趋势
        "goal 是活动量目标",              # 4 三种区间性质
        "未达目标",                       # 4 不许说指标异常
        "findings",                      # 5 先提醒可信度
        "数据不足以判断",                 # 6 不确定就明说
        "3~5 个小节",                     # 7 篇幅与结构
        "不超过 600 字",
        "必须有一个「下一步」小节",        # 7 必须有下一步
        "改善数据可比性",                 # 8 下一步的范围
        "固定用同一台设备",               # 8 示例
        "不要给医疗、营养或训练处方",      # 9 不许开处方
        "这项具体检查",                   # 9 不许建议具体检查（措辞随版本变化，兜底见下）
        "本解读不是医学建议",              # 10 结尾提醒
        "咨询医生",
    ]
    contains_all(system, [c for c in constraints if c != "这项具体检查"],
                 "system prompt 包含 10 条安全约束的全部要件")
    check("约束 9 不许建议具体检查/治疗",
          "具体检查" in system or "治疗" in system, system[-400:])
    check("user 消息里嵌入了事实 JSON（缩进 1）",
          '"metrics"' in msgs[1]["content"] and "\n " in msgs[1]["content"])
    check("user 消息说明这是已经算好的事实",
          "已经计算好的事实" in msgs[1]["content"])
    check("user 消息要求包含「下一步」", "下一步" in msgs[1]["content"])
    check("en 模式追加英文提示但约束不变",
          "请用英文回答" in build_messages(facts, "en")[0]["content"]
          and "你不是医生" in build_messages(facts, "en")[0]["content"])
    check("facts 里的真实数字确实出现在 user 消息里", "8860.0" in msgs[1]["content"])


def main() -> int:
    print("Python {0}｜桩模块：{1}".format(
        sys.version.split()[0], "、".join(_STUBBED) or "无（同事模块已就绪）"))
    verify_formatting()
    verify_empty()
    if not verify_full():
        return 1
    verify_privacy()
    facts = build_facts(make_dataset(), make_analysis(), make_findings())
    verify_facts()
    verify_llm(facts)
    verify_messages(facts)

    failed = [name for name, ok in _RESULTS if not ok]
    print("\n" + "=" * 72)
    print("共 {0} 项检查，通过 {1} 项，失败 {2} 项".format(
        len(_RESULTS), len(_RESULTS) - len(failed), len(failed)))
    for name in failed:
        print("  ❌ " + name)
    print("示例报告：{0}".format(SAMPLE_PATH))
    print("空数据报告：{0}".format(EMPTY_PATH))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
