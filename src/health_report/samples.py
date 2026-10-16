"""合成样例数据。

这一份数据同时承担三个职责：

1. `health-report demo` 的输入 —— 不装任何东西、不联网、不需要自备文件，
   跑一条命令就能看到完整报告长什么样（30 秒可验证）。
2. `examples/` 里示例文件的来源 —— 用 `write_apple_xml` / `write_inbody_csv`
   生成，让解析器有可复现的测试夹具。
3. 规则层的测试夹具 —— **数据陷阱是故意埋进去的**，每一处都对应
   `docs/PITFALLS.md` 里的一条规则，见下面的对照表。

⚠️ 这里没有任何真实个人数据。身高、体重、日期、设备名都是编的，
和仓库作者本人的体测数据无关。数值是按公式确定性生成的，不用随机数，
所以每次跑出来的报告都一样，便于对比 diff。

埋进去的陷阱与预期触发的规则
--------------------------
| 数据特征                                   | 预期规则                    |
|--------------------------------------------|-----------------------------|
| 体重同时来自「智能秤」与「InBody270」        | SRC_MIXED                   |
| 同一天两台设备体重差 2.4 kg                 | SAME_DAY_CONFLICT           |
| 上面两条同时让这两个指标**不做判定**          | verdict_note = 来源不一致    |
| 5 月 20 日身体总水分 44.0 L（上限 43.4）     | HYDRATION_AMPLIFIES         |
| 5 月 20 日水分导致 BMI 判定与数值冲突        | LABEL_CONFLICT              |
| 左右腿阻抗 +30% 以上                        | IMPEDANCE_SHIFT             |
| 腿部节段百分比的隐含标准值漂移 7~8%          | PCT_BASELINE_SHIFT          |
| 节段同时有 kg 与百分比两种形式               | PCT_BASELINE_CAVEAT         |
| 步数只有 69% 的日子有数据、最新一天低于目标   | MISSING_DAYS + RANGE_OUT(goal) |
| 腰围只有 1 个数据点                         | TREND_TOO_SHORT             |
| 静息心率 54 次/分（低于筛查下限 60）         | RANGE_OUT(screen)           |
| HRV、深睡等没有公认参考区间                  | ref_origin == "none"        |

注意 **没有**埋 `MASS_BALANCE`：这份合成数据的
`水分 + 蛋白质 + 无机盐 + 体脂肪 == 体重` 在两次测量里都成立（这是设备的自检恒等式，
真实报告也总是成立的）。那条规则的正反两个方向都在 `tests/test_analysis.py` 里单测，
不在 demo 里制造假故障。

另外，`synthetic_dataset()` 里的**睡眠时刻**三个指标（入睡 / 醒来 / 睡眠中点）
刻意带上了逐日波动，并保留一个每 7 天一次的「周末晚睡」模式 —— 这样下游的
「睡眠规律性」在 demo 报告里就有东西可算，而不是一条直线。
"""

from __future__ import annotations

import math
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .model import Dataset, Observation, RefRange, Workout

# ── 时间与身份（全部虚构） ────────────────────────────────

INBODY_A_DATE = "2026-01-10"
INBODY_B_DATE = "2026-05-20"
DAILY_START = date(2026, 5, 1)
DAILY_DAYS = 61

PROFILE = {"height_cm": 172.0, "age": 28, "sex": "男", "birth_date": "1998-03-15"}

SCALE_SOURCE = "智能秤"
INBODY_SOURCE = "InBody270"
WATCH_SOURCE = "Apple Watch"

#: 合成 XML 里整夜作息的逐日位移（分钟，按 7 天循环）。
#:
#: 为什么需要它：若每晚作息完全相同，`sleep_onset_min` / `sleep_wake_min` /
#: `sleep_mid_min` 就每晚一致，**睡眠规律性恒为 0**——那条路径等于没被测到。
#: 位移作用在整夜所有时刻上，所以各段**时长不变**，原有睡眠时长指标的断言不受影响。
#: 第 0 项必须是 0，这样"首夜 onset=310 / wake=765 / mid=537.5"三个被测试
#: 钉住的期望值保持不变。
_SLEEP_SHIFT_MIN: Tuple[int, ...] = (0, 0, 25, 45, -30, -60, 45)


