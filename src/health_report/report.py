"""报告生成层：把确定性计算结果渲染成一份可复核的 Markdown 报告。

三条设计约束
------------
1. **这里不算数。** 所有数值都来自 `analysis.Analysis` 与 `pitfalls.Finding`，
   报告层只负责排版、措辞与格式化。第五节的文字完全由这些字段拼出，
   不调用任何模型，也不做任何一次除法。
2. **免责声明紧跟标题、在任何结论之前。** 不允许出现"先给结论、末尾免责"的排版。
3. **语言模型的文字只进第六节**，并且必须标注来源；它不参与计算，
   也不影响第一到五节的任何内容。

`build_facts()` 产出的是**已经算好的结构化事实**（可直接 `json.dumps`），
绝不包含 `dataset.observations` 那种逐条原始数据。

关于 `lang`：`lang="zh"` 是完整语言；`lang="en"` 会切换标题、表头与结论措辞，
但 `DISCLAIMER` 常量与 `catalog` 里的类别名、指标名固定为中文。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import catalog
from .model import Dataset

# 同事在并行开发 analysis.py / pitfalls.py。这两个模块是本层的数据来源，
# 正常路径下直接使用它们的定义；缺失时退化成占位类型，让报告层仍可单独
# 导入与调试（报告层全程只按属性读取，不做 isinstance，因此占位类型不影响运行）。
try:  # pragma: no cover - 正常路径
    from .analysis import Analysis, MetricTrend, minutes_to_clock
except ImportError:  # pragma: no cover - 仅用于报告层单独调试

    class MetricTrend:  # type: ignore[no-redef]
        """占位类型：真实定义在 analysis.py。"""

    class Analysis:  # type: ignore[no-redef]
        """占位类型：真实定义在 analysis.py。"""

    def minutes_to_clock(minutes):  # type: ignore[misc]
        """占位实现：真实定义在 analysis.py。"""
        if minutes is None:
            return "—"
        total = int(round(18 * 60 + minutes)) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"


try:  # pragma: no cover - 正常路径
    from .pitfalls import Finding
except ImportError:  # pragma: no cover - 仅用于报告层单独调试

    class Finding:  # type: ignore[no-redef]
        """占位类型：真实定义在 pitfalls.py。"""


# ──────────────────────────────────────────────────────────── 常量

DISCLAIMER = (
    "本报告由程序根据你导入的数据自动生成，用于整理数值与趋势，"
    "它不是医学诊断，也不能替代医生、体检报告或任何专业医疗判断。"
    "报告里的参考区间来自设备说明书或公开的人群参考数据，只用来提示数值所处的位置；"
    "任何一项读数都可能受测量条件、设备型号、水合状态等因素影响，"
    "跨设备的差值不能直接当作真实变化。"
    "如果某项数值持续偏离参考区间，或你同时有不适症状，请带着原始数据咨询医生。"
)

# 这两类指标太细，主表不显示，只在附录 B 出现。
HIDDEN_CATEGORIES: Tuple[str, ...] = ("生物电阻抗", "设备建议与评分")

# 免责声明之后、任何结论之前出现的可信度图标。
_SEV_ICON: Dict[str, str] = {"alert": "🔴", "warn": "🟠", "info": "🔵"}
_SEV_RANK: Dict[str, int] = {"alert": 0, "warn": 1, "info": 2}

# 区间性质的中文说法。
_KIND_CN: Dict[str, str] = {"range": "正常参考区间", "goal": "活动目标", "screen": "筛查阈值"}

# 第五节"体成分与体重"关心的指标（按这个顺序输出）。
_BODY_METRICS: Tuple[str, ...] = (
    "body_mass",
    "body_fat_mass",
    "body_fat_pct",
    "lean_body_mass",
    "skeletal_muscle_mass",
)

# 第五节"不足以判断"要排除的类别（设备原始读数与设备自算结果没有判定意义）。
_INSUFFICIENT_EXCLUDE: Tuple[str, ...] = HIDDEN_CATEGORIES + ("基本",)

# 表格列数（用于分隔行）。
_TABLE_HEAD_ZH = "| 指标 | 最新值 | 参考区间 | 判定 | 变化 | 数据点 | 来源 |"
_TABLE_HEAD_EN = "| Metric | Latest | Reference | Verdict | Change | Points | Sources |"
_TABLE_SEP = "|---|---|---|---|---|---|---|"


# ──────────────────────────────────────────────────────────── 小工具


def _t(zh: str, en: str, lang: str = "zh") -> str:
    """按语言挑一句话。zh 是主语言。"""
    return en if str(lang).lower().startswith("en") else zh


def _g(obj: Any, name: str, default: Any = None) -> Any:
    """兼容 dataclass 与 dict 两种取字段方式，缺失或 None 都返回 default。

    同事的 Finding / MetricTrend 都是 dataclass，但报告层对 dict 形式同样容错，
    这样任何一个字段缺失或为 None 都不会让报告崩掉。
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        v = obj.get(name, default)
    else:
        v = getattr(obj, name, default)
    return default if v is None else v


def _esc(text: Any) -> str:
    """表格/正文安全化：竖线换成全角，换行折成空格。"""
    s = "" if text is None else str(text)
    s = s.replace("|", "｜").replace("\r", " ").replace("\n", " ").strip()
    return s


