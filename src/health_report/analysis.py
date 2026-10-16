"""分析层：把一堆 `Observation` 变成本项目的「事实底座」。

这一层只出**数字**和**机器可读的枚举**，不做任何判定措辞。
"偏高""偏低""未达目标"这类话属于 pitfalls.py 与报告层；
analysis 的产物要能被程序稳定消费，也要能被人复核。

计算口径（改这里等于改全项目的语义，务必一起改文档）
--------------------------------------------------

1. **参考区间 `ref` 必须与被判定的值同源**
   1) 被判定的那条观测自带 `Observation.ref` -> 用它，`ref_origin = "device"`；
   2) 否则找**同一 source** 最近一条带 ref 的观测 -> 用它的区间，`ref_origin = "device"`；
   3) 都没有 -> `catalog.builtin_ref(metric, profile)`，`ref_origin = "builtin"`；
   4) 仍没有 -> `ref = None`、`ref_origin = "none"`。
   "没有区间"不是失败，是正确的诚实。

   ⚠️ **为什么必须同源**：拿 A 设备的读数去套 B 设备的参考区间，正是本项目要防的
   那类错误（"算对了但读错了"）。例如家用秤报 21.2% 体脂，而 InBody 的参考区间是
   10~20%——两者都不是为对方标定的。设备自带区间优先，是因为它最贴近**那台机器**
   的模型；跨设备就失去了这个前提，所以退回内置人群表（并如实标注来源）。

2. **来源不一致时不判定**：同一指标的观测来自 >1 个 `source` 时，
   `verdict` 强制为 `"na"`，`verdict_note = "来源不一致，不做判定"`。
   两套测量系统之间连绝对值都不可比（见 pitfalls 的 `SRC_MIXED`），
   在一个混合了两种尺子的序列上给"正常/异常"是没有意义的。
   `ref` 仍然保留供展示。

3. **`verdict`**：用 **`last.value`**（最新一次测量，不是均值）与 `ref` 比。
   `ref.contains(last.value)` 为 True -> `"in"`；为 False 时，若高于 `ref.high`
   -> `"high"`，若低于 `ref.low` -> `"low"`；无法判断 -> `"na"`。
   只有 `spec.verdict == True` 的指标才参与判定：其它指标强制
   `ref_kind = ""`、`verdict = "na"`，但 `ref` 仍然保留供报告层展示。
   `ref_kind` 原样带出 `ref.kind`（`"range"` / `"goal"` / `"screen"`），
   报告层靠它决定措辞：没走到步数目标不等于「指标异常」。

   `verdict_note` 解释 `verdict == "na"` 的**原因**（无区间 / 来源不一致 /
   指标不参与判定），报告层据此决定措辞，不能一律写成"只做趋势"。

3. **快照类 vs 累计类**
   * `agg == "snapshot"`（体重、体脂率、阻抗…）：`delta = last - first`，
     `pct = delta / |first| * 100`（first 为 0 时 None），
     `per_30d = delta / span_days * 30`（span_days 为 0 时 None）。
   * `agg == "sum"` / `"mean"`（步数、能量、睡眠…）：**`delta` / `pct` /
     `per_30d` 一律为 None**——把两天的步数相减没有意义。改为给
     `mean` / `median` / `min_value` / `max_value` / `daily_mean` / `coverage`。
     - `daily_mean`：sum 类 = 所有观测值之和 ÷ `span_days`（缺数据的日子按 0 算，
       所以这个数会**系统性低估**，覆盖率低时必须一起看）；
       mean 类 = 有数据日的日均值。
     - `coverage = days / span_days`（span_days > 0 时），只对 sum/mean 类计算。

4. **`insufficient`**：snapshot 类若 `n < 2` 或 `span_days < 14` 为 True；
   sum/mean 类若 `days < 7` 为 True。`kind` 一律按
   `n >= 2 且 span_days >= 14` -> `"trend"`，`n == 1` -> `"single"`，
   其余 -> `"sparse"`（sum 类只有 3 天数据时，kind 是 sparse、insufficient 是 True）。

5. **时间口径**：`span_days` 是「最早的观测日期」到「最晚的观测日期」相差的天数，
   取自整个 `Dataset`；单个指标的 `span_days` 也是首末日期相差的天数，
   同一天测两次算 0 天（此时不折算速率，`per_30d = None`）。

6. **`workout_summary`**：按运动类型聚合，缺的值一律 `None`，
   绝不用 0 假装「有数据且为零」。`avg_hr_overall` 按有记录时长的训练加权。
"""

from __future__ import annotations

import datetime
import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import date as _date
from typing import Any, Dict, List, Optional, Tuple

from . import catalog
from .model import Dataset, Observation, RefRange

__all__ = [
    "MetricTrend",
    "Analysis",
    "analyze",
    "REF_ORIGIN_DEVICE",
    "REF_ORIGIN_BUILTIN",
    "REF_ORIGIN_NONE",
    "VERDICT_IN",
    "VERDICT_LOW",
    "VERDICT_HIGH",
    "VERDICT_NA",
    "AGG_SNAPSHOT",
    "AGG_SUM",
    "AGG_MEAN",
    "KIND_SINGLE",
    "KIND_TREND",
    "KIND_SPARSE",
    "TREND_MIN_DAYS",
    "AGG_MIN_DAYS",
    "UNKNOWN_SOURCE",
]

# ── 枚举取值（报告层按这些字面量判断，不要随便改） ──────────

