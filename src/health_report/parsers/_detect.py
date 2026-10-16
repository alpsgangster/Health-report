"""解析器共用的识别与日期解析工具（内部模块，不对外承诺接口）。

这里集中了三件容易被各处重复实现的事情：

1. **格式识别**：`sniff_kind()` 只看后缀与表头，不做完整解析。
   CSV/TSV 一律先按 `utf-8-sig` 读，成功即用（本项目自己的导出是 UTF-8 带 BOM）；
   失败再退回 `gbk`——体成分仪与老版本 Excel 导出的中文 CSV 通常是 GBK。
2. **表头别名**：`column_map()` 把"日期 / 时间 / date / timestamp"这类不同写法
   映射到统一字段名，大小写与空格不敏感。
3. **宽松日期解析**：`parse_date_strict()` 只认 InBody 那几种固定格式；
   `parse_date_lenient()` 额外吃 `YYYY年M月D日`、ISO `T` 分隔、带时区偏移等写法。

已知限制：识别完全基于文件名与表头，不读数据分布。所以一个刻意把表头写成
InBody 样式、但内容是别的东西的文件会被误判——此时应改用 `parse_generic()`
直接指定解析器。
"""

from __future__ import annotations

import csv
import io
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple  # noqa: F401  (Any 供 open_binary_input 的返回类型用)

__all__ = [
    "APPLE_SUFFIXES",
    "GENERIC_VALUE_HEADERS",
    "INBODY_META_HEADERS",
    "METRIC_HEADERS",
    "VALUE_HEADERS",
    "DATE_HEADERS",
    "UNIT_HEADERS",
    "SOURCE_HEADERS",
    "read_text",
    "iter_rows",
    "sniff_delimiter",
    "normalize_header",
    "column_map",
    "find_column",
    "parse_date_strict",
    "parse_date_lenient",
    "looks_like_date_column",
    "sniff_kind",
    "open_binary_input",
    "describe_kind",
]

# ──────────────────────────────────────────────────────────── 后缀与表头常量

APPLE_SUFFIXES: Tuple[str, ...] = (".zip", ".xml")

# InBody 长格式里属于"元数据"的列名，识别日期列时要跳过它们
INBODY_META_HEADERS: Tuple[str, ...] = (
    "分类", "指标", "单位", "变化量", "报告参考范围", "参考范围", "备注",
)

DATE_HEADERS: Tuple[str, ...] = ("date", "日期", "时间", "time", "timestamp", "测量日期", "测量时间")
METRIC_HEADERS: Tuple[str, ...] = ("metric", "指标", "metric_id", "item", "项目", "项目名")
VALUE_HEADERS: Tuple[str, ...] = ("value", "数值", "值", "result", "结果")
UNIT_HEADERS: Tuple[str, ...] = ("unit", "单位")
SOURCE_HEADERS: Tuple[str, ...] = ("source", "来源", "device", "设备")

# 通用长格式里可以当「数值」用的列（InBody 没有 value 列，这里只为识别服务）
GENERIC_VALUE_HEADERS: Tuple[str, ...] = VALUE_HEADERS

_DATE_PATTERNS_STRICT: Tuple[str, ...] = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
)

_DATE_PATTERNS_LENIENT: Tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d",
    "%Y%m%d",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y年%m月%d日",
    "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
)

_YEAR_MIN = 1950
_YEAR_MAX = 2100


# ──────────────────────────────────────────────────────────── 读文件


