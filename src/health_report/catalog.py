"""指标目录：把各设备千奇百怪的字段名，归一成统一的指标 ID。

这是本项目的核心资产。三条规则：

1. **一个指标只有一个 ID 和一个单位。** 解析阶段做单位换算，下游永远
   见不到"磅/斤/公斤混着来"的问题。

2. **参考区间有优先级：设备自带的 > 内置人群参考表 > 没有。**
   第三档不是失败，而是正确的诚实：没有参考区间就只报趋势，
   不硬编一个"正常"出来。医生看的化验单上每个项目都有自己的参考范围，
   体成分设备也一样——设备自带的区间比通用表更贴近这台机器的模型。

3. **区间分三种性质，报告里措辞不同：**
   `range` 正常参考区间 / `goal` 活动量目标 / `screen` 筛查阈值。
   没走到 8000 步不等于"指标异常"，把两者混为一谈是这类工具最常见的错误。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .model import RefRange

# ──────────────────────────────────────────────────────────── 类别


CATEGORY_ORDER: Tuple[str, ...] = (
    "基本",
    "体成分",
    "肥胖评估",
    "肌肉均衡",
    "节段脂肪",
    "心肺与恢复",
    "活动量",
    "睡眠",
    "生物电阻抗",
    "设备建议与评分",
)

CATEGORY_HINT: Dict[str, str] = {
    "基本": "身高、年龄、性别等，作为其它指标的解释前提。",
    "体成分": "来自体成分仪（InBody 等）或智能秤的全身测量。",
    "肥胖评估": "以体重、脂肪与腰围为基础的筛查性指标。",
    "肌肉均衡": "分节段的肌肉量。注意：百分比基准可能随设备模型变化，跨时间只比 kg。",
    "节段脂肪": "分节段的脂肪量。同样只跨时间比 kg。",
    "心肺与恢复": "静息心率、心率变异性、最大摄氧量等。受训练与睡眠影响大。",
    "活动量": "按日累计的步数、能量、锻炼时长。这里给的是目标，不是正常范围。",
    "睡眠": "按日汇总的睡眠时长与分期。",
    "生物电阻抗": "设备原始读数。它们的漂移会解释体成分读数的漂移，本身不做判定。",
    "设备建议与评分": "设备给出的建议值与综合评分。不是测量结果，不参与范围判定。",
}


# ──────────────────────────────────────────────────────────── 指标定义


@dataclass(frozen=True)
class MetricSpec:
    id: str
    cn: str
    unit: str
    category: str
    agg: str = "snapshot"          # snapshot（当日取最后一条）| sum（当日累加）| mean（当日平均）
    precision: int = 1
    aliases: Tuple[str, ...] = ()
    apple_types: Tuple[str, ...] = ()
    verdict: bool = True           # 是否参与"是否落在参考区间内"的判定
    noise: float = 0.0             # 小于此变化量视为测量噪声，不作为变化解读
    # 内置参考区间（仅在没有设备自带区间时启用）
    ref_low: Optional[float] = None
    ref_high: Optional[float] = None
    ref_low_inclusive: bool = True
    ref_high_inclusive: bool = True
    ref_kind: str = "range"
    ref_source: str = ""
    ref_note: str = ""
    ref_text: str = ""             # 区间文字不便于由数字推出时用（例如 "<10"）
    ref_fn: str = ""               # 需要年龄/性别时，指向下面的 _REF_FNS
    hint: str = ""


# ── 内置参考区间的动态函数（依赖年龄 / 性别） ────────────────


def _sex(profile: Dict[str, Any]) -> str:
    raw = str(profile.get("sex") or "").strip().lower()
    if raw in ("m", "male", "男", "男性", "1"):
        return "male"
    if raw in ("f", "female", "女", "女性", "0"):
        return "female"
    return ""


def _age(profile: Dict[str, Any]) -> Optional[int]:
    try:
        return int(profile.get("age"))
    except (TypeError, ValueError):
        return None


def _ref_body_fat(profile: Dict[str, Any]) -> Optional[RefRange]:
    s = _sex(profile)
    if s == "male":
        return RefRange(low=10, high=20, source="体脂率分级（成年男性，粗略参考区间）",
                        kind="range",
                        text="",
                        )
    if s == "female":
        return RefRange(low=18, high=28, source="体脂率分级（成年女性，粗略参考区间）", kind="range")
    return None  # 不知道性别就不给区间，比给错强


def _ref_smi(profile: Dict[str, Any]) -> Optional[RefRange]:
    s = _sex(profile)
    if s == "male":
        return RefRange(low=7.0, source="AWGS 2019 四肢骨骼肌量指数筛查阈值（男性 <7.0 需进一步评估）",
                        kind="screen")
    if s == "female":
        return RefRange(low=5.7, source="AWGS 2019 四肢骨骼肌量指数筛查阈值（女性 <5.7 需进一步评估）",
                        kind="screen")
    return None


def _ref_waist(profile: Dict[str, Any]) -> Optional[RefRange]:
    s = _sex(profile)
    if s == "male":
        return RefRange(high=90, source="中国成人腰围判定（男性 ≥90 cm 为中心性肥胖）", kind="screen")
    if s == "female":
        return RefRange(high=85, source="中国成人腰围判定（女性 ≥85 cm 为中心性肥胖）", kind="screen")
    return None


def _ref_whr(profile: Dict[str, Any]) -> Optional[RefRange]:
    s = _sex(profile)
    if s == "male":
        return RefRange(high=0.90, source="WHO 腰臀比（男性 ≥0.90 风险升高）", kind="screen")
    if s == "female":
        return RefRange(high=0.85, source="WHO 腰臀比（女性 ≥0.85 风险升高）", kind="screen")
    return None


# Cooper Institute 有氧能力常模的近似中位水平（参考用，非诊断）
_VO2_LOW = {
    "male": [(29, 42.5), (39, 40.5), (49, 36.5), (59, 32.5), (200, 28.0)],
    "female": [(29, 36.5), (39, 34.5), (49, 31.5), (59, 28.5), (200, 25.0)],
}


def _ref_vo2max(profile: Dict[str, Any]) -> Optional[RefRange]:
    s, a = _sex(profile), _age(profile)
    if not s or a is None:
        return None
    for cutoff, low in _VO2_LOW[s]:
        if a <= cutoff:
            return RefRange(
                low=low, kind="screen",
                source=f"Cooper Institute 有氧能力常模，{s == 'male' and '男' or '女'}{a} 岁约中位水平（参考用，非诊断）",
            )
    return None


_REF_FNS: Dict[str, Callable[[Dict[str, Any]], Optional[RefRange]]] = {
    "body_fat": _ref_body_fat,
    "smi": _ref_smi,
    "waist": _ref_waist,
    "whr": _ref_whr,
    "vo2max": _ref_vo2max,
}


# ── 「哪一侧才值得留意」────────────────────────────────────
#
# 默认 both：超出任何一侧都值得看一眼。
# 但有一批指标只有**一侧**是真正需要留意的信号，落在另一侧是常见甚至好事。
# 把这种情况也报成"异常"，就是制造没必要的焦虑——而这类焦虑恰恰会让人
# 忽略真正该看的那条提示。
#
# 这一层是**关于指标本身**的领域判断，与区间边界从哪来无关。所以即使区间
# 是设备印的（例如 InBody 的「去脂体重 46.7~57.0」），"高于上限"也仍然不是
# 异常：肌肉量大的人本来就会高于这个以一般人群为基准的上限。
#
# 实测依据（真实数据）：一份 1336 天的 Apple 导出里，静息心率中位 56、
# 78.7% 的天数低于 60。旧口径会把几乎每一天都报成「超出筛查参考」。
REF_CONCERN: Dict[str, str] = {
    # 只有偏高才需要留意
    "resting_hr": "high",           # 长期训练者静息心率常低于 60，低不是异常
    "waist_circumference": "high",  # 腰围只有超标是风险信号
    "whr": "high",
    "visceral_fat_level": "high",
    "visceral_fat_area": "high",
    "total_body_water": "high",     # 体成分仪的用法就是盯"超标"，见 HYDRATION_AMPLIFIES
    "body_fat_pct": "high",
    "body_fat_mass": "high",
    "obesity_degree": "high",
    # 只有偏低才需要留意
    "lean_body_mass": "low",        # 高于上限通常是肌肉量充足，不是异常
    "skeletal_muscle_mass": "low",
    "protein_mass": "low",
    "mineral_mass": "low",
    "smi": "low",                   # 这是个筛查下限，低于它才需要进一步评估
    "vo2max": "low",
    "oxygen_saturation": "low",
}


def ref_concern_of(metric_id: str) -> str:
    """这个指标只有哪一侧需要留意（both / high / low）。"""
    return REF_CONCERN.get(metric_id, "both")


# ──────────────────────────────────────────────────────────── 指标表
#
# 说明：Apple 的类型名省掉了公共前缀 HKQuantityTypeIdentifier，
# 由 apple.py 在查表前统一去掉。

_SPECS: Tuple[MetricSpec, ...] = (
    # ── 基本 ────────────────────────────────────────────────
    MetricSpec("height_cm", "身高", "cm", "基本", precision=1, verdict=False,
               aliases=("身高", "height"),
               apple_types=("Height",),
               hint="换算 BMI、体表面积等要用；也用于核对设备是否算错。"),

    # ── 体成分 ──────────────────────────────────────────────
    MetricSpec("body_mass", "体重", "kg", "体成分", precision=1, noise=0.3,
               aliases=("体重", "重量", "body mass", "body weight", "weight", "body_weight"),
               apple_types=("BodyMass",),
               hint="不同秤之间不可直接比；见 docs/PITFALLS.md 第 1 条。"),
    MetricSpec("bmi", "BMI", "kg/m2", "肥胖评估", precision=1, noise=0.2,
               aliases=("bmi", "身体质量指数bmi", "身体质量指数", "body mass index"),
               apple_types=("BodyMassIndex",),
               ref_low=18.5, ref_high=24.9, ref_kind="screen",
               ref_source="WHO 成人 BMI 分类",
               ref_note="亚洲人群常用 18.5~23.9 作为超重界值。此项为筛查参考，不构成诊断。",
               hint="BMI 不区分肌肉与脂肪，肌肉量大的人会偏高。"),
    MetricSpec("body_fat_pct", "体脂率", "%", "体成分", precision=1, noise=1.0,
               aliases=("体脂率", "体脂百分比", "体脂肪百分比", "pbf", "body fat percentage",
                        "body fat", "体脂"),
               apple_types=("BodyFatPercentage",),
               ref_fn="body_fat",
               ref_note="不同机构分级差异较大；体成分仪自带的百分比区间优先于本区间。",
               hint="生物电阻抗法受水合状态影响，单次波动 1~2 个百分点是正常的。"),
    MetricSpec("body_fat_mass", "体脂肪量", "kg", "体成分", precision=1, noise=0.5,
               aliases=("体脂肪", "体脂肪量", "脂肪量", "fat mass", "body fat mass"),
               hint="跨时间比较优先看这个 kg 值，而不是百分比。"),
    MetricSpec("lean_body_mass", "去脂体重", "kg", "体成分", precision=1, noise=0.5,
               aliases=("去脂体重", "瘦体重", "lean body mass", "lbm", "fat free mass"),
               apple_types=("LeanBodyMass",),
               hint="体重 − 体脂肪。同样受水合状态影响。"),
    MetricSpec("skeletal_muscle_mass", "骨骼肌量", "kg", "体成分", precision=1, noise=0.5,
               aliases=("骨骼肌", "骨骼肌量", "skeletal muscle", "smm"),
               hint="体成分仪的核心输出之一。"),
    MetricSpec("smi", "骨骼肌指数 SMI", "kg/m2", "体成分", precision=1, noise=0.2,
               aliases=("骨骼肌指数smi", "smi", "skeletal muscle index"),
               ref_fn="smi",
               hint="四肢骨骼肌量 ÷ 身高²。低于筛查阈值只意味着需要进一步评估，不是诊断。"),
    MetricSpec("total_body_water", "身体总水分", "L", "体成分", precision=1, noise=0.5,
               aliases=("身体总水分", "总水分", "体水分", "total body water", "tbw"),
               hint="超出参考上限时，同一份报告里的体重与去脂体重都会被抬高。"),
    MetricSpec("protein_mass", "蛋白质", "kg", "体成分", precision=1, noise=0.2,
               aliases=("蛋白质", "蛋白质含量", "protein"),
               hint="体成分仪的估算值，不是血液蛋白检测。"),
    MetricSpec("mineral_mass", "无机盐", "kg", "体成分", precision=2, noise=0.1,
               aliases=("无机盐", "矿物质", "mineral"),
               hint="体成分仪的估算值，不能替代骨密度检查。"),
    MetricSpec("bmr", "基础代谢率", "kcal", "体成分", precision=0, verdict=False,
               aliases=("基础代谢率", "基础代谢", "bmr", "basal metabolic rate"),
               hint="设备按去脂体重估算，不是实测代谢。"),

    # ── 肥胖评估 ────────────────────────────────────────────
    MetricSpec("waist_circumference", "腰围", "cm", "肥胖评估", precision=1, noise=1.0,
               aliases=("腰围", "waist", "waist circumference"),
               apple_types=("WaistCircumference",),
               ref_fn="waist",
               hint="用软尺在脐水平测量；同一姿势同一时点比较才有意义。"),
    MetricSpec("whr", "腰臀比", "—", "肥胖评估", precision=2, noise=0.02,
               aliases=("腰臀比", "whr", "waist hip ratio", "waist-to-hip ratio"),
               ref_fn="whr",
               hint="脂肪分布指标，比单看体重更能反映代谢风险。"),
    MetricSpec("visceral_fat_level", "内脏脂肪等级", "级", "肥胖评估", precision=0, noise=0.5,
               aliases=("内脏脂肪等级", "内脏脂肪", "visceral fat level", "visceral fat"),
               ref_high=10, ref_high_inclusive=False, ref_kind="screen", ref_text="<10",
               ref_source="体成分仪常用判定：等级 <10 视为正常范围",
               hint="设备的经验评分，不是影像学测量。"),
    MetricSpec("visceral_fat_area", "内脏脂肪面积", "cm2", "肥胖评估", precision=1, noise=1.0,
               aliases=("内脏脂肪面积", "visceral fat area", "vfa"),
               ref_high=100, ref_kind="screen",
               ref_source="常用筛查界值：≥100 cm² 提示内脏脂肪堆积（CT/MRI 为准，设备估值为参考）",
               hint="体成分仪的估算值，与 CT 测量不可直接互换。"),
    MetricSpec("obesity_degree", "肥胖度", "%", "肥胖评估", precision=0, noise=1.0,
               aliases=("肥胖度", "obesity degree"),
               hint="相对标准体重的百分比，标准体重定义各设备不同。"),

    # ── 肌肉均衡（分节段肌肉量） ────────────────────────────
    MetricSpec("lean_left_arm", "左臂肌肉量", "kg", "肌肉均衡", precision=2, noise=0.15,
               aliases=("左臂肌肉量", "左上肢肌肉量", "left arm lean mass"), hint=""),
    MetricSpec("lean_right_arm", "右臂肌肉量", "kg", "肌肉均衡", precision=2, noise=0.15,
               aliases=("右臂肌肉量", "右上肢肌肉量", "right arm lean mass"), hint=""),
    MetricSpec("lean_trunk", "躯干肌肉量", "kg", "肌肉均衡", precision=1, noise=0.3,
               aliases=("躯干肌肉量", "躯干", "trunk lean mass"), hint=""),
    MetricSpec("lean_left_leg", "左腿肌肉量", "kg", "肌肉均衡", precision=2, noise=0.2,
               aliases=("左腿肌肉量", "左下肢肌肉量", "left leg lean mass"), hint=""),
    MetricSpec("lean_right_leg", "右腿肌肉量", "kg", "肌肉均衡", precision=2, noise=0.2,
               aliases=("右腿肌肉量", "右下肢肌肉量", "right leg lean mass"), hint=""),

    # 分节段"百分比"形式（相对该节段标准值）。跨时间不可比，见 PITFALLS 第 2 条。
    MetricSpec("lean_pct_left_arm", "左臂肌肉量百分比", "%", "肌肉均衡", precision=1, noise=2.0,
               verdict=False, aliases=("左臂肌肉量百分比", "左上肢肌肉百分比"), hint=""),
    MetricSpec("lean_pct_right_arm", "右臂肌肉量百分比", "%", "肌肉均衡", precision=1, noise=2.0,
               verdict=False, aliases=("右臂肌肉量百分比", "右上肢肌肉百分比"), hint=""),
    MetricSpec("lean_pct_trunk", "躯干肌肉量百分比", "%", "肌肉均衡", precision=1, noise=2.0,
               verdict=False, aliases=("躯干肌肉量百分比", "躯干肌肉百分比"), hint=""),
    MetricSpec("lean_pct_left_leg", "左腿肌肉量百分比", "%", "肌肉均衡", precision=1, noise=2.0,
               verdict=False, aliases=("左腿肌肉量百分比", "左下肢肌肉百分比"), hint=""),
    MetricSpec("lean_pct_right_leg", "右腿肌肉量百分比", "%", "肌肉均衡", precision=1, noise=2.0,
               verdict=False, aliases=("右腿肌肉量百分比", "右下肢肌肉百分比"), hint=""),

    # ── 节段脂肪 ────────────────────────────────────────────
    MetricSpec("fat_left_arm", "左臂脂肪量", "kg", "节段脂肪", precision=1, noise=0.15,
               aliases=("左臂脂肪量", "左上肢脂肪量"), hint=""),
    MetricSpec("fat_right_arm", "右臂脂肪量", "kg", "节段脂肪", precision=1, noise=0.15,
               aliases=("右臂脂肪量", "右上肢脂肪量"), hint=""),
    MetricSpec("fat_trunk", "躯干脂肪量", "kg", "节段脂肪", precision=1, noise=0.3,
               aliases=("躯干脂肪量", "躯干脂肪"), hint=""),
    MetricSpec("fat_left_leg", "左腿脂肪量", "kg", "节段脂肪", precision=1, noise=0.2,
               aliases=("左腿脂肪量", "左下肢脂肪量"), hint=""),
    MetricSpec("fat_right_leg", "右腿脂肪量", "kg", "节段脂肪", precision=1, noise=0.2,
               aliases=("右腿脂肪量", "右下肢脂肪量"), hint=""),
    MetricSpec("fat_pct_left_arm", "左臂脂肪百分比", "%", "节段脂肪", precision=1, noise=2.0,
               verdict=False, aliases=("左臂脂肪百分比", "左臂脂肪率"), hint=""),
    MetricSpec("fat_pct_right_arm", "右臂脂肪百分比", "%", "节段脂肪", precision=1, noise=2.0,
               verdict=False, aliases=("右臂脂肪百分比", "右臂脂肪率"), hint=""),
    MetricSpec("fat_pct_trunk", "躯干脂肪百分比", "%", "节段脂肪", precision=1, noise=2.0,
               verdict=False, aliases=("躯干脂肪百分比", "躯干脂肪率"), hint=""),
    MetricSpec("fat_pct_left_leg", "左腿脂肪百分比", "%", "节段脂肪", precision=1, noise=2.0,
               verdict=False, aliases=("左腿脂肪百分比", "左腿脂肪率"), hint=""),
    MetricSpec("fat_pct_right_leg", "右腿脂肪百分比", "%", "节段脂肪", precision=1, noise=2.0,
               verdict=False, aliases=("右腿脂肪百分比", "右腿脂肪率"), hint=""),

    # ── 心肺与恢复 ──────────────────────────────────────────
    MetricSpec("resting_hr", "静息心率", "次/分", "心肺与恢复", precision=0, noise=2.0,
               aliases=("静息心率", "resting heart rate", "rhr"),
               apple_types=("RestingHeartRate",),
               ref_low=60, ref_high=100, ref_kind="screen",
               ref_source="成人静息心率常用参考区间",
               ref_note="长期训练者静息心率常低于 60，本身不代表异常；若同时伴有头晕、乏力等不适，请就医判断。",
               hint="趋势比单次值有意义：同样条件下逐步下降通常说明有氧能力改善。"),
    MetricSpec("hrv_sdnn", "心率变异性 HRV(SDNN)", "ms", "心肺与恢复", precision=1, noise=5.0,
               aliases=("hrv", "hrvsdnn", "心率变异性sdnn", "心率变异性", "heart rate variability"),
               apple_types=("HeartRateVariabilitySDNN",),
               ref_note="没有公认的通用正常范围：个体差异极大，只能看自己基线的相对变化。",
               hint="看「比自己的 30 天基线高还是低」，不要和别人比。"),
    MetricSpec("vo2max", "最大摄氧量 VO₂max", "ml/kg/min", "心肺与恢复", precision=1, noise=1.0,
               aliases=("最大摄氧量", "vo2max", "vo₂max", "vo2 max"),
               apple_types=("VO2Max",),
               ref_fn="vo2max",
               hint="Apple Watch 的户外快走/跑步估算值，受配速与地形影响。"),
    MetricSpec("heart_rate", "心率", "次/分", "心肺与恢复", precision=0, verdict=False, agg="mean",
               aliases=("心率", "heart rate", "hr"),
               apple_types=("HeartRate",),
               hint="全天散点数据，报告里只给分布。"),
    MetricSpec("walking_hr_avg", "步行平均心率", "次/分", "心肺与恢复", precision=0,
               verdict=False, aliases=("步行平均心率", "walking heart rate average"),
               apple_types=("WalkingHeartRateAverage",),
               hint="同速度下逐步下降，通常说明有氧能力改善。"),
    MetricSpec("respiratory_rate", "呼吸频率", "次/分", "心肺与恢复", precision=1,
               aliases=("呼吸频率", "respiratory rate"),
               apple_types=("RespiratoryRate",),
               ref_low=12, ref_high=20, ref_kind="screen", ref_source="成人静息呼吸频率常用参考",
               hint="睡眠期间的呼吸频率波动值得关注。"),
    MetricSpec("oxygen_saturation", "血氧饱和度", "%", "心肺与恢复", precision=1,
               aliases=("血氧饱和度", "血氧", "oxygen saturation", "spo2"),
               apple_types=("OxygenSaturation",),
               ref_low=95, ref_high=100, ref_kind="screen", ref_source="成人血氧饱和度常用参考",
               ref_note="腕式设备在运动、低温、佩戴松动时读数不可靠，单次偏低不等于异常。",
               hint="看长期分布，不要因单次读数紧张。"),

    # ── 活动量（目标是 goal，不是正常范围；措辞由 ref_kind 区分） ──
    MetricSpec("steps", "步数", "步", "活动量", precision=0, agg="sum", verdict=True,
               aliases=("步数", "steps", "step count"),
               apple_types=("StepCount",),
               ref_low=8000, ref_kind="goal",
               ref_source="常见公共卫生步数建议（非临床阈值）",
               hint="按日累计。设备在未佩戴时会漏记，缺数据的日子要单独标注。"),
    MetricSpec("active_energy", "活动能量", "kcal", "活动量", precision=0, agg="sum",
               verdict=True, aliases=("活动能量", "活动热量", "active energy"),
               apple_types=("ActiveEnergyBurned",),
               ref_low=300, ref_kind="goal", ref_source="常见每日活动消耗目标（非临床阈值）", hint=""),
    MetricSpec("basal_energy", "基础能量", "kcal", "活动量", precision=0, agg="sum",
               verdict=False, aliases=("基础能量", "基础代谢消耗", "basal energy"),
               apple_types=("BasalEnergyBurned",),
               hint="与体成分仪估算的基础代谢率口径不同，不要混用。"),
    MetricSpec("exercise_minutes", "锻炼分钟", "分钟", "活动量", precision=0, agg="sum",
               verdict=True, aliases=("锻炼分钟", "锻炼时间", "apple exercise time"),
               apple_types=("AppleExerciseTime",),
               ref_low=30, ref_kind="goal",
               ref_source="WHO 成人每周 150 分钟中等强度活动的等效日均值", hint=""),
    MetricSpec("stand_minutes", "站立时间", "分钟", "活动量", precision=0, agg="sum",
               verdict=False, aliases=("站立时间", "apple stand time"),
               apple_types=("AppleStandTime",), hint=""),
    MetricSpec("flights_climbed", "爬楼层数", "层", "活动量", precision=0, agg="sum",
               verdict=False, aliases=("爬楼层数", "flights climbed"),
               apple_types=("FlightsClimbed",), hint=""),
    MetricSpec("distance_walking_running", "步行跑步距离", "km", "活动量", precision=2, agg="sum",
               verdict=False, aliases=("步行跑步距离", "步行距离", "walking running distance"),
               apple_types=("DistanceWalkingRunning",), hint=""),
    MetricSpec("distance_cycling", "骑行距离", "km", "活动量", precision=2, agg="sum",
               verdict=False, aliases=("骑行距离", "cycling distance"),
               apple_types=("DistanceCycling",), hint=""),

    # ── 睡眠 ────────────────────────────────────────────────
    MetricSpec("sleep_total", "总睡眠时长", "小时", "睡眠", precision=2, agg="mean", noise=0.3,
               aliases=("总睡眠时长", "实际睡眠", "睡眠时长", "sleep duration", "sleep total"),
               ref_low=7, ref_high=9, ref_kind="range",
               ref_source="AASM/SRS 成人推荐睡眠时长",
               ref_note="推荐区间针对健康成人；个体需求存在差异，长期明显不足或过多值得与医生讨论。",
               hint="跨天归属：归到醒来那天。"),
    MetricSpec("sleep_deep", "深睡", "小时", "睡眠", precision=2, agg="mean", verdict=False,
               aliases=("深睡", "深睡眠", "deep sleep"), hint="分期判定的准确度有限，看趋势不看单夜。"),
    MetricSpec("sleep_rem", "REM 睡眠", "小时", "睡眠", precision=2, agg="mean", verdict=False,
               aliases=("rem", "rem睡眠", "快速眼动"), hint=""),
    MetricSpec("sleep_core", "核心睡眠", "小时", "睡眠", precision=2, agg="mean", verdict=False,
               aliases=("核心睡眠", "浅睡", "core sleep"), hint=""),
    MetricSpec("sleep_awake", "夜间清醒", "小时", "睡眠", precision=2, agg="mean", verdict=False,
               aliases=("夜间清醒", "清醒", "awake"), hint=""),
    MetricSpec("sleep_inbed", "卧床时间", "小时", "睡眠", precision=2, agg="mean", verdict=False,
               aliases=("卧床时间", "卧床", "in bed"), hint="卧床 − 睡着 = 入睡耗时与夜间清醒。"),

    # ── 睡眠时刻（入睡 / 醒来 / 睡眠中点，用于算「睡眠规律性」） ──
    #
    # 这三个指标回答的是「几点睡、几点醒」，而不是「睡了多久」。睡眠规律性
    # 比睡眠时长更容易改，与恢复状态的关系也更稳，所以单独成一组指标。
    #
    # ⚠️ 取值口径：**醒来日的前一天 18:00 起算的分钟数**，不是"钟点"。
    # 时间在午夜处不连续：23:30 与 00:30 只差 60 分钟，按"钟点"算却会得到 23 的差，
    # 于是均值、标准差、分位数全都会算错。选 18:00 作参考点，是因为绝大多数人的
    # 入睡时刻在 18:00 之后、醒来在次日 18:00 之前，所有取值因此落在
    # `0 ~ 约 900` 的一个**单调区间**里，可以直接算标准差、均值与分位数。
    # 示例：睡眠 2026-05-17 23:10 → 2026-05-18 07:05，醒来日 = 2026-05-18，
    # 参考点 = 2026-05-17 18:00，于是入睡 310 分钟、醒来 785 分钟、中点 547.5 分钟。
    #
    # 三者的取值只由 `Asleep*` 阶段决定（不含 Awake / InBed，见 apple.py），
    # 且**没有公认参考范围**（作息因人而异，"正常"入睡时刻能差好几个小时），
    # 所以 verdict=False、不给区间：只能和**自己的基线**比。
    MetricSpec("sleep_onset_min", "入睡时刻", "分钟", "睡眠", precision=0, agg="mean",
               verdict=False, noise=15.0,
               aliases=("入睡时刻", "入睡时间", "sleep onset", "sleep_onset_min"),
               hint="当夜**入睡**时刻，写成「醒来日的前一天 18:00 起算的分钟数」"
                    "（2026-05-17 23:10 入睡 → 310）。用分钟数而不是钟点，是因为时间在"
                    "午夜处不连续：23:30 与 00:30 只差 60 分钟，按钟点算却是 23 的差，"
                    "均值与标准差都会算错；18:00 这个参考点让取值落在 0~900 的单调区间里，"
                    "可以直接算均值、标准差与分位数。它是「睡眠规律性」的主要输入，"
                    "比睡眠时长更容易改。没有公认参考范围，只与自己的基线比。"),
    MetricSpec("sleep_wake_min", "醒来时刻", "分钟", "睡眠", precision=0, agg="mean",
               verdict=False, noise=15.0,
               aliases=("醒来时刻", "醒来时间", "起床时刻", "sleep wake", "sleep_wake_min"),
               hint="当夜**醒来**时刻，与入睡时刻**同一个参考点**"
                    "（醒来日的前一天 18:00 起算的分钟数；2026-05-18 07:05 醒来 → 785）。"
                    "用同一个参考点是为了让「醒来 − 入睡」直接等于睡眠时长，"
                    "两个序列也能一起算标准差。只看 Asleep* 段：不上床时间、也不含"
                    "醒来后赖在床上的时间。没有公认参考范围，只与自己的基线比。"),
    MetricSpec("sleep_mid_min", "睡眠中点", "分钟", "睡眠", precision=0, agg="mean",
               verdict=False, noise=15.0,
               aliases=("睡眠中点", "睡眠中间点", "sleep midpoint", "sleep_mid_min"),
               hint="睡眠中点 = (入睡时刻 + 醒来时刻) / 2，参考点同上"
                    "（醒来日的前一天 18:00 起算的分钟数；310 与 785 → 547.5）。"
                    "它是睡眠规律性的标准代理指标：同时受入睡与醒来两端影响，"
                    "比单看入睡时刻更稳，也是文献里最常用的「社会时差」计算基础。"
                    "没有公认参考范围，只与自己的基线比。"),

    # ── 生物电阻抗（解释用，不做判定） ──────────────────────
    MetricSpec("z_right_arm_20k", "右上肢阻抗 20kHz", "Ω", "生物电阻抗", precision=1, noise=5.0,
               verdict=False, aliases=("右上肢20khz", "右上肢阻抗20khz"), hint=""),
    MetricSpec("z_left_arm_20k", "左上肢阻抗 20kHz", "Ω", "生物电阻抗", precision=1, noise=5.0,
               verdict=False, aliases=("左上肢20khz", "左上肢阻抗20khz"), hint=""),
    MetricSpec("z_trunk_20k", "躯干阻抗 20kHz", "Ω", "生物电阻抗", precision=1, noise=2.0,
               verdict=False, aliases=("躯干20khz", "躯干阻抗20khz"), hint=""),
    MetricSpec("z_right_leg_20k", "右下肢阻抗 20kHz", "Ω", "生物电阻抗", precision=1, noise=5.0,
               verdict=False, aliases=("右下肢20khz", "右下肢阻抗20khz"), hint=""),
    MetricSpec("z_left_leg_20k", "左下肢阻抗 20kHz", "Ω", "生物电阻抗", precision=1, noise=5.0,
               verdict=False, aliases=("左下肢20khz", "左下肢阻抗20khz"), hint=""),
    MetricSpec("z_right_arm_100k", "右上肢阻抗 100kHz", "Ω", "生物电阻抗", precision=1, noise=5.0,
               verdict=False, aliases=("右上肢100khz", "右上肢阻抗100khz"), hint=""),
    MetricSpec("z_left_arm_100k", "左上肢阻抗 100kHz", "Ω", "生物电阻抗", precision=1, noise=5.0,
               verdict=False, aliases=("左上肢100khz", "左上肢阻抗100khz"), hint=""),
    MetricSpec("z_trunk_100k", "躯干阻抗 100kHz", "Ω", "生物电阻抗", precision=1, noise=2.0,
               verdict=False, aliases=("躯干100khz", "躯干阻抗100khz"), hint=""),
    MetricSpec("z_right_leg_100k", "右下肢阻抗 100kHz", "Ω", "生物电阻抗", precision=1, noise=5.0,
               verdict=False, aliases=("右下肢100khz", "右下肢阻抗100khz"), hint=""),
    MetricSpec("z_left_leg_100k", "左下肢阻抗 100kHz", "Ω", "生物电阻抗", precision=1, noise=5.0,
               verdict=False, aliases=("左下肢100khz", "左下肢阻抗100khz"), hint=""),

    # ── 设备建议与评分 ──────────────────────────────────────
    MetricSpec("inbody_score", "体成分评分", "分", "设备建议与评分", precision=0, verdict=False,
               aliases=("总分", "体成分评分", "inbody评分", "inbody score"), hint="设备自己的模型，不能跨设备比。"),
    MetricSpec("target_weight", "设备建议目标体重", "kg", "设备建议与评分", precision=1, verdict=False,
               aliases=("目标体重", "target weight"), hint="设备按标准体重公式给出，不等于医学建议。"),
    MetricSpec("weight_control", "体控建议：体重", "kg", "设备建议与评分", precision=1, verdict=False,
               aliases=("体重控制", "weight control"), hint="正数表示建议增加，负数表示建议减少。"),
    MetricSpec("fat_control", "体控建议：脂肪", "kg", "设备建议与评分", precision=1, verdict=False,
               aliases=("脂肪控制", "fat control"), hint=""),
    MetricSpec("muscle_control", "体控建议：肌肉", "kg", "设备建议与评分", precision=1, verdict=False,
               aliases=("肌肉控制", "muscle control"), hint=""),
    MetricSpec("recommended_kcal", "建议热量摄入", "kcal", "设备建议与评分", precision=0, verdict=False,
               aliases=("建议热量摄入", "推荐热量摄入", "recommended kcal"), hint="设备按活动量估算，不是营养上的具体方案。"),
)


# ──────────────────────────────────────────────────────────── 索引

SPECS: Tuple[MetricSpec, ...] = tuple(_SPECS)
BY_ID: Dict[str, MetricSpec] = {s.id: s for s in SPECS}

APPLE_TO_METRIC: Dict[str, str] = {}
for _s in SPECS:
    for _t in _s.apple_types:
        APPLE_TO_METRIC[_t] = _s.id


_PUNCT = " \t\u3000_＿-—–－·・/\\|,，.。:：;；%％()（）[]【】{}<>《》'\"“”‘’+*#"


def normalize(name: str) -> str:
    """别名匹配用的归一化：小写、去空格、去标点。"""
    if not name:
        return ""
    s = name.strip().lower()
    for ch in _PUNCT:
        s = s.replace(ch, "")
    return s


_ALIAS_INDEX: Dict[str, str] = {}
for _s in SPECS:
    for _a in _s.aliases:
        _ALIAS_INDEX.setdefault(normalize(_a), _s.id)
    # 中文名本身也是别名
    _ALIAS_INDEX.setdefault(normalize(_s.cn), _s.id)
    _ALIAS_INDEX.setdefault(normalize(_s.id), _s.id)

# 长别名优先：否则 "骨骼肌" 会先把 "骨骼肌指数smi" 吃掉，"体重" 会吃掉 "目标体重"。
_ALIAS_SORTED: List[Tuple[str, str]] = sorted(_ALIAS_INDEX.items(), key=lambda kv: -len(kv[0]))


def resolve(name: str) -> Optional[str]:
    """把设备/表头里的指标名解析成统一指标 ID；不认识返回 None。

    先精确匹配，再按别名长度从长到短做包含匹配。
    """
    if not name:
        return None
    n = normalize(name)
    if not n:
        return None
    hit = _ALIAS_INDEX.get(n)
    if hit:
        return hit
    for alias, mid in _ALIAS_SORTED:
        if len(alias) >= 2 and alias in n:
            return mid
    return None


def get(metric_id: str) -> Optional[MetricSpec]:
    return BY_ID.get(metric_id)


def display_name(metric_id: str) -> str:
    s = BY_ID.get(metric_id)
    return s.cn if s else metric_id


def builtin_ref(metric_id: str, profile: Dict[str, Any]) -> Optional[RefRange]:
    """内置参考区间。没有就返回 None——这不是失败，是诚实。"""
    s = BY_ID.get(metric_id)
    if not s:
        return None
    if s.ref_fn:
        fn = _REF_FNS.get(s.ref_fn)
        r = fn(profile or {}) if fn else None
        if r is not None:
            if r.concern == "both":
                r = RefRange(low=r.low, high=r.high, low_inclusive=r.low_inclusive,
                             high_inclusive=r.high_inclusive, text=r.text,
                             source=r.source, kind=r.kind,
                             concern=ref_concern_of(metric_id))
            return r
        # 动态函数给不出结果（缺性别 / 年龄）就退回静态值，通常两者都没有
    if s.ref_low is None and s.ref_high is None:
        return None
    return RefRange(low=s.ref_low, high=s.ref_high,
                    low_inclusive=s.ref_low_inclusive, high_inclusive=s.ref_high_inclusive,
                    kind=s.ref_kind, source=s.ref_source, text=s.ref_text,
                    concern=ref_concern_of(metric_id))


def categories_in_order(metric_ids: List[str]) -> List[str]:
    """返回出现过的类别，按 CATEGORY_ORDER 排序。"""
    present = {BY_ID[m].category for m in metric_ids if m in BY_ID}
    ordered = [c for c in CATEGORY_ORDER if c in present]
    extra = sorted(present - set(ordered))
    return ordered + extra
