"""通用长格式解析器：任何「一行一个测量值」的表格都能进来。

支持输入
--------
`.csv` / `.tsv` / `.txt`，分隔符自动识别（逗号 / 制表符 / 分号）；
编码在 `utf-8-sig` 与 `gbk` 之间自动退回。表头别名（大小写与空格不敏感）：

============== ==========================================================
统一字段        可接受的表头写法
============== ==========================================================
日期            `date` / `日期` / `时间` / `time` / `timestamp`
指标            `metric` / `指标` / `metric_id` / `item` / `项目`
数值            `value` / `数值` / `值` / `result`
单位            `unit` / `单位`
来源            `source` / `来源` / `device` / `设备`
============== ==========================================================

典型用法（自己从家用秤、血压计导出的表）：

    date,metric,value,unit,source
    2026-01-10,体重,74.0,kg,智能秤
    2026-01-10 07:10,体脂率,22.3,%,智能秤

行为约定
--------
* 指标名一律走 `catalog.resolve()`；认不出的名字**不静默丢弃**，最后集中记一条 note。
* 单位走 `units.convert()`；缺失或换算失败则按原值接收，并记一次 note。
* 日期解析很宽松：`YYYY-MM-DD`、`YYYY/MM/DD`、`YYYY-MM-DD HH:MM:SS`、
  `YYYY年M月D日`、ISO `T` 分隔、带时区偏移都能吃；解析不了的行跳过并计数上报。
* 同一指标同一天有多条时，按 `catalog` 里该指标的 `agg` 聚合（`sum`/`mean`/`snapshot`），
  和 Apple 解析器保持同一套口径——这样两个来源的数据能直接放在一起比。

已知限制
--------
* 只处理「一个数值列」的长格式。宽表（每个指标一列）请先转置，
  或让上游按长格式导出。
* 不读 Excel 的 `.xlsx`；请先另存为 CSV。
* 时区信息（`+0800` 这类）会被丢掉，按字面本地时间归属日期。
* 来源名（`source` 列或 `source_name` 参数）默认过一遍 `privacy.mask_source_name()`
  剥掉机主姓名前缀；本地调试要看原名时传 `mask_source_names=False`。
  本参数只为和其它解析器保持接口一致，不影响解析逻辑。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .. import catalog, privacy, units
from ..model import Dataset, Observation
from ._detect import (
    DATE_HEADERS,
    METRIC_HEADERS,
    SOURCE_HEADERS,
    UNIT_HEADERS,
    VALUE_HEADERS,
    find_column,
    iter_rows,
    parse_date_lenient,
)

__all__ = ["parse_generic", "parse_date_value"]


_EMPTY_TOKENS = ("", "—", "-", "–", "--", "无", "n/a", "na", "none", "null")


def _is_empty(text: Any) -> bool:
    return ("" if text is None else str(text)).strip().lower() in _EMPTY_TOKENS


def _to_float(text: Any) -> Optional[float]:
    s = ("" if text is None else str(text)).strip()
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


def parse_date_value(raw: str) -> Optional[Tuple[str, str]]:
    """解析日期单元格，返回 ('YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS')；解析不了返回 None。"""
    dt = parse_date_lenient(raw)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S")


def _resolve_columns(path: str, header: List[str]) -> Dict[str, int]:
    """定位日期/指标/数值/单位/来源五列，缺关键列时抛带表头的中文 ValueError。"""
    cols: Dict[str, int] = {}
    date_col = find_column(header, DATE_HEADERS)
    metric_col = find_column(header, METRIC_HEADERS)
    value_col = find_column(header, VALUE_HEADERS)
    missing = []
    if date_col is None:
        missing.append("日期（date/日期/时间/time/timestamp）")
    if metric_col is None:
        missing.append("指标（metric/指标/metric_id/item/项目）")
    if value_col is None:
        missing.append("数值（value/数值/值/result）")
    if missing:
        raise ValueError(
            f"通用长格式需要日期、指标、数值三列，缺少：{'；'.join(missing)}。\n"
            f"文件：{path}\n实际读到的表头：{header}\n"
            "请把表头改成 date,metric,value,unit,source 这类写法（unit/source 可选）。"
        )
    cols["date"] = int(date_col)
    cols["metric"] = int(metric_col)
    cols["value"] = int(value_col)
    unit_col = find_column(header, UNIT_HEADERS)
    source_col = find_column(header, SOURCE_HEADERS)
    if unit_col is not None:
        cols["unit"] = unit_col
    if source_col is not None:
        cols["source"] = source_col
    return cols


def parse_generic(path: str, source_name: str = "",
                  mask_source_names: bool = True) -> Dataset:
    """解析通用长格式表格，返回统一 `Dataset`。

    `mask_source_names=True`（默认）时，来源名（`source` 列或 `source_name` 参数）
    先过 `privacy.mask_source_name()`；为 False 时完全不脱敏（仅本地调试）。
    """
    ds = Dataset()
    ds.files.append(os.path.basename(os.path.normpath(path)))
    # 相同来源名只脱敏一次：一张表里可能有几万行
    masked: Dict[str, str] = {}

    def mask(src: str) -> str:
        if not mask_source_names or not src:
            return src
        hit = masked.get(src)
        if hit is None:
            hit = privacy.mask_source_name(src)
            masked[src] = hit
        return hit

    # 来源名只在这一处定稿，后面的每一行观测都用它（mask 内已判开关）
    source_name = mask(source_name)

    header, rows = iter_rows(path)
    cols = _resolve_columns(path, list(header))

    unknown_names: List[str] = []
    unknown_seen: set = set()
    unit_notes: List[str] = []
    unit_seen: set = set()
    skipped_rows = 0
    n_obs = 0
    n_aggregated_days = 0

    # (指标, 日期) -> 聚合状态。口径与 apple.py 一致，便于两个来源直接比较。
    aggregates: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for row in rows:
        def cell(key: str) -> str:
            i = cols.get(key)
            if i is None or i >= len(row):
                return ""
            return str(row[i]).strip()

        name = cell("metric")
        if not name:
            skipped_rows += 1
            continue
        metric_id = catalog.resolve(name)
        if metric_id is None:
            if name not in unknown_seen:
                unknown_seen.add(name)
                unknown_names.append(name)
            skipped_rows += 1
            continue
        spec = catalog.get(metric_id)
        if spec is None:
            skipped_rows += 1
            continue

        parsed = parse_date_value(cell("date"))
        if parsed is None:
            skipped_rows += 1
            continue
        day, ts = parsed

        raw = _to_float(cell("value"))
        if raw is None:
            skipped_rows += 1
            continue

        unit_raw = cell("unit")
        value: Optional[float]
        if _is_empty(unit_raw):
            value = raw
            key = (metric_id, unit_raw)
            if key not in unit_seen:
                unit_seen.add(key)
                unit_notes.append(
                    f"单位缺失，按原值处理：{catalog.display_name(metric_id)}"
                    f"（目标单位「{spec.unit}」）。跨来源比较时请留意口径。"
                )
        else:
            value = units.convert(raw, unit_raw, spec.unit)
            if value is None:
                value = raw
                key = (metric_id, unit_raw)
                if key not in unit_seen:
                    unit_seen.add(key)
                    unit_notes.append(
                        f"单位无法换算，已按原值保留：{catalog.display_name(metric_id)}"
                        f"（{metric_id}）的单位「{unit_raw}」→「{spec.unit}」。"
                    )

        if metric_id == "body_fat_pct" and units.looks_like_ratio_value(unit_raw) and value <= 1.0:
            value *= 100.0

        row_source = mask(cell("source") or source_name or "")
        key2 = (metric_id, day)
        state = aggregates.get(key2)
        if state is None:
            state = {"agg": spec.agg, "total": 0.0, "weight": 0.0, "count": 0,
                     "snap_ts": "", "snap_value": None, "snap_source": "", "last_ts": ""}
            aggregates[key2] = state
        state["count"] += 1
        state["total"] += float(value)
        state["weight"] += 1.0
        if ts > str(state["last_ts"]):
            state["last_ts"] = ts
        if state["snap_value"] is None or ts >= str(state["snap_ts"]):
            state["snap_ts"] = ts
            state["snap_value"] = float(value)
            state["snap_source"] = row_source

    for (metric_id, day), st in sorted(aggregates.items()):
        spec = catalog.get(metric_id)
        if spec is None:
            continue
        agg = str(st["agg"])
        if agg == "snapshot":
            value = st["snap_value"]
            ts = str(st["snap_ts"])
            src = str(st["snap_source"]) or source_name or "通用表格"
        elif agg == "mean":
            value = (st["total"] / st["weight"]) if st["weight"] else None
            ts = str(st["last_ts"]) or day
            src = source_name or "通用表格"
        else:  # sum
            value = st["total"]
            ts = str(st["last_ts"]) or day
            src = source_name or "通用表格"
        if value is None:
            continue
        if int(st["count"]) > 1:
            n_aggregated_days += 1
        ds.observations.append(
            Observation(metric=metric_id, date=day, value=float(value), unit=spec.unit,
                        source=src, ts=ts, ref=None, kind="measured")
        )
        n_obs += 1

    # ── 提示 ──────────────────────────────────────────────
    for text in unit_notes[:10]:
        ds.add_note(text)
    if unknown_names:
        head = "、".join(unknown_names[:20])
        more = f"（另有 {len(unknown_names) - 20} 项未列出）" if len(unknown_names) > 20 else ""
        ds.add_note(
            f"有 {len(unknown_names)} 个指标名没能对应到 catalog.py 里的指标，已跳过：{head}{more}。"
            "若需要进报告，请在 catalog.py 里补一条别名。"
        )
    if skipped_rows:
        ds.add_note(
            f"通用表格解析：跳过 {skipped_rows} 行（日期或数值解析失败、指标不认识）。"
        )
    if n_aggregated_days:
        ds.add_note(
            f"有 {n_aggregated_days} 个「指标 + 日期」组合出现过多条记录，已按 catalog 的聚合口径合并"
            "（累加型求和、心率类求平均、体重类取当日最晚一条）。"
        )
    dates = sorted({o.date for o in ds.observations if o.date})
    span = f"，日期 {dates[0]} → {dates[-1]}（{len(dates)} 天）" if dates else ""
    ds.add_note(
        f"通用表格总览：{len(rows)} 行输入，产生 {n_obs} 条观测，"
        f"识别到 {len(ds.metric_ids())} 个指标{span}。"
    )
    ds.dedupe()
    return ds