def _num_str(value: Optional[float], precision: int) -> Optional[str]:
    """按 precision 格式化数字，去掉多余尾零。None 返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    p = 0 if precision is None else max(0, int(precision))
    s = "{0:.{1}f}".format(f, p)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-", "-0"):
        s = "0"
    return s


def _fmt(value: Optional[float], precision: int = 1, unit: str = "", signed: bool = False) -> str:
    """把数值格式化成人类读的字符串。

    * `None` 或非有限值 → `—`
    * 小数位由 metric spec 的 precision 决定，precision 为 0 时不出现小数点
    * 去掉多余尾零（71.20 → 71.2，2.00 → 2）
    * unit 为 `—` 或空时不拼单位
    * `signed=True` 时正数前面补 `+`（用于变化列）
    """
    s = _num_str(value, precision)
    if s is None:
        return "—"
    if signed and s != "0" and not s.startswith("-"):
        s = "+" + s
    u = "" if unit is None or str(unit).strip() in ("", "—", "-") else str(unit)
    return (s + " " + u) if u else s


def _pct_str(pct: Optional[float]) -> str:
    """百分比固定一位小数并带符号：-3.0% / +1.2%。"""
    if pct is None:
        return ""
    try:
        f = float(pct)
    except (TypeError, ValueError):
        return ""
    if math.isnan(f) or math.isinf(f):
        return ""
    return "{0:+.1f}%".format(f)


def _hm(minutes: Optional[float]) -> str:
    """分钟 → 「Xh Ym」。缺失返回 —。"""
    if minutes is None:
        return "—"
    try:
        total = float(minutes)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(total) or total < 0:
        return "—"
    hours = int(total // 60)
    mins = int(round(total - hours * 60))
    if mins >= 60:
        hours += 1
        mins -= 60
    return "{0}h {1}m".format(hours, mins)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _jnum(value: Any) -> Optional[float]:
    """转成可 JSON 序列化的有限浮点数；做不到就返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _jint(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _deep_clean(obj: Any) -> Any:
    """把任意对象变成 `json.dumps` 一定吃得下的结构。

    tuple/set → list，dataclass → dict，其余不可序列化的对象 → str，
    NaN/Infinity → None。
    """
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        return {str(k): _deep_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_deep_clean(v) for v in obj]
    for attr in ("to_dict",):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return _deep_clean(fn())
            except Exception:
                break
    if hasattr(obj, "__dict__"):
        try:
            return _deep_clean(vars(obj))
        except Exception:
            pass
    return str(obj)


# ──────────────────────────────────────────────────────────── 指标取值


def _spec_of(metric: str) -> Optional[catalog.MetricSpec]:
    try:
        return catalog.get(metric or "")
    except Exception:
        return None


def _precision_of(metric: str, spec: Any = None) -> int:
    s = spec if spec is not None else _spec_of(metric)
    if s is None:
        return 1
    try:
        return max(0, int(getattr(s, "precision", 1) or 0))
    except (TypeError, ValueError):
        return 1


def _unit_of(metric: str, trend: Any = None, spec: Any = None) -> str:
    s = spec if spec is not None else _spec_of(metric)
    unit = getattr(s, "unit", "") if s is not None else ""
    if not unit:
        unit = _g(trend, "unit", "") or ""
    unit = str(unit or "").strip()
    return "" if unit in ("—", "-") else unit


def _cn_of(metric: str, trend: Any = None) -> str:
    cn = _g(trend, "cn", "")
    if cn:
        return str(cn)
    try:
        return catalog.display_name(metric or "")
    except Exception:
        return str(metric or "")


def _category_of(metric: str, trend: Any = None) -> str:
    cat = _g(trend, "category", "")
    if cat:
        return str(cat)
    s = _spec_of(metric)
    return str(getattr(s, "category", "") or "其它")


def _ref_label(ref: Any) -> str:
    """参考区间的人类可读描述，兼容 RefRange / dict / None。"""
    if ref is None:
        return "—"
    fn = getattr(ref, "label", None)
    if callable(fn):
        try:
            return _esc(fn() or "—")
        except Exception:
            return "—"
    if isinstance(ref, dict):
        text = ref.get("text") or ""
        if text:
            return _esc(text)
        low, high = ref.get("low"), ref.get("high")
        if low is not None and high is not None:
            return "{0}~{1}".format(_num_str(low, 2), _num_str(high, 2))
        if low is not None:
            return ">={0}".format(_num_str(low, 2))
        if high is not None:
            return "<{0}".format(_num_str(high, 2))
        return "—"
    return _esc(ref)


def _ref_kind(trend: Any, ref: Any = None) -> str:
    kind = _g(trend, "ref_kind", "")
    if not kind:
        kind = getattr(ref, "kind", "") if ref is not None else ""
    if not kind and isinstance(ref, dict):
        kind = ref.get("kind") or ""
    kind = str(kind or "").strip().lower()
    return kind if kind in ("range", "goal", "screen") else ""


def _ref_source(trend: Any, ref: Any = None, spec: Any = None) -> str:
    src = ""
    if ref is not None:
        src = getattr(ref, "source", "") or (ref.get("source", "") if isinstance(ref, dict) else "")
    if not src:
        s = spec if spec is not None else _spec_of(_g(trend, "metric", ""))
        src = getattr(s, "ref_source", "") if s is not None else ""
    return str(src or "")


# verdict == "na" 的原因 -> (中文, English)。键必须与 analysis.py 里的常量一致。
_VERDICT_NOTES = {
    "无可用参考区间": ("只做趋势", "trend only"),
    "来源不一致，不做判定": ("来源不一致，不做判定", "sources disagree, no verdict"),
    "该指标不参与判定": ("不参与判定", "not judged"),
}


def _is_benign_side(ref: Any, verdict: Any) -> bool:
    """这个「超出」是不是落在**不需要留意的那一侧**。

    例如静息心率 54（区间下限 60）：低对长期训练者是常态，不是异常。
    这一侧要降调处理，否则报告一边在第一节说"不是异常"，一边在表格里写
    「超出筛查参考」，两处自相矛盾。
    """
    concern = str(getattr(ref, "concern", "both") or "both").strip().lower()
    v = str(verdict or "").strip().lower()
    if v not in ("low", "high"):
        return False
    return (concern == "high" and v == "low") or (concern == "low" and v == "high")


def _verdict_text(verdict: Any, ref_kind: str, lang: str = "zh", note: str = "",
                  benign: bool = False) -> str:
    """判定措辞。必须区分区间性质：没达到步数目标不等于指标异常。

    `note`（来自 `MetricTrend.verdict_note`）区分「不判定」的三种原因，
    它们不能一律写成"只做趋势"：

      * `无可用参考区间`   -> 口径问题，写「只做趋势」即可
      * `来源不一致，不做判定` -> **数据问题**，必须原样显示出来，这是本次分析
                                 最要紧的提示之一，不能跟"只做趋势"混为一谈
      * `该指标不参与判定` -> 原样显示（生物电阻抗、设备建议与评分等）

    `benign`（见 `_is_benign_side`）表示落在不需要留意的那一侧，
    措辞要降调，不能写成「超出筛查参考」吓人。
    """
    v = str(verdict or "").strip().lower()
    k = str(ref_kind or "").strip().lower()
    if v not in ("in", "low", "high"):
        n = str(note or "").strip()
        pair = _VERDICT_NOTES.get(n)
        if pair is not None:
            return _t(pair[0], pair[1], lang)
        return _t("只做趋势", "trend only", lang)
    if v == "in":
        if k == "goal":
            return _t("已达目标", "goal met", lang)
        if k == "screen":
            return _t("在筛查参考内", "within screening reference", lang)
        return _t("在参考区间内", "within reference range", lang)
    if benign:
        return (_t("偏低（通常不视为异常）", "low (usually not a concern)", lang) if v == "low"
                else _t("偏高（通常不视为异常）", "high (usually not a concern)", lang))
    if k == "screen":
        return _t("超出筛查参考", "outside screening reference", lang)
    if k == "goal":
        return _t("未达目标", "goal not met", lang) if v == "low" else _t("高于目标", "above goal", lang)
    if v == "low":
        return _t("低于参考下限", "below reference lower bound", lang)
    return _t("高于参考上限", "above reference upper bound", lang)


def _noise_of(metric: str) -> float:
    s = _spec_of(metric)
    try:
        return float(getattr(s, "noise", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ──────────────────────────────────────────────────────────── 表格


def _change_cell(trend: Any, precision: int, unit: str, metric: str, lang: str) -> str:
    """变化列。

    snapshot（单点类）给首末差值 + 百分比；sum / mean（汇总类）给日均，
    因为首末差在日间波动大的指标上会严重失真。变化量小于 catalog 里的
    noise 阈值时视为测量噪声，只报数不解读。
    """
    agg = str(_g(trend, "agg", "snapshot") or "snapshot").strip().lower()
    if agg in ("sum", "mean"):
        # ⚠️ 这里必须和「判定」用的是同一个数。
        #
        # 有两个不同的"日均值"：
        #   mean       = 有数据那些天的平均（缺数据的日子**不参与**）
        #   daily_mean = 总和 ÷ 全部跨度天数（缺数据的日子按 0 算，偏保守）
        # 判定的基准是 mean（缺数据不等于那天没走路）。如果这里显示 daily_mean，
        # 表格就会出现「日均 6230 步 / 已达目标（目标 ≥8000）」这种自相矛盾——
        # 读者看到的数和被拿去比较的数不是同一个。
        avg = _jnum(_g(trend, "mean", None))
        if avg is None:
            avg = _jnum(_g(trend, "daily_mean", None))
        if avg is None:
            return "—"
        txt = _t("有数据日均 {0}".format(_fmt(avg, precision, unit)),
                 "avg over days with data {0}".format(_fmt(avg, precision, unit)), lang)
        # 覆盖率低时必须说明，否则读者会把"有数据日均"当成整段时间的真实水平
        cov = _jnum(_g(trend, "coverage", None))
        days = _jint(_g(trend, "days", 0))
        span = _jint(_g(trend, "span_days", 0))
        if agg == "sum" and cov is not None and cov < 0.9 and span > 0:
            txt += _t("（覆盖 {0}/{1} 天）".format(days, span),
                      " (covers {0}/{1} days)".format(days, span), lang)
        return txt
    n = _jint(_g(trend, "n", 0))
    delta = _jnum(_g(trend, "delta", None))
    if n < 2 or delta is None:
        return "—"
    noise = _noise_of(metric)
    txt = _fmt(delta, precision, unit, signed=True)
    if noise > 0 and abs(delta) < noise:
        return txt + _t("（噪声内）", " (within noise)", lang)
    pct = _pct_str(_jnum(_g(trend, "pct", None)))
    if pct:
        return "{0}（{1}）".format(txt, pct) if lang != "en" else "{0} ({1})".format(txt, pct)
    return txt


def _points_cell(trend: Any, lang: str) -> str:
    n = _jint(_g(trend, "n", 0))
    days = _jint(_g(trend, "days", 0))
    if days and days != n:
        return _t("{0} 点/{1} 天".format(n, days), "{0} pts / {1} days".format(n, days), lang)
    return str(n)


def _sources_cell(trend: Any, lang: str) -> str:
    sources = _g(trend, "sources", None) or []
    if isinstance(sources, str):
        sources = [sources]
    names = [_esc(s) for s in sources if str(s).strip()]
    if not names:
        return _t("未记录", "not recorded", lang)
    txt = " / ".join(names)
    if len(names) > 1:
        txt += " ⚠️"
    return txt


def _trend_cells(trend: Any, lang: str = "zh") -> List[str]:
    """一个 MetricTrend → 一行表格单元格。"""
    metric = str(_g(trend, "metric", "") or "")
    spec = _spec_of(metric)
    precision = _precision_of(metric, spec)
    unit = _unit_of(metric, trend, spec)
    cn = _cn_of(metric, trend)
    last = _g(trend, "last", None)
    latest_val = _jnum(_g(last, "value", None)) if last is not None else None
    ref = _g(trend, "ref", None)
    kind = _ref_kind(trend, ref)
    verdict = _verdict_text(_g(trend, "verdict", "na"), kind, lang,
                            _g(trend, "verdict_note", ""),
                            _is_benign_side(ref, _g(trend, "verdict", "na")))
    ref_cell = _ref_label(ref) if ref is not None else "—"
    if ref is not None and kind == "goal":
        ref_cell += _t("（目标）", " (goal)", lang)
    elif ref is not None and kind == "screen":
        ref_cell += _t("（筛查）", " (screening)", lang)
    return [
        "**" + _esc(cn) + "**",
        _fmt(latest_val, precision, unit),
        ref_cell,
        _esc(verdict),
        _esc(_change_cell(trend, precision, unit, metric, lang)),
        _points_cell(trend, lang),
        _sources_cell(trend, lang),
    ]


def _metric_table(trends: Sequence[Any], lang: str = "zh") -> str:
    """内部表格渲染（带语言开关）。"""
    rows = [t for t in (trends or []) if t is not None]
    if not rows:
        return _t("（本节没有可展示的指标。）", "(No metrics to show in this section.)", lang)
    lines = [
        _TABLE_HEAD_EN if str(lang).lower().startswith("en") else _TABLE_HEAD_ZH,
        _TABLE_SEP,
    ]
    for t in rows:
        lines.append("| " + " | ".join(_trend_cells(t, lang)) + " |")
    return "\n".join(lines)


def render_metric_table(trends: list) -> str:
    """把一组 MetricTrend 渲染成 Markdown 表格（中文表头）。"""
    return _metric_table(trends, "zh")


# ──────────────────────────────────────────────────────────── 集合整理


def _trends_of(analysis: Any) -> Dict[str, Any]:
    trends = _g(analysis, "trends", None) or {}
    if not isinstance(trends, dict):
        return {}
    return {str(k): v for k, v in trends.items() if v is not None}


def _findings_of(findings: Any) -> List[Any]:
    if not findings:
        return []
    if isinstance(findings, dict):
        findings = [findings]
    return [f for f in findings if f is not None]


def _sev(finding: Any) -> str:
    s = str(_g(finding, "severity", "info") or "info").strip().lower()
    return s if s in _SEV_ICON else "info"


def _ordered_metrics(analysis: Any, trends: Dict[str, Any]) -> List[str]:
    """指标顺序：优先用同事的 ordered_metrics()，否则按 category 顺序兜底。"""
    fn = getattr(analysis, "ordered_metrics", None)
    if callable(fn):
        try:
            got = [str(m) for m in (fn() or []) if m is not None]
            got = [m for m in got if m in trends]
            if got:
                return got
        except Exception:
            pass
    order_index = {c: i for i, c in enumerate(catalog.CATEGORY_ORDER)}
    return sorted(
        trends.keys(),
        key=lambda m: (order_index.get(_category_of(m, trends.get(m)), len(order_index)),
                       _cn_of(m, trends.get(m))),
    )


def _by_category(analysis: Any, trends: Dict[str, Any]) -> Dict[str, List[str]]:
    """按类别分组，顺序遵循 CATEGORY_ORDER。"""
    fn = getattr(analysis, "by_category", None)
    if callable(fn):
        try:
            got = fn()
            if isinstance(got, dict) and got:
                out: Dict[str, List[str]] = {}
                for cat, ids in got.items():
                    keep = [str(m) for m in (ids or []) if str(m) in trends]
                    if keep:
                        out[str(cat)] = keep
                if out:
                    return _sort_categories(out)
        except Exception:
            pass
    groups: Dict[str, List[str]] = {}
    for metric in _ordered_metrics(analysis, trends):
        groups.setdefault(_category_of(metric, trends.get(metric)), []).append(metric)
    return _sort_categories(groups)


def _sort_categories(groups: Dict[str, List[str]]) -> Dict[str, List[str]]:
    order_index = {c: i for i, c in enumerate(catalog.CATEGORY_ORDER)}
    keys = sorted(groups.keys(), key=lambda c: (order_index.get(c, len(order_index)), str(c)))
    return {k: groups[k] for k in keys}


def _is_none_ref(trend: Any) -> bool:
    """「只做趋势」的指标：这个指标**根本没有任何可用区间**。

    判据是「有没有任何可用区间」，不是「本次判定用的 ref 是不是空」——这两件事不同：

      * 体重：设备（InBody）给过参考范围，但最新读数来自另一台没给区间的设备，
        于是 `ref=None` 却 `range_available=True`。**不能**列进这一节：
        设备明明印着参考范围，说"没有可用的参考区间"会让人以为报告漏读了他的报告。
        真正的原因是来源不一致，那由 `SRC_MIXED` 在第一节讲。
      * HRV(SDNN)：任何设备都没给过区间，内置表也没有 -> `range_available=False`
        -> 列进这一节，原因取 catalog 的 `ref_note`。这是有价值的提醒。
    """
    avail = _g(trend, "range_available", None)
    if avail is not None:
        return not bool(avail)
    # 退回按 ref_origin 判断（手工构造的 MetricTrend 没有这个字段）
    origin = str(_g(trend, "ref_origin", "") or "").strip().lower()
    if origin == "none":
        return True
    if not origin and _g(trend, "ref", None) is None:
        return True
    return False


def _out_of_ref(analysis: Any, trends: Dict[str, Any]) -> List[Any]:
    """超出参考区间的指标（优先用同事的 out_of_ref()）。"""
    got = None
    fn = getattr(analysis, "out_of_ref", None)
    if callable(fn):
        try:
            got = list(fn() or [])
        except Exception:
            got = None
    if got is None:
        got = [t for t in trends.values() if str(_g(t, "verdict", "")).lower() in ("low", "high")]
    out: List[Any] = []
    seen = set()
    for t in got:
        if t is None:
            continue
        metric = str(_g(t, "metric", "") or "")
        if metric and metric in trends:
            t = trends[metric]
        if not metric or metric in seen:
            continue
        if _category_of(metric, t) in HIDDEN_CATEGORIES:
            continue
        seen.add(metric)
        out.append(t)
    return out


def _insufficient(analysis: Any, trends: Dict[str, Any]) -> List[Any]:
    """数据点不足、无法判断的指标（排除生物电阻抗、设备建议与评分、基本项）。

    判定完全跟随 `MetricTrend.insufficient` 这个字段，不在这里二次推断，
    避免把「点少但结论明确」的指标误判成数据不足。
    """
    out: List[Any] = []
    for metric, trend in trends.items():
        if _category_of(metric, trend) in _INSUFFICIENT_EXCLUDE:
            continue
        if bool(_g(trend, "insufficient", False)):
            out.append(trend)
    return out


def _data_span(dataset: Dataset, analysis: Any) -> Optional[Tuple[str, str]]:
    span = _g(analysis, "span", None)
    if isinstance(span, (list, tuple)) and len(span) >= 2 and span[0] and span[1]:
        return (str(span[0]), str(span[1]))
    try:
        got = dataset.date_span()
    except Exception:
        got = None
    return (str(got[0]), str(got[1])) if got else None


def _span_days(dataset: Dataset, analysis: Any, span: Optional[Tuple[str, str]]) -> int:
    days = _jint(_g(analysis, "span_days", 0))
    if days > 0:
        return days
    if not span:
        return 0
    try:
        a = datetime.strptime(span[0][:10], "%Y-%m-%d")
        b = datetime.strptime(span[1][:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0
    return max(0, (b - a).days)


def _sources_text(analysis: Any, dataset: Dataset, lang: str) -> str:
    sources = _g(analysis, "sources", None)
    if not sources:
        try:
            sources = dataset.all_sources()
        except Exception:
            sources = []
    names = [str(s) for s in (sources or []) if str(s).strip()]
    if not names:
        return _t("未记录", "not recorded", lang)
    return " / ".join(names)


def _files_text(dataset: Dataset) -> str:
    files = [str(f) for f in (getattr(dataset, "files", None) or []) if str(f).strip()]
    return "、".join(files) if files else "未记录"


# ──────────────────────────────────────────────────────────── 第一节辅助


def _reason_text(metric: str, trend: Any, lang: str) -> str:
    """只做趋势指标的原因：优先用 catalog 里的 ref_note，否则给通用说明。"""
    spec = _spec_of(metric)
    note = str(getattr(spec, "ref_note", "") or "") if spec is not None else ""
    if not note:
        note = _t(
            "没有可用的参考区间，只报数值与趋势，不下正常或异常的判断。",
            "No usable reference range: value and trend are reported without a normal/abnormal verdict.",
            lang,
        )
    return note


def _reason_lines(metrics: List[str], trends: Dict[str, Any], lang: str) -> List[str]:
    """只做趋势指标的分组说明。

    一开始是一行一个指标，结果真实数据上出现了 12 行一字不差的
    「没有可用的参考区间，只报数值与趋势」——把一节有用的提示变成了噪声墙，
    读者会直接跳过。所以按**原因**分组：
      * 原因只对应 1 个指标 -> `- **指标名**：原因`
      * 原因对应多个指标     -> `- **原因**：指标1、指标2、…（共 N 项）`
    这样特有的原因（例如 HRV 那句「没有公认的通用正常范围」）仍然单独成行、
    不会被通用说明淹没，而通用说明只占一行。
    """
    groups: Dict[str, List[str]] = {}
    for m in metrics:
        groups.setdefault(_reason_text(m, trends.get(m), lang), []).append(m)

    # 通用的那条排最后，让有信息量的特有原因先出现
    generic = _t("没有可用的参考区间，只报数值与趋势，不下正常或异常的判断。",
                 "No usable reference range: value and trend are reported without a "
                 "normal/abnormal verdict.", lang)
    keys = [k for k in groups if k != generic] + ([generic] if generic in groups else [])

    lines: List[str] = []
    for note in keys:
        names = [_cn_of(m, trends.get(m)) for m in groups[note]]
        if len(names) == 1:
            lines.append("- **" + _esc(names[0]) + "**：" + _esc(note))
        else:
            joined = "、".join("**" + _esc(n) + "**" for n in names)
            lines.append(_t(
                "- {0}（共 {1} 项）：{2}".format(joined, len(names), _esc(note)),
                "- {0} ({1} metrics): {2}".format(joined, len(names), _esc(note)),
                lang))
    return lines


# ──────────────────────────────────────────────────────────── build_facts


def _metric_fact(metric: str, trend: Any, lang: str = "zh") -> Dict[str, Any]:
    """一个指标 → 给语言模型的事实卡片（只有算好的数字，没有原始观测）。"""
    spec = _spec_of(metric)
    precision = _precision_of(metric, spec)
    unit = _unit_of(metric, trend, spec)
    ref = _g(trend, "ref", None)
    kind = _ref_kind(trend, ref)
    origin = str(_g(trend, "ref_origin", "") or "").strip().lower() or ("device" if ref is not None else "none")
    first = _g(trend, "first", None)
    last = _g(trend, "last", None)
    sources = _g(trend, "sources", None) or []
    if isinstance(sources, str):
        sources = [sources]
    latest = _jnum(_g(last, "value", None)) if last is not None else None
    return {
        "metric": metric,
        "cn": _cn_of(metric, trend),
        "unit": unit,
        "category": _category_of(metric, trend),
        "agg": str(_g(trend, "agg", "snapshot") or "snapshot"),
        "latest": latest,
        "latest_date": str(_g(last, "date", "") or "") or None,
        "first": _jnum(_g(first, "value", None)) if first is not None else None,
        "delta": _jnum(_g(trend, "delta", None)),
        "pct": _jnum(_g(trend, "pct", None)),
        "per_30d": _jnum(_g(trend, "per_30d", None)),
        "mean": _jnum(_g(trend, "mean", None)),
        "daily_mean": _jnum(_g(trend, "daily_mean", None)),
        "n": _jint(_g(trend, "n", 0)),
        "days": _jint(_g(trend, "days", 0)),
        # 覆盖率（0~1）：有数据的天数 ÷ 总跨度天数。
        # 为什么要放进事实摘要：实测模型会自己用 days/span_days 算占比，
        # 然后写「约 53~56%」这种**自己算出来的数**——数字溯源校验无法认它，
        # 整句就被删了。把占比直接给它，模型就只需要叙述、不需要计算。
        "coverage": _jnum(_g(trend, "coverage", None)),
        "precision": precision,
        "ref_label": _ref_label(ref) if ref is not None else "",
        "ref_kind": kind,
        "ref_origin": origin,
        "ref_source": _ref_source(trend, ref, spec),
        "verdict": str(_g(trend, "verdict", "na") or "na").strip().lower(),
        "verdict_note": str(_g(trend, "verdict_note", "") or ""),
        "verdict_text": _verdict_text(_g(trend, "verdict", "na"), kind, "zh",
                                       _g(trend, "verdict_note", ""),
                                       _is_benign_side(ref, _g(trend, "verdict", "na"))),
        # 哪一侧才需要留意。给模型这个字段，它才不会把"静息心率低于下限"
        # 或"去脂体重高于上限"写成需要处理的问题。
        "ref_concern": str(getattr(ref, "concern", "both") or "both"),
        "benign_side": _is_benign_side(ref, _g(trend, "verdict", "na")),
        "sources": [str(s) for s in sources if str(s).strip()],
        "kind": str(_g(trend, "kind", "") or ""),
        "noise": _noise_of(metric),
    }


def build_facts(dataset: Dataset, analysis: Analysis, findings: list) -> Dict[str, Any]:
    """产出给语言模型的结构化事实。

    这里给的是**已经算好的结论级数字**：最新值、首值、变化量、日均、判定、
    参考区间。逐条原始观测（`dataset.observations`）绝不会出现在返回值里——
    那是用户的原始数据，模型不需要，也不该看到。
    返回值保证可以 `json.dumps(..., ensure_ascii=False)`。
    """
    ds = dataset if dataset is not None else Dataset()
    trends = _trends_of(analysis)
    fs = _findings_of(findings)
    order = _ordered_metrics(analysis, trends)
    span = _data_span(ds, analysis)

    metrics: List[Dict[str, Any]] = []
    for metric in order:
        trend = trends.get(metric)
        if _category_of(metric, trend) in HIDDEN_CATEGORIES:
            continue
        metrics.append(_metric_fact(metric, trend))

    out_of_ref: List[Dict[str, Any]] = []
    for trend in _out_of_ref(analysis, trends):
        metric = str(_g(trend, "metric", "") or "")
        fact = _metric_fact(metric, trend)
        out_of_ref.append({
            "metric": fact["metric"],
            "cn": fact["cn"],
            "unit": fact["unit"],
            "latest": fact["latest"],
            "latest_date": fact["latest_date"],
            "verdict": fact["verdict"],
            "verdict_text": fact["verdict_text"],
            "ref_label": fact["ref_label"],
            "ref_kind": fact["ref_kind"],
            "ref_source": fact["ref_source"],
        })

    insufficient: List[Dict[str, Any]] = []
    for trend in _insufficient(analysis, trends):
        metric = str(_g(trend, "metric", "") or "")
        n = _jint(_g(trend, "n", 0))
        days = _jint(_g(trend, "days", 0))
        insufficient.append({
            "metric": metric,
            "cn": _cn_of(metric, trend),
            "n": n,
            "days": days,
            "why": "只有 {0} 个数据点（{1} 天），不足以判断趋势。".format(n, days),
        })

    findings_out: List[Dict[str, Any]] = []
    for finding in fs:
        metric_ids = _g(finding, "metrics", None) or []
        if isinstance(metric_ids, str):
            metric_ids = [metric_ids]
        findings_out.append({
            "code": str(_g(finding, "code", "") or ""),
            "severity": _sev(finding),
            "title": str(_g(finding, "title", "") or ""),
            "detail": str(_g(finding, "detail", "") or ""),
            "metrics": [str(m) for m in metric_ids],
        })

    workouts = _deep_clean(_g(analysis, "workout_summary", None) or {"has_data": False})
    if not isinstance(workouts, dict):
        workouts = {"has_data": False}
    _add_workout_totals(workouts)

    facts = {
        "profile": _deep_clean(getattr(ds, "profile", None) or {}),
        "data": {
            "span": [span[0], span[1]] if span else None,
            "span_days": _span_days(ds, analysis, span),
            "sources": [str(s) for s in (_g(analysis, "sources", None) or []) if str(s).strip()],
            "files": [str(f) for f in (getattr(ds, "files", None) or [])],
            # 原始记录条数（解析前的规模）。实测模型会写「280 万条原始记录」——
            # 这是真话，但如果它不在事实摘要里，数字溯源校验会把整句删掉。
            "record_count": _jint(getattr(ds, "n_records", 0)) or None,
        },
        "metrics": metrics,
        "out_of_ref": out_of_ref,
        "insufficient": insufficient,
        "findings": findings_out,
        "workouts": workouts,
        "notes": [str(n) for n in (_g(analysis, "notes", None) or []) if str(n).strip()],
        # 睡眠规律性与指标关联：这是"情况推测"的实际内容，
        # 必须进 facts，否则 AI 那节只能复述数字、写不出任何推断。
        "sleep_regularity": _sleep_regularity_fact(_g(analysis, "sleep_regularity", None)),
        "associations": [_association_fact(a)
                         for a in (_g(analysis, "associations", None) or [])],
    }
    return _deep_clean(facts)


def _add_workout_totals(workouts: Dict[str, Any]) -> None:
    """给训练汇总补上**总量**（就地修改）。

    为什么必须补：`by_activity` 里只有各类型的 minutes / kcal，总量得自己加。
    实测模型很自然地把它们加起来，写出「累计约 704 小时，约 20.2 万 kcal」——
    那两个数字**不在事实摘要里**，于是被数字溯源校验判成编造、整句删掉。
    但模型算得没错，错在我们没给。

    这正是本项目一贯的分工：**规则层算，模型只叙述**。少给一个总量，
    就等于逼模型自己算，然后又不认它算的结果。
    """
    if not isinstance(workouts, dict) or not workouts.get("has_data"):
        return
    by_activity = workouts.get("by_activity")
    if not isinstance(by_activity, dict) or not by_activity:
        return

    total_minutes = 0.0
    total_kcal = 0.0
    total_km = 0.0
    has_km = False
    for row in by_activity.values():
        if not isinstance(row, dict):
            continue
        minutes = _jnum(row.get("minutes"))
        kcal = _jnum(row.get("kcal"))
        km = _jnum(row.get("km"))
        if minutes is not None:
            total_minutes += minutes
        if kcal is not None:
            total_kcal += kcal
        if km is not None:
            total_km += km
            has_km = True

    workouts["total_minutes"] = round(total_minutes, 1)
    workouts["total_hours"] = round(total_minutes / 60.0, 2)
    workouts["total_kcal"] = round(total_kcal, 1)
    if has_km:
        workouts["total_km"] = round(total_km, 2)


def _sleep_regularity_fact(sr: Any) -> Optional[Dict[str, Any]]:
    """把睡眠规律性整理成给模型的事实卡（含人类可读的钟点）。"""
    if sr is None:
        return None
    onset = _jnum(_g(sr, "onset_median_min", None))
    wake = _jnum(_g(sr, "wake_median_min", None))
    return {
        "n_nights": _jint(_g(sr, "n", 0)),
        "window_days": _jint(_g(sr, "window_days", 0)),
        "mid_std_min": _jnum(_g(sr, "mid_std_min", None)),
        "onset_median_clock": minutes_to_clock(onset),
        "wake_median_clock": minutes_to_clock(wake),
        "onset_std_min": _jnum(_g(sr, "onset_std_min", None)),
        "wake_std_min": _jnum(_g(sr, "wake_std_min", None)),
        "level": str(_g(sr, "level", "") or ""),
        "recent_n": _jint(_g(sr, "recent_n", 0)),
        "recent_mid_std_min": _jnum(_g(sr, "recent_mid_std_min", None)),
        "thresholds_note": "30 / 60 分钟为经验分档，不是诊断标准",
    }


def _association_fact(a: Any) -> Dict[str, Any]:
    """把一条关联整理成给模型的事实卡。"""
    return {
        "key": str(_g(a, "key", "") or ""),
        "cn": str(_g(a, "cn", "") or ""),
        "x_metric": str(_g(a, "x_metric", "") or ""),
        "y_metric": str(_g(a, "y_metric", "") or ""),
        "lag_days": _jint(_g(a, "lag_days", 0)),
        "n": _jint(_g(a, "n", 0)),
        "rho": _jnum(_g(a, "rho", None)),
        "delta": _jnum(_g(a, "delta", None)),
        "noise": _jnum(_g(a, "noise", None)),
        "evidence": str(_g(a, "evidence", "C") or "C"),
        "note": str(_g(a, "note", "") or ""),
        "groups": [_deep_clean(dict(g)) for g in (_g(a, "groups", None) or [])],
        "caveat": "相关，不是因果",
    }


# ──────────────────────────────────────────────────────────── build_report


def _span_line(metric_count: int, span: Optional[Tuple[str, str]], days: int, analysis: Any,
               ds: Dataset, lang: str) -> str:
    if span:
        span_txt = _t("{0} → {1}（共 {2} 天）".format(span[0], span[1], days),
                      "{0} → {1} ({2} days)".format(span[0], span[1], days), lang)
    else:
        span_txt = _t("没有可用数据", "no usable data", lang)
    return ("**" + _t("数据跨度", "Data span", lang) + "**：" + span_txt
            + " ｜ **" + _t("数据来源", "Sources", lang) + "**：" + _sources_text(analysis, ds, lang)
            + " ｜ **" + _t("指标数", "Metrics", lang) + "**：" + str(metric_count))


def _credibility_section(fs: List[Any], trends: Dict[str, Any], order: List[str],
                         lang: str, notes: Optional[List[str]] = None) -> List[str]:
    """第一节：数据可信度提示 + 只做趋势的指标。"""
    out = ["## " + _t("一、数据可信度提示", "1. Data credibility notes", lang), ""]
    notable = [f for f in fs if _sev(f) in ("alert", "warn")]
    if notable:
        for finding in sorted(fs, key=lambda x: _SEV_RANK.get(_sev(x), 9)):
            out.append("### " + _SEV_ICON.get(_sev(finding), "🔵") + " " + _esc(_g(finding, "title", "")))
            out.append("")
            detail = _g(finding, "detail", "")
            out.append(_esc(detail) if detail else
                       _t("（该提示没有更多细节，请核对数据来源文件。）",
                          "(No further detail for this note; please check the source files.)", lang))
            out.append("")
    else:
        out.append(_t("未发现影响结论可信度的问题。", "No credibility issues were found.", lang))
        out.append("")

    out.append("### " + _t("只做趋势、不做判定的指标", "Metrics tracked as trends, without a verdict", lang))
    out.append("")
    none_ref = [m for m in order if _is_none_ref(trends.get(m))]
    shown = [m for m in none_ref if _category_of(m, trends.get(m)) not in HIDDEN_CATEGORIES]
    hidden_none = [m for m in none_ref if _category_of(m, trends.get(m)) in HIDDEN_CATEGORIES]
    if not none_ref:
        out.append(_t("（本报告里每个指标都有可用的参考区间或活动目标，没有只做趋势的例外项。）",
                      "(Every metric has a usable reference range or goal in this report.)", lang))
    else:
        out.extend(_reason_lines(shown, trends, lang))
        if hidden_none:
            out.append(_t(
                "- 另有 {0} 项设备原始读数或设备自算结果（生物电阻抗、设备建议与评分）同样只做趋势，明细见附录 B。".format(len(hidden_none)),
                "- {0} more device raw readings / device-generated values are also trend-only; see Appendix B.".format(len(hidden_none)),
                lang))
    out.append("")

    # ── 解析提示（数据来源与口径）────────────────────────────
    #
    # ⚠️ 这一节曾经不存在：`dataset.notes` 只被塞进 `--json facts`，人类可读的
    # Markdown 正文里一个字都看不到。后果是解析器辛苦写下的口径说明
    # （「步数有 1096 天存在多来源记录，本次按主设备取值；若改为无条件累加，
    # 总步数会从 X 放大到 Y」）读者永远看不到——而这恰恰是最需要看到的一条。
    notes = [str(n) for n in (notes or []) if str(n).strip()]
    if notes:
        out.append("### " + _t("解析提示（数据来源与口径）",
                               "Parser notes (data sources and conventions)", lang))
        out.append("")
        limit = 12
        for n in notes[:limit]:
            out.append("- " + _esc(n))
        if len(notes) > limit:
            out.append(_t(f"- 另有 {len(notes) - limit} 条解析提示（多为单位与来源说明）。",
                          f"- {len(notes) - limit} more parser notes (mostly units and sources).",
                          lang))
        out.append("")
    return out


def _sleep_regularity_block(analysis: Any, out: List[str], lang: str) -> None:
    """睡眠规律性（第五节的子小节）。

    为什么值得单列：**规律性比时长更容易改，而且与恢复状态关系更稳**。
    时长可以靠周末补回来，睡眠中点的漂移补不回来。
    """
    sr = getattr(analysis, "sleep_regularity", None)
    if sr is None:
        return
    level_cn = {
        "regular": _t("规律", "regular", lang),
        "moderate": _t("一般", "moderately irregular", lang),
        "irregular": _t("不规律", "irregular", lang),
    }.get(str(getattr(sr, "level", "")), _t("未知", "unknown", lang))

    out.append("### " + _t("睡眠规律性", "Sleep regularity", lang))
    out.append("")
    mid_std = _jnum(_g(sr, "mid_std_min", None))
    if mid_std is not None:
        out.append(_t(
            "- 睡眠中点（入睡与醒来的中点）的标准差是 **{0}**（{1} 夜），属于「{2}」。".format(
                _fmt(mid_std, 1, "分钟"), _jint(_g(sr, "n", 0)), level_cn),
            "- Standard deviation of the sleep midpoint is **{0}** over {1} nights: {2}.".format(
                _fmt(mid_std, 1, "min"), _jint(_g(sr, "n", 0)), level_cn), lang))
    onset_c = minutes_to_clock(_jnum(_g(sr, "onset_median_min", None)))
    wake_c = minutes_to_clock(_jnum(_g(sr, "wake_median_min", None)))
    if onset_c != "—" or wake_c != "—":
        out.append(_t(
            "- 入睡时间中位 **{0}**，醒来时间中位 **{1}**；入睡时刻标准差 {2} 分钟，"
            "醒来时刻标准差 {3} 分钟。".format(
                onset_c, wake_c,
                _fmt(_jnum(_g(sr, "onset_std_min", None)), 0, ""),
                _fmt(_jnum(_g(sr, "wake_std_min", None)), 0, "")),
            "- Median sleep onset **{0}**, median wake **{1}**.".format(onset_c, wake_c), lang))
    rms = _jnum(_g(sr, "recent_mid_std_min", None))
    if rms is not None:
        out.append(_t(
            "- 近 90 天：中点标准差 **{0}**（{1} 夜）。".format(
                _fmt(rms, 1, "分钟"), _jint(_g(sr, "recent_n", 0))),
            "- Last 90 days: midpoint SD **{0}** over {1} nights.".format(
                _fmt(rms, 1, "min"), _jint(_g(sr, "recent_n", 0))), lang))
    out.append(_t(
        "- 分档阈值（30 / 60 分钟）是**经验分档**，不是诊断标准；这里只说明离散程度，"
        "不判断好或不好。要看改善，比较上面「近 90 天」与全期的差。",
        "- The 30 / 60 minute bands are rules of thumb, not diagnostic thresholds.", lang))
    out.append("")


#: 证据等级 -> 报告里的中文标注
_EVIDENCE_CN = {"A": "证据较强", "B": "证据中等", "C": "仅报数"}


def _association_block(analysis: Any, out: List[str], lang: str) -> None:
    """指标关联（第五节的子小节）。

    **必须写清"相关不是因果"**：这一节是最容易被过度解读的地方，
    也是客户最容易拿去当结论用的地方。
    """
    items = list(getattr(analysis, "associations", None) or [])
    if not items:
        return
    out.append("### " + _t("指标之间的关联", "Associations between metrics", lang))
    out.append("")
    out.append(_t(
        "下面每一条都是**相关，不是因果**：两个指标一起变，可能是其中一个影响另一个，"
        "也可能是有第三个原因同时影响两者。证据等级为「仅报数」的条目只列数值，不做解读。",
        "Every item below is a **correlation, not causation**.", lang))
    out.append("")
    out.append(_t(
        "证据等级看的是**效应大小相对于该指标的测量噪声**，不是相关系数。"
        "所以会出现「相关系数很高、等级却不高」的情况——那表示关系很一致，"
        "但幅度还不到这台设备的重复测量误差量级，不足以指导决定。",
        "The evidence grade reflects the **effect size relative to the metric's measurement "
        "noise**, not the correlation coefficient.", lang))
    out.append("")

    for a in items:
        cn = _g(a, "cn", "")
        n = _jint(_g(a, "n", 0))
        ev = str(_g(a, "evidence", "C") or "C")
        rho = _jnum(_g(a, "rho", None))
        head = "- **{0}**（{1}，n={2}".format(
            _esc(str(cn)), _EVIDENCE_CN.get(ev, ev), n)
        if rho is not None:
            head += _t("，秩相关 {0}".format(_fmt(rho, 2, "")),
                       ", Spearman rho {0}".format(_fmt(rho, 2, "")), lang)
        head += "）"
        out.append(head)
        lag = _jint(_g(a, "lag_days", 0))
        if lag:
            out.append(_t("    - 比较方式：当天与 **{0} 天后**配对。".format(lag),
                          "    - Paired with a **{0}-day** lag.".format(lag), lang))
        for g in (_g(a, "groups", []) or []):
            label = _g(g, "label", "")
            ymed = _jnum(_g(g, "y_median", None))
            gn = _jint(_g(g, "n", 0))
            xmed = _jnum(_g(g, "x_median", None))
            x_txt = ""
            if str(_g(a, "x_metric", "")) != "workout" and xmed is not None:
                x_txt = _t("（该组睡眠/活动中位 {0}）".format(_fmt(xmed, 2, "")),
                           " (group median {0})".format(_fmt(xmed, 2, "")), lang)
            out.append(_t(
                "    - **{0}**：{1} 中位 **{2}**，n={3}{4}".format(
                    _esc(str(label)), _esc(catalog.display_name(str(_g(a, "y_metric", "")))),
                    _fmt(ymed, 2, ""), gn, x_txt),
                "    - **{0}**: median {1}, n={2}".format(
                    _esc(str(label)), _fmt(ymed, 2, ""), gn), lang))
        delta = _jnum(_g(a, "delta", None))
        noise = _jnum(_g(a, "noise", 0.0)) or 0.0
        if delta is not None and noise > 0:
            if abs(delta) < noise:
                out.append(_t(
                    "    - 组间差异 {0} 小于该指标的测量噪声（{1}），"
                    "**视为基本没有差别**。".format(_fmt(delta, 2, "", signed=True),
                                                 _fmt(noise, 2, "")),
                    "    - Group difference is within measurement noise.", lang))
            else:
                out.append(_t(
                    "    - 组间差异 {0}，该指标的测量噪声是 {1}（约 {2} 倍）。".format(
                        _fmt(delta, 2, "", signed=True), _fmt(noise, 2, ""),
                        _fmt(abs(delta) / noise, 1, "")),
                    "    - Group difference {0} vs noise {1}.".format(
                        _fmt(delta, 2, "", signed=True), _fmt(noise, 2, "")), lang))
        note = str(_g(a, "note", "") or "")
        if note:
            out.append("    - " + _esc(note))
    out.append("")


def _category_section(analysis: Any, trends: Dict[str, Any], lang: str) -> List[str]:
    """第三节：分类详情。"""
    out = ["## " + _t("三、分类详情", "3. Details by category", lang), ""]
    groups = _by_category(analysis, trends)
    for cat, ids in groups.items():
        if cat in HIDDEN_CATEGORIES:
            continue
        ids = [m for m in ids if m in trends]
        if not ids:
            continue
        out.append("### " + _esc(cat))
        out.append("")
        hint = catalog.CATEGORY_HINT.get(str(cat), "")
        out.append(_esc(hint) if hint else
                   _t("本类别没有额外的口径说明。", "No additional note for this category.", lang))
        out.append("")
        if len(ids) == 1:
            metric = ids[0]
            trend = trends[metric]
            spec = _spec_of(metric)
            unit = _unit_of(metric, trend, spec)
            precision = _precision_of(metric, spec)
            last = _g(trend, "last", None)
            latest = _jnum(_g(last, "value", None)) if last is not None else None
            kind = _ref_kind(trend, _g(trend, "ref", None))
            date = str(_g(last, "date", "") or "")
            line = _t("**{0}**：最新 {1}（{2}），{3}。".format(
                          _esc(_cn_of(metric, trend)), _fmt(latest, precision, unit),
                          _esc(date) if date else _t("无日期", "no date", lang),
                          _verdict_text(_g(trend, "verdict", "na"), kind, lang,
                                        _g(trend, "verdict_note", ""))),
                      "**{0}**: latest {1} ({2}), {3}.".format(
                          _esc(_cn_of(metric, trend)), _fmt(latest, precision, unit),
                          _esc(date) if date else "no date",
                          _verdict_text(_g(trend, "verdict", "na"), kind, lang,
                                        _g(trend, "verdict_note", ""))),
                      lang)
            out.append(line)
        else:
            out.append(_metric_table([trends[m] for m in ids], lang))
        # 指标口径说明：只有真正有 hint 的指标才占行，避免留下多余空行
        hint_lines: List[str] = []
        for metric in ids:
            spec = _spec_of(metric)
            spec_hint = str(getattr(spec, "hint", "") or "") if spec is not None else ""
            if spec_hint:
                hint_lines.append("- **" + _esc(_cn_of(metric, trends[metric])) + "**：" + _esc(spec_hint))
        if hint_lines:
            out.append("")
            out.extend(hint_lines)
        out.append("")
    if any(_category_of(m, trends.get(m)) in HIDDEN_CATEGORIES for m in trends):
        out.append(_t("生物电阻抗与设备建议/评分类指标属于设备原始读数或设备自算结果，放在附录 B。",
                      "Bioimpedance readings and device-generated scores are listed in Appendix B.", lang))
        out.append("")
    return out


def _workout_section(summary: Any, lang: str) -> List[str]:
    """第四节：训练汇总（没有训练数据时整节省略）。"""
    if not _g(summary, "has_data", False):
        return []
    out = ["## " + _t("四、训练汇总", "4. Workout summary", lang), ""]
    total = _jint(_g(summary, "total", 0))
    span = _g(summary, "span", None)
    if isinstance(span, (list, tuple)) and len(span) >= 2 and span[0] and span[1]:
        span_txt = "{0} → {1}".format(_esc(span[0]), _esc(span[1]))
    else:
        span_txt = "—"
    avg_hr = _jnum(_g(summary, "avg_hr_overall", None))
    out.append("- **" + _t("总次数", "Total sessions", lang) + "**：" + str(total))
    out.append("- **" + _t("时间跨度", "Date span", lang) + "**：" + span_txt)
    out.append("- **" + _t("总体平均心率", "Overall average heart rate", lang) + "**："
               + _fmt(avg_hr, 0, _t("次/分", "bpm", lang)))
    out.append("")
    by_activity = _g(summary, "by_activity", None) or {}
    if isinstance(by_activity, dict) and by_activity:
        if str(lang).lower().startswith("en"):
            out.append("| Activity | Sessions | Duration | Distance | Energy | Avg HR |")
        else:
            out.append("| 运动类型 | 次数 | 总时长 | 总距离 | 总热量 | 平均心率 |")
        out.append("|---|---|---|---|---|---|")
        for activity, info in by_activity.items():
            minutes = _jnum(_g(info, "minutes", None))
            km = _jnum(_g(info, "km", None))
            kcal = _jnum(_g(info, "kcal", None))
            hr = _jnum(_g(info, "avg_hr", None))
            out.append("| " + " | ".join([
                _esc(activity),
                str(_jint(_g(info, "count", 0))),
                _hm(minutes),
                _fmt(km, 2, "km"),
                _fmt(kcal, 0, "kcal"),
                _fmt(hr, 0, _t("次/分", "bpm", lang)),
            ]) + " |")
        out.append("")
    return out


def _rule_section(analysis: Any, trends: Dict[str, Any], lang: str) -> List[str]:
    """第五节：规则层结论。完全由 analysis 的字段拼出，确定性文字。"""
    out = ["## " + _t("五、规则层结论", "5. Rule-based conclusions", lang), ""]
    out.append("### " + _t("体成分与体重", "Body composition and weight", lang))
    out.append("")
    lines: List[str] = []
    skipped_mixed: List[str] = []
    for metric in _BODY_METRICS:
        trend = trends.get(metric)
        if trend is None or str(_g(trend, "kind", "")) != "trend":
            continue
        # ⚠️ 跨来源的首末差值不能写进结论：那正是第一节 SRC_MIXED 警告过的事。
        #    报告自己犯这个错是最坏的——它一边说"不能当作真实变化"，
        #    一边在结论里把它当成变化写出来。
        if len(_g(trend, "sources", []) or []) > 1:
            skipped_mixed.append(_cn_of(metric, trend))
            continue
        first = _g(trend, "first", None)
        last = _g(trend, "last", None)
        if first is None or last is None:
            continue
        spec = _spec_of(metric)
        precision = _precision_of(metric, spec)
        unit = _unit_of(metric, trend, spec)
        v_first = _jnum(_g(first, "value", None))
        v_last = _jnum(_g(last, "value", None))
        if v_first is None or v_last is None:
            continue
        delta = _jnum(_g(trend, "delta", None))
        if delta is None:
            delta = v_last - v_first
        pct = _jnum(_g(trend, "pct", None))
        if pct is None and v_first:
            pct = (v_last - v_first) / v_first * 100.0
        d1 = str(_g(first, "date", "") or "")
        d2 = str(_g(last, "date", "") or "")
        parts = [_fmt(delta, precision, unit, signed=True), _pct_str(pct)]
        detail = "，".join([p for p in parts if p])
        if d1 and d2:
            date_txt = d1 + " → " + d2
            detail = (detail + "；" + date_txt) if detail else date_txt
        lines.append("- **" + _esc(_cn_of(metric, trend)) + "**：{0} → {1}（{2}）".format(
            _fmt(v_first, precision, unit), _fmt(v_last, precision, unit), detail))
    if lines:
        out.extend(lines)
    else:
        out.append(_t("没有形成趋势的体成分指标（数据点不足或时间跨度太短）。",
                      "No body-composition metric has enough data to form a trend yet.", lang))
    if skipped_mixed:
        # 明确说出来比静默省略好：用户会想知道"体重怎么没写"
        out.append("")
        out.append(_t(
            "- ⚠️ {0} 的首末差值没有列出：这些指标的读数来自多个来源，"
            "跨来源的差值不能当作真实变化（见第一节）。要看趋势，请固定用同一台设备。".format(
                "、".join("**" + _esc(n) + "**" for n in skipped_mixed)),
            "- ⚠️ First-to-last deltas for {0} are omitted: their readings come from more "
            "than one source, so the cross-source delta is not a real change (see section 1).".format(
                ", ".join(_esc(n) for n in skipped_mixed)),
            lang))
    out.append("")

    # ── 睡眠规律性 ─────────────────────────────────────────
    _sleep_regularity_block(analysis, out, lang)

    # ── 指标之间的关联 ─────────────────────────────────────
    _association_block(analysis, out, lang)

    out.append("### " + _t("超出参考区间的指标", "Metrics outside their reference range", lang))
    out.append("")
    oor = _out_of_ref(analysis, trends)
    # 拆成两组：需要留意的越界 vs 落在「不需要留意那一侧」的越界。
    # 不拆的话，这一节会把「蛋白质高于上限」（肌肉量充足）和「身体总水分超上限」
    # 用同一种告警口吻并列，与第一节的降调说明自相矛盾。
    concerning: List[Any] = []
    benign: List[Any] = []
    for trend in oor:
        if _is_benign_side(_g(trend, "ref", None), _g(trend, "verdict", "")):
            benign.append(trend)
        else:
            concerning.append(trend)

    if not oor:
        out.append(_t("- 没有指标超出参考区间。", "- No metric is outside its reference range.", lang))
    else:
        if not concerning:
            out.append(_t("- 没有需要留意的越界项。", "- Nothing here needs attention.", lang))
        for trend in concerning:
            metric = str(_g(trend, "metric", "") or "")
            spec = _spec_of(metric)
            precision = _precision_of(metric, spec)
            unit = _unit_of(metric, trend, spec)
            ref = _g(trend, "ref", None)
            kind = _ref_kind(trend, ref)
            last = _g(trend, "last", None)
            latest = _jnum(_g(last, "value", None)) if last is not None else None
            src = _ref_source(trend, ref, spec)
            note = "- **{0}**：{1}，{2}（参考区间 {3}".format(
                _esc(_cn_of(metric, trend)),
                _fmt(latest, precision, unit),
                _verdict_text(_g(trend, "verdict", ""), kind, lang,
                              _g(trend, "verdict_note", "")),
                _ref_label(ref))
            if kind:
                note += "，" + _KIND_CN.get(kind, _t("参考区间", "reference range", lang))
            if src:
                note += "，来源：" + _esc(src)
            note += "）"
            out.append(note)
        if benign:
            items = []
            for trend in benign:
                metric = str(_g(trend, "metric", "") or "")
                spec = _spec_of(metric)
                precision = _precision_of(metric, spec)
                unit = _unit_of(metric, trend, spec)
                last = _g(trend, "last", None)
                latest = _jnum(_g(last, "value", None)) if last is not None else None
                items.append("**{0}**（{1} {2}）".format(
                    _esc(_cn_of(metric, trend)), _fmt(latest, precision, unit),
                    _t("落在不需要留意的一侧", "on the side that is not a concern", lang)))
            out.append(_t(
                "- 以下指标也落在参考区间外，但**落在不需要留意的那一侧**，"
                "不作为异常提示，只当背景信息（原因见第一节）：" + "、".join(items) + "。",
                "- The following are also outside their range but on the side that is not a "
                "concern; they are background information, not warnings: " + ", ".join(items) + ".",
                lang))
    out.append("")

    out.append("### " + _t("不足以判断的部分", "Not enough data to judge", lang))
    out.append("")
    insuff = _insufficient(analysis, trends)
    if not insuff:
        out.append(_t("当前没有因数据点不足而无法判断的指标。",
                      "No metric is currently blocked by a lack of data points.", lang))
    else:
        for trend in insuff:
            metric = str(_g(trend, "metric", "") or "")
            n = _jint(_g(trend, "n", 0))
            days = _jint(_g(trend, "days", 0))
            out.append(_t("- **{0}**：只有 {1} 个数据点（{2} 天），不足以判断趋势；需要更多同条件下的测量。".format(
                              _esc(_cn_of(metric, trend)), n, days),
                          "- **{0}**: only {1} data point(s) over {2} day(s); more measurements under the same conditions are needed.".format(
                              _esc(_cn_of(metric, trend)), n, days),
                          lang))
    out.append("")

    # ── 跨设备可比性 ───────────────────────────────────────
    #
    # 这一节存在的理由：Apple 会把不同设备、不同时期的数据合并进同一条曲线，
    # **而且不标注换了设备**。实测某份真实导出里 VO₂max 来自 3 台不同设备。
    # 所以"趋势"这一节的每一句话，都要在这一节被提醒一次口径。
    handover_trends = [t for t in trends.values() if getattr(t, "handovers", None)]
    if handover_trends:
        handover_trends.sort(key=lambda t: (-len(t.handovers), t.metric))
        out.append("### " + _t("跨设备可比性", "Cross-device comparability", lang))
        out.append("")
        out.append(_t(
            "以下指标的时间线里存在**设备接替**（Apple 不标注这件事）。"
            "不同设备甚至同一设备换算法版本后，对同一个生理量的基线读数并不相同，"
            "所以曲线在换设备处的台阶很可能是**测量变了**而不是身体变了。"
            "读这些趋势时，只在同一台设备的时段内比较：",
            "The following metrics have a **device handover** in their timeline (Apple does not "
            "mark this). Compare trends only within a single device's period:", lang))
        for t in handover_trends[:8]:
            new, old, day = t.handovers[0]
            extra = _t("（共 {0} 处）".format(len(t.handovers)),
                       " ({0} handovers)".format(len(t.handovers))) if len(t.handovers) > 1 else ""
            out.append(_t(
                "- **{0}**：{1} 前后从「{2}」换到「{3}」{4}".format(
                    _esc(t.cn), _esc(day), _esc(old), _esc(new), extra),
                "- **{0}**: switched from \"{1}\" to \"{2}\" around {3}{4}".format(
                    _esc(t.cn), _esc(old), _esc(new), _esc(day), extra), lang))
        if len(handover_trends) > 8:
            out.append(_t("- 另有 {0} 个指标同理。".format(len(handover_trends) - 8),
                          "- {0} more metrics are affected.".format(len(handover_trends) - 8),
                          lang))
        out.append("")

    out.append(_t("以上结论全部来自确定性计算，可以逐条核对第二节的数值。",
                  "Every conclusion above comes from deterministic computation and can be checked line by line against section 2.",
                  lang))
    out.append("")
    return out


def _ai_section(ai_text: str, ai_model: str, lang: str, ai_note: str = "") -> List[str]:
    """第六节：AI 解读。没开启时也要明确告诉用户「不联网也是完整的」。

    `ai_note` 用于说明**为什么没有 AI 文字**（后端没配好、超时、或者
    AI 的文字因数字溯源校验失败被丢弃）。这一条必须显式写出来：
    "本节为空"如果不给原因，读者会以为自己没开；而如果 AI 其实返回了
    但被我们判定为编造数字而丢弃，那更需要说清——那正是校验起了作用。
    """
    out = ["## " + _t("六、AI 解读", "6. AI interpretation", lang), ""]
    text = (ai_text or "").strip()
    note = (ai_note or "").strip()
    if text:
        model = _esc(ai_model) if ai_model else _t("未注明", "not specified", lang)
        out.append(_t("_模型：{0}。以下内容由语言模型生成，仅解释上面的数值，不参与计算，也不改变第一到五节的结论。_".format(model),
                      "_Model: {0}. The text below was generated by a language model. It only explains the numbers above; it does not compute anything or change sections 1-5._".format(model),
                      lang))
        out.append("")
        out.append(text)
        out.append("")
        if note:
            out.append("> " + _esc(note))
            out.append("")
        return out
    out.append(_t("本节为空：没有生成 AI 解读，所以这里没有任何文字。",
                  "This section is empty: no AI interpretation was generated.", lang))
    if note:
        out.append("")
        out.append("> " + _esc(note))
    out.append("")
    out.append(_t("开启方法（可选，纯本地运行不需要联网）：", "How to enable it (optional; fully local use needs no network):", lang))
    out.append("")
    out.append("- " + _t("设置环境变量 `HEALTH_REPORT_API_KEY`（必需，填你自己的接口密钥）；",
                         "Set `HEALTH_REPORT_API_KEY` (required, your own API key);", lang))
    out.append("- " + _t("可选 `HEALTH_REPORT_BASE_URL`（OpenAI 兼容接口地址，默认 `https://api.openai.com/v1`，填什么就用什么，末尾斜杠会被去掉）；",
                         "optionally `HEALTH_REPORT_BASE_URL` (OpenAI-compatible endpoint, default `https://api.openai.com/v1`);", lang))
    out.append("- " + _t("可选 `HEALTH_REPORT_MODEL`（默认 `gpt-4o-mini`）与 `HEALTH_REPORT_TIMEOUT`。",
                         "optionally `HEALTH_REPORT_MODEL` (default `gpt-4o-mini`) and `HEALTH_REPORT_TIMEOUT`.", lang))
    out.append("")
    out.append(_t("> **不开启也是完整的。** 第一节到第五节的每一个数字和每一句结论都由本地确定性计算得出。"
                  "AI 解读只是在它们之上多写一段白话，外加一节「下一步」——而那一节被限制在"
                  "「改善数据可比性」的范围内（固定设备、统一测量条件、增加数据点），不给医疗或营养上的具体方案。"
                  "关掉不影响报告的正确性与可用性。",
                  "> **The report is complete without it.** Every number and conclusion in sections 1-5 comes "
                  "from local deterministic computation. The AI section only adds plain-language explanation "
                  "plus a \"next steps\" part, and that part is limited to improving data comparability — no "
                  "medical or nutritional prescriptions.", lang))
    out.append("")
    return out


def _method_section(lang: str) -> List[str]:
    """附录 A：方法与口径。"""
    out = ["## " + _t("附录 A：方法与口径", "Appendix A: Methods and conventions", lang), ""]
    bullets = [
        _t("- **单位归一**：所有单位在解析阶段就已经归一（kg / cm / % / 次每分 / ms / km / kcal / 小时 / 级 / Ω），"
           "报告里不会再出现磅、斤、英尺混用的问题。",
           "- **Units**: normalised during parsing (kg / cm / % / bpm / ms / km / kcal / hours / level / ohms)."),
        _t("- **参考区间优先级**：被判定的那条观测自带的区间 > 同一设备最近提供的区间 > 项目内置的人群参考表 "
           "> 只做趋势、不下判定。末档是刻意保留的诚实：没有可比区间时，本报告不会硬编一个「正常」出来。",
           "- **Reference priority**: range attached to the judged observation > range from the same device's "
           "most recent reading > built-in population reference > trend only, no verdict."),
        _t("- **区间必须与读数同源**：设备自带的区间只能用于**它自己那台设备**的读数。"
           "拿 A 设备的数值去套 B 设备的区间，会得出一个看起来专业、实际无意义的「正常/异常」。"
           "所以内置人群参考表只在设备没提供区间时兜底，并会如实标注来源。",
           "- **A range must come from the same source as the reading it judges.** Applying device A's range to "
           "device B's value yields a professional-looking but meaningless verdict; the built-in population "
           "reference is only a fallback and is always labelled as such."),
        _t("- **三种区间性质**：`range` 是正常参考区间；`goal` 是活动量目标（没达到只是没达到目标，不等于指标异常）；"
           "`screen` 是筛查阈值（越界只意味着值得进一步评估，同样不是诊断）。报告里的判定措辞严格区分这三者。",
           "- **Three kinds of reference**: `range` = normal reference; `goal` = activity target; `screen` = screening threshold. The verdict wording keeps them apart."),
        _t("- **「哪一侧值得留意」**：有一批指标只有一侧是真正需要留意的信号，落在另一侧是常见甚至好事"
           "（静息心率偏低、去脂体重/骨骼肌量/蛋白质偏高、血氧偏高…）。这类越界会被降调成"
           "「通常不视为异常」，并在第一节说明原因，不列入需要留意清单。"
           "这一判断**按指标给出，不随区间边界变化**——即使区间是设备印的也一样，"
           "因为「肌肉量大的人高于一般人群的上限算不算异常」是关于指标本身的问题。",
           "- **Which side matters**: for some metrics only one side is a real signal; the other side is "
           "common or even favourable (low resting heart rate, high lean mass / muscle / protein, high SpO2). "
           "Such values are downgraded to background information. This judgement is per-metric and does "
           "**not** follow the range's provenance — it is a statement about the metric, not the bounds."),
        _t("- **变化量的噪声阈值**：每个指标在 catalog 里都带一个 `noise` 值，变化量小于该值时视为测量噪声，"
           "表格里会标「噪声内」，不做解读。",
           "- **Noise threshold**: each metric carries a `noise` value in the catalog; smaller changes are labelled as within noise and not interpreted."),
        _t("- **汇总类指标**：`sum`（按日累加）与 `mean`（按日平均）类指标看变化时给的是日均值，不是首末两点之差——"
           "首末差在日间波动大的指标上会严重失真。判定也用日均值：最后一天常常只是导出当天的部分数据，"
           "拿它去比每日目标会误判。有数据日的均值与覆盖率会一起显示。",
           "- **Aggregated metrics**: for `sum` / `mean` metrics the change column shows an average rather than a "
           "first-vs-last difference, and the verdict uses that same average (the last day is often partial data). "
           "Coverage is shown alongside."),
        _t("- **同一指标来自多台设备（Apple 导出常见）**：按日累加的指标（步数、距离、爬楼、能量）会按"
           "「主设备优先」取值，解析器会说明各口径下的总量差异；不做这件事的话，两台设备同时记录会被叠加。"
           "用 `--source-policy` 切换口径，用 `--prefer-source` 指定主设备。"
           "快照类指标（体重、体脂率）的多来源是**分歧**而不是口径问题，因此不做判定。",
           "- **Multi-device metrics (common in Apple exports)**: daily-sum metrics use a primary-device policy and the "
           "parser reports the totals under each policy; without it, two devices recording the same period are summed "
           "together. Switch with `--source-policy`, pick the device with `--prefer-source`. For snapshot metrics "
           "(weight, body fat) multiple sources are a **disagreement**, so no verdict is issued."),
        _t("- **可比性**：同一指标出现多个来源时，来源列会标 ⚠️，**该指标不做判定**（判定列写「来源不一致，不做判定」），"
           "而且结论一节不会列出它的首末差值——跨设备的读数差不能直接当作真实变化。要看趋势请固定用同一台设备。",
           "- **Comparability**: when a metric's readings come from more than one source, the source column shows a ⚠️, "
           "**no verdict is issued**, and the conclusions section omits its first-to-last delta. Fix on one device to read a trend."),
        _t("- **本报告不构成医学诊断**：" + DISCLAIMER,
           "- **Not a medical diagnosis**: " + DISCLAIMER),
    ]
    out.extend(bullets)
    out.append("")
    return out


def _appendix_b(trends: Dict[str, Any], ids: List[str], lang: str) -> List[str]:
    """附录 B：全部指标明细（含主表排除的两类）。"""
    out = ["## " + _t("附录 B：全部指标明细", "Appendix B: All metrics in detail", lang), ""]
    out.append(_t("这里包含主表没有列出的两类指标：生物电阻抗（设备原始读数，它的漂移会解释体成分读数的漂移）"
                  "与设备建议与评分（设备自算结果，不参与范围判定）。",
                  "This appendix adds the two categories excluded from the main table: bioimpedance raw readings and device-generated values.", lang))
    out.append("")
    if ids:
        out.append(_metric_table([trends[m] for m in ids], lang))
    else:
        out.append(_t("（本次没有设备原始读数或设备建议类指标。）",
                      "(No bioimpedance or device-generated metrics in this dataset.)", lang))
    out.append("")
    return out


def build_report(dataset: Dataset, analysis: Analysis, findings: list, *,
                 ai_text: str = "", ai_model: str = "", lang: str = "zh",
                 title: str = "健康数据报告", include_appendix: bool = True,
                 ai_note: str = "") -> str:
    """产出 Markdown 报告。

    `ai_text` 为空时同样产出一份完整可用的报告：第一到第五节全部由
    `analysis` / `findings` 的确定性字段拼出，第六节给出开启 AI 解读的方法。
    """
    ds = dataset if dataset is not None else Dataset()
    trends = _trends_of(analysis)
    fs = _findings_of(findings)
    order = _ordered_metrics(analysis, trends)
    main_ids = [m for m in order if _category_of(m, trends.get(m)) not in HIDDEN_CATEGORIES]
    hidden_ids = [m for m in order if _category_of(m, trends.get(m)) in HIDDEN_CATEGORIES]
    span = _data_span(ds, analysis)
    days = _span_days(ds, analysis, span)

    out: List[str] = []
    # ── 1. 标题 + 生成时间 + 来源文件
    out.append("# " + _esc(title))
    out.append("")
    out.append(_t("生成时间：{0} ｜ 数据来源文件：{1}".format(_now(), _files_text(ds)),
                  "Generated: {0} | Source files: {1}".format(_now(), _files_text(ds)), lang))
    out.append("")
    # ── 2. 跨度 / 来源 / 指标数
    out.append(_span_line(len(trends), span, days, analysis, ds, lang))
    out.append("")
    # ── 3. 免责声明：紧跟标题之后、在任何结论之前
    out.append("> ⚠️ **" + _t("这不是医学诊断。", "This is not a medical diagnosis.", lang) + "** " + DISCLAIMER)
    out.append("")
    # ── 4. 一、数据可信度提示
    out.extend(_credibility_section(fs, trends, order, lang,
                                   getattr(analysis, "notes", None)))
    # ── 5. 二、指标总览
    out.append("## " + _t("二、指标总览", "2. Metrics overview", lang))
    out.append("")
    out.append(_metric_table([trends[m] for m in main_ids], lang))
    out.append("")
    # ── 6. 三、分类详情
    out.extend(_category_section(analysis, trends, lang))
    # ── 7. 四、训练汇总（没有数据则整节省略）
    out.extend(_workout_section(_g(analysis, "workout_summary", None) or {}, lang))
    # ── 8. 五、规则层结论
    out.extend(_rule_section(analysis, trends, lang))
    # ── 9. 六、AI 解读
    out.extend(_ai_section(ai_text, ai_model, lang, ai_note))
    # ── 10. 附录 A：方法与口径
    out.extend(_method_section(lang))
    # ── 11. 附录 B
    if include_appendix:
        out.extend(_appendix_b(trends, hidden_ids, lang))

    text = "\n".join(out).rstrip() + "\n"
    return text
