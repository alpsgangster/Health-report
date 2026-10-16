"""数据陷阱规则层：这份数据里有哪些「会把结论读错」的地方。

这是本项目最有价值的一层。它的职责不是重新算数（那是 analysis 的事），
而是把 analysis 里那些**看起来没问题、其实不能直接下结论**的地方标出来，
让报告层先讲陷阱、再讲趋势。

severity 的语义（报告层按这个排序与配色，改这里等于改全项目的语气）
------------------------------------------------------------------

* `alert`：**这份数据的某个结论不可信**，或某项指标超出筛查阈值、
  需要专业评估。一次报告最多几条，必须是真正会改变结论的事。
* `warn`：读的时候要注意，可能会误读。例如跨设备比较、区间口径不一致。
* `info`：中性提示。例如某指标没有公认参考区间，或者变化量小于测量噪声，
  只做趋势解读、不做结论。

排序规则：`alert` > `warn` > `info`，同级按 code 字典序，结果稳定可复现。

规则总表（code -> severity）
----------------------------

| code | severity | 一句话 |
|---|---|---|
| SRC_MIXED | warn | 同一指标出现多个来源，跨来源差值不是真实变化 |
| RANGE_OUT | warn（goal 为 info） | 最新值落在参考区间外 |
| PCT_BASELINE_CAVEAT | info | 节段百分比跨时间不可比（永远成立的 caveat） |
| PCT_BASELINE_SHIFT | warn | 反推的隐含标准值漂移，证明设备那把尺子变了 |
| HYDRATION_AMPLIFIES | warn | 总水分超出上限，体重与去脂体重被一并抬高 |
| IMPEDANCE_SHIFT | warn | 分节段阻抗大幅漂移，测量条件变了 |
| DELTA_BELOW_NOISE | info | 变化量小于测量噪声，视为基本未变（合并一条） |
| TREND_TOO_SHORT | info | 点数或跨度不足，无法区分趋势与单次波动（合并一条） |
| LABEL_CONFLICT | warn | 设备文字判定与按区间算出的判定不一致 |
| MISSING_DAYS | warn | 按日统计的指标覆盖率过低，日均值被低估（合并一条） |
| SAME_DAY_CONFLICT | warn | 同一天同指标不同来源的差值超过噪声（合并一条） |
| MASS_BALANCE | warn | 体成分四项加总与体重对不上，该次测量不闭合 |
| DEVICE_HANDOVER | info | 时间线里有设备接替，跨切换点的变化不可比（合并一条） |

`alert` 目前没有规则占用：它是留给「筛查阈值明确越界且需要专业评估」这类
结论的档位，宁可空着，也不为了好看把 warn 提级。

边界要求
--------

* 空数据集、只有 1 个观测、日期格式不合法、`labels` 键名与预期不符——
  都不允许抛异常。`check()` 对空 `Dataset` 必须返回 `[]`。
* 每条 Finding 的 title / detail 必须带具体数字，不写「某指标偏高」这种废话。
* 禁止诊断性措辞：可以写「建议咨询医生」「值得进一步评估」，
  不可以写「你患了」「这说明你有」「建议服用」「建议剂量」。
* 注释与 docstring 用中文；字符串里不用全角引号，需要引号用「」。
* 注释里不写真实用户的体测数值：说明陷阱的结构即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import catalog
from .analysis import AGG_SUM, UNKNOWN_SOURCE, MetricTrend, Analysis, ref_for_observation
from .model import Dataset, Observation

__all__ = [
    "Finding",
    "check",
    "severity_rank",
    # 规则码
    "SRC_MIXED",
    "RANGE_OUT",
    "PCT_BASELINE_CAVEAT",
    "PCT_BASELINE_SHIFT",
    "HYDRATION_AMPLIFIES",
    "IMPEDANCE_SHIFT",
    "DELTA_BELOW_NOISE",
    "TREND_TOO_SHORT",
    "LABEL_CONFLICT",
    "MISSING_DAYS",
    "SAME_DAY_CONFLICT",
    "MASS_BALANCE",
    "DEVICE_HANDOVER",
    # 阈值常量
    "SEV_ALERT",
    "SEV_WARN",
    "SEV_INFO",
    "segment_pairs",
    "IMPEDANCE_ABS_THRESHOLD",
    "IMPEDANCE_PCT_THRESHOLD",
    "PCT_BASELINE_SHIFT_THRESHOLD",
    "MAX_PCT_SHIFT_EVIDENCE",
    "MISSING_DAYS_COVERAGE",
    "MAX_SAME_DAY_EVIDENCE",
    "MASS_BALANCE_TOLERANCE",
]

# ──────────────────────────────────────────────────────────── 规则码

SRC_MIXED = "SRC_MIXED"
RANGE_OUT = "RANGE_OUT"
PCT_BASELINE_CAVEAT = "PCT_BASELINE_CAVEAT"
PCT_BASELINE_SHIFT = "PCT_BASELINE_SHIFT"
HYDRATION_AMPLIFIES = "HYDRATION_AMPLIFIES"
IMPEDANCE_SHIFT = "IMPEDANCE_SHIFT"
DELTA_BELOW_NOISE = "DELTA_BELOW_NOISE"
TREND_TOO_SHORT = "TREND_TOO_SHORT"
LABEL_CONFLICT = "LABEL_CONFLICT"
MISSING_DAYS = "MISSING_DAYS"
SAME_DAY_CONFLICT = "SAME_DAY_CONFLICT"
MASS_BALANCE = "MASS_BALANCE"
DEVICE_HANDOVER = "DEVICE_HANDOVER"

# ──────────────────────────────────────────────────────────── 严重度

SEV_ALERT = "alert"
SEV_WARN = "warn"
SEV_INFO = "info"

#: 排序权重：alert 在前，同级按 code 字典序。
severity_rank: Dict[str, int] = {SEV_ALERT: 0, SEV_WARN: 1, SEV_INFO: 2}

# ──────────────────────────────────────────────────────────── 阈值

#: 分节段阻抗最近两次变化超过这个绝对值（Ω）即认为测量条件变了。
IMPEDANCE_ABS_THRESHOLD = 15.0
#: 或者相对变化超过这个百分比。
IMPEDANCE_PCT_THRESHOLD = 15.0
#: 反推出的「隐含标准值」相对变化超过这个比例，就证明设备的百分比基准变了。
#: 用 5%：这个量级已经不是四舍五入或读数精度能解释的了。
PCT_BASELINE_SHIFT_THRESHOLD = 0.05
#: PCT_BASELINE_SHIFT 的 evidence 最多列多少对。
MAX_PCT_SHIFT_EVIDENCE = 10
#: sum/mean 类指标覆盖率低于此值时，日均值会被系统性低估。
MISSING_DAYS_COVERAGE = 0.7
#: SAME_DAY_CONFLICT 的 evidence 最多列多少组。
MAX_SAME_DAY_EVIDENCE = 10
#: 体成分四项（水分+蛋白质+无机盐+体脂肪）与体重的最大允许偏差（kg）。
#: 这四项按定义应当加总为体重，超出这个量级说明该次测量本身不闭合。
MASS_BALANCE_TOLERANCE = 0.6

#: 分节段「绝对 kg」与「百分比」的配对表。百分比只作展示，跨时间不可比。
#
# 为什么不能像直觉那样「比较 kg 变化幅度与百分比变化幅度」：
# 两者并不必然脱钩。真实案例里，节段 kg 的相对变化与百分比的绝对变化幅度
# 几乎一致（看起来毫无问题），但反推出来的隐含基数已经悄悄漂移了。
# 所以判据要落在「隐含标准值」本身，而不是两个变化幅度之间的比值。
_SEGMENTS: Tuple[str, ...] = ("left_arm", "right_arm", "trunk", "left_leg", "right_leg")
segment_pairs: Tuple[Tuple[str, str], ...] = tuple(
    (f"{prefix}_{seg}", f"{prefix}_pct_{seg}")
    for prefix in ("lean", "fat")
    for seg in _SEGMENTS
)

#: 本来就不做趋势的类别（阻抗是解释用的原始读数，评分与建议不是测量结果）。
_NO_TREND_CATEGORIES = ("生物电阻抗", "设备建议与评分", "基本")

#: 设备文字判定 -> low / in / high。按优先级从高到低匹配包含关系。
_LABEL_VERDICTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("high", ("超重", "肥胖", "偏高", "过多", "过高", "超标", "超过", "偏高型",
              "过重", "高", "上等")),
    ("low", ("偏瘦", "不足", "过少", "偏低", "过低", "过轻", "偏轻", "少",
             "低", "瘦", "下等")),
    ("in", ("正常", "标准", "普通", "健康", "良好", "适中", "合适", "中等", "合格", "在范围")),
)

#: 区间性质 -> 一句话说明这个区间的语义（RANGE_OUT 的 detail 要用）。
_KIND_MEANING: Dict[str, str] = {
    "goal": "这是活动量目标，不是正常范围：没达到不等于身体异常，只说明当天/当期的活动量没到设定值。",
    "screen": "这是筛查阈值：它用来决定「要不要进一步评估」，不是诊断标准，超出不等于患病。",
    "range": "这是参考区间：它描述的是一般人群的常见范围，个体可以长期稳定地落在区间之外。",
}

#: 落在「不需要留意的那一侧」时，逐指标说明为什么这一侧不代表问题。
#: 没有列到的指标用一句通用说明兜底。
_BENIGN_SIDE: Dict[str, str] = {
    "resting_hr": "长期进行耐力训练的人静息心率本来就常低于 60 次/分，这是有氧能力较好的表现之一。",
    "oxygen_saturation": "腕式设备在运动、低温或佩戴松动时读数会偏低；偏高不构成风险提示。",
    "vo2max": "高于该常模水平说明有氧能力较好，是想要的結果，不是异常。",
    "smi": "高于该筛查阈值说明四肢骨骼肌量充足；只有低于它才意味着需要进一步评估。",
    "lean_body_mass": "去脂体重高于以一般人群为基准的上限，通常是肌肉量充足的表现。",
    "skeletal_muscle_mass": "骨骼肌量高于一般水平是训练效果的体现，不是异常。",
    "protein_mass": "体成分仪估算的蛋白质偏高，通常反映肌肉量充足。",
    "mineral_mass": "体成分仪估算的无机盐偏高，通常反映骨矿含量较好。",
    "body_fat_pct": "低于该区间的下限本身不构成风险提示；但如果同时伴随体重快速下降，值得留意。",
    "body_fat_mass": "脂肪量低于一般水平不构成风险提示；同样要结合体重变化一起看。",
    "total_body_water": "身体总水分低于设备区间时，通常与测量条件（进食、饮水、时段）有关，不必紧张。",
    "waist_circumference": "腰围低于该筛查阈值是好的一面，不需要处理。",
    "whr": "腰臀比低于该阈值不构成风险提示。",
    "visceral_fat_level": "内脏脂肪等级低是好的一面，不需要处理。",
    "visceral_fat_area": "内脏脂肪面积低于该阈值不构成风险提示。",
    "obesity_degree": "肥胖度低于该区间下限通常对应偏瘦，是否要留意取决于整体体重与体脂趋势。",
}


# ──────────────────────────────────────────────────────────── 数据结构


@dataclass
class Finding:
    """一条数据陷阱。

    code:     稳定的机器码（见上面的常量），报告层按它决定排版与图标。
    severity: alert | warn | info，见模块 docstring。
    title:    一句话结论，中文，必须带具体数字。
    detail:   2~4 句解释：为什么会这样、该怎么读、下一步看什么。
    metrics:  相关指标 ID。
    evidence: 结构化的支撑数据，供报告层或人工复核。
    """

    code: str
    severity: str
    title: str
    detail: str
    metrics: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "metrics": list(self.metrics),
            "evidence": dict(self.evidence),
        }


# ──────────────────────────────────────────────────────────── 格式化工具


def _fnum(value: Optional[float], precision: int = 2) -> str:
    """数字转字符串，去掉多余的尾零（1.50 -> 1.5，2.0 -> 2）。"""
    if value is None:
        return "—"
    try:
        s = f"{float(value):.{int(precision)}f}"
    except (TypeError, ValueError):
        return str(value)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _unit_of(metric: str, fallback: str = "") -> str:
    spec = catalog.get(metric)
    return spec.unit if spec else fallback


def _cn_of(metric: str) -> str:
    spec = catalog.get(metric)
    return spec.cn if spec else metric


def _noise_of(metric: str) -> float:
    spec = catalog.get(metric)
    try:
        return float(spec.noise) if spec else 0.0
    except (TypeError, ValueError):
        return 0.0


def _precision_of(metric: str) -> int:
    spec = catalog.get(metric)
    try:
        return int(spec.precision) if spec else 2
    except (TypeError, ValueError):
        return 2


def _val(trend: Optional[MetricTrend], precision: int = 2) -> str:
    if trend is None or trend.last is None:
        return "—"
    return _fnum(trend.last.value, precision)


def _ref_text(ref: Any) -> str:
    if ref is None:
        return "无"
    try:
        return ref.label()
    except Exception:
        return "无"


def _ordered_trends(analysis: Analysis) -> List[MetricTrend]:
    """按 category 顺序取全部趋势条目，保证输出稳定。"""
    try:
        metrics = analysis.ordered_metrics()
    except Exception:
        metrics = sorted(analysis.trends.keys())
    return [analysis.trends[m] for m in metrics if m in analysis.trends]


def _series_map(dataset: Dataset) -> Dict[str, List[Observation]]:
    grouped: Dict[str, List[Observation]] = {}
    for o in getattr(dataset, "observations", None) or []:
        grouped.setdefault(o.metric, []).append(o)
    for metric in grouped:
        grouped[metric].sort(key=lambda o: o.sort_key)
    return grouped


# ──────────────────────────────────────────────────────────── 规则 1


def _rule_src_mixed(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """同一指标出现多个数据来源：跨来源的差值不是真实变化。"""
    out: List[Finding] = []
    for trend in _ordered_trends(analysis):
        sources = list(trend.sources)
        if len(sources) < 2:
            continue
        precision = _precision_of(trend.metric)
        # 按日累计/平均的指标没有"首末差值"（delta 恒为 None），
        # 这时说「跨来源的首末差值 — 步不能当作真实变化」是没有信息量的废话。
        # 对它们来说真正的问题是：多台设备的分段记录会被叠加进同一个每日数字，
        # 于是「哪天戴了哪台设备」决定了当天的数值。
        if trend.is_aggregate:
            tail = (f"这类按日{('累计' if trend.agg == 'sum' else '平均')}的指标，"
                    f"多来源的影响是做加法的：两台设备同时记录时，当天数值会被叠加。"
                    f"所以「哪天戴了哪台」本身就会改变每天的数值。")
            head = (f"{trend.cn}（{trend.metric}）的 {trend.n} 条记录来自 "
                    f"{len(sources)} 个不同来源：{('、'.join(sources))}")
            detail_tail = (f"做法：固定一台作为主设备（`--prefer-source` 可以指定），"
                           f"或改用 `--source-policy primary` 让同一天只取一台设备的值；"
                           f"解析时会为这类指标写一条说明，里面给出不同口径下的总量差异。")
        else:
            tail = (f"跨来源的首末差值 "
                    f"{_fnum(trend.delta, precision)} {trend.unit} 不能当作真实变化")
            head = (f"{trend.cn}（{trend.metric}）的 {trend.n} 条记录来自 "
                    f"{len(sources)} 个不同来源：{('、'.join(sources))}")
            detail_tail = ("想看趋势就只看同一来源内部的变化，"
                           "跨来源先比同一天的差值（见 SAME_DAY_CONFLICT）。")
        out.append(Finding(
            code=SRC_MIXED,
            severity=SEV_WARN,
            title=f"{head}，{tail}",
            detail=(
                f"不同测量系统（例如家用体脂秤与 InBody 体成分仪、或 iPhone 与 Apple Watch）"
                f"的绝对值口径和系统偏差都不一样：同一时刻两台设备测同一个东西，"
                f"差个 1~3 kg（步数则可能差一倍）很常见。"
                f"这条指标共有 {trend.days} 天数据、来自 {('、'.join(sources))}，"
                f"如果按时间把它们连成一条线，看到的很可能是「换了设备」而不是「身体变了」。"
                f"{detail_tail}"
            ),
            metrics=[trend.metric],
            evidence={"metric": trend.metric, "sources": sources,
                      "n": trend.n, "days": trend.days, "agg": trend.agg,
                      "first": (trend.first.value if trend.first else None),
                      "last": (trend.last.value if trend.last else None)},
        ))
    return out


# ──────────────────────────────────────────────────────────── 规则 2


def _rule_range_out(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """最新值落在参考区间外。措辞随区间性质变化，绝不把目标说成异常。"""
    out: List[Finding] = []
    for trend in _ordered_trends(analysis):
        if trend.verdict not in ("low", "high"):
            continue
        ref = trend.ref
        if ref is None or trend.last is None:
            continue
        spec = catalog.get(trend.metric)
        if spec is not None and not spec.verdict:
            continue
        precision = _precision_of(trend.metric)
        # 判定用的是哪一个值：快照类是最新一次，累计类是日均值（见 MetricTrend 的注释）。
        # 这里必须跟着 analysis 的口径走，否则会拿"最后一天的部分数据"去说事。
        judged = trend.verdict_value
        if judged is None and trend.last is not None:
            judged = trend.last.value
        if judged is None:
            continue
        value = _fnum(judged, precision)
        low = trend.verdict == "low"
        kind = trend.ref_kind or ref.kind or "range"
        order = "低于" if low else "高于"
        side = f"参考下限" if low else f"参考上限"
        bound = ref.low if low else ref.high
        range_text = _ref_text(ref)
        source = ref.source or "来源未标注"
        date = trend.last.date

        # 落在「不需要留意的那一侧」时降为 info。
        # 这是本项目最容易制造焦虑的地方：静息心率中位 56、78.7% 的天数低于 60
        # （真实数据实测），旧口径会把几乎每一天都报成「超出筛查参考」。
        # 发这种告警的代价是，真正该看的那条提示被淹没。
        concern = getattr(ref, "concern", "both") or "both"
        benign_side = ((concern == "high" and low) or (concern == "low" and not low))

        if kind == "goal":
            severity = SEV_INFO
            head = "未达目标" if low else "高于目标"
            title = (f"{trend.cn} {value} {trend.unit} {head}"
                     f"（目标 {'≥' if low else '≤'}{_fnum(bound, precision)}，来源：{source}）")
            reading = f"这里说的是「没达到活动量目标」，与身体指标异常是两件事。{_KIND_MEANING['goal']}"
            watch = "把目标当成努力方向看即可，单日未达标不说明任何健康问题。"
        elif benign_side:
            severity = SEV_INFO
            title = (f"{trend.cn} {value} {trend.unit} {order}"
                     f"{'下限' if low else '上限'} {_fnum(bound, precision)}"
                     f"（区间 {range_text}，来源：{source}），这一侧通常不视为异常")
            reading = (
                f"这个指标只有{'偏高' if concern == 'high' else '偏低'}那一侧才是需要留意的信号；"
                f"本次落在{'偏低' if low else '偏高'}一侧。"
                f"{_BENIGN_SIDE.get(trend.metric, '这一侧在该指标上通常不代表问题。')}"
            )
            watch = ("所以这里不作为异常提示，只当作背景信息。"
                     "看趋势比看单次值有意义；若同时有不适症状，请咨询医生。")
        elif kind == "screen":
            severity = SEV_WARN
            title = (f"{trend.cn} {value} {trend.unit} 超出筛查参考范围"
                     f"（{side} {_fnum(bound, precision)}，区间 {range_text}，来源：{source}）")
            reading = f"{_KIND_MEANING['screen']}"
            watch = "单次超出不足以判断，结合趋势和身体感受看；若持续超出，建议咨询医生做进一步评估。"
        else:
            severity = SEV_WARN
            title = (f"{trend.cn} {value} {trend.unit} {order}参考"
                     f"{'下限' if low else '上限'} {_fnum(bound, precision)}"
                     f"（区间 {range_text}，来源：{source}）")
            reading = f"{_KIND_MEANING['range']}"
            watch = "单次超出不足以判断，结合趋势和身体感受看；若持续异常，建议咨询医生。"

        if trend.verdict_basis == "daily_mean":
            basis_txt = (f"本次取的是有数据那些天的日均值 {value} {trend.unit}"
                         f"（共 {trend.days} 天有数据）——"
                         f"按日累计的指标不能用最后一天判，那常常只是当天的部分数据。")
        else:
            basis_txt = f"本次取的是 {date} 的最新值 {value} {trend.unit}"
        detail = (
            f"这个区间来自「{source}」，性质是 "
            f"{'目标（goal）' if kind == 'goal' else ('筛查阈值（screen）' if kind == 'screen' else '参考区间（range）')}。"
            f"{reading}"
            f"{basis_txt}，共 {trend.n} 个点、跨度 {trend.span_days} 天。"
            f"{watch}"
        )
        out.append(Finding(
            code=RANGE_OUT,
            severity=severity,
            title=title,
            detail=detail,
            metrics=[trend.metric],
            evidence={
                "metric": trend.metric, "date": date,
                "value": judged, "unit": trend.unit,
                "verdict_basis": trend.verdict_basis or "last",
                "latest_value": (trend.last.value if trend.last else None),
                "verdict": trend.verdict,
                "ref": ref.to_dict(), "ref_origin": trend.ref_origin,
                "ref_kind": kind, "side": ("low" if low else "high"),
                "n": trend.n, "span_days": trend.span_days,
            },
        ))
    return out


# ──────────────────────────────────────────────────────────── 规则 3


def _obs_of(dataset: Dataset, metric: str) -> List[Observation]:
    """取某指标按时间升序排列的观测（内部用，避免重复分组）。"""
    series = [o for o in (getattr(dataset, "observations", None) or []) if o.metric == metric]
    series.sort(key=lambda o: o.sort_key)
    return series


def _rule_pct_baseline_caveat(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """3a：节段 kg 与百分比同时存在时，务必声明「百分比跨时间不可比」。

    这是永远成立的 caveat（与本次数据是否真的漂移无关），所以 severity 是 info。
    只要有配对就报一条，合并处理，不按节段刷屏。
    """
    pairs: List[Dict[str, Any]] = []
    for kg_metric, pct_metric in segment_pairs:
        kg = analysis.trends.get(kg_metric)
        pct = analysis.trends.get(pct_metric)
        if kg is None or pct is None:
            continue
        if kg.n < 1 or pct.n < 1:
            continue
        pairs.append({
            "kg_metric": kg_metric,
            "pct_metric": pct_metric,
            "cn": kg.cn,
            "kg_series": [[o.date, o.value] for o in _obs_of(dataset, kg_metric)],
            "pct_series": [[o.date, o.value] for o in _obs_of(dataset, pct_metric)],
        })
    if not pairs:
        return []

    names = [p["cn"] for p in pairs]
    head = "、".join(names[:4])
    more = "…" if len(names) > 4 else ""
    return [Finding(
        code=PCT_BASELINE_CAVEAT,
        severity=SEV_INFO,
        title=(f"节段肌肉量/脂肪量同时给了 kg 与百分比，但百分比跨时间不可比"
               f"（涉及 {len(pairs)} 项：{head}{more}）"),
        detail=(
            f"本份数据里 {len(pairs)} 个节段同时有绝对 kg 值和百分比值"
            f"（例如 {names[0]}）。"
            f"设备给出的百分比是「相对该节段标准值」的比值，而这个标准值属于设备内部的参考模型，"
            f"厂商会在固件或算法更新时改动它——所以同一个百分比数字在不同次测量之间不是同一把尺子。"
            f"跨时间比较必须只用 kg 值，百分比只在同一次测量内部用来做左右侧对比。"
            f"另外 kg 本身是生物电阻抗推算值，也受水合状态影响（见 HYDRATION_AMPLIFIES）。"
        ),
        metrics=[p["kg_metric"] for p in pairs] + [p["pct_metric"] for p in pairs],
        evidence={"pairs": pairs},
    )]


def _rule_pct_baseline_shift(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """3b：反推隐含标准值，证明设备的百分数基准真的变过。

    对每一对取各自最近两次测量，`baseline = kg / (pct / 100)`。
    若隐含标准值相对变化超过 5%，说明变的不是这个人，而是设备那把尺子。
    """
    hits: List[Dict[str, Any]] = []
    for kg_metric, pct_metric in segment_pairs:
        kg_series = analysis.trends.get(kg_metric)
        pct_series = analysis.trends.get(pct_metric)
        if kg_series is None or pct_series is None:
            continue
        kg_obs = _obs_of(dataset, kg_metric)
        pct_obs = _obs_of(dataset, pct_metric)
        if len(kg_obs) < 2 or len(pct_obs) < 2:
            continue
        kg_a, kg_b = kg_obs[-2], kg_obs[-1]
        pct_a, pct_b = pct_obs[-2], pct_obs[-1]
        # 百分比的日期必须落在两次 kg 测量附近才有配对意义；
        # 只要两侧各自至少两次测量即可（InBody 报告里两者同日产生）。
        if pct_a.value in (None, 0) or pct_b.value in (None, 0):
            continue
        try:
            baseline_a = kg_a.value / (pct_a.value / 100.0)
            baseline_b = kg_b.value / (pct_b.value / 100.0)
        except ZeroDivisionError:
            continue
        if not baseline_a:
            continue
        change = (baseline_b - baseline_a) / abs(baseline_a)
        if abs(change) <= PCT_BASELINE_SHIFT_THRESHOLD:
            continue
        hits.append({
            "kg_metric": kg_metric, "pct_metric": pct_metric,
            "cn": kg_series.cn,
            "date_a": kg_a.date, "kg_a": kg_a.value, "pct_a": pct_a.value,
            "baseline_a": baseline_a,
            "date_b": kg_b.date, "kg_b": kg_b.value, "pct_b": pct_b.value,
            "baseline_b": baseline_b,
            "baseline_change_pct": change * 100.0,
        })
    if not hits:
        return []

    hits.sort(key=lambda h: (-abs(h["baseline_change_pct"]), h["kg_metric"]))
    shown = hits[:MAX_PCT_SHIFT_EVIDENCE]
    h0 = shown[0]
    kp, pp = _precision_of(h0["kg_metric"]), _precision_of(h0["pct_metric"])
    display = shown[:4]
    head = "、".join(f"{h['cn']} {_fnum(abs(h['baseline_change_pct']), 1)}%" for h in display)
    if len(display) < len(shown):
        head += f"，共 {len(shown)} 项"
    elif len(hits) > len(shown):
        head += f"，另有 {len(hits) - len(shown)} 项未列出"
    return [Finding(
        code=PCT_BASELINE_SHIFT,
        severity=SEV_WARN,
        title=(f"{h0['cn']}的隐含标准值漂移 {_fnum(abs(h0['baseline_change_pct']), 1)}%："
               f"设备参考模型可能已变化，该节段百分比跨时间不可比"
               f"（共 {len(hits)} 项超出 {_fnum(PCT_BASELINE_SHIFT_THRESHOLD * 100, 0)}%：{head}）"),
        detail=(
            f"把 kg 与百分比对照，可以反推出设备换算时用的隐含基数："
            f"{h0['cn']} 在 {h0['date_a']} 是 {_fnum(h0['kg_a'], kp)} kg / {_fnum(h0['pct_a'], pp)}% "
            f"→ 基数 {_fnum(h0['baseline_a'], 2)} kg，"
            f"到 {h0['date_b']} 是 {_fnum(h0['kg_b'], kp)} kg / {_fnum(h0['pct_b'], pp)}% "
            f"→ 基数 {_fnum(h0['baseline_b'], 2)} kg，"
            f"变化 {_fnum(h0['baseline_change_pct'], 1)}%。"
            f"变的不是这个人，而是设备那把尺子（标准值本身被改动过，"
            f"本次共 {len(hits)} 个节段超出 {_fnum(PCT_BASELINE_SHIFT_THRESHOLD * 100, 0)}%）。"
            f"所以「{_fnum(h0['pct_a'], pp)}% 变成 {_fnum(h0['pct_b'], pp)}%」这种说法没有意义，"
            f"只能看 kg 值本身（见 PCT_BASELINE_CAVEAT）。"
            f"再提醒一层：kg 是生物电阻抗推算值，受水合状态影响（见 HYDRATION_AMPLIFIES），"
            f"要去掉设备因素，下次测量请固定时段与条件。"
        ),
        metrics=[h["kg_metric"] for h in shown] + [h["pct_metric"] for h in shown],
        evidence={"threshold": PCT_BASELINE_SHIFT_THRESHOLD,
                  "count": len(hits), "pairs": shown},
    )]


# ──────────────────────────────────────────────────────────── 规则 4


def _rule_hydration_amplifies(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """总水分超出参考上限：体重与去脂体重会被同时抬高。"""
    trend = analysis.trends.get("total_body_water")
    if trend is None or trend.last is None or trend.ref is None:
        return []
    ref = trend.ref
    if ref.high is None or trend.last.value <= ref.high:
        return []
    precision = _precision_of("total_body_water")
    value = _fnum(trend.last.value, precision)
    over = _fnum(trend.last.value - ref.high, precision)
    origin = "设备自带区间" if trend.ref_origin == "device" else "内置参考区间"
    return [Finding(
        code=HYDRATION_AMPLIFIES,
        severity=SEV_WARN,
        title=(f"身体总水分 {value} L 高于{origin}上限 {_fnum(ref.high, precision)} L"
               f"（超出 {over} L），同期的体重与去脂体重读数被一并抬高"),
        detail=(
            f"体成分仪用生物电阻抗推算成分，水分是它的推算基础之一："
            f"{trend.last.date} 的总水分 {value} L 比上限 {_fnum(ref.high, precision)} L 高 {over} L"
            f"（区间 {_ref_text(ref)}，来源：{ref.source or '未标注'}）。"
            f"总水分偏高（水肿、测量前饮水/进食/运动、未排空等）会同时把体重和去脂体重抬高，"
            f"所以同期的「体重下降」可能被放大，甚至方向被改写。"
            f"建议下次测量统一条件：晨起、空腹、排尿后、同一时段、同一台设备，测前静坐 5 分钟。"
        ),
        metrics=["total_body_water"],
        evidence={"metric": "total_body_water", "date": trend.last.date,
                  "value": trend.last.value, "ref": ref.to_dict(),
                  "ref_origin": trend.ref_origin,
                  "excess": trend.last.value - ref.high},
    )]


# ──────────────────────────────────────────────────────────── 规则 5


def _rule_impedance_shift(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """分节段阻抗在最近两次测量间大幅漂移：测量条件变了。"""
    hits: List[Dict[str, Any]] = []
    for trend in _ordered_trends(analysis):
        spec = catalog.get(trend.metric)
        if spec is None or spec.category != "生物电阻抗":
            continue
        if trend.n < 2 or trend.first is None or trend.last is None:
            continue
        first_v, last_v = trend.first.value, trend.last.value
        delta = last_v - first_v
        pct = (delta / abs(first_v) * 100.0) if first_v else None
        abs_hit = abs(delta) > IMPEDANCE_ABS_THRESHOLD
        pct_hit = pct is not None and abs(pct) > IMPEDANCE_PCT_THRESHOLD
        if not (abs_hit or pct_hit):
            continue
        hits.append({
            "metric": trend.metric, "cn": trend.cn, "date_first": trend.first.date,
            "date_last": trend.last.date, "first": first_v, "last": last_v,
            "delta": delta, "pct": pct, "abs_hit": abs_hit, "pct_hit": pct_hit,
        })
    if not hits:
        return []

    hits.sort(key=lambda h: (-(abs(h["pct"]) if h["pct"] is not None else 0.0), h["metric"]))
    shown = hits[:3]
    head = "、".join(
        f"{h['cn']} {_fnum(h['first'], 1)}→{_fnum(h['last'], 1)} Ω"
        f"（{_fnum(h['delta'], 1)} Ω，{_fnum(h['pct'], 1)}%）"
        for h in shown
    )
    more = f"，另有 {len(hits) - len(shown)} 个节段同样超出阈值" if len(hits) > len(shown) else ""
    return [Finding(
        code=IMPEDANCE_SHIFT,
        severity=SEV_WARN,
        title=(f"{len(hits)} 个节段的阻抗在最近两次测量间变化超过 "
               f"{_fnum(IMPEDANCE_ABS_THRESHOLD, 0)} Ω 或 {_fnum(IMPEDANCE_PCT_THRESHOLD, 0)}%：{head}{more}"),
        detail=(
            f"阻抗大幅漂移通常不是身体成分真的变了，而是测量条件变了：水合状态、皮肤温度、"
            f"电极接触与体位、测量时段都会直接影响读数。"
            f"本次共 {len(hits)} 个节段超出阈值（绝对值 > {_fnum(IMPEDANCE_ABS_THRESHOLD, 0)} Ω "
            f"或相对变化 > {_fnum(IMPEDANCE_PCT_THRESHOLD, 0)}%），"
            f"该节段的肌肉量/脂肪量读数可比性下降，跨这两次直接相减要谨慎。"
            f"下一步：确认两次测量的条件是否一致，必要时在相同条件下复测一次再比较。"
        ),
        metrics=[h["metric"] for h in hits],
        evidence={"thresholds": {"abs_ohm": IMPEDANCE_ABS_THRESHOLD,
                                 "pct": IMPEDANCE_PCT_THRESHOLD},
                  "segments": hits},
    )]


# ──────────────────────────────────────────────────────────── 规则 6


def _rule_delta_below_noise(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """变化量小于测量噪声，合并成一条：这些指标视为基本未变。"""
    hits: List[Dict[str, Any]] = []
    for trend in _ordered_trends(analysis):
        if trend.n < 2 or trend.delta is None:
            continue
        noise = _noise_of(trend.metric)
        if noise <= 0:
            continue
        if abs(trend.delta) < noise:
            hits.append({
                "metric": trend.metric, "cn": trend.cn, "delta": trend.delta,
                "noise": noise, "unit": trend.unit,
                "first": trend.first.value if trend.first else None,
                "last": trend.last.value if trend.last else None,
            })
    if not hits:
        return []

    hits.sort(key=lambda h: (abs(h["delta"]) / h["noise"] if h["noise"] else 0.0, h["metric"]))
    shown = hits[:3]
    head = "；".join(
        f"{h['cn']} {_fnum(h['delta'], _precision_of(h['metric']))} {h['unit']}"
        f"（噪声 {_fnum(h['noise'], _precision_of(h['metric']))} {h['unit']}）"
        for h in shown
    )
    more = f"；另有 {len(hits) - len(shown)} 个指标同样如此" if len(hits) > len(shown) else ""
    first_hit = shown[0]
    return [Finding(
        code=DELTA_BELOW_NOISE,
        severity=SEV_INFO,
        title=(f"{len(hits)} 个指标的变化小于测量噪声，视为基本未变：{head}{more}"),
        detail=(
            f"以 {first_hit['cn']} 为例：首末相差 "
            f"{_fnum(first_hit['delta'], _precision_of(first_hit['metric']))} {first_hit['unit']}，"
            f"而该指标的测量噪声是 {_fnum(first_hit['noise'], _precision_of(first_hit['metric']))} {first_hit['unit']}——"
            f"变化幅度还没到设备自身的重复测量误差量级（生物电阻抗、家用秤、腕式光学心率都有这个量级）。"
            f"所以不能把这么小的变化当成「减重成功」或「状态变差」的证据，它更可能是测量波动。"
            f"要判断是否真的变了，看跨度更长（至少 2~4 周）的趋势，以及同方向的多个指标是否一起动。"
        ),
        metrics=[h["metric"] for h in hits],
        evidence={"count": len(hits), "items": hits},
    )]


# ──────────────────────────────────────────────────────────── 规则 7


def _rule_trend_too_short(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """数据点或跨度不足，无法区分真实趋势与单次波动。"""
    hits: List[Dict[str, Any]] = []
    for trend in _ordered_trends(analysis):
        if not trend.insufficient:
            continue
        if trend.category in _NO_TREND_CATEGORIES:
            continue
        hits.append({
            "metric": trend.metric, "cn": trend.cn, "n": trend.n,
            "days": trend.days, "span_days": trend.span_days,
            "agg": trend.agg, "kind": trend.kind,
        })
    if not hits:
        return []

    hits.sort(key=lambda h: (h["span_days"], h["n"], h["metric"]))
    shown = hits[:6]
    head = "、".join(f"{h['cn']}（{h['n']} 个点 / 跨度 {h['span_days']} 天）" for h in shown)
    more = f"，另有 {len(hits) - len(shown)} 个指标同理" if len(hits) > len(shown) else ""
    return [Finding(
        code=TREND_TOO_SHORT,
        severity=SEV_INFO,
        title=(f"{len(hits)} 个指标的数据量不足以谈趋势：{head}{more}"),
        detail=(
            f"快照类指标（体重、体脂率这类）至少要有 2 个点、跨度 14 天以上，"
            f"按日累计或平均的指标（步数、能量、睡眠）至少要有 7 天数据，才谈得上「趋势」。"
            f"本次这些指标不满足：点数少或跨度短，两个点之间的差既可能是真实变化，"
            f"也可能是单次测量波动（同一个人的体重一天内差 1 kg 很常见）。"
            f"下一步：这几项先只当作单次记录看，等积累够 2~4 周数据再比较趋势。"
        ),
        metrics=[h["metric"] for h in hits],
        evidence={"count": len(hits), "items": hits},
    )]


# ──────────────────────────────────────────────────────────── 规则 8


def _label_verdict(text: str) -> Optional[str]:
    """把设备给的中文文字判定归到 low / in / high；认不出返回 None。"""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    for verdict, keywords in _LABEL_VERDICTS:
        for kw in keywords:
            if kw in s:
                return verdict
    return None


def _rule_label_conflict(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """设备自己的文字判定与按参考区间算出的判定不一致。"""
    labels = getattr(dataset, "labels", None) or {}
    if not labels:
        return []
    out: List[Finding] = []
    for key in sorted(labels.keys()):
        try:
            parts = str(key).split("@")
            raw_name = parts[0]
            day = parts[1] if len(parts) >= 2 else ""
            metric = catalog.resolve(raw_name)
            if not metric:
                continue
            label_value = labels[key]
            if isinstance(label_value, dict):
                label_value = label_value.get("text") or label_value.get("label") or ""
            label_verdict = _label_verdict(label_value)
            if label_verdict is None:
                continue

            trend = analysis.trends.get(metric)
            if trend is None or trend.ref is None:
                continue
            spec = catalog.get(metric)
            if spec is not None and not spec.verdict:
                continue

            # 取该天（没有就用最新）的观测值，用**该条观测自己的**参考区间重新判一次。
            #
            # 两条必须遵守的约束：
            #  1. 不能直接用 trend.ref —— 那个区间属于 trend.last 的来源，
            #     而这里的值可能来自另一台设备（跨设备套区间是典型的读错）。
            #  2. 同一天有多条记录时，**优先取自带参考区间的那一条**：
            #     文字判定和参考范围是同一份报告印出来的，
            #     所以带区间的那条就是"说出这句话的那台设备"。
            #     否则会拿 A 设备的判定去比 B 设备的数值（本规则自己的假阳性来源）。
            day_obs: Optional[Observation] = None
            if day:
                same_day = [o for o in (dataset.observations or [])
                            if o.metric == metric and o.date == day]
                if same_day:
                    same_day.sort(key=lambda o: o.sort_key)
                    with_ref = [o for o in same_day if o.ref is not None]
                    day_obs = (with_ref or same_day)[-1]
            value: Optional[float] = None
            if day_obs is not None:
                value = day_obs.value
            elif trend.last is not None and (not day or trend.last.date == day):
                day_obs = trend.last
                value = trend.last.value
            if value is None:
                continue

            ref, _ref_origin = ref_for_observation(
                metric, day_obs, dataset.series(metric), dataset.profile)
            if ref is None:
                continue

            computed = _verdict_of_value(ref, value)
            if computed is None or computed == label_verdict:
                continue

            precision = _precision_of(metric)
            out.append(Finding(
                code=LABEL_CONFLICT,
                severity=SEV_WARN,
                title=(f"设备给的 {trend.cn} 判定「{str(label_value).strip()}」与数值判定不一致"
                       f"（{trend.cn} {_fnum(value, precision)} {trend.unit}，"
                       f"区间 {_ref_text(ref)}，{day or (trend.last.date if trend.last else '')}）"),
                detail=(
                    f"设备在 {day or '该次'} 给出的文字结论是「{str(label_value).strip()}」"
                    f"（对应 {label_verdict}），而按同一份数据里的 {trend.cn} = {_fnum(value, precision)} {trend.unit} "
                    f"与区间 {_ref_text(ref)}（来源：{ref.source or '未标注'}）对照，落在 {computed} 一侧，两者不一致。"
                    f"这通常说明设备的判定阈值用的不是同一个标准，例如同一套 BMI 会因人群不同"
                    f"而把超重界值放在 23 或 24.9。"
                    f"至于以哪个为准，取决于你想对照哪一套标准——这里不做结论，"
                    f"建议先确认设备用的是哪套标准，再长期固定用同一套比较（不要每次换标准）。"
                ),
                metrics=[metric],
                evidence={
                    "key": str(key), "metric": metric, "date": day,
                    "label": str(label_value).strip(), "label_verdict": label_verdict,
                    "value": value, "unit": trend.unit,
                    "computed_verdict": computed,
                    "ref": ref.to_dict(), "ref_origin": trend.ref_origin,
                },
            ))
        except Exception:
            # 标签格式千奇百怪，单条解析失败就跳过，不影响其它规则。
            continue
    return out


def _verdict_of_value(ref: Any, value: Optional[float]) -> Optional[str]:
    """用给定区间判断一个值的 low / in / high；判断不了返回 None。"""
    if ref is None or value is None:
        return None
    try:
        inside = ref.contains(value)
    except Exception:
        return None
    if inside is None:
        return None
    if inside:
        return "in"
    if ref.high is not None and value > ref.high:
        return "high"
    if ref.low is not None and value < ref.low:
        return "low"
    return None


# ──────────────────────────────────────────────────────────── 规则 9


def _rule_missing_days(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """按日累计/平均的指标覆盖率过低：日均值会被系统性低估。"""
    hits: List[Dict[str, Any]] = []
    for trend in _ordered_trends(analysis):
        if trend.agg not in (AGG_SUM, "mean"):
            continue
        if trend.coverage is None:
            continue
        if trend.coverage < MISSING_DAYS_COVERAGE:
            hits.append({
                "metric": trend.metric, "cn": trend.cn,
                "days": trend.days, "span_days": trend.span_days,
                "coverage": trend.coverage, "agg": trend.agg,
                "daily_mean": trend.daily_mean, "unit": trend.unit,
                "n": trend.n,
            })
    if not hits:
        return []

    hits.sort(key=lambda h: (h["coverage"], h["metric"]))
    shown = hits[:4]
    head = "、".join(
        f"{h['cn']}（{h['days']}/{h['span_days']} 天，覆盖率 {_fnum(h['coverage'] * 100, 0)}%）"
        for h in shown
    )
    more = f"，另有 {len(hits) - len(shown)} 个指标同理" if len(hits) > len(shown) else ""
    # detail 里的例子也要取一个真正被列出的指标，不能引用没出现在标题里的项。
    example = shown[0]
    return [Finding(
        code=MISSING_DAYS,
        severity=SEV_WARN,
        title=(f"{len(hits)} 个按日统计的指标覆盖率低于 "
               f"{_fnum(MISSING_DAYS_COVERAGE * 100, 0)}%，日均值被低估：{head}{more}"),
        detail=(
            f"这些指标是按日累计或按日平均的，而日均值是按整个跨度折算的："
            f"缺数据的日子被当成 0 算进去了（设备未佩戴、没同步、漏记都会造成缺日）。"
            f"例如上表中覆盖率 {_fnum(example['coverage'] * 100, 0)}% 的 {example['cn']}，"
            f"它的日均值 {_fnum(example['daily_mean'], _precision_of(example['metric']))} {example['unit']} "
            f"并不代表「这些天平均就这么少」，只代表「整个跨度平均下来这么多」。"
            f"所以不要把这一段的日均值和前一段直接比较——先看覆盖率是否接近，"
            f"或者只比较都有数据的那些天。"
        ),
        metrics=[h["metric"] for h in hits],
        evidence={"threshold": MISSING_DAYS_COVERAGE, "count": len(hits), "items": hits},
    )]


# ──────────────────────────────────────────────────────────── 规则 10


def _rule_same_day_conflict(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """同一天同一指标来自不同来源，差值超过测量噪声。"""
    conflicts: List[Dict[str, Any]] = []
    for metric, series in _series_map(dataset).items():
        noise = _noise_of(metric)
        by_day: Dict[str, List[Observation]] = {}
        for o in series:
            by_day.setdefault(o.date, []).append(o)
        for day in sorted(by_day.keys()):
            items = by_day[day]
            if len(items) < 2:
                continue
            for i in range(len(items) - 1):
                a = items[i]
                for j in range(i + 1, len(items)):
                    b = items[j]
                    src_a, src_b = a.source or "未知来源", b.source or "未知来源"
                    if src_a == src_b:
                        continue
                    diff = b.value - a.value
                    if noise > 0 and abs(diff) <= noise:
                        continue
                    if noise <= 0 and abs(diff) == 0:
                        continue
                    conflicts.append({
                        "metric": metric, "date": day,
                        "source_a": src_a, "value_a": a.value,
                        "source_b": src_b, "value_b": b.value,
                        "diff": diff, "noise": noise,
                    })
    if not conflicts:
        return []

    conflicts.sort(key=lambda c: (c["metric"], c["date"], c["source_a"], c["source_b"]))
    shown = conflicts[:MAX_SAME_DAY_EVIDENCE]
    metric_names = []
    for c in conflicts:
        if c["metric"] not in metric_names:
            metric_names.append(c["metric"])
    head_items = []
    for c in shown[:3]:
        head_items.append(
            f"{_fnum(c['date'])} {_cn_of(c['metric'])}：{c['source_a']} "
            f"{_fnum(c['value_a'], _precision_of(c['metric']))} vs {c['source_b']} "
            f"{_fnum(c['value_b'], _precision_of(c['metric']))} "
            f"{_unit_of(c['metric'])}（差 {_fnum(c['diff'], _precision_of(c['metric']))}）"
        )
    c0 = shown[0]
    return [Finding(
        code=SAME_DAY_CONFLICT,
        severity=SEV_WARN,
        title=(f"{len(conflicts)} 组同一天同指标的双来源读数差值超过测量噪声："
               + "；".join(head_items)),
        detail=(
            f"同一天测同一个东西，两台设备的读数应该接近；本次差得比较明显"
            f"（例如 {c0['date']} 的 {_cn_of(c0['metric'])}：{c0['source_a']} "
            f"{_fnum(c0['value_a'], _precision_of(c0['metric']))} vs {c0['source_b']} "
            f"{_fnum(c0['value_b'], _precision_of(c0['metric']))}，差 "
            f"{_fnum(c0['diff'], _precision_of(c0['metric']))}，超过该指标的噪声 "
            f"{_fnum(c0['noise'], _precision_of(c0['metric']))}）。"
            f"这说明它们的系统偏差不可忽略，把两台设备的数字混在一条趋势线上看，"
            f"看到的可能是「换了设备」而不是身体变化。"
            f"做法：选一台作为主设备，长期只用它比趋势；另一台只在同一天做交叉核验。"
        ),
        metrics=metric_names,
        evidence={"count": len(conflicts), "items": shown,
                  "truncated": len(conflicts) > len(shown)},
    )]


# ──────────────────────────────────────────────────────────── 规则 11

#: 体成分质量守恒的四个组成项（顺序固定，用于标题与 evidence）。
_MASS_COMPONENTS: Tuple[str, ...] = (
    "total_body_water", "protein_mass", "mineral_mass", "body_fat_mass",
)


def _rule_mass_balance(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """体成分四项与体重是否闭合：水分 + 蛋白质 + 无机盐 + 体脂肪 应当等于体重。

    这是对设备/录入的**内部一致性校验**：经复核的真实报告里两次都精确吻合，
    所以平时什么都不报（不为了刷存在感而报 info）；一旦对不上，
    说明那一次测量本身有问题，那一天的其它数值也要打折看待。

    口径：**按来源分别校验**。同一天不同设备（体成分仪 / 家用秤）本来就
    不该混着加总，混起来算出的残差是规则自己的错误，不是数据的错误。
    """
    series = _series_map(dataset)
    # 五个指标都得存在，否则不硬算（缺指标时残差没有意义）。
    needed = _MASS_COMPONENTS + ("body_mass",)
    for m in needed:
        if m not in series or not series[m]:
            return []

    # source -> date -> metric -> value（同日多条取时间最后一条）
    by_source: Dict[str, Dict[str, Dict[str, float]]] = {}
    for m in needed:
        for o in series[m]:
            if not o.date:
                continue
            src = o.source or UNKNOWN_SOURCE
            by_source.setdefault(src, {}).setdefault(o.date, {})[m] = o.value

    rows: List[Dict[str, Any]] = []
    bad: List[Dict[str, Any]] = []
    for src in sorted(by_source.keys()):
        for day in sorted(by_source[src].keys()):
            values = by_source[src][day]
            if any(m not in values for m in needed):
                continue
            water = values["total_body_water"]
            protein = values["protein_mass"]
            mineral = values["mineral_mass"]
            fat = values["body_fat_mass"]
            mass = values["body_mass"]
            residual = abs((water + protein + mineral + fat) - mass)
            row = {"date": day, "source": src,
                   "water": water, "protein": protein, "mineral": mineral,
                   "fat": fat, "mass": mass, "residual": residual}
            rows.append(row)
            if residual > MASS_BALANCE_TOLERANCE:
                bad.append(row)
    if not rows or not bad:
        return []

    bad.sort(key=lambda r: (-r["residual"], r["date"], r["source"]))
    r0 = bad[0]
    total = r0["water"] + r0["protein"] + r0["mineral"] + r0["fat"]
    title = (f"{r0['date']} 的体成分数据不闭合：水分 {_fnum(r0['water'], 1)} + "
             f"蛋白质 {_fnum(r0['protein'], 1)} + 无机盐 {_fnum(r0['mineral'], 2)} + "
             f"体脂肪 {_fnum(r0['fat'], 1)} = {_fnum(total, 1)} kg，"
             f"报告体重 {_fnum(r0['mass'], 1)} kg，差 {_fnum(r0['residual'], 1)} kg"
             f"（来源：{r0['source']}）")
    if len(bad) > 1:
        title += f"，共 {len(bad)} 个日期不闭合"
    return [Finding(
        code=MASS_BALANCE,
        severity=SEV_WARN,
        title=title,
        detail=(
            f"体成分仪的报告是自洽的：身体总水分 + 蛋白质 + 无机盐 + 体脂肪按定义应当等于体重。"
            f"本次按来源分别检查了 {len(rows)} 个五项齐全的日期，其中 {len(bad)} 个偏差超过 "
            f"{_fnum(MASS_BALANCE_TOLERANCE, 1)} kg，最大的一个就是 {r0['date']}（{r0['source']}）的 "
            f"{_fnum(r0['residual'], 1)} kg。"
            f"加不起来通常说明这一次测量本身不可靠（电极接触、体位、设备状态），"
            f"或者数值在录入环节出了错；同一天的其它读数（体脂率、节段数据）也要一起打折看待。"
            f"下一步：先核对原始报告上这一天的纸面数值，确认不是录入问题，"
            f"然后在相同条件下复测一次。"
        ),
        metrics=list(_MASS_COMPONENTS) + ["body_mass"],
        evidence={"tolerance": MASS_BALANCE_TOLERANCE,
                  "checked_dates": len(rows),
                  "rows": rows},
    )]


# ──────────────────────────────────────────────────────────── 规则 13


def _rule_device_handover(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """时间线上存在设备接替：跨切换点的变化不可比。

    为什么单列一条规则：Apple 会把不同设备/不同时期的数据合并进同一条曲线，
    **而且不标注换设备**。于是"换设备造成的台阶"会被读成生理变化。
    实测：某份真实导出里 VO₂max 来自 3 台不同设备，静息心率来自 3 台，
    HRV 来自 3 台——这些指标近年的"变化"里有多少是设备差异，从曲线本身看不出来。

    合并成**一条** Finding（按接替点数量排序，最多列 6 个指标），
    否则在多设备用户的报告里会刷满一整页——那正是我们刚修掉的那个毛病。

    severity 取 info 而不是 warn：它是**读趋势时的口径提醒**，
    不是数据错误。而且 warn 要让位给真正需要注意的健康信号。
    """
    hits: List[Dict[str, Any]] = []
    for trend in _ordered_trends(analysis):
        if not trend.handovers:
            continue
        hits.append({
            "metric": trend.metric,
            "cn": trend.cn,
            "overlap": trend.source_overlap,
            "overlap_days": trend.overlap_days,
            "handovers": [list(h) for h in trend.handovers],
            "sources": list(trend.sources),
        })
    if not hits:
        return []

    hits.sort(key=lambda h: (-len(h["handovers"]), h["metric"]))
    shown = hits[:6]

    def _one(h: Dict[str, Any]) -> str:
        new, old, day = h["handovers"][0]
        more = f"（共 {len(h['handovers'])} 处）" if len(h["handovers"]) > 1 else ""
        return f"{h['cn']}：{day} 前后从「{old}」换到「{new}」{more}"

    head = "；".join(_one(h) for h in shown)
    tail = f"，另有 {len(hits) - len(shown)} 个指标同理" if len(hits) > len(shown) else ""

    return [Finding(
        code=DEVICE_HANDOVER,
        severity=SEV_INFO,
        title=f"{len(hits)} 个指标的时间线里有设备接替：{head}{tail}",
        detail=(
            "Apple 会把不同设备、不同时期的数据合并进同一条曲线，**并且不标注换了设备**。"
            "不同设备（甚至同一设备换了算法版本）对同一个生理量的基线读数并不相同，"
            "所以曲线在换设备处的那个台阶，很可能是**测量变了**而不是身体变了。"
            "实测某份真实导出里，最大摄氧量来自 3 台不同设备、静息心率来自 3 台、"
            "心率变异性来自 3 台——这些指标近年的变化里有多少是设备差异，"
            "单看曲线是分不出来的。"
            "读法：只在**同一台设备的时段内**比较趋势；跨切换点要谨慎。"
            "如果确实想知道换设备有没有影响，可以在换的时候新旧两台同时戴 1~2 周做重叠校验。"
            "本条是用启发式找出来的（连续段 + 主导设备变化），只作提示，不是断言。"
        ),
        metrics=[h["metric"] for h in hits],
        evidence={"count": len(hits), "items": shown,
                  "truncated": len(hits) > len(shown)},
    )]


# ──────────────────────────────────────────────────────────── 入口

_RULES = (
    _rule_src_mixed,
    _rule_range_out,
    _rule_pct_baseline_caveat,
    _rule_pct_baseline_shift,
    _rule_hydration_amplifies,
    _rule_impedance_shift,
    _rule_delta_below_noise,
    _rule_trend_too_short,
    _rule_label_conflict,
    _rule_missing_days,
    _rule_same_day_conflict,
    _rule_mass_balance,
    _rule_device_handover,
)


def check(dataset: Dataset, analysis: Analysis) -> List[Finding]:
    """跑全部规则，返回稳定排序的 Findings。

    对空 Dataset 返回 `[]`；任何单条规则内部出错都不会影响其它规则。
    """
    findings: List[Finding] = []
    if dataset is None or analysis is None:
        return findings
    for rule in _RULES:
        try:
            findings.extend(rule(dataset, analysis) or [])
        except Exception:
            # 规则层宁可少报一条，也不能让整份报告崩掉。
            continue
    findings.sort(key=lambda f: (severity_rank.get(f.severity, 9), f.code))
    return findings