def _d(i: int) -> str:
    return (DAILY_START + timedelta(days=i)).isoformat()


def _slug(name: str) -> str:
    """稳定的伪 metric id，只用于样例生成，不参与 catalog。"""
    return name


# ── InBody 两次测量的数值（虚构） ─────────────────────────

# 设备报告参考范围。身体总水分故意让 5 月那次越界。
_R_WATER = RefRange(low=35.5, high=43.4, text="35.5~43.4", source="设备报告参考范围")
_R_MASS = RefRange(low=57.0, high=76.0, text="57.0~76.0", source="设备报告参考范围")
_R_BMI = RefRange(low=18.5, high=23.0, text="18.5~23.0", source="设备报告参考范围")
_R_PBF = RefRange(low=10.0, high=20.0, text="10.0~20.0", source="设备报告参考范围")
_R_FAT = RefRange(low=7.3, high=14.6, text="7.3~14.6", source="设备报告参考范围")
_R_PROTEIN = RefRange(low=9.2, high=11.2, text="9.2~11.2", source="设备报告参考范围")
_R_MINERAL = RefRange(low=3.18, high=3.88, text="3.18~3.88", source="设备报告参考范围")
_R_VISCERAL = RefRange(high=10, high_inclusive=False, text="<10", source="设备报告参考范围")
_R_WHR = RefRange(low=0.80, high=0.90, text="0.80~0.90", source="设备报告参考范围")

_INBODY_ROWS: List[Tuple[str, RefRange, Optional[float], Optional[float]]] = [
    # (metric_id, ref, A 值, B 值)
    #
    # ⚠️ 体成分仪的四个非水分成分必须加总等于体重，否则 MASS_BALANCE 会报"不闭合"
    #    （这是设备的自检恒等式）。所以每次测量的
    #        水分 + 蛋白质 + 无机盐 + 体脂肪 == 体重
    #    都要成立，且 体脂率 = 体脂肪 / 体重，去脂体重 = 体重 − 体脂肪。
    #    A: 42.5 + 11.5 + 3.50 + 16.5 = 74.0；16.5/74.0 = 22.3%；74.0−16.5 = 57.5
    #    B: 44.0 + 11.6 + 3.60 + 12.0 = 71.2；12.0/71.2 = 16.9%；71.2−12.0 = 59.2
    ("body_mass", _R_MASS, 74.0, 71.2),
    ("bmi", _R_BMI, 25.0, 24.1),
    ("body_fat_pct", _R_PBF, 22.3, 16.9),
    ("body_fat_mass", _R_FAT, 16.5, 12.0),
    ("lean_body_mass", None, 57.5, 59.2),
    ("skeletal_muscle_mass", None, 32.5, 33.1),
    ("total_body_water", _R_WATER, 42.5, 44.0),
    ("protein_mass", _R_PROTEIN, 11.5, 11.6),
    ("mineral_mass", _R_MINERAL, 3.50, 3.60),
    ("bmr", None, 1600.0, 1615.0),
    ("visceral_fat_level", _R_VISCERAL, 8.0, 6.0),
    ("whr", _R_WHR, 0.88, 0.86),
    ("smi", None, 8.8, 8.6),
    ("inbody_score", None, 78.0, 80.0),
    # 分节段肌肉量：kg 与百分比两种形式都在（触发 PCT_BASELINE_CAVEAT）
    # 腿部百分比的隐含标准值在两次之间漂移 7~8%（触发 PCT_BASELINE_SHIFT）
    ("lean_left_arm", None, 3.30, 3.45),
    ("lean_right_arm", None, 3.35, 3.50),
    ("lean_trunk", None, 26.0, 27.2),
    ("lean_left_leg", None, 9.10, 8.40),
    ("lean_right_leg", None, 9.20, 8.50),
    ("lean_pct_left_arm", None, 104.0, 109.0),
    ("lean_pct_right_arm", None, 105.0, 110.0),
    ("lean_pct_trunk", None, 107.0, 112.0),
    ("lean_pct_left_leg", None, 108.0, 92.0),
    ("lean_pct_right_leg", None, 109.0, 94.0),
    ("fat_left_arm", None, 0.7, 0.4),
    ("fat_right_arm", None, 0.7, 0.4),
    ("fat_trunk", None, 7.5, 6.1),
    ("fat_left_leg", None, 2.2, 1.8),
    ("fat_right_leg", None, 2.3, 1.9),
    # 生物电阻抗：下肢 +30% 以上（触发 IMPEDANCE_SHIFT）
    ("z_right_arm_20k", None, 240.0, 246.0),
    ("z_left_arm_20k", None, 242.0, 248.0),
    ("z_trunk_20k", None, 25.0, 25.5),
    ("z_right_leg_20k", None, 190.0, 250.0),
    ("z_left_leg_20k", None, 195.0, 255.0),
    ("z_right_arm_100k", None, 210.0, 215.0),
    ("z_left_arm_100k", None, 212.0, 217.0),
    ("z_trunk_100k", None, 21.0, 21.5),
    ("z_right_leg_100k", None, 168.0, 218.0),
    ("z_left_leg_100k", None, 172.0, 222.0),
]


