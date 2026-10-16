"""单位归一。

解析器在产生 Observation 之前必须调用这里，保证下游只见到规范单位。
查不到的换算返回 None，由调用方决定是丢弃还是原样保留（并记一条 note）。
"""

from __future__ import annotations

from typing import Optional

# 重量 -> kg
_MASS = {
    "kg": 1.0, "千克": 1.0, "公斤": 1.0,
    "g": 0.001, "克": 0.001,
    "lb": 0.45359237, "lbs": 0.45359237, "磅": 0.45359237,
    "st": 6.35029318, "stone": 6.35029318,
}

# 长度 -> cm
_LENGTH = {
    "cm": 1.0, "厘米": 1.0,
    "m": 100.0, "米": 100.0,
    "mm": 0.1, "毫米": 0.1,
    "in": 2.54, "inch": 2.54, "英寸": 2.54,
    "ft": 30.48, "foot": 30.48, "feet": 30.48, "英尺": 30.48,
}

# 距离 -> km
_DISTANCE = {
    "km": 1.0, "千米": 1.0, "公里": 1.0,
    "m": 0.001, "米": 0.001,
    "mi": 1.609344, "mile": 1.609344, "miles": 1.609344, "英里": 1.609344,
    "yd": 0.0009144, "yard": 0.0009144, "码": 0.0009144,
    "ft": 0.0003048, "feet": 0.0003048,
}

# 能量 -> kcal
_ENERGY = {
    "kcal": 1.0, "千卡": 1.0, "大卡": 1.0, "cal": 0.001, "卡": 0.001,
    "kj": 0.239005736, "千焦": 0.239005736,
    "j": 0.000239005736,
}

# 时间 -> 分钟
_DURATION = {
    "min": 1.0, "mins": 1.0, "minute": 1.0, "minutes": 1.0, "分钟": 1.0,
    "h": 60.0, "hr": 60.0, "hour": 60.0, "hours": 60.0, "小时": 60.0,
    "s": 1.0 / 60.0, "sec": 1.0 / 60.0, "second": 1.0 / 60.0, "秒": 1.0 / 60.0,
}

# 时间 -> 小时（睡眠用）
_HOURS = {
    "h": 1.0, "hr": 1.0, "hour": 1.0, "hours": 1.0, "小时": 1.0,
    "min": 1.0 / 60.0, "minutes": 1.0 / 60.0, "分钟": 1.0 / 60.0,
    "s": 1.0 / 3600.0, "秒": 1.0 / 3600.0,
}

# 一个单位属于哪一族；用于"目标单位 -> 该族换算表"的自动选择
_FAMILIES = {
    "kg": _MASS, "cm": _LENGTH, "km": _DISTANCE, "kcal": _ENERGY,
    "分钟": _DURATION, "小时": _HOURS,
}


def _norm_unit(u: str) -> str:
    """单位归一。

    注意：`·` 和 `•` 要**换成 `/`**，不能直接删掉——否则规范单位
    `ml/kg·min` 会被拆成 `ml/kgmin`，和任何写法都对不上，
    VO₂max 的单位换算就永远失败（这个 bug 真的发生过，测试里有用例守着）。
    """
    if not u:
        return ""
    s = u.strip().replace("／", "/").replace("·", "/").replace("•", "/").replace(" ", "")
    low = s.lower()
    aliases = {
        "bpm": "次/分", "beats/min": "次/分", "/min": "次/分",
        # Apple 健康导出把心率写成无量纲的 count/min（单位其实还是"次/分"）
        "count/min": "次/分", "counts/min": "次/分", "1/min": "次/分",
        "ms": "ms", "毫秒": "ms",
        "%": "%", "％": "%", "percent": "%",
        "kg/m2": "kg/m2", "kg/m²": "kg/m2", "kg/㎡": "kg/m2",
        "kcal/day": "kcal", "kcal/日": "kcal", "kcal/d": "kcal",
        "ml/min/kg": "ml/kg/min", "ml/kg/min": "ml/kg/min", "mlkgmin": "ml/kg/min",
        "级": "级", "levels": "级", "level": "级",
        "分": "分", "score": "分", "points": "分",
        "步": "步", "steps": "步", "count": "步", "counts": "步",
        "层": "层", "flights": "层",
        "ω": "Ω", "ohm": "Ω", "ohms": "Ω", "欧姆": "Ω",
        "岁": "岁", "years": "岁",
    }
    return aliases.get(low, low)


def convert(value: Optional[float], from_unit: str, to_unit: str) -> Optional[float]:
    """把 value 从 from_unit 换算成 to_unit；无法换算返回 None。"""
    if value is None:
        return None
    fu, tu = _norm_unit(from_unit), _norm_unit(to_unit)
    if not fu or not tu:
        return None
    if fu == tu:
        return value
    # Apple 导出用无量纲的 count/counts 表示计数，而 catalog 的规范单位是 步 / 层 / 分
    # （归一表把 count 映射成"步"，所以这里要看**原始**单位，不能只看归一结果，
    #  否则 "count"→"层"（爬楼层数）会被误当成 "步"→"层"）。
    # 同一台设备的这两种写法是 1:1 关系（数值本身就是计数），所以直接采用，不做换算。
    # 这不是"猜单位"，是已知的同一量纲。除这三种计数单位外一律不放行。
    if (from_unit or "").strip().lower() in ("count", "counts") and tu in ("步", "层", "分"):
        return value
    table = _FAMILIES.get(tu)
    if table:
        a, b = table.get(fu), table.get(tu)
        if a is not None and b:
            return value * a / b
    return None


def looks_like_ratio_value(u: str) -> bool:
    """判断这个单位有没有可能承载 0~1 存储的比值（体脂率这类）。

    只回答"单位口径上像不像比值"：**无单位**与 `ratio` 算像；写成 `%` / `％` 不算
    ——单位栏写 `%` 说明数值已经是百分数（22.3），不是 0.158。

    ⚠️ 判断的是 `_norm_unit` 归一**之后**的结果，而 `_norm_unit` 会把 `percent`
    归一成 `%`，所以 `percent` 与 `%` 在这里不可区分，两者都返回 False。
    这个取舍是有意的：`%` 才是绝大多数导出与 catalog 的规范单位。

    ⚠️ 调用方仍然必须自己判断数值是否 ≤ 1：单位像比值、数值却写着 22.3 的
    导出方式是存在的（换算后再判断才稳妥）。本函数不碰数值。
    """
    return _norm_unit(u) in ("", "%", "ratio")