def read_text(path: str, newline: bool = False) -> str:
    """读取文本文件，自动在 utf-8-sig / gbk 之间退回。读不到任何内容时抛中文 ValueError。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到文件：{path}")
    if os.path.isdir(path):
        raise ValueError(f"这是一个目录，不是文件：{path}（若要解析 Apple 导出目录，请把目录路径传给 parse_apple）")
    last_err: Optional[Exception] = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=enc, newline="" if newline else None) as f:
                return f.read()
        except UnicodeDecodeError as exc:  # 换下一个编码重试
            last_err = exc
        except OSError as exc:
            raise ValueError(f"读取文件失败：{path}（{exc}）") from exc
    raise ValueError(
        f"无法解码文件：{path}。已尝试 utf-8 与 gbk，均失败（{last_err}）。"
        "请先把文件转成 UTF-8 或 GBK 编码再试。"
    )


def sniff_delimiter(text: str) -> str:
    """从文本前若干行猜分隔符：制表符 / 分号 / 逗号。"""
    sample = "\n".join(text.splitlines()[:20])
    tabs = sample.count("\t")
    semis = sample.count(";")
    commas = sample.count(",")
    if tabs and tabs >= max(semis, commas):
        return "\t"
    if semis > commas:
        return ";"
    if commas:
        return ","
    return "\t" if tabs else ","


def iter_rows(path: str, delimiter: Optional[str] = None) -> Tuple[List[str], List[List[str]]]:
    """读表格文件，返回 (表头行, 数据行列表)。

    会自动去掉全空行，并裁掉每行末尾因尾随分隔符产生的空单元格之外的多余空白。
    """
    text = read_text(path)
    if not text.strip():
        raise ValueError(f"文件是空的：{path}")
    if delimiter is None:
        delimiter = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: List[List[str]] = []
    for raw in reader:
        if raw and any((c or "").strip() for c in raw):
            rows.append([("" if c is None else c).strip() for c in raw])
    if not rows:
        raise ValueError(f"文件里没有任何有效行：{path}")
    header = rows[0]
    return header, rows[1:]


# ──────────────────────────────────────────────────────────── 表头


def normalize_header(name: str) -> str:
    """表头归一：去掉 BOM、空白、下划线与常见标点，转小写。"""
    if not name:
        return ""
    s = name.lstrip("\ufeff").strip().lower()
    s = s.replace(" ", "").replace("\u3000", "").replace("_", "")
    for ch in ("：", ":", "*", "（", "）", "(", ")", "【", "】", "[", "]"):
        s = s.replace(ch, "")
    return s


def column_map(header: Sequence[str], aliases: Sequence[str]) -> Dict[str, int]:
    """把表头映射成 {统一字段名: 列序号}；同一字段多列时取最左一列。

    aliases 里第一个命中的字段名即为统一字段名，例如 aliases[0] == "date"。
    """
    if not aliases:
        return {}
    canonical = aliases[0]
    index: Dict[str, int] = {}
    for i, raw in enumerate(header):
        n = normalize_header(raw)
        if not n:
            continue
        for alias in aliases:
            if n == normalize_header(alias):
                index.setdefault(canonical, i)
                break
    return index


def find_column(header: Sequence[str], aliases: Sequence[str]) -> Optional[int]:
    """在表头里找第一个匹配别名的列序号；找不到返回 None。"""
    idx = column_map(header, aliases).get(aliases[0])
    return idx


# ──────────────────────────────────────────────────────────── 日期


def _usable(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if _YEAR_MIN <= dt.year <= _YEAR_MAX:
        return dt
    return None


def _prepare(raw: str) -> str:
    """去掉空白与中文年月日之后的多余字符，统一分隔符。"""
    s = (raw or "").strip().strip("\ufeff")
    if not s:
        return ""
    # 全角数字与减号
    s = s.translate(str.maketrans("０１２３４５６７８９－／．", "0123456789-/."))
    return s


def parse_date_strict(raw: str) -> Optional[date]:
    """只认 InBody 报告里的几种固定写法：%Y-%m-%d[ %H:%M[:%S]]、%Y/%m/%d。"""
    s = _prepare(raw)
    if not s or len(s) > 32:
        return None
    for fmt in _DATE_PATTERNS_STRICT:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return _usable(dt)
    return None


def parse_date_lenient(raw: str) -> Optional[datetime]:
    """宽松解析：上面那些 + 中文年月日、ISO T 分隔、带时区偏移、%Y%m%d、美式/英式日月年。"""
    s = _prepare(raw)
    if not s or len(s) > 40:
        return None
    s2 = s.replace("T", " ")
    # 去掉尾部的时区偏移，如 +0800 / +08:00 / Z
    s2 = re.sub(r"\s*(Z|[+-]\d{2}:?\d{2})\s*$", "", s2)
    # 中文日期
    s2 = re.sub(r"年|月", "-", s2).replace("日", " ")
    s2 = s2.strip()
    candidates = [s, s2, s2.replace(".", "-")]
    if " " not in s2:
        candidates.append(s2 + " 00:00:00")
    for cand in candidates:
        if not cand:
            continue
        for fmt in _DATE_PATTERNS_LENIENT:
            try:
                dt = datetime.strptime(cand, fmt)
            except ValueError:
                continue
            ok = _usable(dt)
            if ok is not None:
                return ok
    return None


def looks_like_date_column(values: Sequence[str], strict: bool = True) -> bool:
    """判断一列值是不是日期列：非空单元格必须全部能解析成日期。"""
    seen = 0
    for raw in values:
        s = (raw or "").strip()
        if not s or s in ("—", "-", "－", "--"):
            continue
        seen += 1
        dt = parse_date_strict(s) if strict else parse_date_lenient(s)
        if dt is None:
            return False
    return seen > 0


# ──────────────────────────────────────────────────────────── 识别


def sniff_apple_member(names: Sequence[str]) -> bool:
    """压缩包成员名里是否有 export.xml / 导出.xml。"""
    for n in names:
        base = os.path.basename(n.replace("\\", "/")).lower()
        if base.endswith(".xml") and ("export" in base or "导出" in base):
            return True
    return False


def sniff_kind(path: str) -> Optional[str]:
    """只看后缀与表头，返回 "apple" / "inbody" / "generic"；完全无法判断时返回 None。"""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in APPLE_SUFFIXES:
        return "apple"
    if os.path.isdir(path):
        bases = [path, os.path.join(path, "apple_health_export")]
        for base in bases:
            for name in ("export.xml", "导出.xml"):
                if os.path.exists(os.path.join(base, name)):
                    return "apple"
        for base in bases:
            if not os.path.isdir(base):
                continue
            for entry in os.listdir(base):
                low = entry.lower()
                if low.endswith(".xml") and (low.startswith("export") or entry.startswith("导出")):
                    return "apple"
        return None
    if suffix in (".csv", ".tsv"):
        header, rows = iter_rows(path)
        meta = {normalize_header(h) for h in INBODY_META_HEADERS}
        has_metric = find_column(header, METRIC_HEADERS) is not None
        has_value = find_column(header, VALUE_HEADERS) is not None
        # 通用长格式（有专门的日期列）优先，否则 InBody 宽表会被抢走
        if has_metric and has_value:
            return "generic"
        # InBody 宽表：日期写在**表头**里（如「2026-01-10 07:30」），数据行是数值/文本
        if has_metric:
            for name in header:
                if normalize_header(name) in meta:
                    continue
                if parse_date_strict(name) is not None:
                    return "inbody"
            for i, name in enumerate(header):
                if normalize_header(name) in meta:
                    continue
                col = [r[i] if i < len(r) else "" for r in rows[:30]]
                if looks_like_date_column(col, strict=True):
                    return "inbody"
        return "generic"
    return None


def describe_kind(path: str) -> str:
    """给 detect_kind 用的「支持哪些输入」说明。"""
    return (
        "支持的输入：\n"
        "  1. Apple 健康导出：export.zip / export.xml / 导出.xml / apple_health_export 目录；\n"
        "  2. InBody 报告长格式：.csv / .tsv，含「指标」列与至少一个日期列；\n"
        "  3. 通用长格式：.csv / .tsv，含 metric/指标 与 value/数值 列。"
    )


# ──────────────────────────────────────────────────────────── Apple 输入


def open_binary_input(path: str) -> Tuple[Any, Any, str]:
    """打开 Apple 导出的 XML，返回 (可读的二进制流, 需要 close 的对象, 描述)。

    支持 .zip（自动找里面路径最短的 export*.xml）、裸 .xml、目录
    （依次找 apple_health_export/export.xml、export.xml、导出.xml，
    再退化为任意 export*.xml / 导出*.xml，多个时取体积最大的那个）。
    """
    import zipfile

    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到输入：{path}")

    if path.lower().endswith(".zip"):
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"不是有效的 zip 文件：{path}（{exc}）") from exc
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")
                 and ("export" in os.path.basename(n).lower() or "导出" in os.path.basename(n))]
        if not names:
            raise ValueError(
                f"压缩包里没找到 export.xml：{path}；包内前 10 项为 {zf.namelist()[:10]}"
            )
        names.sort(key=lambda n: (len(n), n))
        chosen = names[0]
        return zf.open(chosen), zf, f"{os.path.basename(path)} -> {chosen}"

    if os.path.isdir(path):
        bases = [path, os.path.join(path, "apple_health_export")]
        for base in bases:
            for name in ("export.xml", "导出.xml"):
                cand = os.path.join(base, name)
                if os.path.isfile(cand):
                    return open(cand, "rb"), None, cand
        for base in bases:
            if not os.path.isdir(base):
                continue
            entries = [e for e in sorted(os.listdir(base))
                       if e.lower().endswith(".xml")
                       and (e.lower().startswith("export") or e.startswith("导出"))]
            if entries:
                entries.sort(key=lambda e: os.path.getsize(os.path.join(base, e)), reverse=True)
                cand = os.path.join(base, entries[0])
                return open(cand, "rb"), None, cand
        raise ValueError(
            f"目录里没找到 export.xml / 导出.xml：{path}。"
            "请确认这是 Apple 健康 App 导出的目录（应包含 apple_health_export/export.xml）。"
        )

    if not path.lower().endswith(".xml"):
        raise ValueError(f"parse_apple 只接受 .zip / .xml / 目录，收到：{path}")
    return open(path, "rb"), None, path