# ── 组装 Dataset ─────────────────────────────────────────


def synthetic_dataset() -> Dataset:
    """生成完整的合成数据集。确定性：同样的输入永远得到同样的输出。"""
    ds = Dataset()
    ds.profile.update(PROFILE)
    ds.files.append("(内置合成样例)")
    ds.add_note("本报告基于内置合成数据生成，不含任何真实个人数据。")

    # ── InBody 两次测量 ──────────────────────────────────
    spec_units = _spec_units()
    for metric, ref, va, vb in _INBODY_ROWS:
        unit = spec_units.get(metric, "")
        for d, v in ((INBODY_A_DATE, va), (INBODY_B_DATE, vb)):
            if v is None:
                continue
            ds.observations.append(Observation(
                metric=metric, date=d, value=float(v), unit=unit,
                source=INBODY_SOURCE, ts=f"{d} 07:30:00", ref=ref,
            ))

    # 设备自己的文字判定：BMI 24.1 落在设备区间内却标「正常」，
    # 但按数值它已经超出上限 —— 触发 LABEL_CONFLICT。
    ds.labels["bmi_judgement@2026-01-10"] = "超重"
    ds.labels["bmi_judgement@2026-05-20"] = "正常"
    ds.labels["body_fat_pct_judgement@2026-05-20"] = "正常"

    # ── Apple 健康风格的每日数据 ─────────────────────────
    for i in range(DAILY_DAYS):
        d = _d(i)

        # 累加型：步数刻意缺 1/3 的日子（覆盖率 ≈ 67%，触发 MISSING_DAYS），
        # 并且有数据日的均值低于 8000 步目标，好让报告里能看到「未达目标」
        # 这条措辞（goal 与 range 的区别正是靠它体现的）。
        if i % 3 != 0:
            ds.observations.append(_obs("steps", d, 7200 + 2200 * math.sin(i / 5.0), 0, WATCH_SOURCE))
        ds.observations.append(_obs("active_energy", d, 480 + 120 * math.sin(i / 4.0), 0, WATCH_SOURCE))
        ds.observations.append(_obs("basal_energy", d, 1650 + 18 * math.sin(i / 9.0), 0, WATCH_SOURCE))
        ds.observations.append(_obs("exercise_minutes", d, max(0.0, 38 + 18 * math.sin(i / 3.0)), 0, WATCH_SOURCE))
        ds.observations.append(_obs("flights_climbed", d, 9.0, 0, WATCH_SOURCE))

        # 快照型
        #
        # ⚠️ 静息心率与 HRV 刻意**取决于前一晚的睡眠时长**，而且量级按现实取
        # （睡眠每多 1 小时，次日静息心率约低 5 次/分、HRV 约高 10 ms —— 都取在
        # 文献里偏强的一端，但仍在可信范围内）。
        #
        # 为什么必须植入这个关系：如果两者各自用不同频率的正弦波，它们之间
        # 就没有真实关联，那么"睡眠 → 次日恢复"这一节的证据等级会永远是
        # 「仅报数」，等于**展示不出这条路径**。夹具必须能触发它，
        # 否则这条功能在 demo 里看起来像没做。
        # （同类教训：sleep_awake 曾因夹具每夜只有一段 Awake 而漏掉一个 bug。）
        sleep_prev = 7.30 + 0.45 * math.sin((i - 1) / 4.0) if i > 0 else 7.30
        ds.observations.append(_obs("resting_hr", d, 54.5 - 5.0 * (sleep_prev - 7.30), 0, WATCH_SOURCE))
        if i % 2 == 0:
            ds.observations.append(_obs("hrv_sdnn", d, 48 + 10.0 * (sleep_prev - 7.30) + 3 * math.cos(i / 5.0), 1, WATCH_SOURCE))
        if i % 10 == 0:
            ds.observations.append(_obs("vo2max", d, 46.0 + i * 0.042, 1, WATCH_SOURCE))

        # 智能秤：和 InBody 是两套测量系统（触发 SRC_MIXED）
        scale_mass = 74.0 - i * 0.022 + 0.25 * math.sin(i / 3.0)
        ds.observations.append(_obs("body_mass", d, scale_mass, 1, SCALE_SOURCE))
        ds.observations.append(_obs("body_fat_pct", d, 21.8 - i * 0.012 + 0.4 * math.sin(i / 4.0), 1, SCALE_SOURCE))

        # 睡眠：归到醒来那天
        ds.observations.append(_obs("sleep_total", d, 7.30 + 0.45 * math.sin(i / 4.0), 2, WATCH_SOURCE))
        ds.observations.append(_obs("sleep_deep", d, 1.05 + 0.15 * math.sin(i / 5.0), 2, WATCH_SOURCE))
        ds.observations.append(_obs("sleep_rem", d, 1.60 + 0.20 * math.sin(i / 6.0), 2, WATCH_SOURCE))
        ds.observations.append(_obs("sleep_core", d, 4.60 + 0.25 * math.sin(i / 4.0), 2, WATCH_SOURCE))
        ds.observations.append(_obs("sleep_awake", d, 0.30 + 0.10 * math.sin(i / 7.0), 2, WATCH_SOURCE))

        # 睡眠时刻（入睡 / 醒来 / 睡眠中点）：取值是「醒来日的前一天 18:00 起算的分钟数」，
        # 不是钟点 —— 时间在午夜处不连续，23:30 与 00:30 按钟点算会得到 23 的差。
        # 这里刻意给入睡时刻加上逐日波动（基准 325 ± 25 分钟，即 300~350），
        # 并保留一个明显的「周末晚睡」模式：每 7 天有一天晚 60 分钟。
        # 这样下游的「睡眠规律性」才有东西可算：中位入睡时刻很稳，
        # 但标准差与分位数能看出那一天的偏移（睡眠中点也因此一起移动）。
        # 三个值必须自洽：mid = (onset + wake) / 2。
        # 注意入睡与醒来都是**整数分钟**，中点是它俩的平均（可能是 .5），
        # 所以 `mid == (onset + wake) / 2` 在取整之后仍然精确成立。
        onset_min = float(round(325.0 + 25.0 * math.sin(i / 5.0), 0))
        if i % 7 == 6:
            onset_min += 60.0   # 周末晚睡：这一天的入睡时刻明显偏晚
        wake_min = float(round(770.0 + 20.0 * math.sin(i / 4.0), 0))
        ds.observations.append(_obs("sleep_onset_min", d, onset_min, 0, WATCH_SOURCE))
        ds.observations.append(_obs("sleep_wake_min", d, wake_min, 0, WATCH_SOURCE))
        ds.observations.append(_obs("sleep_mid_min", d, (onset_min + wake_min) / 2.0, 1, WATCH_SOURCE))

    # 身高：单点，属「基本」类，不参与趋势判定
    ds.observations.append(Observation(
        metric="height_cm", date=DAILY_START.isoformat(), value=172.0, unit="cm",
        source=WATCH_SOURCE, ts=f"{DAILY_START.isoformat()} 08:00:00",
    ))
    # 腰围：只有 1 个点 —— 不足以谈趋势
    ds.observations.append(Observation(
        metric="waist_circumference", date=_d(DAILY_DAYS - 1), value=84.0, unit="cm",
        source="软尺", ts=f"{_d(DAILY_DAYS - 1)} 07:00:00",
    ))

    # ── 训练 ─────────────────────────────────────────────
    for i in range(DAILY_DAYS):
        d = _d(i)
        if i % 5 == 0:
            km = 5.2 + (i % 3) * 0.9
            ds.workouts.append(Workout(
                date=d, activity="跑步", duration_min=km * 5.6, distance_km=km,
                avg_hr=150.0 + (i % 4), max_hr=174.0 + (i % 3),
                energy_kcal=km * 66, pace_sec_per_km=5.6 * 60,
                source=WATCH_SOURCE, ts=f"{d} 18:30:00",
            ))
        if i % 11 == 3:
            km = 24.0 + (i % 4) * 3.0
            ds.workouts.append(Workout(
                date=d, activity="骑行", duration_min=km * 2.1, distance_km=km,
                avg_hr=126.0, max_hr=152.0, energy_kcal=km * 22,
                pace_sec_per_km=2.1 * 60, source=WATCH_SOURCE, ts=f"{d} 09:00:00",
            ))
        if i % 7 == 2:
            ds.workouts.append(Workout(
                date=d, activity="传统力量训练", duration_min=45.0,
                avg_hr=118.0, max_hr=142.0, energy_kcal=260.0,
                source=WATCH_SOURCE, ts=f"{d} 20:00:00",
            ))

    return ds.dedupe()