REF_ORIGIN_DEVICE = "device"
REF_ORIGIN_BUILTIN = "builtin"
REF_ORIGIN_NONE = "none"

VERDICT_IN = "in"
VERDICT_LOW = "low"
VERDICT_HIGH = "high"
VERDICT_NA = "na"

AGG_SNAPSHOT = "snapshot"
AGG_SUM = "sum"
AGG_MEAN = "mean"

KIND_SINGLE = "single"
KIND_TREND = "trend"
KIND_SPARSE = "sparse"

#: snapshot 类指标至少要 2 个点、跨度至少 14 天才谈得上趋势。
TREND_MIN_DAYS = 14
#: sum/mean 类指标至少要有 7 天数据。
AGG_MIN_DAYS = 7

#: 没有 source 的观测统一记成这个名字（与 model.Dataset.sources_of 一致）。
UNKNOWN_SOURCE = "未知来源"


# ──────────────────────────────────────────────────────────── 趋势条目


@dataclass
class MetricTrend:
    """单个指标的分析结果。字段含义见模块 docstring。"""

    metric: str
    cn: str
    unit: str
    category: str
    agg: str
    n: int
    days: int
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
    ref_origin: str = REF_ORIGIN_NONE
    ref_kind: str = ""
    verdict: str = VERDICT_NA
    sources: List[str] = field(default_factory=list)
    span_days: int = 0
    coverage: Optional[float] = None
    insufficient: bool = False
    daily_mean: Optional[float] = None
    kind: str = KIND_SINGLE
    # verdict == "na" 的原因，供报告层区分措辞：
    # "" | "无可用参考区间" | "来源不一致，不做判定" | "该指标不参与判定"
    verdict_note: str = ""
    # 这个指标**到底有没有**任何可用区间（设备任何一次给的，或内置人群表）。
    # 与"本次判定用的 ref"不同：体重可能设备给过区间、但最新读数来自另一台没给区间的
    # 设备，于是 ref=None 却 range_available=True。
    # 报告层靠它决定要不要把该指标列进「只做趋势」——把"设备明明印了参考范围"
    # 的指标列进那一节，会让用户以为报告漏读了他手上的报告。
    # None 表示未知（手工构造的 MetricTrend），报告层会退回按 ref_origin 判断。
    range_available: Optional[bool] = None
    # 本次判定用的是哪个值，以及它的口径。
    #   "last"        -> last.value（快照类：你现在是多少）
    #   "daily_mean"  -> 有数据那些天的日均值（累计类：不能用部分数据的最后一天判）
    verdict_value: Optional[float] = None
    verdict_basis: str = "last"
    # 这个指标的多来源是「并发」还是「接替」，以及可能的设备接替点。
    #
    #   "single"     只有一台设备
    #   "concurrent" 多台设备在时间上重叠（同一段时间被两台记过）
    #   "sequential" 多台设备按时间接替（换了设备）
    #
    # 这两件事对报告的含义完全不同：并发影响**数值口径**（累计类会被双计，
    # 由解析器的 --source-policy 解决），接替影响**跨时间可比性**
    # （换设备造成的台阶不是身体变化）。由 DEVICE_HANDOVER 规则消费。
    source_overlap: str = "single"
    overlap_days: int = 0
    # [(新设备, 旧设备, 切换日期), ...]，启发式结果，用于提示而非精确断言
    handovers: List[Tuple[str, str, str]] = field(default_factory=list)

    # 便于报告层直接读：是否是「按日累计 / 按日平均」的指标
    @property
    def is_aggregate(self) -> bool:
        return self.agg in (AGG_SUM, AGG_MEAN)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "cn": self.cn,
            "unit": self.unit,
            "category": self.category,
            "agg": self.agg,
            "n": self.n,
            "days": self.days,
            "first": self.first.to_dict() if self.first else None,
            "last": self.last.to_dict() if self.last else None,
            "delta": self.delta,
            "pct": self.pct,
            "per_30d": self.per_30d,
            "mean": self.mean,
            "median": self.median,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "ref": self.ref.to_dict() if self.ref else None,
            "ref_origin": self.ref_origin,
            "verdict_note": self.verdict_note,
            "range_available": self.range_available,
            "verdict_value": self.verdict_value,
            "verdict_basis": self.verdict_basis,
            "source_overlap": self.source_overlap,
            "overlap_days": self.overlap_days,
            "handovers": [list(h) for h in self.handovers],
            "ref_kind": self.ref_kind,
            "verdict": self.verdict,
            "sources": list(self.sources),
            "span_days": self.span_days,
            "coverage": self.coverage,
            "insufficient": self.insufficient,
            "daily_mean": self.daily_mean,
            "kind": self.kind,
        }


@dataclass
class SleepRegularity:
    """睡眠规律性。

    **为什么用「睡眠中点」的标准差作为主指标**：睡眠中点（入睡与醒来的中点）
    同时包含了"几点睡"和"几点起"，所以它的离散程度是作息规律性的单一最佳概括。
    比只看总时长有用得多——时长可以靠周末补，中点漂移补不回来。

    三个阈值（30 / 60 分钟）是**经验分档**，不是诊断标准，报告里要如实这么说。
    """

    n: int
    window_days: int
    onset_median_min: Optional[float] = None
    wake_median_min: Optional[float] = None
    onset_std_min: Optional[float] = None
    wake_std_min: Optional[float] = None
    mid_std_min: Optional[float] = None
    level: str = "unknown"          # regular | moderate | irregular | unknown
    recent_n: int = 0
    recent_mid_std_min: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n": self.n, "window_days": self.window_days,
            "onset_median_min": self.onset_median_min,
            "wake_median_min": self.wake_median_min,
            "onset_std_min": self.onset_std_min,
            "wake_std_min": self.wake_std_min,
            "mid_std_min": self.mid_std_min,
            "level": self.level,
            "recent_n": self.recent_n,
            "recent_mid_std_min": self.recent_mid_std_min,
        }


