"""InBody 报告解析器：把体成分仪导出的「宽表」报告变成统一的 `Dataset`。

支持输入
--------
InBody 报告长格式 CSV（`01_InBody体测/InBody数据对比.csv` 那种），表头形如：

    分类,指标,单位,2026-01-10 07:30,2026-05-20 07:30,变化量,报告参考范围,备注

* 日期列数量不定（1~5 次测试都支持），靠 `datetime.strptime` 识别，
  表头属于 `分类 / 指标 / 单位 / 变化量 / 报告参考范围 / 备注` 的列会被跳过。
* 「分类」列忽略（类别由 catalog.py 定义）；「变化量」列忽略（由下游算，不信任设备的加减）。
* 名字以「判定」结尾的行（`BMI判定`、`体脂百分比判定`）是**文字结论**，不是数值，
  写进 `Dataset.labels["<metric>_judgement@<日期>"]`。
* 「报告参考范围」会解析成 `RefRange` 挂到该行产生的每个 `Observation` 上，
  设备自带区间的优先级高于内置人群区间（见 model.py 的说明）。

已知限制
--------
* 只认 InBody 这种「指标在行、日期在列」的宽表。通用长格式（`metric,date,value,unit`）
  请用 `parse_generic()`，本模块会明确报错而不是猜。
* 「备注」列里夹带的百分比进度（如 `117.0%→123.0%`）不会解析成数据，只当文字说明。
* 参考区间只取**第一个**：`1518~1774 / 1484~1731` 这类多区间会记一条 note 说明取值。
* 单位列写「—」时按原值接收（体成分仪的输出口径通常已与 spec.unit 一致），
  并记一次 note；若设备用的是磅/斤而单位列恰好缺失，本模块无法察觉。
* 来源名默认过一遍 `privacy.mask_source_name()` 剥掉机主姓名前缀
  （Apple 的默认命名习惯是「某某的 Apple Watch」）。本地调试要看原名时传
  `mask_source_names=False`；本参数只为和其它解析器保持接口一致，不影响解析逻辑。
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import List, Optional, Sequence, Tuple

from .. import catalog, privacy, units
from ..model import Dataset, Observation, RefRange
from ._detect import find_column, iter_rows, normalize_header, parse_date_strict

__all__ = ["parse_inbody", "parse_reference_text"]


# 表头的别名集合
_META_HEADERS: Tuple[str, ...] = ("分类", "指标", "单位", "变化量", "报告参考范围", "参考范围", "备注")
_NAME_ALIASES: Tuple[str, ...] = ("指标", "项目", "指标名称")
_UNIT_ALIASES: Tuple[str, ...] = ("单位",)
_REF_ALIASES: Tuple[str, ...] = ("报告参考范围", "参考范围")
_NOTE_ALIASES: Tuple[str, ...] = ("备注", "说明")
_CATEGORY_ALIASES: Tuple[str, ...] = ("分类", "类别", "项目类别")

_EMPTY_TOKENS = ("", "—", "-", "–", "--", "无", "n/a", "na", "none", "null")

# 范围文本里可用的分隔符（含全角波浪号与「至」）
_TILDE_CHARS = ("~", "～", "〜", "～", "－", "–", "—", "至", "-")

PROFILE_METRIC_NAMES = {
    "身高": "height_cm",
    "年龄": "age",
    "性别": "sex",
}
# 身高同时要进 profile 与 Observation
_HEIGHT_NAMES = ("身高", "身高cm")


# ──────────────────────────────────────────────────────────── 小工具


def _is_empty(text: str) -> bool:
    return (text or "").strip().lower() in _EMPTY_TOKENS


def _to_float(text: str) -> Optional[float]:
    s = (text or "").strip()
    if _is_empty(s):
        return None
    s = s.replace(",", "").replace("%", "").strip()
    if s.startswith("+"):
        s = s[1:]
    s = s.translate(str.maketrans("０１２３４５６７８９．－", "0123456789.-"))
    try:
        return float(s)
    except ValueError:
        return None


def _find_date_columns(header: Sequence[str], rows: Sequence[Sequence[str]]) -> List[int]:
    """找出日期列的序号，按列顺序返回。

    表头属于「分类/指标/单位/变化量/报告参考范围/备注」的列直接跳过。
    其余列先看**表头本身**能不能解析成日期（InBody 报告的日期就写在表头里，
    数据行反而是数值），解析不了再整列抽样，只有非空单元格全部能解析成日期才算
    日期列（避免把 `0.80` 这种数值列误认成日期）。
    """
    meta = {normalize_header(h) for h in _META_HEADERS}
    out: List[int] = []
    for i, name in enumerate(header):
        if normalize_header(name) in meta:
            continue
        if parse_date_strict(name) is not None:
            out.append(i)
            continue
        nonempty = [(r[i] if i < len(r) else "") for r in rows[:40]]
        nonempty = [v for v in nonempty if not _is_empty(v)]
        if nonempty and all(parse_date_strict(v) is not None for v in nonempty):
            out.append(i)
    return out


def _column_date(header: Sequence[str], col: int) -> str:
    """从表头单元格解析出日期；解析不出来返回空串。"""
    raw = header[col] if col < len(header) else ""
    d = parse_date_strict(raw)
    return d.strftime("%Y-%m-%d") if d else ""


# ──────────────────────────────────────────────────────────── 参考区间


def parse_reference_text(text: str, metric_name: str = "") -> Tuple[Optional[RefRange], List[str]]:
    """把「报告参考范围」的文本解析成 `RefRange`。

    返回 (区间或 None, 提示信息列表)。识别不了就返回 None——**不要硬编一个区间**，
    没有参考范围是诚实的信息，编一个出来会让报告说假话。
    """
    notes: List[str] = []
    raw = (text or "").strip()
    if _is_empty(raw):
        return None, notes

    body = raw
    kind = "range"
    source = "设备报告参考范围"
    low: Optional[float] = None
    high: Optional[float] = None
    low_inc, high_inc = True, True

    # 多个区间（1518~1774 / 1484~1731）：取第一个并说明
    if "/" in body or "／" in body:
        parts = [p for p in body.replace("／", "/").split("/") if p.strip()]
        if len(parts) > 1:
            notes.append(
                f"「{metric_name or '该指标'}」给出多个参考区间（{raw}），已取第一个「{parts[0].strip()}」。"
            )
        body = parts[0].strip() if parts else body

    # 单侧阈值：<10 / <=10 / >7 / >=7 / ≤ / ≥
    m = _match_single_sided(body)
    if m is not None:
        op, num = m
        if op in ("<", "≤", "<="):
            high, high_inc = num, op == "<=" or op == "≤"
        else:
            low, low_inc = num, op == ">=" or op == "≥"
        return RefRange(low=low, high=high, low_inclusive=low_inc, high_inclusive=high_inc,
                        text=raw, source=source, kind=kind), notes

    # 双边界
    pair = _split_pair(body)
    if pair is not None:
        a, b = pair
        low, high = (a, b) if a <= b else (b, a)
        return RefRange(low=low, high=high, low_inclusive=True, high_inclusive=True,
                        text=raw, source=source, kind=kind), notes

    # 解析不了：保留原文但**不给数字区间**，让下判定时明确"无法判断"
    notes.append(f"参考范围「{raw}」无法解析成数值区间，已原样保留文字，判定时会跳过这一项。")
    return RefRange(text=raw, source=source, kind=kind), notes


def _match_single_sided(body: str) -> Optional[Tuple[str, float]]:
    import re

    m = re.match(r"^\s*(<=|≤|=<|<|>=|≥=>|>)\s*([0-9]+(?:\.[0-9]+)?)\s*$", body)
    if not m:
        return None
    op = m.group(1)
    num = float(m.group(2))
    if op in ("<=", "≤", "=<"):
        return "<=", num
    if op == "<":
        return "<", num
    if op in (">=", "≥", "=>"):
        return ">=", num
    return ">", num


def _split_pair(body: str) -> Optional[Tuple[float, float]]:
    """把 'A~B' / 'A-B' / 'A 至 B' 拆成两个数。"""
    import re

    tokens = re.split(r"[~～〜至]|(?<=\d)\s*[-–—－]\s*(?=\d)", body.strip())
    tokens = [t.strip() for t in tokens if t and t.strip()]
    if len(tokens) < 2:
        return None
    a, b = _to_float(tokens[0]), _to_float(tokens[1])
    if a is None or b is None:
        return None
    return a, b


# ──────────────────────────────────────────────────────────── 主入口


def parse_inbody(path: str, source_name: str = "InBody",
                 mask_source_names: bool = True) -> Dataset:
    """解析 InBody 报告长格式 CSV，返回统一 `Dataset`。

    `mask_source_names=True`（默认）时，写进 `Observation.source` 的来源名先过
    `privacy.mask_source_name()`；为 False 时完全不脱敏（仅本地调试）。
    """
    ds = Dataset()
    ds.files.append(os.path.basename(os.path.normpath(path)))
    # 来源名只在这一处定稿，后面的每一行观测都用它
    if mask_source_names:
        source_name = privacy.mask_source_name(source_name)

    header, rows = iter_rows(path)
    date_cols = _find_date_columns(header, rows)
    if not date_cols:
        raise ValueError(
            f"没找到日期列，这不是 InBody 长格式导出：{path}。"
            "InBody 报告的日期应在表头（如「2026-01-10 07:30」）；"
            "通用长格式请用 metric,date,value,unit 四列（见 docs/DATA_FORMATS.md）。"
        )

    name_col = find_column(header, _NAME_ALIASES)
    if name_col is None:
        raise ValueError(
            f"表头里没有「指标」列：{path}。实际读到的表头为：{header}。"
            "InBody 长格式需要「指标」列来识别每个测量项目。"
        )
    unit_col = find_column(header, _UNIT_ALIASES)
    ref_col = find_column(header, _REF_ALIASES)
    note_col = find_column(header, _NOTE_ALIASES)
    # 「分类」列平时忽略（类别由 catalog 定义），只在行名解析不出来时当兜底线索
    category_col = find_column(header, _CATEGORY_ALIASES)

    # 数据里的日期范围用来推算年龄
    all_dates: List[date] = []
    for col in date_cols:
        d = parse_date_strict(header[col] if col < len(header) else "")
        if d:
            all_dates.append(d)
    latest_date = max(all_dates) if all_dates else None

    n_obs = 0
    n_labels = 0
    n_multi_ref = 0
    n_notes_added = 0
    unresolved: List[str] = []
    unit_issues: List[str] = []
    _unit_issue_seen: set = set()

    for row in rows:
        name = (row[name_col] if name_col < len(row) else "").strip()
        if not name:
            continue
        unit_raw = (row[unit_col] if unit_col is not None and unit_col < len(row) else "").strip()
        category = (row[category_col] if category_col is not None and category_col < len(row) else "").strip()

        # 1) 文字判定行：BMI判定 / 体脂百分比判定 ... -> Dataset.labels
        if name.endswith("判定"):
            base_name = name[:-2].strip()
            key = catalog.resolve(base_name) or catalog.normalize(base_name)
            for col in date_cols:
                value = (row[col] if col < len(row) else "").strip()
                if _is_empty(value):
                    continue
                ds.labels[f"{key}_judgement@{_column_date(header, col)}"] = value
                n_labels += 1
            continue

        # 2) 只有 profile 意义的行
        if name == "年龄":
            for col in date_cols:
                v = _to_float(row[col] if col < len(row) else "")
                if v is not None:
                    ds.profile["age"] = int(v)
                    break
            continue
        if name == "性别":
            for col in date_cols:
                v = (row[col] if col < len(row) else "").strip()
                if not _is_empty(v):
                    ds.profile["sex"] = v
                    break
            continue

        # 3) 其余行都要能 resolve 成指标
        metric_id = catalog.resolve(name)
        if metric_id is None:
            # 「分类」列常常承载行名省略掉的信息（例如真实报告里分类=内脏脂肪等级、
            # 而行名只写「等级」）。**只在行名解析不出来时**才用分类兜底，
            # 正常行一律不受分类影响，所以不会发生"分类把好行带歪"的情况。
            metric_id = catalog.resolve(category) if category else None
        if metric_id is None:
            if name not in unresolved:
                unresolved.append(name)
            continue
        spec = catalog.get(metric_id)
        if spec is None:  # 理论上不会发生
            if name not in unresolved:
                unresolved.append(name)
            continue

        if name in _HEIGHT_NAMES:
            # 身高：进 profile，同时产生 Observation
            for col in date_cols:
                v = _to_float(row[col] if col < len(row) else "")
                if v is None:
                    continue
                value = _convert_or_raw(v, unit_raw, spec.unit, name, unit_issues, _unit_issue_seen)
                if value is None:
                    continue
                ds.profile.setdefault("height_cm", value)
                day = _column_date(header, col)
                ds.observations.append(
                    Observation(metric=metric_id, date=day, value=float(value),
                                unit=spec.unit, source=source_name, ts=day,
                                ref=None, kind="measured")
                )
                n_obs += 1
            continue

        # 4) 参考区间（整行共用）
        ref: Optional[RefRange] = None
        if ref_col is not None:
            ref_text = (row[ref_col] if ref_col < len(row) else "").strip()
            ref, ref_notes = parse_reference_text(ref_text, name)
            for n in ref_notes:
                if "多个参考区间" in n:
                    n_multi_ref += 1
                ds.add_note(n)

        # 5) 逐个日期列产生观测
        for col in date_cols:
            raw_text = (row[col] if col < len(row) else "").strip()
            v = _to_float(raw_text)
            if v is None:
                continue  # "—"、"超重" 这类文本：跳过该单元格，不硬凑 0
            if metric_id == "body_fat_pct" and 0 < v <= 1.0:
                v *= 100.0
            value = _convert_or_raw(v, unit_raw, spec.unit, name, unit_issues, _unit_issue_seen)
            if value is None:
                continue
            day = _column_date(header, col)
            ds.observations.append(
                Observation(metric=metric_id, date=day, value=float(value), unit=spec.unit,
                            source=source_name,
                            ts=(header[col] if col < len(header) else day).strip()[:19] or day,
                            ref=ref, kind="measured")
            )
            n_obs += 1

        # 6) 备注列
        if note_col is not None and n_notes_added < 30:
            note_text = (row[note_col] if note_col < len(row) else "").strip()
            if not _is_empty(note_text) and note_text not in ds.notes:
                ds.add_note(f"{name}：{note_text}")
                n_notes_added += 1

    # ── 收尾提示 ──────────────────────────────────────────
    for text in unit_issues[:10]:
        ds.add_note(text)
    if unresolved:
        head = "、".join(unresolved[:20])
        more = f"（另有 {len(unresolved) - 20} 项未列出）" if len(unresolved) > 20 else ""
        ds.add_note(
            f"有 {len(unresolved)} 个测量项目没能对应到 catalog.py 里的指标，已跳过：{head}{more}。"
            "若其中项目需要进报告，请在 catalog.py 里补别名。"
        )
    if n_multi_ref:
        ds.add_note(f"有 {n_multi_ref} 个指标给出了多个参考区间，均只取第一个用于判定。")

    if latest_date is not None and "age" not in ds.profile:
        birth = str(ds.profile.get("birth_date") or "")
        if birth:
            try:
                born = datetime.strptime(birth[:10], "%Y-%m-%d").date()
                ds.profile["age"] = int(
                    latest_date.year - born.year
                    - ((latest_date.month, latest_date.day) < (born.month, born.day))
                )
            except ValueError:
                ds.add_note(f"无法解析出生日期「{birth}」，年龄未推算。")

    span = ""
    dates = sorted({o.date for o in ds.observations if o.date})
    if dates:
        span = f"，日期 {dates[0]} → {dates[-1]}（{len(dates)} 次测量）"
    ds.add_note(
        f"InBody 报告总览：{len(rows)} 个指标行，产生 {n_obs} 条观测，"
        f"文字判定 {n_labels} 条，识别到 {len(ds.metric_ids())} 个指标{span}。"
    )
    ds.dedupe()
    return ds


def _convert_or_raw(value: float, unit_raw: str, target_unit: str, name: str,
                    issues: List[str], seen: set) -> Optional[float]:
    """单位换算；单位列是「—」或换算不出来时按原值处理，并记一次 note。"""
    if not _is_empty(unit_raw):
        converted = units.convert(value, unit_raw, target_unit)
        if converted is not None:
            return converted
    key = (name, unit_raw, target_unit)
    if key not in seen:
        seen.add(key)
        issues.append(
            f"单位缺失，按原值处理：{name}（单位列「{unit_raw or '空'}」，目标单位「{target_unit}」）。"
        )
    return value
