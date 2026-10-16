"""解析器：把各设备的原始导出，变成同一个 `Dataset`。

三个解析器各管一种来源，`parse_path()` 负责自动识别：

============ ============================================================ ==================
类型          识别的输入                                                    入口
============ ============================================================ ==================
`apple`      `.zip` / `.xml` / Apple 导出目录（含 export.xml / 导出.xml）    `parse_apple`
`inbody`     InBody 报告长格式 CSV（表头有「指标」列与日期列）               `parse_inbody`
`generic`    通用长格式 CSV/TSV（metric, date, value, unit, source）        `parse_generic`
============ ============================================================ ==================

用法：

    from health_report.parsers import parse_path, detect_kind

    detect_kind("export.zip")        # -> "apple"
    ds = parse_path("InBody数据对比.csv")   # 自动选解析器

识别只看后缀与表头，不读数据分布。识别错了就跳过 `parse_path()`，直接调用对应的
`parse_*()` 函数，参数多传一个 `source_name` 用来标注数据来源。
"""

from __future__ import annotations

import os
from typing import Any, Dict

from ..model import Dataset
from ._detect import describe_kind, sniff_kind
from .apple import parse_apple
from .generic import parse_generic
from .inbody import parse_inbody

__all__ = [
    "parse_apple",
    "parse_inbody",
    "parse_generic",
    "parse_path",
    "detect_kind",
]


def detect_kind(path: str) -> str:
    """判断输入该用哪个解析器，返回 "apple" / "inbody" / "generic"。

    规则：
      1. 后缀 `.zip` / `.xml` → apple；目录里存在 export.xml / 导出.xml / apple_health_export → apple；
      2. `.csv` / `.tsv`：表头含「指标」列且存在能解析成日期的列 → inbody；
         含 metric/指标 且含 value/数值 → generic；其余也按 generic 处理（由
         `parse_generic` 报出缺了哪一列，比在这里含糊地猜更有用）；
      3. 其它后缀 → 抛 `ValueError`，错误信息里列出支持的输入。

    不存在的路径一律先抛 `FileNotFoundError`，避免"文件没找到"被当成"格式不支持"。
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到输入：{path}\n{describe_kind(path)}")
    kind = sniff_kind(path)
    if kind is None:
        raise ValueError(f"无法识别的输入格式：{path}\n{describe_kind(path)}")
    return kind


def parse_path(path: str, **kwargs: Any) -> Dataset:
    """自动识别来源并解析，返回 `Dataset`。

    `**kwargs` 透传给对应的解析器。各解析器都接受的关键字：

    ==================== ==========================================================
    关键字                含义
    ==================== ==========================================================
    `source_name`        调用方指定的来源名（Apple / 通用表格用它兜底）。
    `mask_source_names`  默认 True：来源名过 `privacy.mask_source_name()` 剥掉机主姓名前缀。
    ==================== ==========================================================

    下面两个只对 Apple 有意义（只有 Apple 导出里存在"同一天多台设备"）：

    * `source_policy`：`primary`（默认）/ `per-day-max` / `sum`；
    * `prefer_sources`：来源优先级顺序，如 `["Apple Watch"]`。

    它们**在 InBody / 通用表格上是允许传入但不起作用的**——这两个来源的数据里
    没有"同一天多台设备各报一份"这回事。这样上层不必先判类型再决定传哪些参数。
    这一步只过滤这三个 Apple 专有键；其余名字写错仍然照常抛 `TypeError`，
    免得"参数名打错"被悄悄吃掉。
    """
    kind = detect_kind(path)
    kwargs = _drop_apple_only(kwargs)
    if kind == "apple":
        return parse_apple(path, **kwargs)
    if kind == "inbody":
        return parse_inbody(path, **kwargs)
    return parse_generic(path, **kwargs)


#: 只有 Apple 解析器认识的键（其余关键字一律透传，写错就抛 TypeError）。
_APPLE_ONLY_KEYS = ("source_policy", "prefer_sources")


def _drop_apple_only(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """返回去掉 Apple 专有键的一份副本；`source_policy`/`prefer_sources` 非法值仍会校验。

    注意：`parse_apple()` 自己会校验 `source_policy` 的取值，所以这里不做静默纠正；
    只有当输入**不是** Apple 导出时才把这两个键去掉。
    """
    return {k: v for k, v in kwargs.items() if k not in _APPLE_ONLY_KEYS}