@dataclass
class Association:
    """两个指标之间的关联（**相关，不是因果**）。

    报告里比相关系数更好用的是 `groups`：按 x 的中位数把日子分成两组，
    直接给"睡得少的那些天，次日静息心率中位多少"——客户能读懂这个。
    rho 同时给出，因为它不依赖分组阈值。
    """

    key: str
    cn: str
    x_metric: str
    y_metric: str
    lag_days: int
    n: int
    rho: Optional[float] = None
    groups: List[Dict[str, Any]] = field(default_factory=list)
    evidence: str = "C"             # A | B | C
    note: str = ""
    # 分组对比型的关联（训练日 vs 非训练日）额外给组间差异与测量噪声，
    # 用于按「差异有没有超过噪声」分级——这比只看相关系数更贴合本项目的一贯判据。
    delta: Optional[float] = None
    noise: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "cn": self.cn,
            "x_metric": self.x_metric, "y_metric": self.y_metric,
            "lag_days": self.lag_days, "n": self.n, "rho": self.rho,
            "groups": [dict(g) for g in self.groups],
            "evidence": self.evidence, "note": self.note,
            "delta": self.delta, "noise": self.noise,
        }


#: 睡眠中点标准差的分档阈值（分钟）。经验分档，不是诊断标准。
MODERATE_MID_STD_MIN = 30.0
IRREGULAR_MID_STD_MIN = 60.0
#: "近期"窗口天数（睡眠规律性会同时给全期与近期两个数）。
RECENT_WINDOW_DAYS = 90
#: 算规律性至少要有这么多夜，否则不给结论。
MIN_SLEEP_NIGHTS = 14
#: 关联分析的证据分级阈值。**是启发式**，写在报告里让读者自己判断。
ASSOC_MIN_N = 20
ASSOC_A_N, ASSOC_A_RHO = 150, 0.30
ASSOC_B_N, ASSOC_B_RHO = 60, 0.15


