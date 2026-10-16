"""统一数据模型。

所有解析器（Apple 健康、InBody、通用 CSV）都必须把输入变成同一个 `Dataset`，
下游的规则层和报告层只认这个模型，不认任何设备格式。

关键约定
--------
* 单位在解析阶段就归一（kg / cm / % / 次/分 / ms / km / kcal / 小时 / 级）。
  下游永远不会遇到"有的用磅有的用公斤"这种问题。
* **参考区间跟着观测值走**：`Observation.ref` 保存"输入自带的"区间
  （InBody 报告上的"报告参考范围"）。它比内置人群区间优先——
  设备用的是它自己的模型，通常比通用表更贴近这个人的测量条件。
* `source` 记录设备/App 名，用于可比性判断：同一个指标来自两套测量系统时，
  它们的差值不能直接当"变化"看。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# ──────────────────────────────────────────────────────────── 参考区间


@dataclass(frozen=True)
class RefRange:
    """一个参考区间。

    low/high 为 None 表示该侧开放（例如"<10 级"只有上界）。
    `source` 说明这个区间从哪来，会原样出现在报告里，方便读者复核。
    """

    low: Optional[float] = None
    high: Optional[float] = None
    low_inclusive: bool = True
    high_inclusive: bool = True
    text: str = ""
    source: str = ""
    # range = 正常参考区间；goal = 活动量目标；screen = 筛查阈值。
    # 报告里三者的措辞不同，不能把"没达到步数目标"说成"指标异常"。
    kind: str = "range"
    # 哪一侧才是需要留意的信号：both | high | low。
    #
    # 这一层是**关于指标本身**的领域判断，不是关于这个区间的：例如静息心率
    # 只有偏高才需要留意（长期训练者常低于 60），而 SMI / VO₂max 只有偏低才需要。
    # 所以它由 catalog 按指标给出，即使区间边界来自设备也不改这一判断——
    # 否则体成分仪印的「去脂体重 46.7~57.0」会把"高于上限"报成异常，
    # 而肌肉量大的人高于这个上限恰恰是好事。
    concern: str = "both"

    def contains(self, value: Optional[float]) -> Optional[bool]:
        """落在区间内返回 True，外返回 False，无法判断返回 None。"""
        if value is None or (self.low is None and self.high is None):
            return None
        if self.low is not None:
            if self.low_inclusive:
                if value < self.low:
                    return False
            elif value <= self.low:
                return False
        if self.high is not None:
            if self.high_inclusive:
                if value > self.high:
                    return False
            elif value >= self.high:
                return False
        return True

    def label(self) -> str:
        """人类可读的区间描述，例如 '35.5~43.4' 或 '<10' 或 '>7'。"""
        if self.text:
            return self.text
        if self.low is not None and self.high is not None:
            return f"{_num(self.low)}~{_num(self.high)}"
        if self.low is not None:
            # 用 ≥ / ≤ 而不是 >= / <=：这些区间会直接出现在中文报告里
            return f"{'≥' if self.low_inclusive else '>'}{_num(self.low)}"
        if self.high is not None:
            return f"{'≤' if self.high_inclusive else '<'}{_num(self.high)}"
        return "—"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _num(v: float) -> str:
    """数字转字符串，去掉多余的小数零（8.50 -> 8.5，18.0 -> 18）。"""
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def _is_iso_date(s: str) -> bool:
    """`YYYY-MM-DD` 且是真日期。用于把脏数据挡在日期排序之外。"""
    if not isinstance(s, str) or len(s) < 10:
        return False
    try:
        datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return True


# ──────────────────────────────────────────────────────────── 观测值


@dataclass(frozen=True)
class Observation:
    """一次测量。

    metric:  统一指标 ID（见 catalog.py），例如 "body_mass"
    date:    'YYYY-MM-DD'，趋势与聚合按这个字段分组
    value:   已归一到 spec.unit 的数值
    unit:    归一后的单位（冗余保存，便于报告层直接显示）
    source:  设备 / App 名，例如 "InBody270"、"iPhone"、"家用秤"
    ts:      完整时间戳 'YYYY-MM-DD HH:MM:SS'（同一天多条时用来定序）
    ref:     输入自带的参考区间（可为 None）
    kind:    measured（设备直接测的）| derived（算出来的，如 BMI）
    """

    metric: str
    date: str
    value: float
    unit: str
    source: str = ""
    ts: str = ""
    ref: Optional[RefRange] = None
    kind: str = "measured"

    @property
    def sort_key(self) -> Tuple[str, str]:
        return (self.date, self.ts or self.date)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class Workout:
    """一次训练。v0.1 只做汇总统计，不进入指标趋势。"""

    date: str
    activity: str
    duration_min: Optional[float] = None
    distance_km: Optional[float] = None
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    energy_kcal: Optional[float] = None
    pace_sec_per_km: Optional[float] = None
    source: str = ""
    ts: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ──────────────────────────────────────────────────────────── 数据集


@dataclass
class Dataset:
    """一次分析的全部输入。多个文件可以 merge 成一个 Dataset。"""

    observations: List[Observation] = field(default_factory=list)
    workouts: List[Workout] = field(default_factory=list)
    profile: Dict[str, Any] = field(default_factory=dict)   # height_cm / age / sex
    labels: Dict[str, str] = field(default_factory=dict)    # 设备给的文字判定，如 {"bmi_judgement": "超重"}
    notes: List[str] = field(default_factory=list)          # 解析过程中的提示与告警
    files: List[str] = field(default_factory=list)          # 输入来源文件名
    #: 原始记录条数（Apple 导出里的 `<Record>` 数），**解析前**的规模。
    #:
    #: 为什么要单列：这个数真实、有意义，而且模型很自然会在解读里引用
    #: （实测写了「280 万条原始记录」）。如果它不在事实摘要里，数字溯源校验
    #: 会把整句删掉——即使那句话是对的。所以由解析器数好、传给报告层。
    n_records: int = 0

    # ---------------------------------------------------------- 查询

    def series(self, metric: str) -> List[Observation]:
        """某指标的全部观测，按时间升序。"""
        return sorted((o for o in self.observations if o.metric == metric), key=lambda o: o.sort_key)

    def latest(self, metric: str) -> Optional[Observation]:
        s = self.series(metric)
        return s[-1] if s else None

    def metric_ids(self) -> List[str]:
        seen: List[str] = []
        for o in self.observations:
            if o.metric not in seen:
                seen.append(o.metric)
        return seen

    def sources_of(self, metric: str) -> List[str]:
        """某指标出现过的数据来源（去重、保持出现顺序）。"""
        out: List[str] = []
        for o in self.series(metric):
            s = o.source or "未知来源"
            if s not in out:
                out.append(s)
        return out

    def date_span(self) -> Optional[Tuple[str, str]]:
        """最早与最晚的日期。无法解析成日期的观测会被跳过。

        这里必须过滤：日期是字符串，如果混进 "不是日期" 这种值，
        字符串 min/max 会给出没有意义的结果，而 span 会出现在报告首页。
        """
        dates = [o.date for o in self.observations if _is_iso_date(o.date)]
        if not dates:
            return None
        return (min(dates), max(dates))

    def all_sources(self) -> List[str]:
        out: List[str] = []
        for o in self.observations:
            s = o.source or "未知来源"
            if s not in out:
                out.append(s)
        for w in self.workouts:
            s = w.source or "未知来源"
            if s not in out:
                out.append(s)
        return out

    def days_covered(self, metric: str) -> int:
        return len({o.date for o in self.series(metric)})

    # ---------------------------------------------------------- 组装

    def add(self, obs: Iterable[Observation]) -> None:
        self.observations.extend(obs)

    def add_note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes.append(text)

    def merge(self, other: "Dataset") -> "Dataset":
        """把另一个数据集并进来（原地修改并返回 self）。"""
        self.observations.extend(other.observations)
        self.workouts.extend(other.workouts)
        for k, v in other.profile.items():
            if v is not None and self.profile.get(k) is None:
                self.profile[k] = v
        for k, v in other.labels.items():
            self.labels.setdefault(k, v)
        for n in other.notes:
            self.add_note(n)
        for f in other.files:
            if f not in self.files:
                self.files.append(f)
        # 原始记录条数**累加**（多个文件合并时它是个总量）。
        # 漏了这一步会让 n_records 一直是 0——因为 CLI 总是先建一个空 Dataset
        # 再 merge 进来，于是解析器数好的数就在这里被丢掉了（实测踩到：
        # facts 里 record_count 为 None，模型写「280 万条原始记录」被判成编造）。
        self.n_records += int(getattr(other, "n_records", 0) or 0)
        return self

    def dedupe(self) -> "Dataset":
        """去掉完全重复的观测（同一文件重复解析时会用到）。"""
        seen = set()
        keep: List[Observation] = []
        for o in self.observations:
            key = (o.metric, o.date, o.ts, round(o.value, 6), o.source)
            if key in seen:
                continue
            seen.add(key)
            keep.append(o)
        self.observations = keep
        return self

    # ---------------------------------------------------------- 序列化

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": dict(self.profile),
            "labels": dict(self.labels),
            "notes": list(self.notes),
            "files": list(self.files),
            "observations": [o.to_dict() for o in self.observations],
            "workouts": [w.to_dict() for w in self.workouts],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Dataset":
        ds = cls(
            profile=dict(data.get("profile") or {}),
            labels=dict(data.get("labels") or {}),
            notes=list(data.get("notes") or []),
            files=list(data.get("files") or []),
        )
        for od in data.get("observations") or []:
            ref = od.get("ref")
            ds.observations.append(
                Observation(
                    metric=od["metric"],
                    date=od["date"],
                    value=float(od["value"]),
                    unit=od.get("unit", ""),
                    source=od.get("source", ""),
                    ts=od.get("ts", ""),
                    ref=RefRange(**ref) if ref else None,
                    kind=od.get("kind", "measured"),
                )
            )
        for wd in data.get("workouts") or []:
            ds.workouts.append(Workout(**wd))
        return ds
