"""Apple 健康导出解析器：把 `export.xml` / `export.zip` 变成统一的 `Dataset`。

支持输入
--------
* `export.zip`（自动在包内找路径最短的 `export*.xml`）；
* 裸 `export.xml`（或 `导出.xml`，语言不同文件名会变）；
* Apple 导出目录（依次找 `<dir>/apple_health_export/export.xml`、`<dir>/export.xml`、
  `<dir>/导出.xml`，再退化为任意 `export*.xml` / `导出*.xml`，多个时取体积最大的）。

设计要点
--------
1. **流式解析**：几 GB 的 `export.xml` 不能整棵读进内存。优先 `lxml.etree.iterparse`
   （没有 lxml 就退回标准库 `xml.etree.ElementTree.iterparse`），每处理完一个顶层元素
   立即 `el.clear()` 并删掉已处理的兄弟节点，因此内存占用与文件大小无关。
2. **按 (指标, 日期) 聚合**：心率一天几千条，原始记录不能直接进 `Dataset`。
   聚合方式取 `catalog.get(metric_id).agg`：
   `sum` 累加（步数/能量/距离/爬楼/锻炼分钟）、`mean` 算术平均（心率）、
   `snapshot` 取当日时间戳最晚的那条（体重/体脂/静息心率等）。
3. **跨天归属**：`Observation.date` 取本地日期。睡眠按**醒来那天**归属
   （即 `endDate` 所在日期），因为人会把"昨晚睡了 7 小时"记在起床那天。
4. **单位归一**：一律走 `units.convert(value, 原单位, spec.unit)`；换算不出来时
   **保留原值**并记一条 note，而不是丢数据——宁可数值口径可疑，也不要静默少一条记录。
5. **同日多来源显式化**：按日累加型指标（步数、能量、距离、爬楼、锻炼分钟）在真实
   导出里常常同一天同时有 Apple Watch 与 iPhone 的记录。HealthKit 对**重叠时间区间**
   的去重算法没有公开规范，本工具不去猜一个区间合并算法，而是把口径变成显式选择
   （见 `parse_apple` 的 `source_policy` / `prefer_sources`），并逐指标写一条 note
   说明放大倍数。绝不能默认无条件累加：那既会把数值算高，又会让
   `Dataset.sources_of()` 只剩一个来源，使 `SRC_MIXED` 规则**静默失效**。
6. **睡眠分两组指标**：6 个**时长**指标（`sleep_total` / 分期 / 清醒 / 卧床）走上面的
   聚合路径，数值与旧版逐日一致；另加 3 个**时刻**指标（入睡时刻 / 醒来时刻 /
   睡眠中点，见 `_emit_sleep_clock`），由每晚 `Asleep*` 段的最早 start 与最晚 end
   算出，取值是"醒来日的前一天 18:00 起算的分钟数"——用分钟数而不是钟点，是因为
   时间在午夜处不连续（23:30 与 00:30 只差 60 分钟，按钟点算却是 23 的差）。

已知限制
--------
* `Correlation`（血压/体重指数这类复合条目）与 `ClinicalRecord`（医院检验报告）
  本版本不解析，只统计条数；其中的血压等数据不会出现在 `Dataset` 里。
* `ActivitySummary`（Apple 的每日汇总）只在**没有**对应的按日 Record 时用于补齐
  步数/锻炼分钟/站立时间/距离，避免同一指标被算两遍（详见 `_use_activity_summary`）。
* 时区偏移被丢弃：Apple 导出的是设备本地时间，若跨时区旅行，同日归属可能与
  实际所在地不同，这是导出格式本身的限制。
* 运动类型表 `ACTIVITY_CN` 覆盖 Apple 常见的 80 余种，未收录的类型原样保留英文。
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import catalog, privacy, units
from ..model import Dataset, Observation, Workout
from ._detect import open_binary_input

try:  # pragma: no cover - 取决于运行环境
    from lxml import etree as _LETREE  # type: ignore

    _HAVE_LXML = True
except Exception:  # pragma: no cover
    _LETREE = None  # type: ignore[assignment]
    _HAVE_LXML = False

from xml.etree import ElementTree as _ETREE

__all__ = ["parse_apple", "ACTIVITY_CN", "APPLE_PREFIX", "SOURCE_POLICIES"]


APPLE_PREFIX = "HKQuantityTypeIdentifier"
CATEGORY_PREFIX = "HKCategoryTypeIdentifier"

# 关心的顶层标签（其余标签在 iterparse 里就被过滤掉了）
TOP_TAGS = ("Record", "Workout", "Correlation", "ActivitySummary", "Me")
_TOP_TAG_SET = set(TOP_TAGS)
# 需要取子元素的标签（Me 只有属性，不需要子元素）
CHILD_TAGS = ("Workout", "Correlation", "ActivitySummary")

# HKWorkoutActivityType -> 中文名（照抄参考实现 02_Apple健康数据/parse_apple_health.py 的表）
ACTIVITY_CN: Dict[str, str] = {
    "AmericanFootball": "美式橄榄球", "Archery": "射箭", "AustralianFootball": "澳式足球",
    "Badminton": "羽毛球", "Barre": "芭蕾塑形", "Baseball": "棒球", "Basketball": "篮球",
    "Biking": "骑行", "Bowling": "保龄球", "Boxing": "拳击", "Climbing": "攀岩",
    "CoreTraining": "核心训练", "Cricket": "板球", "CrossTraining": "交叉训练",
    "Curling": "冰壶", "Cycling": "骑行", "Dance": "舞蹈", "DanceInspiredTraining": "舞蹈训练",
    "DiscSports": "飞盘类", "DownhillSkiing": "高山滑雪", "Elliptical": "椭圆机",
    "EquestrianSports": "马术", "Fencing": "击剑", "Fishing": "钓鱼", "FitnessGaming": "体感游戏",
    "Flexibility": "柔韧训练", "FunctionalStrengthTraining": "功能性力量训练",
    "Golf": "高尔夫", "Gymnastics": "体操", "Handball": "手球", "Hiking": "徒步",
    "Hockey": "冰球", "Hunting": "狩猎", "IceSkating": "滑冰", "JumpRope": "跳绳",
    "Kickboxing": "踢拳", "Lacrosse": "长曲棍球", "MartialArts": "武术", "MindAndBody": "身心练习",
    "MixedCardio": "混合有氧", "MixedMetabolicCardioTraining": "混合代谢有氧",
    "Other": "其他", "PaddleSports": "桨板/皮划艇", "Play": "玩耍", "PreparationAndRecovery": "热身与恢复",
    "Racquetball": "壁球", "Rowing": "划船机", "Rugby": "橄榄球", "Running": "跑步",
    "Sailing": "帆船", "SkatingSports": "轮滑", "SnowSports": "雪上运动", "Snowboarding": "单板滑雪",
    "Soccer": "足球", "Softball": "垒球", "Squash": "壁球", "StairClimbing": "爬楼",
    "Stairs": "爬楼", "StepTraining": "踏板训练", "Surfing": "冲浪", "Swimming": "游泳",
    "TableTennis": "乒乓球", "TaiChi": "太极", "Tennis": "网球", "TrackAndField": "田径",
    "TraditionalStrengthTraining": "传统力量训练", "Volleyball": "排球", "Walking": "步行",
    "WaterFitness": "水中健身", "WaterPolo": "水球", "WaterSports": "水上运动",
    "WheelchairRunPace": "轮椅跑", "WheelchairWalkPace": "轮椅步行", "Wrestling": "摔跤",
    "Yoga": "瑜伽", "SwimBikeRun": "铁人三项", "Transition": "换项", "UnderwaterDiving": "潜水",
    "HighIntensityIntervalTraining": "HIIT",
}

# HKCategoryValueSleepAnalysis* -> 指标 ID
SLEEP_STAGE: Dict[str, str] = {
    "HKCategoryValueSleepAnalysisAsleep": "sleep_total",
    "HKCategoryValueSleepAnalysisAsleepCore": "sleep_core",
    "HKCategoryValueSleepAnalysisAsleepDeep": "sleep_deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "sleep_rem",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "sleep_total",
    "HKCategoryValueSleepAnalysisAwake": "sleep_awake",
    "HKCategoryValueSleepAnalysisInBed": "sleep_inbed",
}
# 计入"总睡眠"的阶段（卧床与清醒不算）
SLEEP_ASLEEP_IDS = frozenset({"sleep_core", "sleep_deep", "sleep_rem", "sleep_total"})

#: 睡眠的全部指标（total/core/deep/rem/awake/inbed）。
#: 它们的数值都是"同一天多段记录合并出来的"，语义与按日累加型不同，
#: 因此**全部不参与同日多来源口径**（`primary` / `per-day-max` / `sum`），
#: 以保证睡眠数值与改动前逐日一致。
SLEEP_METRIC_IDS = frozenset(SLEEP_STAGE.values())

#: 每日数值 = 当天各分段时长**之和**的睡眠指标 —— 也就是全部 6 个。
#:
#: 这里必须包含 `sleep_awake` 与 `sleep_inbed`。它们同样是"这一段持续了多久"，
#: 而不是"一次持续多久"，按段取平均会得到毫无意义的数字：实测某份真实导出里
#: `sleep_awake` 中位数会掉到 **0.033 h（2 分钟/夜）**、`sleep_inbed` 掉到
#: **0.66 h**（一夜不可能只在床上待 40 分钟）。
#: 这两个指标在 catalog 里标的是 `agg="mean"`（因为下游要按天平均），
#: 所以不能靠 `agg` 判断，必须在这里显式列出。
SLEEP_DURATION_IDS = frozenset(SLEEP_STAGE.values())

#: 「睡眠时刻」三个指标：入睡时刻 / 醒来时刻 / 睡眠中点（见 catalog 里的同名 spec）。
#:
#: 它们与上面 6 个**时长**指标是两组东西：时长回答"睡了多久"，时刻回答"几点睡、几点醒"。
#: 下游的「睡眠规律性」要用后者，因为入睡时刻比睡眠时长更容易改、与恢复状态的关系也更稳。
#: 这三个指标的数值不是"同一天各段时长相加"，而是"当晚 asleep 段的最早 start 与最晚 end"，
#: 所以它们既不走 `_Acc`（聚合器），也不属于 `SLEEP_METRIC_IDS`（同日多来源口径）。
SLEEP_CLOCK_IDS: Tuple[str, ...] = ("sleep_onset_min", "sleep_wake_min", "sleep_mid_min")

#: 睡眠时刻的参考点：**醒来日的前一天 18:00**。
#:
#: 为什么不用"钟点"：时间在午夜处不连续，23:30 与 00:30 只差 60 分钟，
#: 按钟点算却会得到 23 的差，均值、标准差、分位数全会算错。
#: 选 18:00 作参考点，是因为绝大多数人的入睡时刻在 18:00 之后、醒来在次日 18:00 之前，
#: 于是所有取值落在 `0 ~ 约 900` 的一个**单调区间**里，可以直接算标准差与均值。
SLEEP_CLOCK_REF_HOUR = 18

#: 一夜的 asleep 段跨度超过这个小时数，就认为当晚混进了小睡，跳过这三个指标。
#:
#: Apple 把午睡也记成 `SleepAnalysis`，且按**结束日**归属：午睡结束在当天，
#: 于是"当夜"（昨天 23:00 入睡）与"今天的小睡"会被合并到同一个日期上，
#: 跨度变成 15~20 小时。正常一夜不会超过 16 小时。
SLEEP_CLOCK_MAX_SPAN_HOURS = 16.0

_EMPTY_TOKENS = ("", "—", "-", "–", "--", "n/a", "na", "none", "null")

# ActivitySummary 字段 -> 指标 ID（仅在对应日没有按日 Record 时用于补齐）
_ACTIVITY_SUMMARY_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("activeEnergyBurned", "active_energy"),
    ("appleExerciseTime", "exercise_minutes"),
    ("appleStandHours", "stand_minutes"),
    ("appleMoveTime", "exercise_minutes"),
)

# WorkoutStatistics 里可能带来距离的类型
_DISTANCE_STAT_TYPES: Tuple[str, ...] = (
    "DistanceWalkingRunning", "DistanceCycling", "DistanceSwimming", "DistanceWheelchair",
)


# ──────────────────────────────────────────────────────────── 小工具


def _local_ts(raw: str) -> str:
    """把 '2026-01-10 07:30:00 +0800' 变成 'YYYY-MM-DD HH:MM:SS'（丢掉时区偏移）。"""
    if not raw:
        return ""
    s = raw.strip().replace("T", " ")
    if " " in s:
        head, tail = s.rsplit(" ", 1)
        # 尾部是时区偏移（+0800 / +08:00 / Z）时丢掉它
        if tail[:1] in ("+", "-") and len(tail) >= 5 and ":" not in tail[:6]:
            s = head
        elif tail.upper() == "Z":
            s = head
    return s[:19]


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in _EMPTY_TOKENS:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean(raw: Any) -> str:
    """设备字段里的空值写法统一成空字符串。"""
    s = ("" if raw is None else str(raw)).strip()
    return "" if s.lower() in _EMPTY_TOKENS else s


def _date_of(ts: str) -> str:
    return ts[:10] if len(ts) >= 10 else ""


def _hours_between(start: str, end: str) -> Optional[float]:
    try:
        t0 = datetime.strptime(start[:19], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(end[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return (t1 - t0).total_seconds() / 3600.0


def _clock_minutes(ts: str, wake_day: str) -> Optional[float]:
    """把时间戳换算成「**醒来日的前一天 18:00** 起算的分钟数」。

    例：`2026-05-17 23:10` 且醒来日 `2026-05-18` -> 310.0。

    为什么不直接用"钟点"：时间在午夜处不连续 —— 23:30 与 00:30 只差 60 分钟，
    但按"小时数"（23 与 0）算会得到 23 的差，于是均值、标准差、分位数全错。
    取 18:00 作参考点，是因为绝大多数人的入睡在 18:00 之后、醒来在次日 18:00 之前，
    所有取值因此落在 `0 ~ 约 900` 的一个**单调区间**里，可以直接算标准差、均值、分位数。
    这一点与 catalog 里三条 spec 的 `hint` 是同一句话，改一处就要改另一处。

    这里直接做时间相减得到分钟数，**不经过 `units.convert`**：参考点不是一个
    "原单位"，输入里根本没有可换算的量（对比 `_hours_between` 的用法）。
    """
    if not ts or not wake_day:
        return None
    try:
        t = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        ref_day = datetime.strptime(wake_day[:10], "%Y-%m-%d").date() - timedelta(days=1)
    except (ValueError, TypeError):
        return None
    ref = datetime.combine(ref_day, time(hour=SLEEP_CLOCK_REF_HOUR))
    return (t - ref).total_seconds() / 60.0


def _activity_cn(raw: str) -> str:
    """HKWorkoutActivityTypeRunning -> 跑步；没收录就保留英文名。"""
    key = (raw or "").replace("HKWorkoutActivityType", "").strip()
    if not key:
        return "未知"
    return ACTIVITY_CN.get(key, key)


def _hk_type(raw: str, prefix: str) -> str:
    """去掉公共前缀；顺带把 HKQuantityTypeIdentifier 与 HKCategoryTypeIdentifier 都兼容掉。"""
    s = (raw or "").strip()
    for p in (prefix, APPLE_PREFIX, CATEGORY_PREFIX):
        if p and s.startswith(p):
            return s[len(p):]
    return s


# ──────────────────────────────────────────────────────────── 元素迭代


def _iter_top_elements(fobj: Any):
    """流式产出 (localname, attrib, children)。

    children 是 [(localname, attrib), ...]，只有 Workout/Correlation/ActivitySummary 会取。
    每处理完一个元素就 clear() 并删掉已处理的兄弟节点，避免几 GB 文件吃光内存。
    """
    if _HAVE_LXML:
        context = _LETREE.iterparse(fobj, events=("end",), tag=list(TOP_TAGS))
        for _, el in context:
            tag = _LETREE.QName(el).localname
            attrib = dict(el.attrib)
            if tag in CHILD_TAGS:
                children = [(_LETREE.QName(c).localname, dict(c.attrib)) for c in el]
            else:
                children = []
            el.clear()
            # 删掉已解析过的兄弟节点（lxml 专有）
            parent = el.getparent()
            if parent is not None:
                while el.getprevious() is not None:
                    del parent[0]
            yield tag, attrib, children
    else:
        for _, el in _ETREE.iterparse(fobj, events=("end",)):
            tag = el.tag if isinstance(el.tag, str) else ""
            if tag not in _TOP_TAG_SET:
                continue
            attrib = dict(el.attrib)
            children = [(c.tag, dict(c.attrib)) for c in el] if tag in CHILD_TAGS else []
            el.clear()
            # 标准库版本没有 getparent：只能靠 clear() 让子节点变成空壳，
            # 根元素仍会持有这些空壳，但空壳的内存开销极小。
            yield tag, attrib, children


# ──────────────────────────────────────────────────────────── 聚合


class _Acc:
    """单指标单日的聚合累加器。

    * `sum`：求总和（步数、能量、距离、爬楼、锻炼分钟）。
    * `mean`：算术平均，每条记录权重 1（全天散点心率）。
    * `mean` + `durations=True`：需要**求和**但 catalog 标了 `agg="mean"` 的情况，
      目前只有 `sleep_*`——它们的数值本身就是时长，同一天多条分段记录要相加。
      此时 `total` 是时长之和，同时保留 `count`（记录条数）与 `per_source`
      （逐来源的时长与条数）。
    * `snapshot`：只保留当日 `ts` 最晚的那条及其来源。

    除 `snapshot` 外，`per_source` 一律记录 **每个来源的 (数值之和, 条数)**。
    这是修「同一天多台设备被无条件累加」的关键：只有先按来源拆开，
    才能在落成 Observation 时按口径选一个来源（见 `_resolve_sources`）。
    `snapshot` 不需要它——它本来就只留一条记录。
    """

    __slots__ = ("agg", "durations", "total", "weight", "count",
                 "snap_ts", "snap_value", "snap_source", "last_ts", "per_source")

    def __init__(self, agg: str, durations: bool = False) -> None:
        self.agg = agg
        self.durations = durations
        self.total = 0.0
        self.weight = 0.0
        self.count = 0
        self.snap_ts = ""
        self.snap_value: Optional[float] = None
        self.snap_source = ""
        self.last_ts = ""
        # 逐来源累计：[数值之和, 条数]；snapshot 模式不填
        self.per_source: Dict[str, List[float]] = {}

    def feed(self, value: float, ts: str, source: str, weight: float = 1.0) -> None:
        """喂入一条记录。weight 只在 durations 模式下有意义（即该条记录的时长）。"""
        if ts and ts > self.last_ts:
            self.last_ts = ts
        if self.agg == "snapshot":
            if self.snap_value is None or ts >= self.snap_ts:
                self.snap_ts = ts
                self.snap_value = value
                self.snap_source = source
            return
        self.count += 1
        self.total += value
        if self.durations:
            self.weight += weight if weight else value
        else:
            self.weight += 1.0
        slot = self.per_source.setdefault(source or "", [0.0, 0.0])
        slot[0] += value
        slot[1] += 1

    def value(self) -> Optional[float]:
        if self.agg == "snapshot":
            return self.snap_value
        if self.count == 0:
            return None
        if self.agg == "mean":
            if self.durations:
                # 同日多段相加：total 已经是时长之和
                return self.total
            return self.total / self.count
        return self.total

    def by_source(self) -> Dict[str, float]:
        """逐来源的聚合值。

        `sum` 与 durations（睡眠）是各来源的**和**；`mean` 是各来源的**算术平均**
        ——两者都是「如果今天只看这一台设备，会得到多少」。snapshot 返回空字典。
        """
        if self.agg == "mean" and not self.durations:
            return {s: v[0] / v[1] for s, v in self.per_source.items() if v[1] > 0}
        return {s: v[0] for s, v in self.per_source.items() if v[1] > 0}

    def source_counts(self) -> Dict[str, int]:
        """逐来源的记录条数（同一天同一指标的记录被拆到各自来源下）。"""
        return {s: int(v[1]) for s, v in self.per_source.items() if v[1] > 0}

    def source(self, fallback: str) -> str:
        if self.agg == "snapshot":
            return self.snap_source or fallback
        return fallback


# ──────────────────────────────────────────────────────────── 同日多来源口径


#: 支持的多来源口径。列表顺序即提示语里的列举顺序，不要随便改（有测试与文档引用）。
SOURCE_POLICIES: Tuple[str, ...] = ("primary", "per-day-max", "sum")

#: 口径的中文名，出现在 ValueError 与 note 里。
SOURCE_POLICY_CN: Dict[str, str] = {
    "primary": "主设备优先（同一天只取一台设备）",
    "per-day-max": "每日取各来源里的最大值",
    "sum": "无条件累加（同一天的多台设备会被算两遍）",
}


def _validate_policy(policy: str) -> None:
    """口径非法时抛带中文说明的 ValueError。

    在打开文件**之前**就调用：`parse_apple` 可能要读几 GB 的导出，
    参数写错不该等五分钟才报错。
    """
    if policy not in SOURCE_POLICIES:
        options = "、".join(f"{p}（{SOURCE_POLICY_CN[p]}）" for p in SOURCE_POLICIES)
        raise ValueError(
            f"不认识的来源口径「{policy}」。可选：{options}。"
            "默认用 primary：同一天只取优先级最高的那台设备，避免把两台设备的记录加在一起。"
        )


def _norm_source_key(name: str) -> str:
    """来源名比较用的归一化：小写、去掉所有空白。"""
    return "".join(str(name or "").split()).lower()


def _source_matches(source: str, prefer: str) -> bool:
    """`prefer` 是否指向 `source`（宽松匹配）。

    规则：
    1. 原始来源名与**脱敏后**的来源名都参与比较，所以
       `Apple Watch` 能命中 `某某的Apple Watch`；
    2. 大小写与空白不敏感；
    3. 相等命中；`prefer` 长度 >= 2 时，子串命中也算（避免单字把全部设备都匹配上）。
    """
    p = _norm_source_key(prefer)
    if not p:
        return False
    raw = _norm_source_key(source)
    masked = _norm_source_key(privacy.mask_source_name(source))
    if p == raw or p == masked:
        return True
    if len(p) < 2:
        return False
    return p in raw or p in masked


def _ordered_sources(by_source: Dict[str, float], counts: Dict[str, int],
                     prefer: Sequence[str]) -> List[str]:
    """按优先级给当天的来源排序。

    调用方给的 `prefer` 顺序在前（谁先写谁优先）；其余来源按**全数据集记录条数**
    降序——记录最多的那台通常是主力设备；最后按来源名排序，保证结果稳定可复现。
    """
    def key(src: str) -> Tuple[int, int, int, str]:
        for i, p in enumerate(prefer):
            if _source_matches(src, p):
                return (0, i, 0, src)
        return (1, 0, -int(counts.get(src, 0)), src)

    return sorted(by_source.keys(), key=key)


def _resolve_sources(by_source: Dict[str, float], counts: Dict[str, int],
                     policy: str, prefer: Sequence[str],
                     agg: str = "sum",
                     day_counts: Optional[Dict[str, int]] = None
                     ) -> Tuple[float, str, Dict[str, float]]:
    """把「同一天各来源的值」按口径压成 (采用的数值, 采用的来源, 各来源的原始数值)。

    * `by_source`：来源 -> 该来源当天的值（由 `_Acc.by_source()` 给出）。
    * `counts`：来源 -> **全数据集**记录条数，只用来决定同一优先级里的先后；
      逐日的条数在 `_Acc.source_counts()` 里。
    * `policy` 取值见 `SOURCE_POLICIES`；非法值抛带中文说明的 `ValueError`。
    * `agg` / `day_counts`：`mean` 类指标（只有心率）的 `sum` 口径必须回到旧行为
      ——把当天**所有记录合并后取平均**。这一步要按各来源的条数加权，所以需要
      额外的 `day_counts`；对 `sum` 类指标这两个参数没有作用。
    * 当天没有可用数据时返回 `(0.0, "", {})`，调用方据此跳过这一天。

    这里**不去猜** Apple 的重叠区间去重算法（没有公开规范、不可复现）：
    要么只取一台设备，要么取每日最大值，要么无条件累加——三者都说得清口径。
    """
    _validate_policy(policy)
    if not by_source:
        return 0.0, "", {}
    order = _ordered_sources(by_source, counts, prefer)
    if policy == "sum":
        if agg == "mean" and day_counts:
            # 旧行为 = 合并当天全部记录再平均 = 各来源平均值按条数加权
            weights = {s: float(day_counts.get(s, 0)) for s in by_source}
            total_w = sum(weights.values())
            if total_w > 0:
                pooled = sum(by_source[s] * weights[s] for s in by_source) / total_w
                return float(pooled), order[0], dict(by_source)
        # 旧行为：保留为显式选项，提示里会说明它把数值算高
        return float(sum(by_source.values())), order[0], dict(by_source)
    if policy == "per-day-max":
        # 并列时按优先级取，保证同样的输入给出同样的来源名
        best = order[0]
        for src in order[1:]:
            if by_source[src] > by_source[best]:
                best = src
        return float(by_source[best]), best, dict(by_source)
    # primary：优先级最高的那台设备当天没数据时自动回退到下一台，不会整天丢数据
    head = order[0]
    return float(by_source[head]), head, dict(by_source)


def _tally_multisource(stats: Dict[str, Dict[str, Any]], metric_id: str,
                       by_source: Dict[str, float], chosen_value: float,
                       chosen_source: str, legacy_value: float) -> None:
    """累计某个指标的"同日多来源"统计，供 `_multisource_notes` 写说明。

    一次记三种口径的值，这样无论本次用哪种口径，都能报出"改用另一种会差多少"：
    采用的 / 旧口径（累加或合并平均）/ 每日取最大。
    """
    st = stats.get(metric_id)
    if st is None:
        st = {"days": 0, "multi": 0, "chosen_total": 0.0, "legacy_total": 0.0,
              "max_total": 0.0, "chosen_srcs": Counter(), "all_srcs": []}
        stats[metric_id] = st
    st["days"] += 1
    if len(by_source) > 1:
        st["multi"] += 1
    st["chosen_total"] += float(chosen_value)
    st["legacy_total"] += float(legacy_value)
    st["max_total"] += float(max(by_source.values()))
    if chosen_source and chosen_source not in st["all_srcs"]:
        st["all_srcs"].append(chosen_source)
    for src in by_source:
        if src not in st["all_srcs"]:
            st["all_srcs"].append(src)
    st["chosen_srcs"][chosen_source] += 1


def _ratio(a: float, b: float) -> str:
    """a / b，保留两位；b 为 0 时返回「—」（不编一个数出来）。"""
    if not b:
        return "—"
    return f"{a / b:.2f}"


def _pct_diff(a: float, b: float) -> str:
    """|a − b| / b 的百分比，保留一位；b 为 0 时返回「—」。"""
    if not b:
        return "—"
    return f"{abs(a - b) / abs(b) * 100.0:.1f}%"


def _multisource_notes(stats: Dict[str, Dict[str, Any]], policy: str,
                       mask: Any, max_notes: int = 6) -> List[str]:
    """给"同一天有多台设备"的 sum/mean 指标各写一条口径说明。

    信息量按重要性排：多来源天数（含百分比）、本次口径与主设备、
    改用旧口径会差多少、以及"Apple 不去重"的诚实提示。
    累加类讲「全年合计」，平均类（只有心率）讲「日均」——对心率讲总和没有意义。
    来源名一律走 `mask`（即 `privacy.mask_source_name`），不得带出机主姓名。
    """
    multi = [(mid, st) for mid, st in stats.items() if st["multi"] > 0]
    if not multi:
        return []
    # 影响大的排前面（多来源天数多的先讲）；指标 ID 只用于稳定排序
    multi.sort(key=lambda kv: (-int(kv[1]["multi"]), kv[0]))

    notes: List[str] = []
    limit = max_notes if len(multi) <= max_notes else max_notes - 1
    for metric_id, st in multi[:limit]:
        spec = catalog.get(metric_id)
        cn = spec.cn if spec else metric_id
        prec = int(spec.precision) if spec else 1
        unit = spec.unit if spec else ""
        is_mean = bool(spec) and spec.agg == "mean"
        days = int(st["days"])
        n_multi = int(st["multi"])
        pct = (n_multi / days * 100.0) if days else 0.0
        top = st["chosen_srcs"].most_common(1)
        main_src = mask(top[0][0]) if top and top[0][0] else "未知来源"
        n_src = len({mask(s) for s in st["all_srcs"]})
        chosen = float(st["chosen_total"])
        legacy = float(st["legacy_total"])
        best_day = float(st["max_total"])
        policy_cn = SOURCE_POLICY_CN.get(policy, policy)

        def fmt(v: float) -> str:
            return f"{v:,.{prec}f}"

        text = (
            f"{cn}：{days:,} 天有数据，其中 {n_multi:,} 天（{pct:.1f}%）同一天有多台设备"
            f"（共 {n_src} 台）。本次口径「{policy_cn}」，最常采用的设备是「{main_src}」，"
        )
        if is_mean:
            text += f"日均 {fmt(chosen / days)} {unit}。"
            if policy == "sum":
                text += (
                    f"注意：该口径就是旧行为——把同一天各设备的记录合并后取平均，"
                    f"日均 {fmt(legacy / days)} {unit}；"
                    f"若改成每日取各来源最大值，日均 {fmt(best_day / days)} {unit}。"
                )
            else:
                text += (
                    f"若改成把同一天各设备的记录合并后取平均（旧行为），"
                    f"日均是 {fmt(legacy / days)} {unit}，与本次相差 {_pct_diff(legacy, chosen)}。"
                )
        else:
            text += f"合计 {fmt(chosen)} {unit}。"
            if policy == "sum":
                text += (
                    f"注意：该口径把同一天各设备的值直接相加，本身就会系统性算高；"
                    f"若改成每日取各来源最大值，合计是 {fmt(best_day)} {unit}，"
                    f"本次是它的 {_ratio(chosen, best_day)} 倍。"
                )
            else:
                text += (
                    f"若改成无条件累加，合计会是 {fmt(legacy)} {unit}，"
                    f"是本次的 {_ratio(legacy, chosen)} 倍。"
                )
        text += (
            "Apple 对重叠时间区间的去重算法没有公开规范，本工具不去猜；"
            "要跨时间看趋势，请固定用同一台设备。"
        )
        notes.append(text)

    if len(multi) > max_notes:
        rest = "、".join((catalog.get(mid).cn if catalog.get(mid) else mid)
                        for mid, _ in multi[limit:])
        notes.append(
            f"另有 {len(multi) - limit} 个指标也存在同一天多设备的情况（{rest}），"
            f"同样按「{SOURCE_POLICY_CN.get(policy, policy)}」处理，说明从略。"
        )
    return notes


class _UnitGuard:
    """记录"换算不出来"的情况，同类只记一次，避免几万条记录刷出几万条 note。"""

    def __init__(self) -> None:
        self._seen: set = set()
        self.notes: List[str] = []

    def fallback(self, metric_id: str, raw_unit: str, target_unit: str) -> None:
        key = (metric_id, raw_unit, target_unit)
        if key in self._seen:
            return
        self._seen.add(key)
        shown = raw_unit or "空"
        self.notes.append(
            f"单位无法换算，已按原值保留：{catalog.display_name(metric_id)}（{metric_id}）"
            f"的单位「{shown}」→「{target_unit}」，该值不做单位归一，跨来源比较时请留意。"
        )


# ──────────────────────────────────────────────────────────── 主入口


def parse_apple(path: str, source_name: str = "",
                source_policy: str = "primary",
                prefer_sources: Sequence[str] = (),
                mask_source_names: bool = True) -> Dataset:
    """解析 Apple 健康导出（zip / xml / 目录），返回统一 `Dataset`。

    同日多来源口径（`source_policy` / `prefer_sources`）
    ---------------------------------------------------
    Apple 的导出里，按日累加型指标（步数/能量/距离/爬楼/锻炼分钟）常常同一天同时有
    Apple Watch 与 iPhone 的记录：**实测一份 1263 MB 的导出里，步数有 57.4% 的天数
    是多来源**。HealthKit 对重叠时间区间的去重算法没有公开规范、不可复现，
    所以本工具不猜区间合并算法，而是把口径变成显式选择：

    * `"primary"`（默认）：同一天只取**优先级最高的一台设备**，绝不双计；
      主设备当天没数据时自动回退到下一台，所以不会整天丢数据。
    * `"per-day-max"`：同一天取各来源里最大的那个值（近似"取更完整的那份"）。
    * `"sum"`：无条件累加。**这是旧行为，会把同一天的多台设备算两遍**，
      只在明确要看原始累加时才用；产生的 note 会写明它算高了多少。

    优先级 = `prefer_sources` 里写在前面的优先（例如 `["Apple Watch"]`），
    其余来源按**全数据集记录条数**降序。`prefer_sources` 的匹配是宽松的：
    先脱敏再比，大小写与空白不敏感，相等或子串命中都算——
    写 `Apple Watch` 能命中 `某某的Apple Watch`。

    `mask_source_names=True`（默认）时，写进 `Observation.source` / `Workout.source`
    的设备名先过 `privacy.mask_source_name()`，剥掉 Apple 默认命名里的机主姓名前缀；
    为 False 时完全不脱敏（仅本地调试）。

    注意：睡眠指标（`sleep_*`）不参与上述口径——它们是把同一天的多段记录合并出来的
    （见 `SLEEP_METRIC_IDS`），数值与旧版逐日一致。

    除 6 个睡眠**时长**指标外，本函数还会额外产出 3 个「睡眠时刻」指标
    （入睡时刻 / 醒来时刻 / 睡眠中点，见 `SLEEP_CLOCK_IDS` 与 `_emit_sleep_clock`）：
    它们由每晚 `Asleep*` 段的最早 start 与最晚 end 算出，取值是"醒来日的前一天 18:00
    起算的分钟数"。这三个是**新增**观测，不改变任何已有观测的数值、来源与时间戳。
    """
    _validate_policy(source_policy)  # 先校验参数：几 GB 的文件不该等读完才报参数错
    ds = Dataset()
    ds.files.append(os.path.basename(os.path.normpath(path)))

    fobj, closer, desc = open_binary_input(path)
    guard = _UnitGuard()
    # 注意：这里不能用 defaultdict——`if key not in aggregates` 一旦触发默认工厂，
    # 就会插进一个 agg 错误的累加器，后面的判断永远命中不了正确的聚合方式。
    aggregates: Dict[Tuple[str, str], _Acc] = {}
    slot_agg: Dict[Tuple[str, str], str] = {}
    sleep_days: set = set()
    sleep_source: Dict[str, str] = {}
    # 逐日的 asleep 段跨度 [最早的 start, 最晚的 end]，只供「睡眠时刻」三个指标用
    asleep_span: Dict[str, List[str]] = {}
    seen_types: Counter = Counter()
    summary_rows: List[Dict[str, Any]] = []
    unknown_apples: set = set()
    # 来源 -> 全数据集记录条数。用于多来源天数里"谁是主设备"的排序依据。
    all_source_counts: Counter = Counter()
    # 同日多来源统计：metric -> 聚合出的指标（多来源天数、各口径总计、采用的来源）
    ms_stats: Dict[str, Dict[str, Any]] = {}
    saw_personal_name = False
    masked_cache: Dict[str, str] = {}

    def mask(src: str) -> str:
        """按需脱敏来源名。缓存结果：几百万条记录不该反复跑正则。"""
        if not mask_source_names or not src:
            return src
        hit = masked_cache.get(src)
        if hit is None:
            hit = privacy.mask_source_name(src)
            masked_cache[src] = hit
        return hit

    n_records = 0
    n_correlations = 0
    n_me = 0
    first_ts = ""
    last_ts = ""
    birth_date = ""

    try:
        for tag, a, children in _iter_top_elements(fobj):
            if tag == "Me":
                n_me += 1
                _apply_me(a, ds)
                birth_date = str(ds.profile.get("birth_date") or birth_date)
            elif tag == "Record":
                n_records += 1
                rtype = a.get("type", "")
                seen_types[rtype] += 1
                src_raw = _clean(a.get("sourceName"))
                if src_raw and src_raw not in all_source_counts:
                    # 只在第一次见到这个设备名时判别，避免几百万次正则
                    if mask_source_names and privacy.looks_like_personal_name(src_raw):
                        saw_personal_name = True
                if src_raw:
                    all_source_counts[src_raw] += 1
                ts = _local_ts(a.get("startDate", ""))
                if ts:
                    if not first_ts or ts < first_ts:
                        first_ts = ts
                    if not last_ts or ts > last_ts:
                        last_ts = ts
                _handle_record(a, rtype, ts, ds, aggregates, slot_agg, sleep_days,
                               sleep_source, asleep_span, guard, unknown_apples)
            elif tag == "Workout":
                w = _build_workout(a, children, ds, mask)
                if w is not None:
                    ds.workouts.append(w)
            elif tag == "Correlation":
                n_correlations += 1
            elif tag == "ActivitySummary":
                summary_rows.append(a)
    finally:
        if closer is not None:
            closer.close()
        else:
            try:
                fobj.close()
            except Exception:  # pragma: no cover - 关不掉也不该影响结果
                pass

    # 聚合结果落成 Observation。**每个 (指标, 日期) 只产出一条**：
    # 多来源的选择在这里就做完，下游 analysis 的语义完全不变。
    for (metric_id, day), acc in sorted(aggregates.items()):
        spec = catalog.get(metric_id)
        unit = spec.unit if spec else ""
        if acc.agg == "snapshot" or acc.durations or metric_id in SLEEP_METRIC_IDS:
            # 快照类取当天最晚一条；睡眠类是把同一天的分段记录合并出来
            # （见 SLEEP_METRIC_IDS），它们不适用"同日多来源"口径，
            # 行为与旧版逐日一致。
            value = acc.value()
            if value is None:
                continue
            base = slot_agg.get((metric_id, day)) or source_name or "Apple Health"
            src = acc.source(base) or "Apple Health"
            ts = acc.snap_ts if acc.agg == "snapshot" else (acc.last_ts or day)
        else:
            by_src = acc.by_source()
            if not by_src:
                continue
            day_counts = acc.source_counts()
            value, chosen, by_src = _resolve_sources(
                by_src, all_source_counts, source_policy, prefer_sources,
                agg=acc.agg, day_counts=day_counts)
            # 旧口径的值（sum 类 = 无条件累加，mean 类 = 合并各来源后取平均），
            # 只用于 note 里报"改用旧口径会差多少"
            legacy, _, _ = _resolve_sources(
                by_src, all_source_counts, "sum", prefer_sources,
                agg=acc.agg, day_counts=day_counts)
            src = chosen or source_name or "Apple Health"
            ts = acc.last_ts or day
            _tally_multisource(ms_stats, metric_id, by_src, value, chosen, legacy)
        ds.observations.append(
            Observation(metric=metric_id, date=day, value=float(value), unit=unit,
                        source=mask(src), ts=ts, ref=None, kind="measured")
        )

    # 睡眠时刻（入睡 / 醒来 / 睡眠中点）在原有指标全部落成之后统一产出：
    # 这一步只**追加**观测，不碰上面任何一条已有观测的数值、来源与时间戳
    # （来源直接取同一天的 sleep_total）。
    sleep_clock_notes: List[str] = []
    _emit_sleep_clock(asleep_span, ds, mask, sleep_clock_notes)
    for n in sleep_clock_notes:
        ds.add_note(n)

    # 口径说明放在最前面：这是报告里最需要被看见的一句话
    for n in _multisource_notes(ms_stats, source_policy, mask):
        ds.add_note(n)
    if saw_personal_name:
        ds.add_note(privacy.MASK_NOTE)

    _use_activity_summary(summary_rows, ds)

    # 原始记录条数传给报告层：它是个真实且有意义的规模数字，
    # 模型很自然会引用（实测写了「280 万条原始记录」）。不传的话
    # 数字溯源校验会把整句删掉——即使那句话完全正确。
    ds.n_records = n_records

    for n in guard.notes:
        ds.add_note(n)
    _write_summary(ds, path, n_records, len(ds.workouts), first_ts, last_ts,
                   n_correlations, seen_types, unknown_apples)
    if birth_date:
        _apply_age(ds, max(last_ts or "", first_ts or ""), birth_date)
    ds.dedupe()
    return ds


# ──────────────────────────────────────────────────────────── Record


def _handle_record(a: Dict[str, Any], rtype: str, ts: str, ds: Dataset,
                   aggregates: Dict[Tuple[str, str], _Acc], slot_agg: Dict[Tuple[str, str], str],
                   sleep_days: set, sleep_source: Dict[str, str],
                   asleep_span: Dict[str, List[str]],
                   guard: _UnitGuard, unknown_apples: set) -> None:
    if not rtype or not ts:
        if rtype:
            unknown_apples.add(rtype)
        return
    source = _clean(a.get("sourceName"))

    if rtype == CATEGORY_PREFIX + "SleepAnalysis":
        _handle_sleep(a, ts, source, aggregates, sleep_days, sleep_source, asleep_span)
        return

    short = _hk_type(rtype, APPLE_PREFIX)
    metric_id = catalog.APPLE_TO_METRIC.get(short)
    if not metric_id:
        unknown_apples.add(rtype)
        return
    spec = catalog.get(metric_id)
    if spec is None:  # 理论上不会发生：catalog 里 apple_types 与 BY_ID 同源
        unknown_apples.add(rtype)
        return

    raw = _to_float(a.get("value"))
    if raw is None:
        return
    raw_unit = _clean(a.get("unit"))

    # 有些指标 Apple 以 0~1 存储，而规范单位是 %（0~100）：体脂率、血氧饱和度都见过。
    #
    # ⚠️ 判据必须是「**规范单位是 %** 且值 ≤ 1」，不能写死成某个指标 id。
    # 早期版本写的是 `if metric_id == "body_fat_pct"`，后果是血氧饱和度在真实数据里
    # 显示成「1 %」（本应 ~97 %），而且因为 1 < 95 被判成「低于筛查下限」，
    # **凭空造出一条假警报**——这正是本项目要防的那类错误。
    #
    # 为什么这个判据安全：catalog 里规范单位为 % 的指标（体脂率、血氧饱和度、
    # 节段百分比、肥胖度）物理量级都在 0~100，出现 ≤ 1 的值只可能是比值存储。
    # 其它指标（单位 kg / 次每分 / 小时…）一律不受影响。
    if spec.unit == "%" and units.looks_like_ratio_value(raw_unit) and raw <= 1.0:
        raw *= 100.0
    value = units.convert(raw, raw_unit, spec.unit)
    if value is None:
        guard.fallback(metric_id, raw_unit, spec.unit)
        value = raw
    # 兜底：单位缺失导致走 fallback 时，再判一次
    if spec.unit == "%" and value <= 1.0:
        value *= 100.0

    day = _date_of(ts)
    if not day:
        return
    key = (metric_id, day)
    if key not in aggregates:
        aggregates[key] = _new_acc(metric_id)
        slot_agg[key] = source or ""
    acc = aggregates[key]
    acc.feed(float(value), ts, source)
    if not slot_agg.get(key) and source:
        slot_agg[key] = source


def _new_acc(metric_id: str) -> _Acc:
    """按指标建累加器；睡眠指标需要"同日分段相加"，见 SLEEP_DURATION_IDS。"""
    spec = catalog.get(metric_id)
    agg = spec.agg if spec else "snapshot"
    # 注意判据是 SLEEP_DURATION_IDS 而不是 agg == "mean"：
    # sleep_awake / sleep_inbed 也是"分段时长求和"，但 catalog 里标的是 mean。
    return _Acc(agg, durations=metric_id in SLEEP_DURATION_IDS)


def _handle_sleep(a: Dict[str, Any], ts: str, source: str,
                  aggregates: Dict[Tuple[str, str], _Acc], sleep_days: set,
                  sleep_source: Dict[str, str],
                  asleep_span: Dict[str, List[str]]) -> None:
    """睡眠：按阶段时长累计，归属到 endDate 所在日期（醒来那天）。

    `asleep_span` 只用于「睡眠时刻」三个指标（见 `_emit_sleep_clock`）：
    逐日记录当晚 asleep 段的 `[最早的 start, 最晚的 end]`。
    它对上面那些时长指标的数值**没有任何影响** —— 那一部分走的是 `aggregates`。
    """
    end_ts = _local_ts(a.get("endDate", "")) or ts
    start_ts = ts
    stage_key = _clean(a.get("value"))
    hours = _hours_between(start_ts, end_ts)
    if hours is None or hours <= 0:
        return
    day = _date_of(end_ts)
    if not day:
        return
    sleep_days.add(day)
    if source:
        sleep_source[day] = source

    stage_id = SLEEP_STAGE.get(stage_key)

    # 睡眠时刻只认 `Asleep*` 段：
    #   * `Awake` 是"夜里醒着"（可能发生在睡眠中间），不是睡眠本身；
    #   * `InBed` 是"躺在床上"（上床前后刷手机的时间都在里面），也不是睡眠本身。
    # 用 InBed 定入睡时刻，会把"上床后躺了一小时"算成入睡；用 Awake 定醒来时刻，
    # 会把夜里短暂清醒当成起床。
    # 认不出的写法与上面的 targets 判断保持一致，按"睡着（未分期）"处理：
    # 宁可算进这一夜，也不要因此丢掉一整夜的时刻。
    if stage_id is None or stage_id in SLEEP_ASLEEP_IDS:
        span = asleep_span.get(day)
        if span is None:
            asleep_span[day] = [start_ts, end_ts]
        else:
            if start_ts < span[0]:
                span[0] = start_ts
            if end_ts > span[1]:
                span[1] = end_ts

    targets: List[str] = []
    if stage_id is None:
        # 未知写法一律按"睡着(未分期)"处理：宁可算进总睡眠，也不要丢掉一夜数据
        targets = ["sleep_total"]
    elif stage_id == "sleep_total":
        targets = ["sleep_total"]
    elif stage_id in SLEEP_ASLEEP_IDS:
        targets = [stage_id, "sleep_total"]
    else:
        # 清醒与卧床只单列，不计入总睡眠
        targets = [stage_id]

    for metric_id in targets:
        spec = catalog.get(metric_id)
        if spec is None:
            continue
        key = (metric_id, day)
        if key not in aggregates:
            aggregates[key] = _new_acc(metric_id)
        acc = aggregates[key]
        # 这里喂的是"这一段睡了多久"；durations 模式用它当权重，加权平均即求和
        acc.feed(hours, end_ts, sleep_source.get(day, ""), weight=hours)
        if end_ts > acc.snap_ts:
            acc.snap_ts = end_ts


def _emit_sleep_clock(asleep_span: Dict[str, List[str]], ds: Dataset, mask: Any,
                      notes: List[str]) -> int:
    """把每夜的 asleep 段跨度落成 3 个「睡眠时刻」指标（入睡 / 醒来 / 睡眠中点）。

    为什么单独做这一步：原有 6 个睡眠指标都是**时长**，回答不了"几点睡"。
    而"几点睡"正是「睡眠规律性」的入口 —— 它比睡眠时长更容易改，
    与恢复状态的关系也更稳，所以值得单列成指标。

    口径（与 catalog 里三条 spec 的 `hint` 一一对应，改这里就要改那里）
    ------------------------------------------------------------
    * `onset` = 当晚所有 `Asleep*` 段里**最早的 start**，`wake` = **最晚的 end**。
      **不包括 `Awake` 与 `InBed`**：它们分别是"躺着没睡着"和"在床上"，
      不是睡眠本身（理由见 `_handle_sleep` 里的注释）。
    * 参考点 = **醒来日的前一天 18:00**，三个指标都是"相对这个参考点的分钟数"。
      时间在午夜处不连续（23:30 与 00:30 只差 60 分钟，按钟点算却是 23 的差），
      用分钟数才能直接算均值、标准差与分位数。换算就是两个 `datetime` 相减，
      不经过 `units.convert`（这里没有"原单位"可换算）。
    * `ts` 取该夜 `wake` 的时间戳；`source` 直接沿用同一天 `sleep_total` 的来源，
      保证与原有睡眠指标口径一致。
    * **跨天防护**：`wake − onset > 16 小时`说明当晚的段里混进了小睡 ——
      Apple 把午睡也记成 `SleepAnalysis`，并按**结束日**归属，于是"当夜"与
      "当天午睡"会被合并到同一天。这时**不产出**这三个指标：混进小睡算出来的
      "入睡时刻"其实是午睡时刻，比不给更糟。提示按"同类只记一次"汇总，
      不逐夜刷屏。
    * **没有 asleep 段的夜晚**（只有 InBed / Awake）自然不在 `asleep_span` 里，
      因此不产出这三个指标；原有指标的数值与来源一个字节都不改。

    返回本次新增的观测条数。
    """
    wanted = [mid for mid in SLEEP_CLOCK_IDS if catalog.get(mid) is not None]
    if not wanted:
        return 0  # catalog 里没有这三条 spec 时不产出，避免造出没有单位口径的观测

    # 同一天 sleep_total 的来源：与原有睡眠指标保持同一个来源口径
    source_of: Dict[str, str] = {}
    for o in ds.observations:
        if o.metric == "sleep_total":
            source_of[o.date] = o.source

    emitted = 0
    skipped = 0
    for day in sorted(asleep_span):
        start_ts, end_ts = asleep_span[day]
        span_hours = _hours_between(start_ts, end_ts)
        if span_hours is None or span_hours <= 0:
            continue
        if span_hours > SLEEP_CLOCK_MAX_SPAN_HOURS:
            skipped += 1
            continue
        onset = _clock_minutes(start_ts, day)
        wake = _clock_minutes(end_ts, day)
        if onset is None or wake is None or wake <= onset:
            continue
        src = source_of.get(day) or mask("Apple Health")
        for metric_id, value in (("sleep_onset_min", onset),
                                 ("sleep_wake_min", wake),
                                 ("sleep_mid_min", (onset + wake) / 2.0)):
            spec = catalog.get(metric_id)
            if spec is None:
                continue
            ds.observations.append(Observation(
                metric=metric_id, date=day, value=float(value), unit=spec.unit,
                source=src, ts=end_ts, ref=None, kind="derived",
            ))
            emitted += 1

    if skipped:
        notes.append(
            f"有 {skipped} 个夜晚的睡着段跨度超过 {SLEEP_CLOCK_MAX_SPAN_HOURS:.0f} 小时"
            f"（多半是把当夜睡眠与当天的小睡合并到了同一天：Apple 把午睡也记成"
            f" SleepAnalysis，并按结束日归属），这些夜晚不产出入睡时刻 / 醒来时刻 / "
            f"睡眠中点三个指标——混进小睡算出来的「入睡时刻」其实是午睡时刻，"
            f"比不给更糟。原有 6 个睡眠时长指标不受影响。"
        )
    return emitted


def _apply_me(a: Dict[str, Any], ds: Dataset) -> None:
    """Me 元素 -> profile 的 birth_date / sex。"""
    dob = _clean(a.get("HKCharacteristicTypeIdentifierDateOfBirth"))
    if dob:
        ds.profile["birth_date"] = dob[:10]
    sex_raw = _clean(a.get("HKCharacteristicTypeIdentifierBiologicalSex"))
    if not sex_raw:
        return
    key = sex_raw.replace("HKBiologicalSex", "").lower()
    sex_cn = {"male": "男", "female": "女", "other": ""}.get(key, "")
    ds.profile["sex"] = sex_cn


def _apply_age(ds: Dataset, last_ts: str, birth_date: str) -> None:
    """年龄 = 数据中最新日期 − 出生日期的整年数。"""
    if not last_ts or not birth_date:
        return
    try:
        born = datetime.strptime(birth_date[:10], "%Y-%m-%d").date()
        latest = datetime.strptime(last_ts[:10], "%Y-%m-%d").date()
    except ValueError:
        ds.add_note(f"无法解析出生日期「{birth_date}」，年龄未推算。")
        return
    years = latest.year - born.year - ((latest.month, latest.day) < (born.month, born.day))
    if years < 0:
        ds.add_note(f"出生日期「{birth_date}」晚于数据日期，年龄未推算。")
        return
    ds.profile["age"] = int(years)


# ──────────────────────────────────────────────────────────── Workout


def _build_workout(a: Dict[str, Any], children: Sequence[Tuple[str, Dict[str, Any]]],
                   ds: Dataset, mask: Any = None) -> Optional[Workout]:
    ts = _local_ts(a.get("startDate", ""))
    day = _date_of(ts)
    if not day:
        return None

    duration_min = units.convert(_to_float(a.get("duration")),
                                 _clean(a.get("durationUnit")) or "min", "分钟")

    dist_raw = _to_float(a.get("totalDistance"))
    dist_unit = _clean(a.get("totalDistanceUnit"))
    distance_km = units.convert(dist_raw, dist_unit, "km") if dist_raw is not None else None

    energy = _to_float(a.get("totalEnergyBurned"))
    energy_unit = _clean(a.get("totalEnergyBurnedUnit")) or "kcal"
    energy = units.convert(energy, energy_unit, "kcal")

    avg_hr = _to_float(a.get("averageHeartRate")) or _to_float(a.get("avgHeartRate"))
    max_hr = _to_float(a.get("maximumHeartRate")) or _to_float(a.get("maxHeartRate"))

    for ctag, cattr in children:
        if ctag != "WorkoutStatistics":
            continue
        st = _hk_type(_clean(cattr.get("type")), APPLE_PREFIX)
        if st == "HeartRate":
            if avg_hr is None:
                avg_hr = _to_float(cattr.get("average"))
            if max_hr is None:
                max_hr = _to_float(cattr.get("maximum"))
        elif st == "ActiveEnergyBurned" and energy is None:
            energy = units.convert(_to_float(cattr.get("sum")), _clean(cattr.get("unit")) or "kcal", "kcal")
        elif st in _DISTANCE_STAT_TYPES and distance_km is None:
            v = _to_float(cattr.get("sum"))
            if v is not None:
                distance_km = units.convert(v, _clean(cattr.get("unit")) or "km", "km")
        # MetadataEntry（HKIndoorWorkout 等）按需求忽略

    pace: Optional[float] = None
    if distance_km and duration_min and distance_km > 0:
        pace = (duration_min * 60.0) / distance_km

    raw_source = _clean(a.get("sourceName"))
    return Workout(
        date=day,
        activity=_activity_cn(_clean(a.get("workoutActivityType"))),
        duration_min=duration_min,
        distance_km=distance_km,
        avg_hr=avg_hr,
        max_hr=max_hr,
        energy_kcal=energy,
        pace_sec_per_km=pace,
        source=(mask(raw_source) if mask is not None else raw_source) or "Apple Health",
        ts=ts,
    )


# ──────────────────────────────────────────────────────────── ActivitySummary


def _use_activity_summary(rows: Sequence[Dict[str, Any]], ds: Dataset) -> None:
    """用 Apple 的每日汇总补齐缺失的日活动量。

    只在**该指标完全没有按日 Record** 时才启用：否则同一份步数会被 Record 与
    ActivitySummary 各算一遍，汇总值直接翻倍。这是本解析器里最容易踩的坑。
    """
    if not rows:
        return
    existing: Dict[str, set] = defaultdict(set)
    for o in ds.observations:
        existing[o.metric].add(o.date)

    filled = 0
    for a in rows:
        day = _date_of(_local_ts(_clean(a.get("dateComponents"))))
        if not day:
            continue
        src = "Apple Health（活动汇总）"
        for field, metric_id in _ACTIVITY_SUMMARY_FIELDS:
            if day in existing.get(metric_id, ()):
                continue  # 当天已有该指标的 Record，跳过，避免重复计数
            raw = a.get(field)
            spec = catalog.get(metric_id)
            if spec is None or raw is None:
                continue
            unit_raw = _clean(a.get(field + "Unit"))
            # appleStandHours 的数值单位是"小时"但字符串写 count，units.convert 认不出来，
            # 这里显式按小时 -> 分钟换算，否则站立时间会以小时数混进"分钟"指标。
            if field == "appleStandHours":
                value = units.convert(_to_float(raw), "小时", spec.unit)
            else:
                value = units.convert(_to_float(raw), unit_raw or spec.unit, spec.unit)
            if value is None:
                continue
            ds.observations.append(
                Observation(metric=metric_id, date=day, value=float(value), unit=spec.unit,
                            source=src, ts=day + " 00:00:00", kind="measured")
            )
            existing[metric_id].add(day)
            filled += 1
    if filled:
        ds.add_note(f"部分日活动量没有按日明细，已用 Apple「活动汇总」(ActivitySummary) 补齐 {filled} 条。")


# ──────────────────────────────────────────────────────────── 总览


def _write_summary(ds: Dataset, path: str, n_records: int, n_workouts: int,
                   first_ts: str, last_ts: str, n_correlations: int,
                   seen_types: Counter, unknown_apples: set) -> None:
    span = f"{first_ts} → {last_ts}" if first_ts else "（没有带时间戳的记录）"
    metrics = sorted(ds.metric_ids())
    ds.add_note(
        f"Apple 健康数据总览：原始 Record {n_records} 条，聚合后 {len(ds.observations)} 条观测，"
        f"识别到 {len(metrics)} 个指标，训练 {n_workouts} 条，时间跨度 {span}。"
    )
    if n_correlations:
        ds.add_note(
            f"导出文件里有 {n_correlations} 条 Correlation（如血压、体重指数这类复合条目），"
            "本版本不解析其中的数值，只做统计。"
        )
    if not ds.observations and not ds.workouts:
        ds.add_note(f"没有从 {os.path.basename(path)} 里解析出任何记录，请确认这是 Apple 健康的 export.xml。")

    unknowns = sorted(unknown_apples)
    if unknowns:
        head = "、".join(unknowns[:20])
        more = f"（另有 {len(unknowns) - 20} 种未列出）" if len(unknowns) > 20 else ""
        ds.add_note(
            f"数据里有 {len(unknowns)} 种本项目暂不认识的 HK 类型，未进入报告：{head}{more}。"
            "若其中有你在意的指标，可在 catalog.py 里补一条 MetricSpec。"
        )


# ──────────────────────────────────────────────────────────── 兼容性别名

# 参考实现里的名字，方便习惯旧脚本的人
local_ts = _local_ts