def _obs(metric: str, d: str, value: float, precision: int, source: str) -> Observation:
    return Observation(
        metric=metric, date=d, value=round(value, precision),
        unit=_spec_units().get(metric, ""), source=source, ts=f"{d} 21:00:00",
    )


_UNITS_CACHE: Optional[Dict[str, str]] = None


def _spec_units() -> Dict[str, str]:
    """从 catalog 取每个指标的规范单位。延迟导入以免循环依赖。"""
    global _UNITS_CACHE
    if _UNITS_CACHE is None:
        from . import catalog
        _UNITS_CACHE = {s.id: s.unit for s in catalog.SPECS}
    return _UNITS_CACHE


# ── 生成示例文件 ─────────────────────────────────────────


def write_inbody_csv(path: str) -> str:
    """写出 InBody 报告长格式 CSV（和 01_InBody体测\\InBody数据对比.csv 同构）。"""
    from . import catalog

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    lines = ["分类,指标,单位,%s,%s,变化量,报告参考范围,备注" % (INBODY_A_DATE, INBODY_B_DATE)]

    def row(cat: str, name: str, unit: str, a, b, ref: str = "—", note: str = "—") -> None:
        def f(v):
            if v is None:
                return "—"
            if isinstance(v, str):
                return v
            s = f"{v:.4f}".rstrip("0").rstrip(".")
            return s or "0"

        delta = ""
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            dv = b - a
            delta = ("+" if dv > 0 else "") + f(dv)
        lines.append(",".join([cat, name, unit, f(a), f(b), delta or "—", ref, note]))

    row("基本信息", "身高", "cm", 172, 172, "—", "—")
    row("基本信息", "年龄", "岁", 28, 28, "—", "—")
    row("基本信息", "性别", "—", "男", "男", "—", "—")

    cat_of = {s.id: s.category for s in catalog.SPECS}
    name_of = {s.id: s.cn for s in catalog.SPECS}
    for metric, ref, va, vb in _INBODY_ROWS:
        r = ref.label() if ref else "—"
        row(cat_of.get(metric, "研究项目"), name_of.get(metric, metric),
            catalog.get(metric).unit if catalog.get(metric) else "—", va, vb, r)
    row("肥胖评估", "BMI判定", "—", "超重", "正常", "—", "—")
    row("肥胖评估", "体脂百分比判定", "—", "偏高", "正常", "—", "—")

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(lines) + "\n")
    return path