def minutes_to_clock(minutes: Optional[float]) -> str:
    """把「前一天 18:00 起算的分钟数」还原成钟点，例如 310 -> '23:10'。"""
    if minutes is None:
        return "—"
    total = int(round(18 * 60 + minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def _day_plus(date_str: str, days: int) -> str:
    try:
        d = datetime.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return ""
    return (d + datetime.timedelta(days=days)).isoformat()


def _median(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else None


def _pstdev(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    return float(statistics.pstdev(vals))


def _ranks(values: List[float]) -> List[float]:
    """平均秩（并列取平均），用于 Spearman。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    """Spearman 秩相关。用秩而不是原始值，因为 HRV、步数这类指标分布很偏。"""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def _grade_association(n: int, rho: Optional[float]) -> str:
    """**已弃用**：只看相关系数会给弱关联过高的等级。

    实测踩到过：demo 里一条 rho = −0.18 的关联被评成「证据中等」，
    可它两个分组的中位数完全相同（55 vs 55）——对读者来说等于没有差别。
    现在统一用 `_grade_group_delta`（组间差异 ÷ 测量噪声）分级，
    相关系数只作为附带的描述性数字出现。

    保留这个函数是为了历史可比性，新代码不要再用。
    """
    if rho is None or n < ASSOC_MIN_N:
        return "C"
    a = abs(rho)
    if n >= ASSOC_A_N and a >= ASSOC_A_RHO:
        return "A"
    if n >= ASSOC_B_N and a >= ASSOC_B_RHO:
        return "B"
    return "C"


def _evidence_from_groups(groups: List[Dict[str, Any]], y_metric: str
                          ) -> Tuple[str, Optional[float], float]:
    """用分组对比来定证据等级，返回 (等级, 组间差异, 测量噪声)。

    统一判据：**差异有没有超过该指标的测量噪声**。
    与 DELTA_BELOW_NOISE 用的是同一套逻辑——这是本项目的一贯口径，
    比"相关系数看起来不小"可靠得多。
    """
    if len(groups) < 2:
        return "C", None, 0.0
    y_low = groups[0].get("y_median")
    y_high = groups[1].get("y_median")
    if y_low is None or y_high is None:
        return "C", None, 0.0
    delta = float(y_high) - float(y_low)
    spec = catalog.get(y_metric)
    noise = float(getattr(spec, "noise", 0.0) or 0.0)
    grade = _grade_group_delta(delta, noise,
                               int(groups[1].get("n", 0)), int(groups[0].get("n", 0)))
    return grade, delta, noise


def _split_groups(pairs: List[Tuple[float, float]]) -> List[Dict[str, Any]]:
    """按 x 的中位数分两组，各组给 x/y 的中位数与样本量。

    比单给相关系数好懂：客户看得懂"睡得少的那些天，次日静息心率中位 58"。
    """
    if len(pairs) < ASSOC_MIN_N:
        return []
    xs = sorted(p[0] for p in pairs)
    cut = xs[len(xs) // 2]
    low = [p for p in pairs if p[0] <= cut]
    high = [p for p in pairs if p[0] > cut]
    out = []
    for label, grp in (("低", low), ("高", high)):
        if len(grp) < 5:
            continue
        out.append({
            "label": label,
            "x_median": _median([p[0] for p in grp]),
            "y_median": _median([p[1] for p in grp]),
            "n": len(grp),
        })
    return out


def _pair_by_lag(xs: Dict[str, float], ys: Dict[str, float],
                 lag_days: int) -> List[Tuple[float, float]]:
    """把两个指标的日值按 lag 配对：x 在 d，y 在 d+lag。"""
    pairs: List[Tuple[float, float]] = []
    for d, xv in xs.items():
        key = d if lag_days == 0 else _day_plus(d, lag_days)
        if not key:
            continue
        yv = ys.get(key)
        if xv is None or yv is None:
            continue
        pairs.append((float(xv), float(yv)))
    return pairs


def _sleep_regularity_from(onset: Dict[str, float], wake: Dict[str, float],
                           mid: Dict[str, float]) -> Optional[SleepRegularity]:
    """用「日期 -> 分钟」的三张表算规律性。数据不足返回 None。"""
    dates = sorted(set(mid) & set(onset) & set(wake))
    if len(dates) < MIN_SLEEP_NIGHTS:
        return None
    recent = set(dates[-RECENT_WINDOW_DAYS:]) if len(dates) > RECENT_WINDOW_DAYS else set(dates)

    mid_std = _pstdev([mid[d] for d in dates])
    level = "unknown"
    if mid_std is not None:
        if mid_std > IRREGULAR_MID_STD_MIN:
            level = "irregular"
        elif mid_std > MODERATE_MID_STD_MIN:
            level = "moderate"
        else:
            level = "regular"
    recent_dates = [d for d in dates if d in recent]
    return SleepRegularity(
        n=len(dates),
        window_days=_day_diff(dates[0], dates[-1]),
        onset_median_min=_median([onset[d] for d in dates]),
        wake_median_min=_median([wake[d] for d in dates]),
        onset_std_min=_pstdev([onset[d] for d in dates]),
        wake_std_min=_pstdev([wake[d] for d in dates]),
        mid_std_min=mid_std,
        level=level,
        recent_n=len(recent_dates),
        recent_mid_std_min=_pstdev([mid[d] for d in recent_dates]),
    )


#: 关联分析的配方：(key, 中文名, x 指标, y 指标, 滞后天数, 说明)
#: 只列出**两个指标都存在时才生效**的配方，缺指标自动跳过。
_ASSOCIATION_SPECS: Tuple[Tuple[str, str, str, str, int, str], ...] = (
    ("sleep_to_resting_hr", "睡眠时长 → 次日静息心率",
     "sleep_total", "resting_hr", 1,
     "睡得短的那一晚，第二天的静息心率会不会更高。"),
    ("sleep_to_hrv", "睡眠时长 → 次日 HRV",
     "sleep_total", "hrv_sdnn", 1,
     "睡得好不好是否反映在第二天的心率变异性上。"),
    ("steps_to_sleep", "白天步数 → 当晚睡眠时长",
     "steps", "sleep_total", 0,
     "白天的活动量与当晚睡眠的关系。"),
)


def _build_associations(dataset: Dataset, trends: Dict[str, MetricTrend]) -> List[Association]:
    """算全部预设的指标关联，外加「训练日 vs 非训练日」。"""
    series: Dict[str, Dict[str, float]] = {}
    for m in trends:
        series[m] = {o.date: o.value for o in dataset.series(m)}

    out: List[Association] = []
    for key, cn, xm, ym, lag, note in _ASSOCIATION_SPECS:
        xs, ys = series.get(xm), series.get(ym)
        if not xs or not ys:
            continue
        pairs = _pair_by_lag(xs, ys, lag)
        if len(pairs) < ASSOC_MIN_N:
            continue
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        groups = _split_groups(pairs)
        # 证据等级用"组间差异 ÷ 测量噪声"定，不用相关系数（见 _evidence_from_groups）
        evidence, delta, noise = _evidence_from_groups(groups, ym)
        out.append(Association(
            key=key, cn=cn, x_metric=xm, y_metric=ym, lag_days=lag,
            n=len(pairs), rho=rho,
            groups=groups,
            evidence=evidence,
            note=note,
            delta=delta, noise=noise,
        ))

    out.extend(_training_vs_rest(dataset, series))
    rank = {"A": 0, "B": 1, "C": 2}
    out.sort(key=lambda a: (rank.get(a.evidence, 9), -a.n, a.key))
    return out


#: 训练日 / 非训练日 对比关心哪些指标（同日比较，与人工核对的口径一致）
_TRAINING_REST_METRICS: Tuple[Tuple[str, str], ...] = (
    ("resting_hr", "静息心率"),
    ("hrv_sdnn", "HRV(SDNN)"),
    ("sleep_total", "总睡眠时长"),
)


def _training_vs_rest(dataset: Dataset,
                      series: Dict[str, Dict[str, float]]) -> List[Association]:
    """训练日 vs 非训练日 的对比。

    这是最容易被客户接受的一类结论（"练的那天和没练的那天，恢复指标差多少"），
    也是可以完全不依赖任何模型、只靠确定性计算得出的。
    """
    train_days = {w.date for w in (getattr(dataset, "workouts", None) or []) if w.date}
    if len(train_days) < 5:
        return []
    out: List[Association] = []
    for metric, cn in _TRAINING_REST_METRICS:
        table = series.get(metric)
        if not table:
            continue
        on = [v for d, v in table.items() if d in train_days]
        off = [v for d, v in table.items() if d not in train_days]
        if len(on) < 5 or len(off) < 5:
            continue
        pairs = [(1.0, v) for v in on] + [(0.0, v) for v in off]
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        med_on, med_off = _median(on), _median(off)
        delta = (med_on - med_off) if (med_on is not None and med_off is not None) else None
        spec = catalog.get(metric)
        noise = float(getattr(spec, "noise", 0.0) or 0.0)
        out.append(Association(
            key=f"training_vs_rest__{metric}",
            cn=f"训练日 vs 非训练日：{cn}",
            x_metric="workout", y_metric=metric, lag_days=0,
            n=len(pairs), rho=rho,
            groups=[
                {"label": "训练日", "x_median": 1.0, "y_median": med_on, "n": len(on)},
                {"label": "非训练日", "x_median": 0.0, "y_median": med_off, "n": len(off)},
            ],
            # 分级看的是「组间差异有没有超过该指标的测量噪声」，
            # 而不是相关系数——二元分组的 rho 不好解释，差异比噪声才是本项目一贯的判据。
            evidence=_grade_group_delta(delta, noise, len(on), len(off)),
            note="同一天比较；训练强度未分层。",
            delta=delta, noise=noise,
        ))
    return out


def _grade_group_delta(delta: Optional[float], noise: float,
                       n_on: int, n_off: int) -> str:
    """按「组间差异 ÷ 测量噪声」分级（启发式）。

    差异落在噪声内 → 一律 C（不解读）：这正是 DELTA_BELOW_NOISE 的同一套逻辑。
    """
    if delta is None:
        return "C"
    if noise > 0 and abs(delta) < noise:
        return "C"
    ratio = abs(delta) / noise if noise > 0 else float("inf")
    if ratio >= 3 and min(n_on, n_off) >= 30:
        return "A"
    if ratio >= 1 and min(n_on, n_off) >= 15:
        return "B"
    return "C"


# ──────────────────────────────────────────────────────────── 分析结果


@dataclass
class Analysis:
    """`analyze()` 的全部产物：趋势 + 画像 + 训练汇总 + 透传的 notes。"""

    trends: Dict[str, MetricTrend]
    profile: Dict[str, Any]
    span: Optional[Tuple[str, str]]
    span_days: int
    sources: List[str]
    notes: List[str]
    workout_summary: Dict[str, Any]
    # 睡眠规律性（缺睡眠时刻指标或夜数不足时为 None）
    sleep_regularity: Optional[SleepRegularity] = None
    # 指标之间的关联（相关，不是因果）。按证据等级排序。
    associations: List[Association] = field(default_factory=list)

    # ---------------------------------------------------------- 视图

    def ordered_metrics(self) -> List[str]:
        """指标按 CATEGORY_ORDER 归类后输出，类内保持首次出现顺序，未知指标排在最后。"""
        present = list(self.trends.keys())
        known = [m for m in present if catalog.get(m) is not None]
        unknown = [m for m in present if catalog.get(m) is None]
        out: List[str] = []
        for cat in catalog.categories_in_order(known):
            out.extend(m for m in known if catalog.get(m).category == cat)
        return out + sorted(unknown)

    def by_category(self) -> Dict[str, List[str]]:
        """类别 -> 指标列表，按 CATEGORY_ORDER 排序。"""
        buckets: Dict[str, List[str]] = {}
        for m in self.ordered_metrics():
            spec = catalog.get(m)
            cat = spec.category if spec else "其它"
            buckets.setdefault(cat, []).append(m)
        return buckets

    def out_of_ref(self) -> List[MetricTrend]:
        """verdict 为 low/high 的指标（只包含真正参与判定的指标）。"""
        return [self.trends[m] for m in self.ordered_metrics()
                if self.trends[m].verdict in (VERDICT_LOW, VERDICT_HIGH)]

    def enough_for_trend(self) -> List[MetricTrend]:
        """数据量足够谈趋势的指标（kind == "trend" 且 not insufficient）。"""
        return [self.trends[m] for m in self.ordered_metrics()
                if self.trends[m].kind == KIND_TREND and not self.trends[m].insufficient]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": dict(self.profile),
            "span": list(self.span) if self.span else None,
            "span_days": self.span_days,
            "sources": list(self.sources),
            "notes": list(self.notes),
            "workout_summary": self.workout_summary,
            "sleep_regularity": self.sleep_regularity.to_dict() if self.sleep_regularity else None,
            "associations": [a.to_dict() for a in self.associations],
            "trends": {m: self.trends[m].to_dict() for m in self.ordered_metrics()},
        }


# ──────────────────────────────────────────────────────────── 内部工具


def _parse_date(text: str) -> Optional[_date]:
    """把 'YYYY-MM-DD'（或带时间的字符串）解析成 date；解析不了返回 None。"""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    if len(s) > 10:
        s = s[:10]
    try:
        y, m, d = s.split("-")
        return _date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def _day_diff(a: Optional[str], b: Optional[str]) -> int:
    """两个日期字符串相差的天数（b - a）；任一无法解析则返回 0。"""
    da, db = _parse_date(a), _parse_date(b)
    if da is None or db is None:
        return 0
    return (db - da).days


def _safe_div(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _min_opt(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def _max_opt(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _mean_opt(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


# ──────────────────────────────────────────────────────────── 单个指标


NOTE_NO_REF = "无可用参考区间"
NOTE_MIXED_SOURCES = "来源不一致，不做判定"
NOTE_NOT_JUDGED = "该指标不参与判定"

#: 同一天有 ≥ 这么多天被两台以上设备记录，就算「并发」而不是「接替」。
#: 取 7 是为了容忍偶发重叠（例如某天同时戴两块表做交叉核验）。
CONCURRENT_DAY_THRESHOLD = 7
#: 一个「设备时代」至少要连续这么多条观测才算数，否则视为碎片并入前一段。
#: 这是启发式：目标是**提示**可能的接替点，不是精确定位。
MIN_ERA_OBS = 5


def source_structure(series: List[Observation]) -> Tuple[str, int, List[Tuple[str, str, str]]]:
    """判断一个指标的多来源是「并发」还是「接替」，并找出可能的设备接替点。

    返回 `(overlap, overlap_days, handovers)`，其中 handovers 是
    `[(新设备, 旧设备, 切换日期), ...]`。

    **这是启发式，请按启发式使用**：它回答的是"这里可能有换设备"，不是
    "某天确实换了设备"。宁可多提示（代价是一条 info），也不要漏——
    漏掉会让"换设备造成的台阶"被读成生理变化，而那正是本项目要防的错
    （实测：某份真实导出里 VO₂max 来自 3 台不同设备）。

    为什么要区分并发与接替：并发影响的是**数值口径**（同一段时间被两台设备
    各记一遍，累计类会双计），接替影响的是**跨时间可比性**（换设备造成台阶）。
    两者的处置完全不同。
    """
    if len(series) < 2:
        return "single", 0, []

    per_day: Dict[str, set] = {}
    for o in series:
        per_day.setdefault(o.date, set()).add(o.source or UNKNOWN_SOURCE)
    overlap_days = sum(1 for srcs in per_day.values() if len(srcs) > 1)

    ordered = sorted(series, key=lambda o: o.sort_key)
    seen: List[str] = []
    for o in ordered:
        s = o.source or UNKNOWN_SOURCE
        if s not in seen:
            seen.append(s)
    if len(seen) < 2:
        return "single", 0, []

    overlap = "concurrent" if overlap_days >= CONCURRENT_DAY_THRESHOLD else "sequential"

    # 切成「同一来源的连续段」，过短的碎片并入前一段（避免单日换设备造成假接替点）
    runs: List[List[Any]] = []
    for o in ordered:
        s = o.source or UNKNOWN_SOURCE
        if runs and runs[-1][0] == s:
            runs[-1][2] = o.date
            runs[-1][3] += 1
        else:
            runs.append([s, o.date, o.date, 1])

    merged: List[List[Any]] = []
    for r in runs:
        if r[3] < MIN_ERA_OBS and merged:
            merged[-1][2] = r[2]            # 并进前一段，不产生接替点
            merged[-1][3] += r[3]
            continue
        merged.append(r)

    # 合并短碎片之后，前后两段可能变成**同一来源**（A 长段 / B 两天的碎片 / A 长段），
    # 它们必须再并起来，否则会造出「A 接替 A」这种假接替点（踩过一次）。
    coalesced: List[List[Any]] = []
    for era in merged:
        if coalesced and coalesced[-1][0] == era[0]:
            coalesced[-1][2] = era[2]
            coalesced[-1][3] += era[3]
            continue
        coalesced.append(era)

    handovers = [(coalesced[i][0], coalesced[i - 1][0], coalesced[i][1])
                 for i in range(1, len(coalesced))]
    return overlap, overlap_days, handovers


def ref_for_observation(metric: str, obs: Optional[Observation],
                        series: List[Observation],
                        profile: Optional[Dict[str, Any]] = None) -> Tuple[Optional[RefRange], str]:
    """返回**该条观测**该用的参考区间及其来源。

    核心约束：**区间必须与观测值同源**。拿 A 设备的读数去套 B 设备的区间，
    正是本项目要防的"算对了但读错了"（例如家用秤的体脂率套 InBody 的区间）。

    优先级：本条自带 > 同一 source 最近一条自带 > 内置人群表 > 无。

    返回 `(ref, origin)`，origin 是 `"device"` / `"builtin"` / `"none"`。
    """
    if obs is not None:
        if obs.ref is not None:
            return obs.ref, REF_ORIGIN_DEVICE
        src = obs.source or ""
        for o in reversed(series):
            if (o.source or "") == src and o.ref is not None:
                return o.ref, REF_ORIGIN_DEVICE
    ref = catalog.builtin_ref(metric, profile or {})
    if ref is not None:
        return ref, REF_ORIGIN_BUILTIN
    return None, REF_ORIGIN_NONE


def _verdict_for(spec: Optional[catalog.MetricSpec], ref: Optional[RefRange],
                 value: Optional[float]) -> str:
    """按口径 2 计算 verdict；不参与判定的指标一律 na。"""
    if spec is not None and not spec.verdict:
        return VERDICT_NA
    if ref is None or value is None:
        return VERDICT_NA
    inside = ref.contains(value)
    if inside is None:
        return VERDICT_NA
    if inside:
        return VERDICT_IN
    if ref.high is not None and value > ref.high:
        return VERDICT_HIGH
    if ref.low is not None and value < ref.low:
        return VERDICT_LOW
    return VERDICT_NA


def _build_trend(metric: str, series: List[Observation], dataset: Dataset) -> MetricTrend:
    """把一个指标的观测序列算成 MetricTrend。任何异常都退回保守值。"""
    spec = catalog.get(metric)
    cn = spec.cn if spec else metric
    unit = spec.unit if spec else (series[-1].unit if series else "")
    category = spec.category if spec else "其它"
    agg = spec.agg if spec else AGG_SNAPSHOT
    precision = spec.precision if spec else 2

    values = [o.value for o in series]
    n = len(series)
    days = len({o.date for o in series})
    first = series[0] if n else None
    last = series[-1] if n else None
    span_days = _day_diff(first.date, last.date) if n >= 2 else 0
    # 同一天测多次时 span_days 为 0，此时按「有数据的天数」兜底，避免除零。
    agg_days = span_days if span_days > 0 else days

    # ── 判定用的值 ──────────────────────────────────────────
    #
    # 快照类（体重、静息心率）用**最新一次**测量 —— 那是"你现在是多少"。
    # 但按日累计类（步数、能量、距离）不能用最后一天：最后一天常常是
    # 导出当天的**部分数据**（实测：某份导出最后一天只有 2969 步，
    # 于是"未达目标"是拿半天数据判的），而且单日波动本来就大。
    # 对这类指标，判定基准应当是有数据那些天的**日均值**。
    judged_value = last.value if last else None
    verdict_basis = "last"
    if agg == AGG_SUM and values:
        judged_value = sum(values) / len(values)
        verdict_basis = "daily_mean"

    # ── 参考区间与判定 ──────────────────────────────────────
    sources_seen = dataset.sources_of(metric)
    # 与被判定的值同源；跨设备比较在这里就被挡住。
    ref, ref_origin = ref_for_observation(metric, last, series, dataset.profile)

    # 「哪一侧值得留意」以 catalog 的判断为准，覆盖区间自带的默认值。
    # 边界可以来自设备（InBody 印的「去脂体重 46.7~57.0」），但"高于这个上限是不是
    # 异常"是关于指标本身的问题——肌肉量大的人高于它恰恰是好事。让设备的区间决定
    # 这件事，就会把用户自己判断为正常的结果报成异常。
    if ref is not None and ref.concern == "both":
        _concern = catalog.ref_concern_of(metric)
        if _concern != "both":
            ref = replace(ref, concern=_concern)

    range_available = (ref is not None) or any(o.ref is not None for o in series)
    verdict = _verdict_for(spec, ref, judged_value)
    verdict_note = ""

    # ── 什么时候放弃判定 ────────────────────────────────────
    #
    # 早期版本是「只要同一指标有多个来源就不判定」。这个判据在 Apple 数据上
    # **触发过宽**：实测某份真实导出里 17 条提示有 15 条是「来源不一致，不做判定」，
    # 几乎所有可判定指标都被压掉——报告等于没有内容可交付。
    #
    # 错在哪里：判定算的是「最新值 vs **与它同源**的区间」，这件事多来源并不妨碍
    # （同源约束已经由 ref_for_observation 保证了）。多来源真正影响的是
    # **跨时间的可比性**，那属于趋势，不属于判定，由 DEVICE_HANDOVER 规则讲。
    #
    # 所以现在只在一种情况下放弃判定：**这个指标确实存在可用区间，但被判定的那条
    # 观测的来源提供不了它**——也就是"有尺子，却不能拿来量这个读数"。
    # 那才是真正说不清的情形，也正是「跨设备套区间」要挡的东西。
    if verdict == VERDICT_NA:
        if spec is not None and not spec.verdict:
            verdict_note = NOTE_NOT_JUDGED
        elif ref is None:
            verdict_note = NOTE_MIXED_SOURCES if range_available else NOTE_NO_REF

    if verdict == VERDICT_NA:
        # 不参与判定的指标：保留 ref 供展示，但不声称区间性质。
        ref_kind = ""
    else:
        ref_kind = ref.kind if ref is not None else ""

    source_overlap, overlap_days, handovers = source_structure(series)

    trend = MetricTrend(
        metric=metric, cn=cn, unit=unit, category=category, agg=agg,
        n=n, days=days, first=first, last=last,
        ref=ref, ref_origin=ref_origin, ref_kind=ref_kind, verdict=verdict,
        sources=sources_seen, span_days=span_days, verdict_note=verdict_note,
        verdict_value=judged_value, verdict_basis=verdict_basis,
        range_available=range_available,
        source_overlap=source_overlap, overlap_days=overlap_days, handovers=handovers,
    )

    trend.mean = _mean_opt(values)
    trend.median = float(statistics.median(values)) if values else None
    trend.min_value = _min_opt(values)
    trend.max_value = _max_opt(values)

    # ── 累计类：算分布与覆盖，不算「变化」 ──────────────────
    if agg in (AGG_SUM, AGG_MEAN):
        if agg == AGG_SUM:
            trend.daily_mean = _safe_div(sum(values), agg_days) if values and agg_days else None
        else:
            by_day = _daily_values(series, agg)
            trend.daily_mean = _mean_opt(list(by_day.values()))
        if span_days > 0:
            trend.coverage = min(1.0, days / float(span_days))
        trend.insufficient = days < AGG_MIN_DAYS
    else:
        # ── 快照类：首末差值 / 相对变化 / 折算速率 ──────────
        if n >= 2 and first is not None and last is not None:
            trend.delta = last.value - first.value
            if first.value != 0:
                trend.pct = trend.delta / abs(first.value) * 100.0
            if span_days > 0:
                trend.per_30d = trend.delta / span_days * 30.0
        trend.insufficient = n < 2 or span_days < TREND_MIN_DAYS

    if n >= 2 and span_days >= TREND_MIN_DAYS:
        trend.kind = KIND_TREND
    elif n == 1:
        trend.kind = KIND_SINGLE
    else:
        trend.kind = KIND_SPARSE
    return trend


def _daily_values(series: List[Observation], agg: str = AGG_SNAPSHOT) -> Dict[str, float]:
    """按日汇总一个指标的观测值。

    * snapshot / sum：同一天取时间上最后一条。解析器应当已经把当日累计量
      汇总成一条（sum 类一天只产生一条观测），这里只做兜底。
    * mean：同一天取当日平均（例如全天散点心率的日均值）。

    逐日平均用 mean 的语义，而不是「取最后一条」，否则报告里的
    「日均睡眠」会退化成「最后一晚的睡眠」。
    """
    order: List[str] = []
    acc: Dict[str, List[float]] = {}
    for o in series:
        if o.date not in acc:
            acc[o.date] = []
            order.append(o.date)
        acc[o.date].append(o.value)
    out: Dict[str, float] = {}
    for d in order:
        vals = acc[d]
        out[d] = (sum(vals) / len(vals)) if agg == AGG_MEAN else vals[-1]
    return out


# ──────────────────────────────────────────────────────────── 训练汇总


def _build_workout_summary(dataset: Dataset) -> Dict[str, Any]:
    """按运动类型聚合训练记录。缺值一律 None，不用 0 冒充。"""
    workouts = list(getattr(dataset, "workouts", None) or [])
    summary: Dict[str, Any] = {
        "total": len(workouts),
        "by_activity": {},
        "span": None,
        "avg_hr_overall": None,
        "has_data": bool(workouts),
    }
    if not workouts:
        return summary

    dates = [w.date for w in workouts if w.date]
    if dates:
        summary["span"] = (min(dates), max(dates))

    buckets: Dict[str, List[Any]] = {}
    for w in workouts:
        name = w.activity or "未记录活动"
        buckets.setdefault(name, []).append(w)

    by_activity: Dict[str, Dict[str, Any]] = {}
    for name in sorted(buckets.keys()):
        items = buckets[name]
        minutes = [w.duration_min for w in items if w.duration_min is not None]
        kms = [w.distance_km for w in items if w.distance_km is not None]
        kcals = [w.energy_kcal for w in items if w.energy_kcal is not None]
        hrs = [w.avg_hr for w in items if w.avg_hr is not None]
        by_activity[name] = {
            "count": len(items),
            "minutes": sum(minutes) if minutes else None,
            "km": sum(kms) if kms else None,
            "kcal": sum(kcals) if kcals else None,
            "avg_hr": _mean_opt(hrs),
        }
    summary["by_activity"] = by_activity

    # 总体平均心率：有记录时长的按分钟数加权，否则退回简单平均。
    weighted_num = 0.0
    weighted_den = 0.0
    plain: List[float] = []
    for w in workouts:
        if w.avg_hr is None:
            continue
        plain.append(w.avg_hr)
        if w.duration_min is not None and w.duration_min > 0:
            weighted_num += w.avg_hr * w.duration_min
            weighted_den += w.duration_min
    if weighted_den > 0:
        summary["avg_hr_overall"] = weighted_num / weighted_den
    elif plain:
        summary["avg_hr_overall"] = sum(plain) / len(plain)
    return summary


# ──────────────────────────────────────────────────────────── 入口


def analyze(dataset: Dataset) -> Analysis:
    """把 Dataset 变成 Analysis。空数据集也要能安全返回。"""
    observations = list(getattr(dataset, "observations", None) or [])

    grouped: Dict[str, List[Observation]] = {}
    order: List[str] = []
    for o in observations:
        if o.metric not in grouped:
            grouped[o.metric] = []
            order.append(o.metric)
        grouped[o.metric].append(o)

    trends: Dict[str, MetricTrend] = {}
    for metric in order:
        series = sorted(grouped[metric], key=lambda o: o.sort_key)
        if not series:
            continue
        try:
            trends[metric] = _build_trend(metric, series, dataset)
        except Exception:
            # 单个指标算不出来，不能拖垮整份分析：退化成最小可用的条目。
            spec = catalog.get(metric)
            trends[metric] = MetricTrend(
                metric=metric,
                cn=spec.cn if spec else metric,
                unit=spec.unit if spec else series[-1].unit,
                category=spec.category if spec else "其它",
                agg=spec.agg if spec else AGG_SNAPSHOT,
                n=len(series), days=len({o.date for o in series}),
                first=series[0], last=series[-1],
                sources=dataset.sources_of(metric),
                insufficient=True, kind=KIND_SPARSE,
            )

    span = dataset.date_span()
    span_days = _day_diff(span[0], span[1]) if span else 0

    def _table(metric: str) -> Dict[str, float]:
        return {o.date: o.value for o in dataset.series(metric)}

    try:
        sleep_regularity = _sleep_regularity_from(
            _table("sleep_onset_min"), _table("sleep_wake_min"), _table("sleep_mid_min"))
    except Exception:
        sleep_regularity = None
    try:
        associations = _build_associations(dataset, trends)
    except Exception:
        associations = []

    return Analysis(
        trends=trends,
        profile=dict(getattr(dataset, "profile", None) or {}),
        span=span,
        span_days=span_days,
        sources=dataset.all_sources(),
        notes=list(getattr(dataset, "notes", None) or []),
        workout_summary=_build_workout_summary(dataset),
        sleep_regularity=sleep_regularity,
        associations=associations,
    )
