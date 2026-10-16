"""设备名脱敏。

**为什么需要这个**：Apple 的默认设备名里带机主姓名——「<姓名>的 Apple Watch」、
「<Name>'s iPhone」。真实导出里确实会出现这种名字（实测某份导出里有 14,883 条
记录来自「某某的Apple Watch」）。

而本工具会把 `sourceName` 打印进报告的「数据来源」和每一行的来源列。于是
**一份用来分享的报告会把机主姓名一起带出去**——这与「本地优先、默认不泄露」的
定位直接冲突。搜索行为里最自然的动作就是"把生成的报告发给别人看看"。

所以默认按 Apple 的默认命名约定剥掉姓名前缀，只保留器械部分：

    「某某的Apple Watch」      -> 「Apple Watch」
    「Zhang San's iPhone」      -> 「iPhone」
    「明成祖钛镶九龙戏珠手镯」     -> 原样保留（不含姓名前缀）
    「iPhone」                  -> 原样保留

保留器械名而不是全部打成「设备A」，是因为用户需要认出哪台是哪台
（例如指定主设备、判断是不是换了装备）。要连器械名一起隐藏，用 `--alias-sources`；
要完全不脱敏（仅本地调试），用 `--show-source-names`。
"""

from __future__ import annotations

import re

__all__ = ["mask_source_name", "looks_like_personal_name", "MASK_NOTE", "ALIAS_PREFIX"]

# 「<姓名>的<器械>」——Apple 中文默认命名
_ZH_POSSESSIVE = re.compile(r"^(?P<name>[^的\s]{1,12})的(?P<rest>\S.*)$")
# 「<Name>'s <device>」/「<Name>’s <device>」——Apple 英文默认命名
_EN_POSSESSIVE = re.compile(r"^(?P<name>.{1,24}?)['’]s\s+(?P<rest>\S.*)$")

# 姓名前缀里不该出现的东西：数字、明显是型号的词。命中就不当作姓名。
_MODEL_HINT = re.compile(r"\d")

MASK_NOTE = ("检测到设备名里含机主姓名（Apple 的默认命名习惯，如「某某的 Apple Watch」），"
             "已在报告与导出文件里隐去姓名部分；器械名保留，便于你分辨设备。"
             "要完全不脱敏（仅本地调试）加 --show-source-names。")

ALIAS_PREFIX = "设备"


def _looks_like_name(part: str) -> bool:
    """粗判这一段像不像人名。

    宁可放宽一点：多剥一层只会让设备名少一截（还能用 --show-source-names 看回来），
    而漏剥一次就是把真名写进了要分享的报告。
    """
    if not part or len(part) > 24:
        return False
    if _MODEL_HINT.search(part):
        # 「Apple Watch Series 7 的...」这种不是姓名前缀
        return False
    return True


def _clean_spaces(s: str) -> str:
    """把各种空白（含 U+00A0 非断行空格）归一成普通空格。

    真实 Apple 导出里的设备名就带 U+00A0：`某某的Apple\\xa0Watch`。
    不归一的话，脱敏后得到的是 `Apple\\xa0Watch`，字符串上不等于 `Apple Watch`——
    显示看不出来，但比较、去重、按名字指定主设备都会受影响。
    """
    return re.sub(r"[\s\u00a0\u2007\u202f]+", " ", s).strip()


def mask_source_name(name: str) -> str:
    """剥掉设备名里的机主姓名前缀。已经是器械名则原样返回。"""
    if not name:
        return name
    s = _clean_spaces(str(name))
    if not s:
        return s
    for pat in (_ZH_POSSESSIVE, _EN_POSSESSIVE):
        m = pat.match(s)
        if m and _looks_like_name(m.group("name")):
            rest = _clean_spaces(m.group("rest"))
            if rest:
                return rest
    return s


def looks_like_personal_name(name: str) -> bool:
    """这个设备名里是否含疑似机主姓名的前缀。"""
    if not name:
        return False
    s = str(name).strip()
    return mask_source_name(s) != s


def alias_source_name(name: str, index: int) -> str:
    """完全别名化：`设备A` / `设备B`（按 index 稳定编号）。"""
    letter = chr(ord("A") + (index % 26))
    suffix = "" if index < 26 else str(index // 26)
    return f"{ALIAS_PREFIX}{letter}{suffix}"