def write_apple_xml(path: str) -> str:
    """写出 Apple 健康导出格式的 XML（结构真实，内容是虚构的）。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    out: List[str] = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<!DOCTYPE HealthData [')

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;"))

    out.append("]>")
    out.append('<HealthData locale="zh_CN">')
    out.append(f' <ExportDate value="{DAILY_START.isoformat()} 12:00:00 +0800"/>')
    out.append(' <Me HKCharacteristicTypeIdentifierDateOfBirth="1998-03-15"'
               ' HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexMale"'
               ' HKCharacteristicTypeIdentifierBloodType="HKBloodTypeUnknown"'
               ' HKCharacteristicTypeIdentifierFitzpatrickSkinType="HKFitzpatrickSkinTypeNotSet"/>')

    def rec(rtype: str, source: str, unit: str, value, start: str, end: str) -> None:
        out.append(
            f' <Record type="{rtype}" sourceName="{esc(source)}" unit="{unit}"'
            f' creationDate="{start} +0800" startDate="{start} +0800" endDate="{end} +0800"'
            f' value="{value}"/>'
        )

    def stamp(d: str, hm: str) -> str:
        return f"{d} {hm}"

    # 每日指标
    for i in range(DAILY_DAYS):
        d = _d(i)
        if i % 3 != 0:
            rec("HKQuantityTypeIdentifierStepCount", WATCH_SOURCE, "count",
                int(9000 + 2500 * math.sin(i / 5.0)), stamp(d, "07:00:00"), stamp(d, "22:00:00"))
        rec("HKQuantityTypeIdentifierActiveEnergyBurned", WATCH_SOURCE, "kcal",
            round(480 + 120 * math.sin(i / 4.0), 1), stamp(d, "00:05:00"), stamp(d, "23:55:00"))
        rec("HKQuantityTypeIdentifierBasalEnergyBurned", WATCH_SOURCE, "kcal",
            round(1650 + 18 * math.sin(i / 9.0), 1), stamp(d, "00:00:00"), stamp(d, "23:59:00"))
        rec("HKQuantityTypeIdentifierAppleExerciseTime", WATCH_SOURCE, "min",
            int(max(0.0, 38 + 18 * math.sin(i / 3.0))), stamp(d, "07:00:00"), stamp(d, "22:00:00"))
        rec("HKQuantityTypeIdentifierFlightsClimbed", WATCH_SOURCE, "count", 9,
            stamp(d, "07:00:00"), stamp(d, "22:00:00"))
        rec("HKQuantityTypeIdentifierRestingHeartRate", WATCH_SOURCE, "count/min",
            round(54.5 + 1.2 * math.sin(i / 6.0), 0), stamp(d, "06:00:00"), stamp(d, "06:00:00"))
        if i % 2 == 0:
            rec("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", WATCH_SOURCE, "ms",
                round(48 + 7 * math.sin(i / 5.0), 1), stamp(d, "06:10:00"), stamp(d, "06:10:00"))
        if i % 10 == 0:
            rec("HKQuantityTypeIdentifierVO2Max", WATCH_SOURCE, "ml/min/kg",
                round(46.0 + i * 0.042, 2), stamp(d, "07:00:00"), stamp(d, "07:00:00"))
        rec("HKQuantityTypeIdentifierBodyMass", SCALE_SOURCE, "kg",
            round(74.0 - i * 0.022 + 0.25 * math.sin(i / 3.0), 2), stamp(d, "07:10:00"), stamp(d, "07:10:00"))
        rec("HKQuantityTypeIdentifierBodyFatPercentage", SCALE_SOURCE, "%",
            round(21.8 - i * 0.012 + 0.4 * math.sin(i / 4.0), 2), stamp(d, "07:10:00"), stamp(d, "07:10:00"))

        # 睡眠：前一夜 23:10 开始，醒来的日期 = d。
        #
        # ⚠️ 这里刻意让「夜间清醒」和「卧床」各有**两段**。原因：解析器一度把
        # 这两个指标当"按段取平均"而不是"按段求和"，于是真实数据里
        # sleep_awake 中位数掉到 2 分钟/夜、sleep_inbed 掉到 0.66 小时。
        # 那个 bug 之所以没被测试抓到，正是因为当时的夹具每夜只有一段 Awake、
        # 而且没有 InBed 记录——只有一段时"求和"和"求平均"结果一样。
        # 夹具必须能区分这两者，否则这个 bug 会悄悄回来。
        #   求和：awake = 0.167+0.167 = 0.333 h，inbed = 3.667+4.583 = 8.25 h
        #   平均：awake = 0.167 h，inbed = 4.125 h
        #
        # 这四段 asleep 也决定了 3 个「睡眠时刻」指标的期望值（参考点 = 醒来日
        # 的前一天 18:00，即 2026-05-01 那夜的参考点是 2026-04-30 18:00）：
        #   onset = 最早的 asleep start = 前一夜 23:10 -> 相对参考点 5h10m = 310
        #   wake  = 最晚的 asleep end   = 当日 06:45 -> 相对参考点 12h45m = 765
        #           （注意**不是** 07:05：那是 InBed 的结束，InBed 不算睡眠本身）
        #   mid   = (310 + 765) / 2 = 537.5
        # tests/test_parsers.py 与 tools/_check_sleep_metrics.py 里钉的就是这三个数。
        prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()

        # 整夜作息按周位移（分钟）：模拟"周末晚睡、工作日起早"。
        #
        # ⚠️ 为什么必须加这个位移：如果每晚都是固定的 23:10→06:45，那么
        # sleep_onset_min / sleep_wake_min / sleep_mid_min 每晚完全相同，
        # **睡眠规律性算出来标准差恒为 0**——那条代码路径等于没有被测到。
        # （同类教训：sleep_awake 曾因夹具每夜只有一段 Awake 而漏掉一个 bug。）
        #
        # 位移作用在**整夜的所有时刻**上，所以每一段的**时长不变**，
        # 原有 6 个睡眠时长指标（含 sleep_awake=1/3h、sleep_inbed=8.25h）
        # 的数值不受影响，相关断言全部保住。
        # 第 0 夜位移为 0，所以"首夜 onset=310 / wake=765 / mid=537.5"
        # 这三个被测试钉住的期望值也不变。
        shift_min = _SLEEP_SHIFT_MIN[i % len(_SLEEP_SHIFT_MIN)]

        def stamp_shifted(day: str, hm: str) -> str:
            """把 (日期, 时刻) 整体平移 shift_min 分钟。

            ⚠️ 必须用**真正的日期时间算术**，不能手工拼 `HH:MM:SS` 字符串。
            第一版是手算 `总分钟 // 60`，结果 `23:10 + 60 分` 变成非法的
            `24:10:00`——解析器按小时 24 解析失败，那一整段被丢掉，
            于是 17 个夜晚的 sleep_total / sleep_core 数值变了。
            用 datetime 做加减就不会有这种越界问题。
            """
            dt = datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M:%S")
            dt = dt + timedelta(minutes=shift_min)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

        segments = [
            ("HKCategoryValueSleepAnalysisInBed", stamp_shifted(prev, "22:50:00"), stamp_shifted(d, "02:30:00")),
            ("HKCategoryValueSleepAnalysisInBed", stamp_shifted(d, "02:30:00"), stamp_shifted(d, "07:05:00")),
            ("HKCategoryValueSleepAnalysisAsleepCore", stamp_shifted(prev, "23:10:00"), stamp_shifted(d, "02:00:00")),
            ("HKCategoryValueSleepAnalysisAsleepDeep", stamp_shifted(d, "02:00:00"), stamp_shifted(d, "03:05:00")),
            ("HKCategoryValueSleepAnalysisAsleepREM", stamp_shifted(d, "03:05:00"), stamp_shifted(d, "04:35:00")),
            ("HKCategoryValueSleepAnalysisAsleepCore", stamp_shifted(d, "04:35:00"), stamp_shifted(d, "06:45:00")),
            ("HKCategoryValueSleepAnalysisAwake", stamp_shifted(d, "06:45:00"), stamp_shifted(d, "06:55:00")),
            ("HKCategoryValueSleepAnalysisAwake", stamp_shifted(d, "06:55:00"), stamp_shifted(d, "07:05:00")),
        ]
        for value, s, e in segments:
            out.append(
                f' <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="{esc(WATCH_SOURCE)}"'
                f' creationDate="{e} +0800" startDate="{s} +0800" endDate="{e} +0800" value="{value}"/>'
            )

    # 训练
    for i in range(DAILY_DAYS):
        d = _d(i)
        if i % 5 == 0:
            km = 5.2 + (i % 3) * 0.9
            mins = km * 5.6
            out.append(
                f' <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="{mins:.2f}"'
                f' durationUnit="min" totalDistance="{km:.2f}" totalDistanceUnit="km"'
                f' totalEnergyBurned="{km * 66:.0f}" totalEnergyBurnedUnit="kcal"'
                f' sourceName="{esc(WATCH_SOURCE)}"'
                f' startDate="{d} 18:30:00 +0800" endDate="{d} 19:30:00 +0800">'
            )
            out.append(f'  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"'
                       f' startDate="{d} 18:30:00 +0800" endDate="{d} 19:30:00 +0800"'
                       f' average="{150 + (i % 4)}" minimum="98" maximum="{174 + (i % 3)}" unit="count/min"/>')
            out.append(' </Workout>')
        if i % 11 == 3:
            km = 24.0 + (i % 4) * 3.0
            out.append(
                f' <Workout workoutActivityType="HKWorkoutActivityTypeCycling" duration="{km * 2.1:.2f}"'
                f' durationUnit="min" totalDistance="{km:.2f}" totalDistanceUnit="km"'
                f' totalEnergyBurned="{km * 22:.0f}" totalEnergyBurnedUnit="kcal"'
                f' sourceName="{esc(WATCH_SOURCE)}"'
                f' startDate="{d} 09:00:00 +0800" endDate="{d} 10:30:00 +0800">'
            )
            out.append(f'  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"'
                       f' startDate="{d} 09:00:00 +0800" endDate="{d} 10:30:00 +0800"'
                       f' average="126" minimum="92" maximum="152" unit="count/min"/>')
            out.append(' </Workout>')
        if i % 7 == 2:
            out.append(
                f' <Workout workoutActivityType="HKWorkoutActivityTypeTraditionalStrengthTraining"'
                f' duration="45.00" durationUnit="min" totalEnergyBurned="260" totalEnergyBurnedUnit="kcal"'
                f' sourceName="{esc(WATCH_SOURCE)}"'
                f' startDate="{d} 20:00:00 +0800" endDate="{d} 20:45:00 +0800">'
            )
            out.append(f'  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate"'
                       f' startDate="{d} 20:00:00 +0800" endDate="{d} 20:45:00 +0800"'
                       f' average="118" minimum="88" maximum="142" unit="count/min"/>')
            out.append(' </Workout>')

    out.append('</HealthData>')
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    return path


def write_all(outdir: str) -> List[str]:
    """把全部示例文件写到 outdir，返回写出的路径列表。"""
    os.makedirs(outdir, exist_ok=True)
    return [
        write_inbody_csv(os.path.join(outdir, "inbody_sample.csv")),
        write_apple_xml(os.path.join(outdir, "apple_export_sample.xml")),
    ]
