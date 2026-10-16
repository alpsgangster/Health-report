"""HTML 交付形态：一份**自包含**的网页报告，浏览器里「打印成 PDF」就是最终交付物。

为什么要有这一层（与 `report.py` 的分工）
----------------------------------------
`report.py` 产出 Markdown：便于 diff、便于复核、便于给模型当上下文。
Markdown 不适合当**客户交付物**——它没有分页、没有图、表格宽度不受控，
而客户真正要的是一份能打印、能发出去的 PDF。

所以这一层只做一件事：把**已经算好的** `Analysis` / `Finding` 渲染成
一份排版受控、可直接打印的 HTML。它和 Markdown 版一样**不算任何数**，
判定措辞、变化列、来源列、区间标注全部复用 `report.py` 里的同一批函数，
保证两份交付物说的是同一句话（口径漂移是这类报告最贵的 bug）。

三条硬约束
----------
1. **自包含、零依赖、零 JS。** 没有任何外部资源（无 `<link>`、无远程 `<img>`、
   无 Web 字体、无 `<script>`）。图表是手写 SVG。**刻意不用 JavaScript**：
   打印不需要它，而且纸质交付物上 hover 提示、折叠动画都不可见——
   静态标注（数值直标、线型区分、文字图例）在纸上才是有效的信息通道。
   唯一「可交互」的东西是不存在的，所以打印结果与屏幕上看到的结构完全一致。
   注意：SVG 不带 `xmlns`，因为 `xmlns="http://www.w3.org/2000/svg"` 会在输出里
   引入一个 `http://` 字面量；HTML5 的内联 SVG 本来就不需要它，
   而 `xml.etree.ElementTree.fromstring` 同样能解析。

2. **确定性。** 同样的输入产生逐字节相同的输出。唯一的例外是封面上的
   「生成时间」那一行，可以用 `generated_at=` 注入（测试与 CI 用它固定输出）。
   所有图表都是**月度聚合**（趋势）或**降采样**（散点）后的结果，
   不依赖任何浮点随机性，也不依赖 dict/set 的遍历顺序。

3. **隐私。** 逐条 `Observation` 的时间戳（`ts`）绝不进 HTML；图上只出现
   「月度中位数 / 四分位区间 / 每夜一个点」这类聚合量。来源名再过一次
   `privacy.mask_source_name()`（幂等）作为防线，`mask_sources=False` 才关闭。

打印（这是本模块的存在理由）
----------------------------
* `@page { size: A4; margin: 16mm 14mm; }`；正文用 pt，图表用 `viewBox` + `width:100%`。
* 主要小节之间强制分页；表格行、图表卡片、提示块 `break-inside: avoid`
  （同时写 `page-break-inside: avoid` 兼容旧引擎）。
* 底色一律浅色，且**关键信息都有文字或线型**：判定列有色块**也有文字**，
  区间带用实线/虚线/点线区分 `range`/`screen`/`goal`，黑白打印机上一样能读。
* `print-color-adjust: exact` 保证浅底色真的被打印出来（否则 Chrome 会丢背景色）。
* **做不到的事如实写出来**：`@page` 里的页码（`@bottom-center`）只有
  Prince / WeasyPrint 这类分页引擎会渲染，Chrome / Edge 的打印对话框会忽略它，
  由浏览器自己的「页眉和页脚」选项决定。这里不假装有页码，见 `_CSS` 里的注释。

对外接口
--------
    build_html(dataset, analysis, findings, *, lang="zh", ai_text="", ai_model="",
               ai_note="", title="健康数据报告", include_appendix=True,
               generated_at=None, mask_sources=True) -> str

`include_appendix` 控制的是**附录 B：全部指标明细**（与 Markdown 版一致）；
附录 A（方法与口径）与免责声明永远输出。
"""

from __future__ import annotations

import calendar
import math
import re
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import catalog
from .model import Dataset, Observation
from .privacy import mask_source_name
from .report import (
    DISCLAIMER,
    HIDDEN_CATEGORIES,
    _BODY_METRICS,
    _KIND_CN,
    _SEV_RANK,
    _by_category,
    _category_of,
    _cn_of,
    _data_span,
    _files_text,
    _findings_of,
    _fmt,
    _g,
    _hm,
    _insufficient,
    _is_benign_side,
    _is_none_ref,
    _jint,
    _jnum,
    _method_section,
    _now,
    _ordered_metrics,
    _out_of_ref,
    _pct_str,
    _precision_of,
    _reason_lines,
    _ref_kind,
    _ref_label,
    _ref_source,
    _sev,
    _span_days,
    _spec_of,
    _t,
    _trend_cells,
    _trends_of,
    _unit_of,
    _verdict_text,
    minutes_to_clock,
)

__all__ = ["build_html", "MonthStat", "monthly_stats", "monthly_stats_of"]


# ──────────────────────────────────────────────────────────── 常量

#: 手写 SVG 的字号（用户单位）。1 单位 ≈ 0.17 mm（宽度 1000 单位铺满正文宽度），
#: 所以 20 单位 ≈ 3.4 mm ≈ 9.6 pt，和正文一样是「可预期的打印尺寸」。
_FS_TICK = 20.0
_FS_LABEL = 22.0
_FS_SMALL = 18.0
_FS_TITLE = 24.0

#: 散点图最多画多少个点（每夜一个点）。真实数据 6 年 ≈ 2200 夜，
#: 全部画出来在 A4 上会糊成一团墨，所以按**等间隔抽样**降采样并在图上注明。
_SCATTER_MAX_POINTS = 1200

#: 步数条形图最多画多少个月（真实数据 6 年 = 72 个月，全画出来 x 轴会挤成一团）。
_BARS_MAX_MONTHS = 36

#: 覆盖率图最多列多少行指标。
_COVERAGE_MAX_ROWS = 30

#: 任何「日期 + 时分秒」都要降级成日期。这是一份**要发出去**的交付物：
#: 解析器写在 `notes` 里的原始记录时间跨度（实测真实导出里是
#: 「时间跨度 2019-07-28 09:49:19 → 2026-10-10 18:01:08」）来自逐条 Record 的
#: 时间戳，不该出现在分享版里。Markdown 版原样打印那句话，HTML 版**只保留日期**：
#: 跨度信息保住了，逐条记录的时间点没了。
#:
#: 只匹配「日期在前」的形状，所以图上的钟点标签（23:31）与附录里的「18:00 起算」
#: 都不受影响；生成时间那一行在替换之前用一个占位符挡着，不会被削掉时分。
_TS_SCRUB = re.compile(r"(\d{4}-\d{2}-\d{2})[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?")

#: 生成时间在最终输出里的占位符（扫描完时间戳后再换回真实值）。
_GEN_TOKEN = "\ue000HR-GENERATED\ue000"

_CSS = """
/* ══ 页面与打印 ═══════════════════════════════════════════════════════
   正文全部用 pt，不用 px：pt 是物理单位，打印出来的字号可预期。 */
@page { size: A4; margin: 16mm 14mm; }

/* 页码：CSS Paged Media 的「页边距盒」只有 Prince / WeasyPrint / Paged.js
   这类分页引擎会渲染。Chrome / Edge 的打印对话框会忽略这一段，由它自己的
   「页眉和页脚」选项加页码。所以这里不假装有页码：支持它的引擎会得到页码，
   不支持的浏览器请在打印时勾选「页眉和页脚」。 */
@page { @bottom-center { content: "第 " counter(page) " / " counter(pages) " 页";
                         font-size: 9pt; color: #666; } }

html { font-size: 10.5pt; }
body { margin: 0 auto; max-width: 200mm; padding: 8mm 6mm; background: #fff; color: #1a1a1a;
  font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
               "Noto Sans CJK SC", "Source Han Sans SC", system-ui, -apple-system, Arial, sans-serif;
  line-height: 1.65; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
svg { font-family: inherit; }

@media print {
  body { max-width: none; padding: 0; }
  .screen-only { display: none !important; }
}

/* ══ 分页 ═══════════════════════════════════════════════════════════
   主要小节之间强制分页。break-* 是新属性，page-break-* 兼容旧引擎，
   两个都写（旧引擎只认后者）。 */
section.sec { break-before: page; page-break-before: always; }
section.cover { break-after: page; page-break-after: always; }
h1, h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
tr, figure.chart, .finding, .callout, .assoc, table { break-inside: avoid; page-break-inside: avoid; }
figure.chart { break-inside: avoid; page-break-inside: avoid; }

/* ══ 标题层级 ═══════════════════════════════════════════════════════ */
h1 { font-size: 24pt; line-height: 1.25; margin: 0 0 4mm 0; letter-spacing: .5pt; }
h2 { font-size: 15pt; margin: 0 0 3mm 0; padding: 0 0 1.5mm 0;
     border-bottom: 1.2pt solid #1f4e79; color: #14395c; }
h3 { font-size: 12pt; margin: 5mm 0 1.5mm 0; color: #14395c; }
h4 { font-size: 11pt; margin: 3mm 0 1mm 0; color: #333; }
p { margin: 0 0 2mm 0; }
ul, ol { margin: 0 0 2.5mm 0; padding-left: 6mm; }
li { margin: 0 0 0.8mm 0; }
code { font-family: Consolas, "Courier New", monospace; font-size: .92em;
       background: #f2f4f7; padding: 0 .6mm; border-radius: .6mm; }
strong { font-weight: 600; }
.small { font-size: 9pt; color: #555; }
.lede { color: #333; }
.screen-only { background: #fff8e1; border: 1pt solid #e0b84c; border-left: 3pt solid #e0b84c;
  padding: 2mm 3mm; margin: 0 0 5mm 0; font-size: 9.5pt; color: #4a3a12; }

/* ══ 封面 ═══════════════════════════════════════════════════════════ */
section.cover { padding-top: 26mm; }
.cover .sub { color: #555; font-size: 11pt; margin-bottom: 8mm; }
.cover .generated { color: #555; font-size: 10pt; }
dl.facts { margin: 6mm 0; border-top: .8pt solid #c9d2dc; border-bottom: .8pt solid #c9d2dc;
           padding: 3mm 0; }
dl.facts dt { float: left; clear: left; width: 22mm; color: #555; font-size: 9.5pt; }
dl.facts dd { margin: 0 0 1.6mm 24mm; }

/* ══ 提示块 ═════════════════════════════════════════════════════════ */
.callout { border: 1pt solid #c9d2dc; border-left: 3pt solid #1f4e79; background: #f6f9fc;
           padding: 2.5mm 3mm; margin: 0 0 3mm 0; }
.callout.danger { border-left-color: #8a1f11; background: #fdf3f1; }
.callout.note { border-left-color: #4a6b3a; background: #f5f9f1; }
.finding { border: 1pt solid #d7dde5; border-left: 3pt solid #8a8f96; padding: 2.5mm 3mm;
           margin: 0 0 3mm 0; background: #fbfcfd; }
.finding.sev-alert { border-left-color: #8a1f11; background: #fdf3f1; }
.finding.sev-warn { border-left-color: #9a6410; background: #fdf8ef; }
.finding.sev-info { border-left-color: #4a6b8a; background: #f5f8fb; }
.finding h3 { margin: 0 0 1mm 0; font-size: 11pt; color: #1a1a1a; }
.finding .sevtag { font-size: 8.5pt; border: .8pt solid #666; border-radius: .8mm;
                   padding: 0 1mm; margin-right: 1.5mm; background: #fff; color: #333;
                   white-space: nowrap; }
.sevtag.alert { background: #8a1f11; color: #fff; border-color: #8a1f11; }
.sevtag.warn { background: #9a6410; color: #fff; border-color: #9a6410; }
.sevtag.info { background: #4a6b8a; color: #fff; border-color: #4a6b8a; }
.nodata { color: #666; font-size: 9.5pt; font-style: normal;
          border-left: 2pt solid #c9d2dc; padding-left: 3mm; margin: 1.5mm 0 3mm 0; }

/* ══ 表格 ═══════════════════════════════════════════════════════════ */
table { border-collapse: collapse; width: 100%; margin: 0 0 3mm 0; font-size: 9.5pt; }
thead th { background: #eaeff5; border-bottom: 1pt solid #9fb0c2; text-align: left;
           padding: 1.2mm 1.5mm; font-weight: 600; color: #14395c; }
tbody td { border-bottom: .6pt solid #dde3ea; padding: 1.2mm 1.5mm; vertical-align: top; }
tbody tr:nth-child(even) td { background: #fafbfc; }
td.num { white-space: nowrap; font-variant-numeric: tabular-nums; }
td.src { font-size: 8.8pt; color: #333; }
.chip { display: inline-block; border: .8pt solid #666; border-radius: .8mm;
        padding: 0 1mm; font-size: 8.8pt; white-space: nowrap; background: #fff; }
.chip.out { background: #8a1f11; border-color: #8a1f11; color: #fff; }
.chip.goalout { background: #fff; border-color: #9a6410; border-style: dashed; color: #6b4408; }
.chip.benign { background: #f0f0f0; border-color: #6b6b6b; color: #2b2b2b; }
.chip.ina { background: #dfe7f0; border-color: #5b7793; color: #14395c; }
.chip.mixed { background: #fdf8ef; border-color: #9a6410; color: #6b4408; }
.chip.na { background: #fff; border-color: #9aa3ad; color: #555; }
.warnmark { color: #9a6410; font-weight: 700; }

/* ══ 图表卡片 ═══════════════════════════════════════════════════════ */
figure.chart { margin: 0 0 3.5mm 0; border: .8pt solid #dde3ea; border-radius: 1mm;
               padding: 2mm 2.5mm 1.5mm 2.5mm; background: #fff; }
figure.chart figcaption { font-size: 9pt; color: #333; margin-bottom: 1mm; }
figure.chart .ct { font-weight: 600; color: #14395c; }
figure.chart .cd { display: block; color: #5a5a5a; font-size: 8.5pt; }
figure.chart svg { width: 100%; height: auto; display: block; }

/* SVG 内部样式（图表用类名，避免每个元素写 inline style，SVG 因此很小） */
svg .fr { fill: #ffffff; stroke: #c9d2dc; stroke-width: 1.2; }
svg .grid { stroke: #cdd6e0; stroke-width: 1; stroke-dasharray: 6 6; }
svg .axline { stroke: #8b98a6; stroke-width: 1.4; }
svg .t { fill: #333333; }
svg .t.dim { fill: #6a7683; }
svg .bandrange { fill: #eef3f8; stroke: #5b7793; stroke-width: 1.6; }
svg .bandscreen { fill: #f4f6f4; stroke: #4a6b3a; stroke-width: 1.6; stroke-dasharray: 12 7; }
svg .bandline { stroke-width: 1.6; }
svg .goalline { stroke: #3a3a3a; stroke-width: 2; stroke-dasharray: 3 7; }
svg .iqr { fill: #dbe4ee; stroke: none; }
svg .series { fill: none; stroke: #1f4e79; stroke-width: 2.4; stroke-linejoin: round; }
svg .dot { fill: #1f4e79; }
svg .dot.single { fill: #ffffff; stroke: #1f4e79; stroke-width: 2; }
svg .hand { stroke: #8a5a00; stroke-width: 1.8; stroke-dasharray: 8 6; }
svg .handlbl { fill: #6b4408; font-weight: 600; }
svg .bar { fill: #2f5d8a; stroke: #1b3a5c; }
svg .bar2 { fill: #7b9bbd; stroke: #33566f; }
svg .barhatch { fill: #ffffff; stroke: #333333; stroke-width: 1.6; }
svg .hatchline { stroke: #555555; stroke-width: 1.2; }
svg .hbar { fill: #3f6f8f; stroke: #23485f; stroke-width: 1.2; }
svg .hbar.low { fill: #ffffff; stroke: #23485f; stroke-width: 1.4; }
svg .noiseband { fill: #eceff2; stroke: #9aa3ad; stroke-width: 1.2; stroke-dasharray: 6 6; }
svg .vstrip { stroke: #ffffff; stroke-width: 1.4; }
svg .vout { fill: #8a1f11; }
svg .vgoalout { fill: #ffffff; stroke: #9a6410; stroke-width: 1.6; stroke-dasharray: 4 3; }
svg .vbenign { fill: #f0f0f0; stroke: #6b6b6b; stroke-width: 1.4; }
svg .vina { fill: #dfe7f0; stroke: #5b7793; stroke-width: 1.4; }
svg .vmixed { fill: #fdf8ef; stroke: #9a6410; stroke-width: 1.4; }
svg .seg { stroke: #ffffff; stroke-width: 1.4; }
svg .segA { fill: #1f4e79; }
svg .segB { fill: #5b8bb0; }
svg .segC { fill: #a8c2d6; }
svg .segD { fill: #e2e8ee; }
svg .inbed { fill: #ffffff; stroke: #6a7683; stroke-width: 1.6; stroke-dasharray: 7 5; }
svg .medcross { stroke: #333333; stroke-width: 1.6; stroke-dasharray: 10 6; }
svg .sdbox { fill: none; stroke: #8a5a00; stroke-width: 1.6; stroke-dasharray: 8 6; }
svg .pt { fill: #1f4e79; fill-opacity: .55; stroke: none; }
svg .dotref { fill: #8a1f11; }
"""


# ──────────────────────────────────────────────────────────── 文本与数值

_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
_ESC_RE = re.compile("|".join(re.escape(k) for k in _ESC))


def _e(value: Any) -> str:
    """HTML/XML 文本与属性安全化。所有进入输出的字符串都必须过这里。"""
    if value is None:
        return ""
    return _ESC_RE.sub(lambda m: _ESC[m.group(0)], str(value))


def _n(v: Any, nd: int = 2) -> str:
    """SVG 坐标/数值格式化：固定小数位、去尾零、非有限值一律 0（绝不输出 NaN）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(x):
        return "0"
    s = "{0:.{1}f}".format(x, max(0, min(6, int(nd))))
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _trunc(text: Any, limit: int = 14) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _thousands(n: Any) -> str:
    try:
        return "{0:,}".format(int(n))
    except (TypeError, ValueError):
        return "0"


# ──────────────────────────────────────────────────────────── SVG 原语


def _svg(w: float, h: float, inner: str, *, title: str, extra: str = "") -> str:
    """包一个合法的 SVG 文档片段。

    刻意**不写 xmlns**：那会在输出里引入 `http://www.w3.org/2000/svg` 字面量，
    而这份交付物的硬约束之一是「输出里不出现 http:// / https://」。
    HTML5 的内联 SVG 本来就在 SVG 命名空间里，`ElementTree.fromstring` 也能解析。
    """
    return (
        '<svg viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" '
        'role="img"{extra}><title>{title}</title>{inner}</svg>'
    ).format(w=_n(w, 1), h=_n(h, 1), title=_e(title), inner=inner,
             extra=(" " + extra) if extra else "")


def _line(x1: float, y1: float, x2: float, y2: float, cls: str = "grid") -> str:
    return '<line class="{c}" x1="{a}" y1="{b}" x2="{d}" y2="{e}"/>'.format(
        c=cls, a=_n(x1, 1), b=_n(y1, 1), d=_n(x2, 1), e=_n(y2, 1))


def _rect(x: float, y: float, w: float, h: float, cls: str = "fr") -> str:
    return '<rect class="{c}" x="{x}" y="{y}" width="{w}" height="{h}"/>'.format(
        c=cls, x=_n(x, 1), y=_n(y, 1), w=_n(max(0.0, w), 1), h=_n(max(0.0, h), 1))


def _circle(cx: float, cy: float, r: float, cls: str = "dot") -> str:
    return '<circle class="{c}" cx="{x}" cy="{y}" r="{r}"/>'.format(
        c=cls, x=_n(cx, 1), y=_n(cy, 1), r=_n(r, 2))


def _poly(points: Sequence[Tuple[float, float]], cls: str) -> str:
    pts = " ".join("{0},{1}".format(_n(x, 1), _n(y, 1)) for x, y in points)
    return '<polygon class="{0}" points="{1}"/>'.format(cls, pts)


def _polyline(points: Sequence[Tuple[float, float]], cls: str) -> str:
    pts = " ".join("{0},{1}".format(_n(x, 1), _n(y, 1)) for x, y in points)
    return '<polyline class="{0}" points="{1}"/>'.format(cls, pts)


def _txt(x: float, y: float, text: Any, cls: str = "t", anchor: str = "start",
         size: float = _FS_TICK, weight: str = "") -> str:
    parts = ['class="{0}"'.format(cls) if cls else "",
             'x="{0}"'.format(_n(x, 1)),
             'y="{0}"'.format(_n(y, 1)),
             'font-size="{0}"'.format(_n(size, 1))]
    if anchor != "start":
        parts.append('text-anchor="{0}"'.format(anchor))
    if weight:
        parts.append('font-weight="{0}"'.format(weight))
    return "<text {0}>{1}</text>".format(" ".join(p for p in parts if p), _e(text))


def _hatch(x: float, y: float, w: float, h: float, step: float = 11.0) -> str:
    """用线段画 45° 斜纹（「/」方向），不依赖 `<pattern>`／`<clipPath>`。

    为什么不直接用 `<pattern>`：每个图表是独立的 `<svg>`，重复的 `id` 在同一份
    文档里是非法的，而给每张图生成唯一 id 会让输出依赖调用顺序（不必要的不确定性）。
    这里解析式地算出每一条斜线与矩形边框的交点，输出确定、合法、还能被黑白打印机
    清楚地区分出来（斜纹＝「视为基本没有差别」）。
    """
    if w <= 0 or h <= 0:
        return ""
    out: List[str] = []
    x0, y0, x1, y1 = x, y, x + w, y + h
    c = x0 + y0
    limit = x1 + y1
    guard = 0
    while c <= limit and guard < 400:
        guard += 1
        sx = max(x0, c - y1)
        ex = min(x1, c - y0)
        if ex > sx:
            out.append(_line(sx, c - sx, ex, c - ex, "hatchline"))
        c += step
    return "".join(out)


# ──────────────────────────────────────────────────────────── 月度聚合


@dataclass(frozen=True)
class MonthStat:
    """一个月的聚合量。

    * `median` —— 该月「日值」的中位数。趋势折线画的就是它。
    * `q1` / `q3` —— 日值的四分位数（不足 4 天时退化成 min/max），画成四分位带。
    * `mean` —— 该月**有数据日**的日均值（步数条形图用它，口径与 analysis 的
      `mean` 一致：缺数据的日子不参与，而不是按 0 算）。
    * `days` —— 该月有数据的天数；`n` —— 该月原始观测条数。
    """

    month: str
    median: float
    q1: Optional[float]
    q3: Optional[float]
    mean: float
    days: int
    n: int


def _obs_agg_with(observations: Iterable[Observation], agg: str) -> List[Tuple[str, float]]:
    """按 agg 口径算「日值」：mean 取当日平均，其余取当日最后一条。

    与 `analysis._daily_values` 同一口径，这样图上看到的数和表格里的判定
    说的是同一件事（「算对了但两处不一致」是这类报告最贵的错误）。
    """
    acc: Dict[str, List[float]] = {}
    order: List[str] = []
    for o in observations:
        d = str(getattr(o, "date", "") or "")
        if not d:
            continue
        if d not in acc:
            acc[d] = []
            order.append(d)
        try:
            acc[d].append(float(o.value))
        except (TypeError, ValueError):
            continue
    want_mean = str(agg or "").strip().lower() == "mean"
    out: List[Tuple[str, float]] = []
    for d in order:
        vals = acc[d]
        if not vals:
            continue
        out.append((d, (sum(vals) / len(vals)) if want_mean else vals[-1]))
    return out


def monthly_stats(observations: Sequence[Observation], agg: str = "snapshot") -> List[MonthStat]:
    """把一串观测聚成**月度**统计量（趋势图的唯一数据来源）。

    为什么按月：真实数据是 6 年 2600+ 天。把 2600 个点画在 A4 上是噪声，
    读者看不出趋势；按月取中位数（而不是均值）能同时压住离群值和日间波动。
    """
    daily = _obs_agg_with(observations, agg)
    buckets: Dict[str, List[float]] = {}
    order: List[str] = []
    for d, v in daily:
        m = d[:7]
        if len(m) != 7:
            continue
        if m not in buckets:
            buckets[m] = []
            order.append(m)
        buckets[m].append(v)
    raw_by_month: Dict[str, int] = {}
    for o in observations:
        m = str(getattr(o, "date", "") or "")[:7]
        if len(m) == 7:
            raw_by_month[m] = raw_by_month.get(m, 0) + 1

    out: List[MonthStat] = []
    for m in sorted(order):
        vals = sorted(buckets[m])
        if len(vals) >= 4:
            try:
                qs = statistics.quantiles(vals, n=4, method="inclusive")
                q1, q3 = float(qs[0]), float(qs[2])
            except (statistics.StatisticsError, ValueError):
                q1, q3 = vals[0], vals[-1]
        else:
            q1, q3 = vals[0], vals[-1]
        out.append(MonthStat(
            month=m,
            median=float(statistics.median(vals)),
            q1=q1, q3=q3,
            mean=sum(vals) / len(vals),
            days=len(vals),
            n=raw_by_month.get(m, len(vals)),
        ))
    return out


def monthly_stats_of(dataset: Dataset, metric: str, agg: str = "snapshot") -> List[MonthStat]:
    """从 `Dataset` 取某指标的月度统计量（`monthly_stats` 的便捷入口）。"""
    ds = dataset if dataset is not None else Dataset()
    try:
        series = ds.series(metric)
    except Exception:  # pragma: no cover - 容错
        series = [o for o in (getattr(ds, "observations", None) or [])
                  if getattr(o, "metric", "") == metric]
    return monthly_stats(series, agg)


def _month_index(month: str) -> int:
    try:
        y, m = str(month).split("-")[:2]
        return int(y) * 12 + (int(m) - 1)
    except (ValueError, AttributeError):
        return 0


def _month_name(index: int) -> str:
    return "{0:04d}-{1:02d}".format(index // 12, index % 12 + 1)


def _month_range(first: str, last: str) -> List[str]:
    a, b = _month_index(first), _month_index(last)
    if b < a:
        a, b = b, a
    if b - a > 600:  # 防御：脏数据不该让分类轴撑到几千个月
        b = a + 600
    return [_month_name(a + i) for i in range(b - a + 1)]


def _nice_ticks(lo: float, hi: float, count: int = 4) -> List[float]:
    """坐标轴刻度：1/2/2.5/5/10 的「好看」步长，确定且可复现。"""
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, count)
    if raw <= 0:
        return [lo]
    mag = 10.0 ** math.floor(math.log10(raw))
    step = mag * 10.0
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if mag * mult >= raw:
            step = mag * mult
            break
    start = math.ceil(lo / step) * step
    out: List[float] = []
    v = start
    guard = 0
    while v <= hi + step * 1e-9 and guard < 24:
        guard += 1
        out.append(round(v, 10))
        v += step
    return out or [lo]


def _clock_ticks(lo: float, hi: float) -> List[float]:
    """睡眠散点图的刻度：取 15/30/60/120/180 分钟里最合适的步长，标签还原成钟点。"""
    span = max(1.0, hi - lo)
    for step in (15.0, 30.0, 60.0, 120.0, 180.0, 240.0):
        if span / step <= 6.0:
            break
    start = math.ceil(lo / step) * step
    out: List[float] = []
    v = start
    guard = 0
    while v <= hi + 1e-9 and guard < 24:
        guard += 1
        out.append(float(v))
        v += step
    return out


# ──────────────────────────────────────────────────────────── 图表


def _ref_bound(ref: Any, name: str) -> Optional[float]:
    """取参考区间的上下界，兼容 RefRange 与 dict。"""
    if ref is None:
        return None
    if isinstance(ref, dict):
        return _jnum(ref.get(name))
    return _jnum(getattr(ref, name, None))


def _chart_trend(trend: Any, months: Sequence[MonthStat], *, lang: str = "zh",
                 w: float = 1000.0, h: float = 210.0) -> str:
    """通用趋势折线图：月度中位数 + 四分位带 + 参考区间 + 设备接替标注。

    这张图承担了图表清单里的第 1、2 两项：

    * **四分位带**（浅色填充）压住日间波动，让「中位数在往哪走」成为主视觉；
    * **参考区间**按性质用不同**线型**，不靠颜色区分——黑白打印机上也分得清：
      `range` 实线框、`screen` 虚线框、`goal` 只在目标值画一条点线
      （目标线而不是区间，因为「没到 8000 步」不是「落在正常范围外」）；
    * **设备接替**画成竖直虚线，顶部标出切换日期与新设备名。
      Apple 会把换设备前后的数据连成一条线且不标注，这条竖线是本项目
      区别于其它工具的地方：它让「台阶是测量变了还是身体变了」有据可查。
    """
    months = [m for m in (months or []) if m is not None]
    if len(months) < 2:
        return ""
    metric = str(_g(trend, "metric", "") or "")
    spec = _spec_of(metric)
    prec = _precision_of(metric, spec)
    unit = _unit_of(metric, trend, spec)
    ref = _g(trend, "ref", None)
    kind = _ref_kind(trend, ref)
    ref_lo = _ref_bound(ref, "low")
    ref_hi = _ref_bound(ref, "high")

    # ── y 值域：数据 + 参考区间一起算，保证区间带完整可见
    lows = [m.median for m in months] + [m.q1 for m in months if m.q1 is not None]
    highs = [m.median for m in months] + [m.q3 for m in months if m.q3 is not None]
    for v in (ref_lo, ref_hi):
        if v is not None:
            lows.append(v)
            highs.append(v)
    lo_v, hi_v = min(lows), max(highs)
    span = hi_v - lo_v
    pad = span * 0.12 if span > 0 else (abs(hi_v) * 0.06 or 1.0)
    lo_v -= pad
    hi_v += pad
    if hi_v <= lo_v:
        hi_v = lo_v + 1.0

    axis = _month_range(months[0].month, months[-1].month)
    pos = {m: i for i, m in enumerate(axis)}
    plotted = [m for m in months if m.month in pos]
    if len(plotted) < 2:
        return ""

    hands_all = [tuple(x) for x in (_g(trend, "handovers", None) or []) if x]
    hands = [x for x in hands_all if len(x) >= 3 and str(x[2])[:7] in pos]
    labels_shown = hands[:3]

    top = 30.0 + 17.0 * len(labels_shown)
    left, right, bottom = 78.0, 20.0, 42.0
    px0, px1 = left, w - right
    py0, py1 = top, h - bottom
    n_axis = len(axis)

    def X(i: int) -> float:
        return px0 + (px1 - px0) * (i / (n_axis - 1) if n_axis > 1 else 0.5)

    def Y(v: float) -> float:
        return py1 - (py1 - py0) * ((v - lo_v) / (hi_v - lo_v))

    body: List[str] = [_rect(px0, py0, px1 - px0, py1 - py0, "fr")]

    # ── y 刻度与网格
    for t in _nice_ticks(lo_v, hi_v, 4):
        if t < lo_v - 1e-9 or t > hi_v + 1e-9:
            continue
        y = Y(t)
        body.append(_line(px0, y, px1, y, "grid"))
        body.append(_txt(px0 - 6.0, y + 6.0, _fmt(t, prec, ""), "t dim", "end", _FS_TICK))
    if unit:
        body.append(_txt(px0 - 6.0, py0 - 8.0,
                         _t("单位：{0}", "unit: {0}", lang).format(unit),
                         "t dim", "end", _FS_SMALL))

    # ── 参考区间：实线框 / 虚线框 / 目标点线
    if kind in ("range", "screen") and (ref_lo is not None or ref_hi is not None):
        cls = "bandrange" if kind == "range" else "bandscreen"
        if ref_lo is not None and ref_hi is not None:
            y_hi, y_lo = Y(min(ref_hi, hi_v)), Y(max(ref_lo, lo_v))
            if y_lo > y_hi:
                y_hi, y_lo = y_lo, y_hi
            body.append(_rect(px0, y_hi, px1 - px0, y_lo - y_hi, cls))
            body.append(_txt(px1 - 4.0, y_hi + 18.0,
                             _ref_label(ref), "t dim", "end", _FS_SMALL))
        else:
            # 单侧开放的区间（例如「<10 级」）不画带：画一条边界线，语义才不错。
            edge = ref_hi if ref_hi is not None else ref_lo
            side = "high" if ref_hi is not None else "low"
            if edge is not None:
                y = Y(min(max(edge, lo_v), hi_v))
                body.append(_line(px0, y, px1, y, "bandline " + cls))
                body.append(_txt(px1 - 4.0, y - 6.0,
                                 _t("{0}：{1}", "{0}: {1}", lang).format(
                                     _t("上限", "upper", lang) if side == "high"
                                     else _t("下限", "lower", lang),
                                     _fmt(edge, prec, unit)),
                                 "t dim", "end", _FS_SMALL))
    elif kind == "goal":
        target = ref_lo if ref_lo is not None else ref_hi
        if target is not None:
            y = Y(min(max(target, lo_v), hi_v))
            body.append(_line(px0, y, px1, y, "goalline"))
            body.append(_txt(px1 - 4.0, y - 6.0,
                             _t("目标 {0}{1}", "target {0}{1}", lang).format(
                                 "≥" if ref_lo is not None else "≤", _fmt(target, prec, unit)),
                             "t dim", "end", _FS_SMALL))

    # ── 四分位带 + 中位线
    up = [(X(pos[m.month]), Y(m.q3 if m.q3 is not None else m.median)) for m in plotted]
    dn = [(X(pos[m.month]), Y(m.q1 if m.q1 is not None else m.median)) for m in reversed(plotted)]
    if len(plotted) >= 2 and any(m.q1 is not None and m.q3 is not None
                                 and abs((m.q3 or 0) - (m.q1 or 0)) > 1e-12 for m in plotted):
        body.append(_poly(list(up) + list(dn), "iqr"))
    line_pts = [(X(pos[m.month]), Y(m.median)) for m in plotted]
    if len(line_pts) >= 2:
        body.append(_polyline(line_pts, "series"))
    for x, y in line_pts:
        body.append(_circle(x, y, 4.2 if len(line_pts) < 30 else 3.2, "dot"))

    # ── 设备接替：竖直虚线 + 顶部设备名
    for k, item in enumerate(hands):
        day = str(item[2])
        new = "" if item[0] is None else str(item[0])
        i = pos.get(day[:7])
        if i is None:
            continue
        x = X(i)
        body.append(_line(x, py0, x, py1, "hand"))
        if k < len(labels_shown):
            ly = 18.0 + 17.0 * k
            anchor = "middle"
            tx = x
            if tx > px1 - 130.0:
                anchor, tx = "end", px1
            elif tx < px0 + 130.0:
                anchor, tx = "start", px0
            body.append(_txt(tx, ly, "{0} → {1}".format(day, _trunc(new, 14)),
                             "handlbl", anchor, _FS_SMALL))

    # ── x 刻度
    step = max(1, int(math.ceil(n_axis / 9.0)))
    short = n_axis > 20
    ticks = list(range(0, n_axis, step))
    if ticks and ticks[-1] != n_axis - 1:
        ticks.append(n_axis - 1)
    for i in ticks:
        label = axis[i]
        if short:
            label = label[2:]
        body.append(_txt(X(i), py1 + 22.0, label, "t dim",
                         "middle" if 0 < i < n_axis - 1 else ("start" if i == 0 else "end"),
                         _FS_TICK))

    # ── 末值直标：纸上没有 hover，最后一个数必须写出来
    last_m = plotted[-1]
    lx, ly = X(pos[last_m.month]), Y(last_m.median)
    body.append(_txt(lx, ly - 10.0 if ly - 10.0 > py0 + 8.0 else ly + 22.0,
                     _fmt(last_m.median, prec, unit), "t", "end" if lx > px1 - 110.0 else "middle",
                     _FS_LABEL, "600"))

    extra = (' data-chart="trend" data-metric="{0}" data-points="{1}" data-days="{2}"'
             ' data-handovers="{3}"').format(
        _e(metric), len(plotted), sum(m.days for m in plotted), len(hands_all))
    title = _t("{0} 月度趋势", "{0} monthly trend", lang).format(_cn_of(metric, trend))
    return _svg(w, h, "".join(body), title=title, extra=extra)


def _sleep_stage_rows(trends: Dict[str, Any], lang: str
                      ) -> Tuple[List[Tuple[str, float, str]], Optional[float]]:
    """睡眠分期构成的输入：四个分期 + 卧床时间。

    返回 `(分期列表, 卧床小时数)`。取值用**各期的中位数**（不是某一夜），
    单夜分期判定误差大，用中位数才撑得住一个「构成」的说法。
    """
    specs = (
        ("sleep_deep", "segA", _t("深睡", "Deep", lang)),
        ("sleep_core", "segB", _t("核心", "Core", lang)),
        ("sleep_rem", "segC", _t("REM", "REM", lang)),
        ("sleep_awake", "segD", _t("清醒", "Awake", lang)),
    )
    rows: List[Tuple[str, float, str]] = []
    for metric, cls, label in specs:
        trend = trends.get(metric)
        if trend is None:
            continue
        v = _jnum(_g(trend, "median", None))
        if v is None:
            v = _jnum(_g(trend, "mean", None))
        if v is None or v <= 0:
            continue
        rows.append((label, float(v), cls))
    inbed_trend = trends.get("sleep_inbed")
    inbed: Optional[float] = None
    if inbed_trend is not None:
        inbed = _jnum(_g(inbed_trend, "median", None))
        if inbed is None:
            inbed = _jnum(_g(inbed_trend, "mean", None))
    return rows, inbed


def _chart_sleep_stages(rows: Sequence[Tuple[str, float, str]], inbed: Optional[float],
                        *, lang: str = "zh", w: float = 1000.0) -> str:
    """睡眠分期构成：水平堆叠条。

    ⚠️ **卧床时间不能和分期叠在同一条上**：卧床时间是各分期的外框
    （卧床 ⊇ 睡眠 + 清醒），叠起来会把总时长算多。所以画成两行、共用同一时间刻度：
    第一行是卧床时间，第二行是各分期堆叠；两行长度之差就是入睡耗时与清醒后赖床的时间。
    每个色块都**同时标出小时数与占比**——颜色只是辅助，纸面上靠文字读。
    """
    rows = [r for r in (rows or []) if r[1] and r[1] > 0]
    if not rows and not inbed:
        return ""
    total_stages = sum(r[1] for r in rows)
    total = max(total_stages, float(inbed or 0.0))
    if total <= 0:
        return ""
    base = float(inbed) if inbed else total_stages

    left, right, top = 132.0, 150.0, 34.0
    bar_h, gap = 44.0, 34.0
    # 没有卧床时间数据时不占那一行，避免图上方留一块空白。
    y0 = top if inbed else None
    y1 = (top + bar_h + gap) if inbed else top
    legend_y = y1 + bar_h + 40.0
    ncol = 2
    legend_rows = int(math.ceil(len(rows) / float(ncol))) if rows else 0
    h = legend_y + 26.0 * max(1, legend_rows) + 16.0
    px0, px1 = left, w - right
    plot_w = px1 - px0

    def XL(v: float) -> float:
        return px0 + plot_w * (v / total)

    body: List[str] = []
    if inbed and y0 is not None:
        body.append(_txt(px0 - 8.0, y0 + bar_h * 0.68,
                         _t("卧床时间", "In bed", lang), "t", "end", _FS_LABEL))
        body.append(_rect(px0, y0, XL(inbed) - px0, bar_h, "inbed"))
        body.append(_txt(XL(inbed) + 8.0, y0 + bar_h * 0.68,
                         _t("{0} 小时", "{0} h", lang).format(_fmt(inbed, 2, "")),
                         "t dim", "start", _FS_SMALL))
    body.append(_txt(px0 - 8.0, y1 + bar_h * 0.68,
                     _t("睡眠分期", "Sleep stages", lang), "t", "end", _FS_LABEL))
    cursor = 0.0
    for label, hours, cls in rows:
        x = XL(cursor)
        width = XL(cursor + hours) - x
        body.append(_rect(x, y1, width, bar_h, "seg " + cls))
        pct = (hours / base * 100.0) if base else 0.0
        txt = "{0} {1}h {2}%".format(label, _fmt(hours, 2, ""), _fmt(pct, 0, ""))
        if width > 150.0:
            body.append(_txt(x + width / 2.0, y1 + bar_h * 0.68, txt, "t", "middle", _FS_SMALL))
        else:
            body.append(_txt(x + width / 2.0, y1 - 8.0, txt, "t", "middle", _FS_SMALL))
        cursor += hours

    # 时间刻度（小时）
    tick_h = 1.0 if total <= 10 else (2.0 if total <= 20 else 4.0)
    v = 0.0
    while v <= total + 1e-9:
        x = XL(v)
        body.append(_line(x, y1 + bar_h, x, y1 + bar_h + 6.0, "axline"))
        body.append(_txt(x, y1 + bar_h + 26.0, "{0}h".format(_fmt(v, 0, "")), "t dim",
                         "middle", _FS_SMALL))
        v += tick_h

    # 图例：色块 + 文字（色块只是辅助，文字才是信息）
    for i, (label, hours, cls) in enumerate(rows):
        col = i % ncol
        rowi = i // ncol
        lx = px0 + col * (plot_w / ncol)
        ly = legend_y + 26.0 * rowi
        body.append(_rect(lx, ly - 13.0, 16.0, 16.0, "seg " + cls))
        pct = (hours / base * 100.0) if base else 0.0
        body.append(_txt(lx + 26.0, ly,
                         "{0}：{1} 小时（{2}%）".format(label, _fmt(hours, 2, ""), _fmt(pct, 1, "")),
                         "t", "start", _FS_SMALL))

    title = _t("睡眠分期构成", "Sleep stage composition", lang)
    extra = ' data-chart="sleep-stages" data-points="{0}"'.format(len(rows))
    return _svg(w, h, "".join(body), title=title, extra=extra)


def _sleep_scatter_points(onset: Dict[str, float], wake: Dict[str, float],
                          limit: int = _SCATTER_MAX_POINTS
                          ) -> Tuple[List[Tuple[float, float]], int]:
    """每夜一个点（x=入睡、y=醒来，都是「前一天 18:00 起算的分钟数」）。

    超出 `limit` 时**等间隔抽样**：6 年 ≈ 2200 夜，全画出来在 A4 上会糊成一团墨，
    反而看不出离散程度。抽样是确定的（按排序后的下标取整），并在图上注明。
    """
    dates = sorted(set(onset) & set(wake))
    total = len(dates)
    pts = [(float(onset[d]), float(wake[d])) for d in dates]
    if limit > 0 and total > limit:
        step = total / float(limit)
        pts = [pts[min(total - 1, int(i * step))] for i in range(limit)]
    return pts, total


def _chart_sleep_scatter(points: Sequence[Tuple[float, float]], sr: Any, *,
                         lang: str = "zh", w: float = 1000.0, h: float = 560.0) -> str:
    """睡眠规律性散点图：x=入睡时刻，y=醒来时刻，中位十字 + ±1 标准差框。

    轴标签还原成**钟点**（内部坐标是「前一天 18:00 起算的分钟数」，
    因为时间在午夜处不连续，按钟点算均值和标准差都会错，见 catalog 的说明）。
    """
    pts = [p for p in (points or []) if p and p[0] is not None and p[1] is not None]
    if len(pts) < 3:
        return ""
    on_med = _jnum(_g(sr, "onset_median_min", None))
    wa_med = _jnum(_g(sr, "wake_median_min", None))
    on_sd = _jnum(_g(sr, "onset_std_min", None))
    wa_sd = _jnum(_g(sr, "wake_std_min", None))
    xs = [p[0] for p in pts] + ([on_med] if on_med is not None else [])
    ys = [p[1] for p in pts] + ([wa_med] if wa_med is not None else [])
    lo_x, hi_x = min(xs), max(xs)
    lo_y, hi_y = min(ys), max(ys)
    if on_sd and on_med is not None:
        lo_x, hi_x = min(lo_x, on_med - on_sd), max(hi_x, on_med + on_sd)
    if wa_sd and wa_med is not None:
        lo_y, hi_y = min(lo_y, wa_med - wa_sd), max(hi_y, wa_med + wa_sd)
    pad_x = max(20.0, (hi_x - lo_x) * 0.06)
    pad_y = max(20.0, (hi_y - lo_y) * 0.06)
    lo_x, hi_x = lo_x - pad_x, hi_x + pad_x
    lo_y, hi_y = lo_y - pad_y, hi_y + pad_y
    if hi_x <= lo_x or hi_y <= lo_y:
        return ""

    margin_l = 148.0 if w > 700 else 100.0
    left, right, top, bottom = margin_l, 30.0, 34.0, 96.0
    px0, px1 = left, w - right
    py0, py1 = top, h - bottom

    def X(v: float) -> float:
        return px0 + (px1 - px0) * ((v - lo_x) / (hi_x - lo_x))

    def Y(v: float) -> float:
        return py1 - (py1 - py0) * ((v - lo_y) / (hi_y - lo_y))

    body: List[str] = [_rect(px0, py0, px1 - px0, py1 - py0, "fr")]
    for t in _clock_ticks(lo_x, hi_x):
        x = X(t)
        body.append(_line(x, py0, x, py1, "grid"))
        body.append(_txt(x, py1 + 24.0, minutes_to_clock(t), "t dim", "middle", _FS_SMALL))
    for t in _clock_ticks(lo_y, hi_y):
        y = Y(t)
        body.append(_line(px0, y, px1, y, "grid"))
        body.append(_txt(px0 - 8.0, y + 6.0, minutes_to_clock(t), "t dim", "end", _FS_SMALL))
    body.append(_txt(px0 - 8.0, py0 - 10.0, _t("入睡时刻", "sleep onset", lang),
                     "t dim", "end", _FS_SMALL))
    body.append(_txt(px0, py1 + 52.0, _t("入睡时刻（钟点）", "sleep onset (clock)", lang),
                     "t dim", "start", _FS_SMALL))

    # 中位十字 + ±1 标准差框。
    # 注意：这里的标准差是**单轴**的（入睡的标准差、醒来的标准差），
    # 框不是联合分布的分位区，只作为「离散程度有多大」的视觉标尺，图上写清楚。
    if on_med is not None and wa_med is not None and on_sd is not None and wa_sd is not None:
        x0, x1 = X(on_med - on_sd), X(on_med + on_sd)
        y0, y1 = Y(wa_med + wa_sd), Y(wa_med - wa_sd)
        body.append(_rect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0), "sdbox"))
    for cx, cy in ((p[0], p[1]) for p in pts):
        body.append(_circle(X(cx), Y(cy), 3.6, "pt"))
    if on_med is not None:
        body.append(_line(X(on_med), py0, X(on_med), py1, "medcross"))
        body.append(_txt(X(on_med) + 6.0, py0 + 20.0,
                         _t("中位入睡 {0}", "median onset {0}", lang).format(
                             minutes_to_clock(on_med)), "t", "start", _FS_SMALL, "600"))
    if wa_med is not None:
        body.append(_line(px0, Y(wa_med), px1, Y(wa_med), "medcross"))
        body.append(_txt(px1 - 6.0, Y(wa_med) - 8.0,
                         _t("中位醒来 {0}", "median wake {0}", lang).format(
                             minutes_to_clock(wa_med)), "t", "end", _FS_SMALL, "600"))

    title = _t("睡眠规律性：每夜入睡与醒来时刻", "Sleep regularity: nightly onset and wake", lang)
    extra = (' data-chart="sleep-scatter" data-points="{0}" data-nights="{1}"'
             ' data-downsampled="{2}"').format(
        len(pts), _jint(_g(sr, "n", 0)), "1" if len(pts) < _jint(_g(sr, "n", 0)) else "0")
    return _svg(w, h, "".join(body), title=title, extra=extra)


def _chart_assoc_bars(a: Any, *, lang: str = "zh", w: float = 1000.0, h: float = 300.0) -> str:
    """关联对比柱状图：每组一根柱，柱上标数值、柱下标 n。

    柱**从 0 起算**（不是从最小值起）：截断纵轴会把 1% 的差别画成 3 倍，
    那是这类图最常见的骗人手法。差异小于测量噪声时，第二根柱改成**斜纹**，
    并在图上写出「视为基本没有差别」——斜纹在黑白打印机上依然分得清。
    """
    groups = [g for g in (_g(a, "groups", None) or []) if isinstance(g, dict)]
    vals: List[Tuple[str, float, int]] = []
    for g in groups:
        y = _jnum(g.get("y_median"))
        if y is None:
            continue
        vals.append((str(g.get("label", "")), float(y), _jint(g.get("n", 0))))
    if len(vals) < 2:
        return ""
    y_max = max(v[1] for v in vals)
    y_min = min(0.0, min(v[1] for v in vals))
    if y_max <= y_min:
        return ""
    top_v = y_max + (y_max - y_min) * 0.30
    noise = _jnum(_g(a, "noise", 0.0)) or 0.0
    delta = _jnum(_g(a, "delta", None))
    within = bool(noise > 0 and delta is not None and abs(delta) < noise)

    left, right, top, bottom = 82.0, 26.0, 58.0, 84.0
    px0, px1 = left, w - right
    py0, py1 = top, h - bottom
    n = len(vals)
    slot = (px1 - px0) / float(n)
    bar_w = min(slot * 0.44, 150.0)

    def Y(v: float) -> float:
        return py1 - (py1 - py0) * ((v - y_min) / (top_v - y_min))

    body: List[str] = [_rect(px0, py0, px1 - px0, py1 - py0, "fr")]
    for t in _nice_ticks(y_min, top_v, 4):
        if t < y_min - 1e-9 or t > top_v + 1e-9:
            continue
        y = Y(t)
        body.append(_line(px0, y, px1, y, "grid"))
        body.append(_txt(px0 - 8.0, y + 6.0, _fmt(t, 2, ""), "t dim", "end", _FS_SMALL))

    # 噪声带：低组中位 ± 该指标的测量噪声。它把「证据等级看的是差异÷噪声」画出来。
    band_label = ""
    if noise > 0:
        base = vals[0][1]
        y_hi, y_lo = Y(min(base + noise, top_v)), Y(max(base - noise, y_min))
        body.append(_rect(px0, y_hi, px1 - px0, y_lo - y_hi, "noiseband"))
        band_label = _t("灰带 = 参考组中位 ± 测量噪声 {0}",
                        "grey band = reference median ± measurement noise {0}", lang).format(
            _fmt(noise, 2, ""))

    for i, (label, value, gn) in enumerate(vals):
        cx = px0 + slot * (i + 0.5)
        x = cx - bar_w / 2.0
        y = Y(value)
        cls = "bar" if i == 0 else "bar2"
        body.append(_rect(x, y, bar_w, py1 - y, cls))
        if within and i == n - 1:
            body.append(_hatch(x, y, bar_w, py1 - y, 10.0))
        body.append(_txt(cx, y - 12.0, _fmt(value, 2, ""), "t", "middle", _FS_LABEL, "600"))
        body.append(_txt(cx, py1 + 26.0, _e(label), "t", "middle", _FS_LABEL))
        body.append(_txt(cx, py1 + 50.0, "n={0}".format(gn), "t dim", "middle", _FS_SMALL))
    body.append(_line(px0, py1, px1, py1, "axline"))

    verdict = ""
    if noise > 0 and delta is not None:
        if within:
            verdict = _t("组间差异 {0} 小于测量噪声 {1}：视为基本没有差别（斜纹柱）",
                         "difference {0} is within measurement noise {1}: no material difference",
                         lang).format(_fmt(delta, 2, "", signed=True), _fmt(noise, 2, ""))
        else:
            verdict = _t("组间差异 {0}，是测量噪声（{1}）的 {2} 倍",
                         "difference {0} is {2}x the measurement noise ({1})", lang).format(
                _fmt(delta, 2, "", signed=True), _fmt(noise, 2, ""),
                _fmt(abs(delta) / noise if noise else 0.0, 1, ""))
    if verdict:
        body.append(_txt(px0, 22.0, verdict, "t", "start", _FS_LABEL, "600"))
    if band_label:
        body.append(_txt(px1, h - 20.0, band_label, "t dim", "end", _FS_SMALL))

    title = _t("分组中位数对比", "Group medians", lang)
    extra = (' data-chart="assoc-bars" data-points="{0}" data-bars="{1}" data-metric="{2}"'
             ).format(sum(v[2] for v in vals), n, _e(str(_g(a, "y_metric", "") or "")))
    return _svg(w, h, "".join(body), title=title, extra=extra)


def _chart_steps_bars(months: Sequence[MonthStat], *, target: Optional[float],
                      lang: str = "zh", w: float = 1000.0, h: float = 320.0,
                      unit: str = "步") -> str:
    """步数条形图：月度**有数据日均** vs 目标线（点线）。

    用「有数据日的日均」而不是「总量 ÷ 当月天数」：后者会把没戴表的日子算成 0，
    让「这个月走得多」变成「这个月戴得多」。缺数据的日子在 x 标签里标出覆盖率。
    """
    rows = [m for m in (months or []) if m is not None and m.days > 0]
    if len(rows) < 2:
        return ""
    truncated = len(rows) > _BARS_MAX_MONTHS
    rows = rows[-_BARS_MAX_MONTHS:]
    y_max = max([m.mean for m in rows] + ([target] if target else []))
    if y_max <= 0:
        return ""
    top_v = y_max * 1.22
    left, right, top, bottom = 92.0, 26.0, 40.0, 74.0
    px0, px1 = left, w - right
    py0, py1 = top, h - bottom
    n = len(rows)
    slot = (px1 - px0) / float(n)
    bar_w = min(slot * 0.62, 90.0)

    def Y(v: float) -> float:
        return py1 - (py1 - py0) * (v / top_v)

    body: List[str] = [_rect(px0, py0, px1 - px0, py1 - py0, "fr")]
    for t in _nice_ticks(0.0, top_v, 4):
        if t < 0 or t > top_v + 1e-9:
            continue
        y = Y(t)
        body.append(_line(px0, y, px1, y, "grid"))
        body.append(_txt(px0 - 8.0, y + 6.0, _fmt(t, 0, ""), "t dim", "end", _FS_SMALL))
    label_step = max(1, int(math.ceil(n / 12.0)))
    for i, m in enumerate(rows):
        cx = px0 + slot * (i + 0.5)
        x = cx - bar_w / 2.0
        y = Y(m.mean)
        low_cov = m.days < 7
        body.append(_rect(x, y, bar_w, py1 - y, "hbar low" if low_cov else "hbar"))
        if low_cov:
            body.append(_hatch(x, y, bar_w, py1 - y, 9.0))
        if i % label_step == 0 or i == n - 1:
            body.append(_txt(cx, y - 10.0, _fmt(m.mean, 0, ""), "t", "middle", _FS_SMALL))
            body.append(_txt(cx, py1 + 24.0, m.month[2:], "t dim", "middle", _FS_TICK))
            body.append(_txt(cx, py1 + 44.0, "{0}/{1}".format(m.days, _days_in_month(m.month)),
                             "t dim", "middle", _FS_SMALL))
    body.append(_line(px0, py1, px1, py1, "axline"))
    if target:
        ty = Y(min(target, top_v))
        body.append(_line(px0, ty, px1, ty, "goalline"))
        body.append(_txt(px0 + 6.0, ty - 8.0,
                         _t("目标 {0} {1}", "target {0} {1}", lang).format(_fmt(target, 0, ""), unit),
                         "t", "start", _FS_SMALL, "600"))
    if truncated:
        body.append(_txt(px1, 24.0,
                         _t("仅显示最近 {0} 个月", "last {0} months only", lang).format(
                             _BARS_MAX_MONTHS), "t dim", "end", _FS_SMALL))
    title = _t("步数：月度有数据日均", "Steps: monthly daily average", lang)
    extra = ' data-chart="steps-bars" data-points="{0}"'.format(n)
    return _svg(w, h, "".join(body), title=title, extra=extra)


def _days_in_month(month: str) -> int:
    """这个月有多少天（用于步数条形图里的「有数据天数/当月天数」）。"""
    try:
        y, m = str(month).split("-")[:2]
        return calendar.monthrange(int(y), int(m))[1]
    except (ValueError, AttributeError, IndexError):
        return 30


def _chart_coverage(rows: Sequence[Tuple[str, int, int, float]], *,
                    lang: str = "zh", w: float = 1000.0, h_per: float = 28.0) -> str:
    """数据覆盖率图：每个指标一根水平条，显示有数据天数占比。

    它回答的是报告里最容易被忽略的问题：**这张图上的数值，有多少天真的在？**
    覆盖率低的指标（`<50%`）用斜纹画，并且条尾始终写「有数据天数/跨度天数 · 百分比」，
    不靠颜色传达信息。
    """
    rows = [r for r in (rows or []) if r]
    if not rows:
        return ""
    n = len(rows)
    h = 26.0 + h_per * n + 16.0
    left, right = 210.0, 210.0
    px0, px1 = left, w - right
    body: List[str] = []
    for i, (label, days, span, ratio) in enumerate(rows):
        y = 26.0 + h_per * i
        body.append(_txt(px0 - 10.0, y + 14.0, _trunc(label, 16), "t", "end", _FS_SMALL))
        body.append(_rect(px0, y, px1 - px0, 18.0, "fr"))
        wdt = max(0.0, min(1.0, float(ratio))) * (px1 - px0)
        body.append(_rect(px0, y, wdt, 18.0, "hbar low" if ratio < 0.5 else "hbar"))
        if ratio < 0.5:
            body.append(_hatch(px0, y, wdt, 18.0, 9.0))
        body.append(_txt(px1 + 10.0, y + 14.0,
                         "{0}/{1} 天 · {2}%".format(days, span, _fmt(ratio * 100.0, 0, "")),
                         "t dim", "start", _FS_SMALL))
    title = _t("数据覆盖率", "Data coverage", lang)
    extra = ' data-chart="coverage" data-points="{0}"'.format(n)
    return _svg(w, h, "".join(body), title=title, extra=extra)


def _chart_verdict_strip(rows: Sequence[Tuple[str, str, str]], *,
                         lang: str = "zh", w: float = 1000.0) -> str:
    """体检式总览条：每个参与判定的指标一行，小色块 + **文字**判定，按严重度排序。

    色块只是让眼睛先定位到「需要留意」的那几行；**判定结论永远有文字**，
    所以黑白打印、色盲、屏幕阅读器都读得到同样的信息。
    """
    rows = [r for r in (rows or []) if r]
    if not rows:
        return ""
    n = len(rows)
    ncol = 2 if n > 14 else 1
    per_col = int(math.ceil(n / float(ncol)))
    row_h = 30.0
    h = 26.0 + row_h * per_col + 10.0
    col_w = w / float(ncol)
    body: List[str] = []
    for i, (label, verdict, cls) in enumerate(rows):
        col = i // per_col
        rowi = i % per_col
        x = 8.0 + col * col_w
        y = 20.0 + row_h * rowi
        body.append(_rect(x, y, 15.0, 15.0, "v" + cls))
        body.append(_txt(x + 24.0, y + 13.0, _trunc(label, 14), "t", "start", _FS_SMALL, "600"))
        body.append(_txt(x + 190.0, y + 13.0, _trunc(verdict, 16), "t dim", "start", _FS_SMALL))
    title = _t("判定总览", "Verdict overview", lang)
    extra = ' data-chart="verdict-strip" data-points="{0}"'.format(n)
    return _svg(w, h, "".join(body), title=title, extra=extra)


# ──────────────────────────────────────────────────────────── HTML 片段

_SEV_TAG: Dict[str, Tuple[str, str]] = {
    "alert": ("需留意", "attention"),
    "warn": ("注意", "note"),
    "info": ("背景信息", "background"),
}

_HEAD_ZH = ("指标", "最新值", "参考区间", "判定", "变化", "数据点", "来源")
_HEAD_EN = ("Metric", "Latest", "Reference", "Verdict", "Change", "Points", "Sources")


def _md_inline(text: Any) -> str:
    """极简行内 Markdown：先整体转义，再放开粗体/等宽，最后**去掉所有链接**。

    去掉链接不是偷懒：这份 HTML 是交付物，硬约束是「不出现任何外部链接」。
    AI 解读里若带了 `[标题](地址)`，这里只保留标题文字。
    """
    out = _e(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", out)
    return out


def _md_bullets(lines: Sequence[str]) -> str:
    """把一组 Markdown 列表行转成 `<ul>`（用于复用 report.py 的既有文案）。"""
    items = [_md_inline(str(ln).strip()[2:]) for ln in (lines or [])
             if str(ln).strip().startswith("- ")]
    if not items:
        return ""
    return "<ul>" + "".join("<li>" + it + "</li>" for it in items) + "</ul>"


def _md_to_html(text: str, lang: str = "zh") -> str:
    """极简块级 Markdown → HTML，只覆盖 AI 解读与小节文案里实际会出现的语法。

    刻意不做完整实现：需要的是「模型写的小节标题 / 列表 / 引用 / 简单表格
    能被排出来」，而不是一个 Markdown 引擎。不认识的语法会原样当段落输出。
    """
    out: List[str] = []
    para: List[str] = []
    lst: List[str] = []
    lst_tag = "ul"
    quote: List[str] = []
    table: List[List[str]] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + _md_inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal lst_tag
        if lst:
            out.append("<{0}>{1}</{0}>".format(lst_tag, "".join(lst)))
            lst.clear()
            lst_tag = "ul"

    def flush_quote() -> None:
        if quote:
            out.append("<blockquote><p>"
                       + "<br>".join(_md_inline(q) for q in quote)
                       + "</p></blockquote>")
            quote.clear()

    def flush_table() -> None:
        if not table:
            return
        rows = [r for r in table
                if not all(re.fullmatch(r":?-{2,}:?", (c or "").strip() or "-") for c in r)]
        if rows:
            head, body = rows[0], rows[1:]
            html = ["<table><thead><tr>"]
            html.extend("<th>" + _md_inline(c) + "</th>" for c in head)
            html.append("</tr></thead><tbody>")
            for r in body:
                html.append("<tr>" + "".join("<td>" + _md_inline(c) + "</td>" for c in r) + "</tr>")
            html.append("</tbody></table>")
            out.append("".join(html))
        table.clear()

    def flush_all() -> None:
        flush_para()
        flush_list()
        flush_quote()
        flush_table()

    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush_all()
            continue
        if s.startswith("|"):
            flush_para()
            flush_list()
            flush_quote()
            cells = [c.strip() for c in s.strip("|").split("|")]
            table.append(cells)
            continue
        flush_table()
        if s.startswith("#"):
            m = re.match(r"^(#{1,6})\s*(.*)$", s)
            if m:
                flush_all()
                level = min(4, max(3, len(m.group(1)) + 1))
                out.append("<h{0}>{1}</h{0}>".format(level, _md_inline(m.group(2))))
                continue
        if s.startswith(">"):
            flush_para()
            flush_list()
            quote.append(s.lstrip("> ").strip())
            continue
        m = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", s)
        if m:
            flush_para()
            flush_quote()
            tag = "ol" if re.match(r"^\d", s) else "ul"
            if lst and tag != lst_tag:
                flush_list()
            lst_tag = tag
            lst.append("<li>" + _md_inline(m.group(1)) + "</li>")
            continue
        flush_list()
        flush_quote()
        para.append(s)
    flush_all()
    return "".join(out)


def _chart_card(caption_title: str, caption_detail: str, svg: str) -> str:
    # 用 _md_inline（先转义再放开粗体/等宽）：catalog 的 hint 与 pitfalls 的 detail
    # 里本来就带 **强调** 标记，转成 <strong> 比原样漏出星号好看，也不会引入注入面。
    return ('<figure class="chart"><figcaption><span class="ct">{0}</span>'
            '<span class="cd">{1}</span></figcaption>{2}</figure>').format(
        _md_inline(caption_title), _md_inline(caption_detail), svg)


def _nodata(reason: str, lang: str = "zh") -> str:
    """「数据不足」占位：画不出图时**不留半张空图**，只写一句为什么。"""
    prefix = "Not enough data: " if str(lang).lower().startswith("en") else "数据不足："
    return '<p class="nodata">{0}</p>'.format(_md_inline(prefix + reason))


def _section(sec_id: str, heading: str, inner: str) -> str:
    return ('<section class="sec" id="{0}">\n<h2>{1}</h2>\n{2}\n</section>\n').format(
        _e(sec_id), _e(heading), inner)


# ──────────────────────────────────────────────────────────── 渲染上下文


@dataclass
class _Ctx:
    """渲染时反复要用到的东西一次算好（含月度聚合缓存，避免重复计算）。"""

    ds: Dataset
    analysis: Any
    findings: List[Any]
    trends: Dict[str, Any]
    order: List[str]
    main_ids: List[str]
    hidden_ids: List[str]
    lang: str
    mask: bool
    span: Optional[Tuple[str, str]]
    span_days: int
    by_metric: Dict[str, List[Observation]]
    cache: Dict[Tuple[str, str], List[MonthStat]]

    def obs(self, metric: str) -> List[Observation]:
        return self.by_metric.get(metric) or []

    def months(self, metric: str, agg: str = "snapshot") -> List[MonthStat]:
        key = (metric, str(agg or "snapshot"))
        if key not in self.cache:
            self.cache[key] = monthly_stats(self.obs(metric), agg)
        return self.cache[key]

    def src(self, name: Any) -> str:
        s = "" if name is None else str(name)
        return mask_source_name(s) if self.mask else s


def _by_metric(ds: Dataset) -> Dict[str, List[Observation]]:
    """一次遍历把观测按指标分桶（性能：真实数据 10 万级观测，不要每个指标扫一遍）。"""
    out: Dict[str, List[Observation]] = {}
    for o in (getattr(ds, "observations", None) or []):
        m = str(getattr(o, "metric", "") or "")
        if not m:
            continue
        out.setdefault(m, []).append(o)
    return out


def _sources_cell(ctx: _Ctx, trend: Any) -> str:
    """来源列（与 `report._sources_cell` 同措辞，额外过一次脱敏）。"""
    raw = _g(trend, "sources", None) or []
    if isinstance(raw, str):
        raw = [raw]
    names = [ctx.src(s) for s in raw if str(s).strip()]
    names = [n for n in names if str(n).strip()]
    if not names:
        return _e(_t("未记录", "not recorded", ctx.lang))
    txt = " / ".join(_e(n) for n in names)
    if len(names) > 1:
        txt += ' <span class="warnmark">⚠️</span>'
    return txt


def _sources_text(ctx: _Ctx) -> str:
    raw = _g(ctx.analysis, "sources", None)
    if not raw:
        try:
            raw = ctx.ds.all_sources()
        except Exception:  # pragma: no cover - 容错
            raw = []
    names = [ctx.src(s) for s in (raw or []) if str(s).strip()]
    names = [str(n) for n in names if str(n).strip()]
    if not names:
        return _e(_t("未记录", "not recorded", ctx.lang))
    return _e(" / ".join(dict.fromkeys(names)))


def _verdict_class(trend: Any) -> str:
    """判定 → 视觉分档（只用于定位；结论永远有文字）。

    分档刻意把「未达目标」与「越界」分开：没走到 8000 步不是指标异常，
    用同一档深色标出来会跟本项目的口径自相矛盾。
    """
    v = str(_g(trend, "verdict", "na") or "na").strip().lower()
    note = str(_g(trend, "verdict_note", "") or "")
    ref = _g(trend, "ref", None)
    if v in ("low", "high"):
        if _is_benign_side(ref, v):
            return "benign"
        if _ref_kind(trend, ref) == "goal":
            return "goalout"
        return "out"
    if v == "in":
        return "ina"
    if note == "来源不一致，不做判定":
        return "mixed"
    return "na"


def _bold_cell(text: Any) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", _e(text))


def _row_cells(ctx: _Ctx, trend: Any) -> List[str]:
    """一行七列。前六列**直接复用** report.py 的 `_trend_cells`。

    复用而不是重写，是为了让 HTML 与 Markdown 的判定措辞、变化列、数据点列
    逐字一致——口径漂移是这类报告最贵的 bug。
    """
    cells = list(_trend_cells(trend, ctx.lang))
    while len(cells) < 7:
        cells.append("—")
    cls = _verdict_class(trend)
    return [
        _bold_cell(cells[0]),
        _e(cells[1]),
        _e(cells[2]),
        '<span class="chip {0}">{1}</span>'.format(cls, _e(cells[3])),
        _e(cells[4]),
        _e(cells[5]),
        _sources_cell(ctx, trend),
    ]


def _metric_table(ctx: _Ctx, trends_list: Sequence[Any]) -> str:
    rows = [t for t in (trends_list or []) if t is not None]
    if not rows:
        return _nodata(_t("这一节没有可展示的指标。", "no metric to show in this section.", ctx.lang), ctx.lang)
    head = _HEAD_EN if str(ctx.lang).lower().startswith("en") else _HEAD_ZH
    out = ["<table><thead><tr>"]
    out.extend("<th>" + _e(h) + "</th>" for h in head)
    out.append("</tr></thead><tbody>")
    for t in rows:
        cells = _row_cells(ctx, t)
        out.append("<tr>")
        out.append('<td class="m">' + cells[0] + "</td>")
        out.append('<td class="num">' + cells[1] + "</td>")
        out.append("<td>" + cells[2] + "</td>")
        out.append("<td>" + cells[3] + "</td>")
        out.append('<td class="num">' + cells[4] + "</td>")
        out.append('<td class="num">' + cells[5] + "</td>")
        out.append('<td class="src">' + cells[6] + "</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


# ──────────────────────────────────────────────────────────── 图表卡片


def _metric_chart(ctx: _Ctx, metric: str, *, h: float = 210.0) -> str:
    trend = ctx.trends.get(metric)
    if trend is None:
        return ""
    agg = str(_g(trend, "agg", "snapshot") or "snapshot")
    months = ctx.months(metric, agg)
    cn = _cn_of(metric, trend)
    days = _jint(_g(trend, "days", 0))
    if len(months) < 2:
        if days <= 0:
            return _nodata(_t("{0}：本次没有任何数据点。".format(cn),
                              "{0}: no data points at all.".format(cn), ctx.lang), ctx.lang)
        return _nodata(_t(
            "{0}（只有 {1} 天数据、{2} 个月）：画不出月度趋势。".format(cn, days, len(months)),
            "{0} (only {1} day(s) over {2} month(s)): no monthly trend.".format(
                cn, days, len(months)), ctx.lang), ctx.lang)
    svg = _chart_trend(trend, months, lang=ctx.lang, h=h)
    if not svg:
        return _nodata(_t("{0}：数据点不足以成图。".format(cn),
                          "{0}: not enough points to plot.".format(cn), ctx.lang), ctx.lang)
    ref = _g(trend, "ref", None)
    kind = _ref_kind(trend, ref)
    bits = [_t("共 {0} 天数据、{1} 个月度点；折线为月度中位数，阴影为四分位区间".format(
                   days, len(months)),
               "{0} days, {1} monthly points; line = monthly median, band = IQR".format(
                   days, len(months)), ctx.lang)]
    if kind == "range" and ref is not None:
        bits.append(_t("实线框为参考区间 {0}".format(_ref_label(ref)),
                       "solid box = reference range {0}".format(_ref_label(ref)), ctx.lang))
    elif kind == "screen" and ref is not None:
        bits.append(_t("虚线框为筛查阈值 {0}".format(_ref_label(ref)),
                       "dashed box = screening range {0}".format(_ref_label(ref)), ctx.lang))
    elif kind == "goal" and ref is not None:
        bits.append(_t("点线为目标值 {0}".format(_ref_label(ref)),
                       "dotted line = target {0}".format(_ref_label(ref)), ctx.lang))
    hands = _g(trend, "handovers", None) or []
    if hands:
        parts = []
        for item in list(hands)[:3]:
            try:
                new, old, day = item[0], item[1], item[2]
            except (TypeError, IndexError):
                continue
            parts.append("{0} {1}→{2}".format(day, ctx.src(old), ctx.src(new)))
        bits.append(_t("竖直虚线＝设备接替（{0}）".format("；".join(parts)),
                       "vertical dashed line = device handover ({0})".format("；".join(parts)),
                       ctx.lang))
        if len(hands) > 3:
            bits.append(_t("另有 {0} 处接替，见第七节".format(len(hands) - 3),
                           "{0} more handovers, see section 7".format(len(hands) - 3), ctx.lang))
    return _chart_card(_t("{0}·月度趋势".format(cn), "{0} monthly trend".format(cn), ctx.lang),
                       "；".join(bits), svg)


def _verdict_rows(ctx: _Ctx) -> List[Tuple[str, str, str]]:
    """体检式总览条的输入：每个参与判定的指标一行，按严重度排序。"""
    rank = {"out": 0, "goalout": 1, "mixed": 2, "benign": 3, "ina": 4, "na": 5}
    order_idx = {m: i for i, m in enumerate(ctx.order)}
    rows: List[Tuple[str, str, str]] = []
    for metric in ctx.main_ids:
        trend = ctx.trends.get(metric)
        if trend is None:
            continue
        cls = _verdict_class(trend)
        if cls in ("na",):
            continue
        ref = _g(trend, "ref", None)
        text = _verdict_text(_g(trend, "verdict", "na"), _ref_kind(trend, ref), ctx.lang,
                             _g(trend, "verdict_note", ""),
                             _is_benign_side(ref, _g(trend, "verdict", "na")))
        rows.append((_cn_of(metric, trend), text, cls))
    rows.sort(key=lambda r: (rank.get(r[2], 9), order_idx))
    return rows


def _coverage_rows(ctx: _Ctx) -> List[Tuple[str, int, int, float]]:
    rows: List[Tuple[str, int, int, float]] = []
    for metric in ctx.main_ids:
        trend = ctx.trends.get(metric)
        if trend is None:
            continue
        days = _jint(_g(trend, "days", 0))
        span = _jint(_g(trend, "span_days", 0)) or ctx.span_days
        cov = _jnum(_g(trend, "coverage", None))
        if cov is None and span > 0:
            cov = min(1.0, days / float(span))
        if cov is None or span <= 0 or days <= 0:
            continue
        rows.append((_cn_of(metric, trend), days, span, float(cov)))
    rows.sort(key=lambda r: (r[3], r[0]))
    return rows


# ──────────────────────────────────────────────────────────── 各个小节


def _cover(ctx: _Ctx, title: str, generated: str) -> str:
    ds = ctx.ds
    if ctx.span:
        span_txt = "{0} → {1}（{2}）".format(ctx.span[0], ctx.span[1],
                                             _t("共 {0} 天".format(ctx.span_days),
                                               "{0} days".format(ctx.span_days), ctx.lang))
    else:
        span_txt = _t("没有可用数据", "no usable data", ctx.lang)
    n_obs = len(getattr(ds, "observations", None) or [])
    n_workouts = len(getattr(ds, "workouts", None) or [])
    n_records = _jint(getattr(ds, "n_records", 0))
    scale_bits = []
    if n_records:
        scale_bits.append(_t("原始记录 {0} 条".format(_thousands(n_records)),
                             "{0} raw records".format(_thousands(n_records)), ctx.lang))
    scale_bits.append(_t("观测 {0} 条".format(_thousands(n_obs)),
                         "{0} observations".format(_thousands(n_obs)), ctx.lang))
    scale_bits.append(_t("训练 {0} 次".format(_thousands(n_workouts)),
                         "{0} workouts".format(_thousands(n_workouts)), ctx.lang))
    scale_bits.append(_t("指标 {0} 项".format(_thousands(len(ctx.trends))),
                         "{0} metrics".format(_thousands(len(ctx.trends))), ctx.lang))
    body = [
        '<h1>{0}</h1>'.format(_e(title)),
        '<p class="sub">{0}</p>'.format(_e(_t(
            "自包含 HTML 交付形态：浏览器里「打印成 PDF」即为最终报告。",
            "Self-contained HTML: print to PDF in the browser for the final report.", ctx.lang))),
        '<p class="generated">{0}</p>'.format(
            _e(_t("生成时间：{0}".format(generated), "Generated: {0}".format(generated), ctx.lang))),
        '<p class="generated">{0}</p>'.format(
            _e(_t("数据来源文件：{0}".format(_files_text(ds)),
                  "Source files: {0}".format(_files_text(ds)), ctx.lang))),
        "<dl class=\"facts\">",
        "<dt>{0}</dt><dd>{1}</dd>".format(_e(_t("数据跨度", "Data span", ctx.lang)), _e(span_txt)),
        "<dt>{0}</dt><dd>{1}</dd>".format(_e(_t("数据来源", "Sources", ctx.lang)), _sources_text(ctx)),
        "<dt>{0}</dt><dd>{1}</dd>".format(_e(_t("数据规模", "Data scale", ctx.lang)),
                                          _e(" ｜ ".join(scale_bits))),
        "</dl>",
    ]
    if n_obs == 0:
        body.append('<div class="callout danger"><p><strong>{0}</strong> {1}</p></div>'.format(
            _e(_t("数据不足：", "Not enough data: ", ctx.lang)),
            _e(_t("本次导入没有解析出任何可用观测，下面的每一节都会明确写「数据不足」，"
                  "不会画出半张空图。", "No observation was parsed from the input; every "
                  "section below says so explicitly instead of drawing empty charts.", ctx.lang))))
    body.append('<div class="callout danger"><p><strong>{0}</strong> {1}</p></div>'.format(
        _e(_t("这不是医学诊断。", "This is not a medical diagnosis.", ctx.lang)), _e(DISCLAIMER)))
    body.append('<div class="callout"><p>{0}</p></div>'.format(_e(_t(
        "打印建议：在浏览器里按 Ctrl+P，目标选「另存为 PDF」，纸张 A4、"
        "边距用默认值、并勾选「背景图形」；页码由浏览器的「页眉和页脚」选项提供"
        "（样式表里的页码规则只有分页引擎会读，浏览器会忽略它）。",
        "To print: Ctrl+P, destination \"Save as PDF\", A4, default margins, and enable "
        "background graphics. Page numbers come from the browser's own header/footer option.",
        ctx.lang))))
    return '<section class="cover">\n' + "\n".join(body) + "\n</section>\n"


def _finding_card(ctx: _Ctx, finding: Any) -> str:
    sev = _sev(finding)
    tag = _SEV_TAG.get(sev, _SEV_TAG["info"])[0 if not str(ctx.lang).lower().startswith("en") else 1]
    title = _g(finding, "title", "")
    detail = _g(finding, "detail", "")
    metrics = _g(finding, "metrics", None) or []
    if isinstance(metrics, str):
        metrics = [metrics]
    names = [_cn_of(str(m), ctx.trends.get(str(m))) for m in metrics if str(m).strip()]
    body = ['<article class="finding sev-{0}">'.format(sev)]
    body.append('<h3><span class="sevtag {0}">{1}</span>{2}</h3>'.format(
        sev, _e(tag), _md_inline(title)))
    if detail:
        body.append("<p>" + _md_inline(detail) + "</p>")
    else:
        body.append("<p>" + _e(_t("（该提示没有更多细节，请核对数据来源文件。）",
                                  "(No further detail for this note.)", ctx.lang)) + "</p>")
    if names:
        body.append('<p class="small">' + _e(_t("相关指标：", "Metrics: ", ctx.lang))
                    + _e("、".join(names)) + "</p>")
    body.append("</article>")
    return "".join(body)


def _sec_credibility(ctx: _Ctx) -> str:
    fs = ctx.findings
    notable = [f for f in fs if _sev(f) in ("alert", "warn")]
    info = [f for f in fs if _sev(f) == "info"]
    body = ['<p class="lede">' + _e(_t(
        "这一节先讲「这份数据里哪些地方会把结论读错」，再讲数值与趋势。"
        "每条提示都带具体数字，可以逐条核对。",
        "This section lists the places where the data itself would mislead a conclusion.",
        ctx.lang)) + "</p>"]
    if notable:
        for f in sorted(notable, key=lambda x: _SEV_RANK.get(_sev(x), 9)):
            body.append(_finding_card(ctx, f))
    else:
        body.append('<p class="callout note">' + _e(_t(
            "未发现影响结论可信度的问题。", "No credibility issues were found.", ctx.lang)) + "</p>")

    # info 级提示不删掉、也不往前挤：放进后面的小节，方便逐条核对而不抢注意力。
    body.append("<h3>" + _e(_t("其余提示（info 级：不影响结论，仅供核对）",
                               "Other notes (info level: no effect on conclusions)", ctx.lang))
                + "</h3>")
    if info:
        for f in info:
            body.append("<h4>" + _md_inline(_g(f, "title", "")) + "</h4>")
            body.append("<p>" + _md_inline(_g(f, "detail", "")) + "</p>")
    else:
        body.append('<p class="nodata">' + _e(_t("没有 info 级提示。", "No info-level notes.", ctx.lang))
                    + "</p>")

    body.append("<h3>" + _e(_t("只做趋势、不做判定的指标",
                               "Metrics tracked as trends, without a verdict", ctx.lang)) + "</h3>")
    none_ref = [m for m in ctx.order if _is_none_ref(ctx.trends.get(m))]
    shown = [m for m in none_ref if _category_of(m, ctx.trends.get(m)) not in HIDDEN_CATEGORIES]
    hidden_none = [m for m in none_ref if _category_of(m, ctx.trends.get(m)) in HIDDEN_CATEGORIES]
    if not none_ref:
        body.append("<p>" + _e(_t("（本报告里每个指标都有可用的参考区间或活动目标，没有只做趋势的例外项。）",
                                  "(Every metric has a usable reference range or goal.)", ctx.lang))
                    + "</p>")
    else:
        bullets = _md_bullets(_reason_lines(shown, ctx.trends, ctx.lang))
        if bullets:
            body.append(bullets)
        if hidden_none:
            body.append("<p>" + _e(_t(
                "另有 {0} 项设备原始读数或设备自算结果（生物电阻抗、设备建议与评分）同样只做趋势，"
                "明细见附录 B。".format(len(hidden_none)),
                "{0} more device raw readings / device-generated values are also trend-only; "
                "see Appendix B.".format(len(hidden_none)), ctx.lang)) + "</p>")

    notes = [str(n) for n in (_g(ctx.analysis, "notes", None) or []) if str(n).strip()]
    if notes:
        body.append("<h3>" + _e(_t("解析提示（数据来源与口径）",
                                   "Parser notes (data sources and conventions)", ctx.lang)) + "</h3>")
        limit = 12
        body.append("<ul>" + "".join("<li>" + _e(n) + "</li>" for n in notes[:limit]) + "</ul>")
        if len(notes) > limit:
            body.append("<p>" + _e(_t("另有 {0} 条解析提示（多为单位与来源说明）。".format(
                len(notes) - limit),
                "{0} more parser notes.".format(len(notes) - limit), ctx.lang)) + "</p>")
    return _section("s-credibility", _t("一、数据可信度提示", "1. Data credibility notes", ctx.lang),
                    "".join(body))


def _sec_overview(ctx: _Ctx) -> str:
    body: List[str] = ['<p class="lede">' + _e(_t(
        "判定列直接复用 Markdown 版报告的口径：「未达目标」是活动量目标没到，"
        "不是指标异常；「超出筛查参考」意味着值得进一步评估，不是诊断；"
        "「只做趋势」表示这个指标没有可用区间；「来源不一致，不做判定」表示有区间、"
        "但拿不到与这条读数同源的区间。",
        "The verdict column reuses the wording of the Markdown report.", ctx.lang)) + "</p>"]

    strip = _verdict_rows(ctx)
    if strip:
        body.append(_chart_card(
            _t("体检式总览：每个参与判定的指标一行", "Verdict overview", ctx.lang),
            _t("按严重度排序：越界需留意 → 未达目标（活动量，虚线框，不是异常）"
               "→ 来源不一致，不做判定 → 落在不需要留意的一侧 → 在参考区间内；"
               "色块只是定位，判定结论一律有文字。",
               "Sorted by severity; every row carries the verdict as text, not only colour.",
               ctx.lang),
            _chart_verdict_strip(strip, w=1000.0, lang=ctx.lang)))
    else:
        body.append(_nodata(_t("没有参与判定的指标。", "no metric takes part in a verdict.", ctx.lang), ctx.lang))

    cov = _coverage_rows(ctx)
    if cov:
        shown = cov[:_COVERAGE_MAX_ROWS]
        detail = _t(
            "覆盖率＝有数据的天数 ÷ 跨度天数（按日累计/平均的指标用 analysis 的口径，"
            "快照类按天数比）；低于 50% 用斜纹。此处列覆盖率最低的 {0} 项，共 {1} 项。".format(
                len(shown), len(cov)),
            "Coverage = days with data / span. The {0} lowest of {1} metrics are shown.".format(
                len(shown), len(cov)), ctx.lang)
        body.append(_chart_card(_t("数据覆盖率", "Data coverage", ctx.lang), detail,
                                _chart_coverage(shown, w=1000.0, lang=ctx.lang)))
    else:
        body.append(_nodata(_t("没有任何指标的跨度足够计算覆盖率。",
                               "no metric has a wide enough span for coverage.", ctx.lang), ctx.lang))

    groups = _by_category(ctx.analysis, ctx.trends)
    for cat, ids in groups.items():
        if cat in HIDDEN_CATEGORIES:
            continue
        ids = [m for m in ids if m in ctx.trends]
        if not ids:
            continue
        body.append("<h3>" + _e(str(cat)) + "</h3>")
        hint = catalog.CATEGORY_HINT.get(str(cat), "")
        if hint:
            body.append('<p class="small">' + _e(hint) + "</p>")
        body.append(_metric_table(ctx, [ctx.trends[m] for m in ids]))
        hint_lines = []
        for metric in ids:
            spec = _spec_of(metric)
            spec_hint = str(getattr(spec, "hint", "") or "") if spec is not None else ""
            if spec_hint:
                hint_lines.append("<li><strong>" + _e(_cn_of(metric, ctx.trends[metric]))
                                  + "</strong>：" + _md_inline(spec_hint) + "</li>")
        if hint_lines:
            body.append("<ul>" + "".join(hint_lines) + "</ul>")
        for metric in ids:
            body.append(_metric_chart(ctx, metric))
    return _section("s-overview", _t("二、指标总览", "2. Metrics overview", ctx.lang),
                    "".join(body))


def _sec_sleep(ctx: _Ctx) -> str:
    lang = ctx.lang
    body: List[str] = ['<p class="lede">' + _e(_t(
        "时长看够不够，分期看构成，规律性看「几点睡、几点醒」——"
        "规律性比时长更容易改，而且与恢复状态的关系更稳。",
        "Duration, composition, and regularity.", lang)) + "</p>"]

    trend = ctx.trends.get("sleep_total")
    if trend is not None:
        body.append(_metric_chart(ctx, "sleep_total", h=320.0))
    else:
        body.append(_nodata(_t("没有总睡眠时长数据。", "no sleep duration data.", lang), lang))

    body.append("<h3>" + _e(_t("睡眠分期构成", "Sleep stage composition", lang)) + "</h3>")
    rows, inbed = _sleep_stage_rows(ctx.trends, lang)
    if rows or inbed:
        svg = _chart_sleep_stages(rows, inbed, w=1000.0, lang=lang)
        if svg:
            n_nights = max([_jint(_g(ctx.trends.get(m), "days", 0))
                            for m in ("sleep_deep", "sleep_core", "sleep_rem", "sleep_awake",
                                      "sleep_inbed") if ctx.trends.get(m)] or [0])
            denom = (_t("占比以卧床时间为分母", "percentages use in-bed time as the denominator", lang)
                     if inbed else
                     _t("本次没有卧床时间数据，占比改以各分期之和为分母",
                        "no in-bed data: percentages use the sum of stages", lang))
            body.append(_chart_card(
                _t("各期时长（全期中位数）", "Stage durations (median over the period)", lang),
                _t("取全期各期的中位数（不是某一夜，单夜分期判定误差大）；{0}。"
                   "卧床时间是各分期的外框，所以画成两行而不是叠在同一条上——"
                   "叠起来会把总时长算多。共 {1} 夜有数据。".format(denom, n_nights),
                   "Median per stage over the whole period; {0}. In-bed is drawn as a separate "
                   "row because it is the outer frame of the stages. {1} nights with data.".format(
                       denom, n_nights), lang), svg))
        else:
            body.append(_nodata(_t("分期数据不足，画不出构成。", "not enough stage data.", lang), lang))
    else:
        body.append(_nodata(_t("本次没有睡眠分期数据（深睡/核心/REM/清醒/卧床）。",
                               "no sleep stage data in this dataset.", lang), lang))

    body.append("<h3>" + _e(_t("睡眠规律性", "Sleep regularity", lang)) + "</h3>")
    sr = _g(ctx.analysis, "sleep_regularity", None)
    if sr is None:
        body.append(_nodata(_t(
            "睡眠规律性需要入睡时刻与醒来时刻，且至少 14 夜数据。",
            "Sleep regularity needs onset/wake times over at least 14 nights.", lang), lang))
    else:
        level_cn = {
            "regular": _t("规律", "regular", lang),
            "moderate": _t("一般", "moderately irregular", lang),
            "irregular": _t("不规律", "irregular", lang),
        }.get(str(_g(sr, "level", "")), _t("未知", "unknown", lang))
        mid_std = _jnum(_g(sr, "mid_std_min", None))
        onset_c = minutes_to_clock(_jnum(_g(sr, "onset_median_min", None)))
        wake_c = minutes_to_clock(_jnum(_g(sr, "wake_median_min", None)))
        items: List[str] = []
        if mid_std is not None:
            items.append(_t(
                "睡眠中点（入睡与醒来的中点）的标准差是 <strong>{0}</strong>（{1} 夜），"
                "属于「{2}」。".format(_fmt(mid_std, 1, "分钟"), _jint(_g(sr, "n", 0)), level_cn),
                "Standard deviation of the sleep midpoint is <strong>{0}</strong> over {1} "
                "nights: {2}.".format(_fmt(mid_std, 1, "min"), _jint(_g(sr, "n", 0)), level_cn),
                lang))
        if onset_c != "—" or wake_c != "—":
            items.append(_t(
                "入睡时间中位 <strong>{0}</strong>，醒来时间中位 <strong>{1}</strong>；"
                "入睡时刻标准差 {2} 分钟，醒来时刻标准差 {3} 分钟。".format(
                    onset_c, wake_c, _fmt(_jnum(_g(sr, "onset_std_min", None)), 0, ""),
                    _fmt(_jnum(_g(sr, "wake_std_min", None)), 0, "")),
                "Median onset <strong>{0}</strong>, median wake <strong>{1}</strong>.".format(
                    onset_c, wake_c), lang))
        rms = _jnum(_g(sr, "recent_mid_std_min", None))
        if rms is not None:
            items.append(_t(
                "近 90 天：中点标准差 <strong>{0}</strong>（{1} 夜）。".format(
                    _fmt(rms, 1, "分钟"), _jint(_g(sr, "recent_n", 0))),
                "Last 90 days: midpoint SD <strong>{0}</strong> over {1} nights.".format(
                    _fmt(rms, 1, "min"), _jint(_g(sr, "recent_n", 0))), lang))
        items.append(_t(
            "分档阈值（30 / 60 分钟）是<strong>经验分档</strong>，不是诊断标准；"
            "这里只说明离散程度，不判断好或不好。要看改善，比较上面「近 90 天」与全期的差。",
            "The 30 / 60 minute bands are rules of thumb, not diagnostic thresholds.", lang))
        body.append("<ul>" + "".join("<li>" + it + "</li>" for it in items) + "</ul>")

        onset = {}
        wake = {}
        for o in ctx.obs("sleep_onset_min"):
            onset[str(getattr(o, "date", ""))] = _jnum(getattr(o, "value", None)) or 0.0
        for o in ctx.obs("sleep_wake_min"):
            wake[str(getattr(o, "date", ""))] = _jnum(getattr(o, "value", None)) or 0.0
        pts, total = _sleep_scatter_points(onset, wake)
        if len(pts) >= 3:
            svg = _chart_sleep_scatter(pts, sr, w=1000.0, lang=lang)
            note = _t(
                "每夜一个点，x＝入睡时刻、y＝醒来时刻（钟点）。十字为中位数，"
                "虚框为 ±1 个标准差——注意两个方向的标准差是分别算的，"
                "框只表示离散程度，不是联合分布的分位区。",
                "One point per night; crosshair = medians, dashed box = ±1 SD per axis.", lang)
            if len(pts) < total:
                note += _t(" 共 {0} 夜，图中等间隔抽样 {1} 夜（避免糊成一团墨）。".format(total, len(pts)),
                           " {0} nights total, {1} sampled evenly.".format(total, len(pts)), lang)
            body.append(_chart_card(_t("每夜入睡与醒来时刻", "Nightly onset and wake", lang),
                                    note, svg))
        else:
            body.append(_nodata(_t("同时有入睡与醒来时刻的夜数太少（少于 3 夜），画不出散点图。",
                                   "fewer than 3 nights have both onset and wake.", lang), lang))
    return _section("s-sleep", _t("三、睡眠", "3. Sleep", ctx.lang), "".join(body))


def _assoc_groups_text(a: Any, lang: str) -> str:
    """关联的分组文字，措辞与 `report._association_block` 一致。"""
    out: List[str] = []
    x_metric = str(_g(a, "x_metric", "") or "")
    for g in (_g(a, "groups", None) or []):
        if not isinstance(g, dict):
            continue
        label = g.get("label", "")
        ymed = _jnum(g.get("y_median"))
        gn = _jint(g.get("n", 0))
        xmed = _jnum(g.get("x_median"))
        x_txt = ""
        if x_metric != "workout" and xmed is not None:
            x_txt = _t("（该组睡眠/活动中位 {0}）".format(_fmt(xmed, 2, "")),
                       " (group median {0})".format(_fmt(xmed, 2, "")), lang)
        out.append(_t(
            "<li><strong>{0}</strong>：{1} 中位 <strong>{2}</strong>，n={3}{4}</li>".format(
                _e(str(label)), _e(catalog.display_name(str(_g(a, "y_metric", "")))),
                _fmt(ymed, 2, ""), gn, _e(x_txt)),
            "<li><strong>{0}</strong>: median {1}, n={2}</li>".format(
                _e(str(label)), _fmt(ymed, 2, ""), gn), lang))
    return "".join(out)


def _assoc_card(ctx: _Ctx, a: Any) -> str:
    """一条关联：标题 + 两组中位数对比图 + 相关系数 + 证据等级 + 「相关不是因果」。"""
    lang = ctx.lang
    cn = str(_g(a, "cn", "") or "")
    ev = str(_g(a, "evidence", "C") or "C")
    ev_cn = {"A": _t("证据较强", "evidence: strong", lang),
             "B": _t("证据中等", "evidence: moderate", lang),
             "C": _t("仅报数", "numbers only", lang)}.get(ev, ev)
    n = _jint(_g(a, "n", 0))
    rho = _jnum(_g(a, "rho", None))
    lag = _jint(_g(a, "lag_days", 0))
    head = "{0}（{1}，n={2}".format(_e(cn), _e(ev_cn), n)
    if rho is not None:
        head += _t("，秩相关 {0}".format(_fmt(rho, 2, "")),
                   ", Spearman rho {0}".format(_fmt(rho, 2, "")), lang)
    head += "）"
    body = ['<article class="assoc"><h3>' + head + "</h3>"]
    svg = _chart_assoc_bars(a, w=1000.0, lang=lang)
    if svg:
        body.append(_chart_card(
            _t("{0}：两组中位数".format(cn), "{0}: group medians".format(cn), lang),
            _t("柱高从 0 起算（截断纵轴会把很小的差别画得很大）；柱上为组中位数、柱下为样本量；"
               "斜纹柱表示差异落在测量噪声内。",
               "Bars start at zero; hatched bar means the difference is within measurement noise.",
               lang), svg))
    else:
        body.append(_nodata(_t("这一条关联的分组中位数不完整，画不出对比柱。",
                               "group medians are incomplete for this association.", lang), lang))
    items: List[str] = []
    if lag:
        items.append(_t("<li>比较方式：当天与 <strong>{0} 天后</strong>配对。</li>".format(lag),
                        "<li>Paired with a <strong>{0}-day</strong> lag.</li>".format(lag), lang))
    items.append(_assoc_groups_text(a, lang))
    delta = _jnum(_g(a, "delta", None))
    noise = _jnum(_g(a, "noise", 0.0)) or 0.0
    if delta is not None and noise > 0:
        if abs(delta) < noise:
            items.append(_t(
                "<li>组间差异 {0} 小于该指标的测量噪声（{1}），"
                "<strong>视为基本没有差别</strong>。</li>".format(
                    _fmt(delta, 2, "", signed=True), _fmt(noise, 2, "")),
                "<li>Group difference is within measurement noise.</li>", lang))
        else:
            items.append(_t(
                "<li>组间差异 {0}，该指标的测量噪声是 {1}（约 {2} 倍）。</li>".format(
                    _fmt(delta, 2, "", signed=True), _fmt(noise, 2, ""),
                    _fmt(abs(delta) / noise, 1, "")),
                "<li>Group difference {0} vs noise {1}.</li>".format(
                    _fmt(delta, 2, "", signed=True), _fmt(noise, 2, "")), lang))
    note = str(_g(a, "note", "") or "")
    if note:
        items.append("<li>" + _e(note) + "</li>")
    items.append(_t("<li><strong>相关，不是因果</strong>：两个指标一起变，"
                    "可能是其中一个影响另一个，也可能是有第三个原因同时影响两者。</li>",
                    "<li><strong>Correlation, not causation.</strong></li>", lang))
    body.append("<ul>" + "".join(items) + "</ul>")
    body.append("</article>")
    return "".join(body)


def _associations(ctx: _Ctx) -> List[Any]:
    items = _g(ctx.analysis, "associations", None) or []
    return [a for a in items if a is not None]


def _sec_recovery(ctx: _Ctx) -> str:
    lang = ctx.lang
    body: List[str] = ['<p class="lede">' + _t(
        "静息心率与心率变异性（HRV）是恢复状态最常用的两个代理指标。"
        "它们没有（也不需要）跨人群的绝对标准：看的是<strong>自己基线</strong>的相对变化。",
        "Resting heart rate and HRV are proxies for recovery; read them against your own "
        "baseline.", lang) + "</p>"]
    for metric in ("resting_hr", "hrv_sdnn"):
        if ctx.trends.get(metric) is None:
            body.append(_nodata(_t("本次没有 {0} 数据。".format(catalog.display_name(metric)),
                                   "no data for {0}.".format(metric), lang), lang))
            continue
        body.append(_metric_chart(ctx, metric, h=320.0))

    body.append("<h3>" + _e(_t("训练日 vs 非训练日", "Training days vs rest days", lang)) + "</h3>")
    body.append("<p>" + _t(
        "这是<strong>组间对比</strong>：把有训练的日子和没训练的日子各自的同日指标中位数放在一起。"
        "训练强度没有分层，所以「练了」不等于「练得一样多」；差异是否值得解读，"
        "看它有没有超过该指标的测量噪声。",
        "A group comparison between training and non-training days; training intensity is not "
        "stratified.", lang) + "</p>")
    train = [a for a in _associations(ctx)
             if str(_g(a, "key", "")).startswith("training_vs_rest__")]
    if train:
        for a in train:
            body.append(_assoc_card(ctx, a))
    else:
        body.append(_nodata(_t(
            "训练记录不足（少于 5 个训练日）或被比较的指标数据太少，做不了训练日/非训练日对比。",
            "not enough training days or paired metric data for this comparison.", lang), lang))
    return _section("s-recovery", _t("四、恢复与负荷", "4. Recovery and load", ctx.lang),
                    "".join(body))


def _sec_assoc(ctx: _Ctx) -> str:
    lang = ctx.lang
    body: List[str] = [
        '<p class="lede">' + _t(
            "下面每一条都是<strong>相关，不是因果</strong>：两个指标一起变，"
            "可能是其中一个影响另一个，也可能是有第三个原因同时影响两者。"
            "证据等级为「仅报数」的条目只列数值，不做解读。",
            "Every item below is a correlation, not causation.", lang) + "</p>",
        '<div class="callout"><p>' + _t(
            "证据等级看的是<strong>效应大小相对于该指标的测量噪声</strong>，不是相关系数。"
            "所以会出现「相关系数很高、等级却不高」的情况——那表示关系很一致，"
            "但幅度还不到这台设备的重复测量误差量级，不足以指导决定。",
            "The evidence grade reflects the effect size relative to the metric's measurement "
            "noise, not the correlation coefficient.", lang) + "</p></div>",
    ]
    others = [a for a in _associations(ctx)
              if not str(_g(a, "key", "")).startswith("training_vs_rest__")]
    if others:
        for a in others:
            body.append(_assoc_card(ctx, a))
    else:
        body.append(_nodata(_t(
            "配对数据不足（每条关联至少需要 20 个配对日），本次没有可算的指标关联。",
            "not enough paired days (at least 20 per association) for any association.", lang), lang))
    return _section("s-associations", _t("五、指标之间的关联", "5. Associations between metrics",
                                         ctx.lang), "".join(body))


def _workout_table(ctx: _Ctx, summary: Any) -> str:
    by_activity = _g(summary, "by_activity", None) or {}
    if not isinstance(by_activity, dict) or not by_activity:
        return _nodata(_t("没有按运动类型的训练明细。", "no per-activity workout detail.", ctx.lang), lang)
    lang = ctx.lang
    head = ([_t("运动类型", "Activity", lang), _t("次数", "Sessions", lang),
             _t("总时长", "Duration", lang), _t("总距离", "Distance", lang),
             _t("总热量", "Energy", lang), _t("平均心率", "Avg HR", lang)]
            if not str(lang).lower().startswith("en")
            else ["Activity", "Sessions", "Duration", "Distance", "Energy", "Avg HR"])
    out = ["<table><thead><tr>"]
    out.extend("<th>" + _e(h) + "</th>" for h in head)
    out.append("</tr></thead><tbody>")
    for activity, info in by_activity.items():
        minutes = _jnum(_g(info, "minutes", None))
        km = _jnum(_g(info, "km", None))
        kcal = _jnum(_g(info, "kcal", None))
        hr = _jnum(_g(info, "avg_hr", None))
        out.append("<tr>")
        out.append("<td>" + _e(activity) + "</td>")
        out.append('<td class="num">' + str(_jint(_g(info, "count", 0))) + "</td>")
        out.append('<td class="num">' + _e(_hm(minutes)) + "</td>")
        out.append('<td class="num">' + _e(_fmt(km, 2, "km")) + "</td>")
        out.append('<td class="num">' + _e(_fmt(kcal, 0, "kcal")) + "</td>")
        out.append('<td class="num">' + _e(_fmt(hr, 0, _t("次/分", "bpm", lang))) + "</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def _sec_activity(ctx: _Ctx) -> str:
    lang = ctx.lang
    body: List[str] = ['<p class="lede">' + _t(
        "活动量指标给的是<strong>目标</strong>，不是正常范围：没走到 8000 步说明没到目标，"
        "与身体指标异常是两件事。",
        "Activity metrics carry a target, not a normal range.", lang) + "</p>"]
    steps = ctx.trends.get("steps")
    if steps is not None:
        months = ctx.months("steps", str(_g(steps, "agg", "sum") or "sum"))
        ref = _g(steps, "ref", None)
        target = _ref_bound(ref, "low") if _ref_kind(steps, ref) == "goal" else None
        svg = _chart_steps_bars(months, target=target, w=1000.0, lang=lang,
                                unit=_unit_of("steps", steps))
        if svg:
            cov = _jnum(_g(steps, "coverage", None))
            note = _t(
                "柱高＝该月「有数据日」的日均步数（不是「总量 ÷ 当月天数」："
                "那会把没戴表的日子算成 0，让「走得少」和「戴得少」分不清）；"
                "点线是目标；x 轴下方是「有数据天数/当月天数」，有数据不足 7 天的月份用斜纹。",
                "Bar = daily average over days with data; dotted line = target.", lang)
            if cov is not None:
                note += _t(" 全期覆盖率 {0}%。".format(_fmt(cov * 100.0, 0, "")),
                           " Overall coverage {0}%.".format(_fmt(cov * 100.0, 0, "")), lang)
            note += _t(" 逐步的趋势折线见第二节。", " The step-by-step trend line is in section 2.",
                       lang)
            body.append(_chart_card(_t("步数：月度日均 vs 目标线", "Steps: monthly average vs target",
                                       lang), note, svg))
        else:
            body.append(_nodata(_t("步数不足 2 个月的数据，画不出月度条形图。",
                                   "fewer than 2 months of step data.", lang), lang))
    else:
        body.append(_nodata(_t("本次没有步数数据。", "no step data in this dataset.", lang), lang))

    body.append("<h3>" + _e(_t("训练汇总", "Workout summary", lang)) + "</h3>")
    summary = _g(ctx.analysis, "workout_summary", None) or {}
    if not _g(summary, "has_data", False):
        body.append(_nodata(_t("本次没有训练记录。", "no workout records in this dataset.", lang), lang))
    else:
        total = _jint(_g(summary, "total", 0))
        span = _g(summary, "span", None)
        span_txt = "—"
        if isinstance(span, (list, tuple)) and len(span) >= 2 and span[0] and span[1]:
            span_txt = "{0} → {1}".format(span[0], span[1])
        items = [
            "<li><strong>" + _e(_t("总次数", "Total sessions", lang)) + "</strong>：" + str(total) + "</li>",
            "<li><strong>" + _e(_t("时间跨度", "Date span", lang)) + "</strong>：" + _e(span_txt) + "</li>",
            "<li><strong>" + _e(_t("总体平均心率", "Overall average heart rate", lang))
            + "</strong>：" + _e(_fmt(_jnum(_g(summary, "avg_hr_overall", None)), 0,
                                      _t("次/分", "bpm", lang))) + "</li>",
        ]
        minutes = _jnum(_g(summary, "total_minutes", None))
        kcal = _jnum(_g(summary, "total_kcal", None))
        km = _jnum(_g(summary, "total_km", None))
        if minutes is not None:
            items.append("<li><strong>" + _e(_t("累计时长", "Total duration", lang)) + "</strong>："
                         + _e(_hm(minutes)) + "</li>")
        if km is not None:
            items.append("<li><strong>" + _e(_t("累计距离", "Total distance", lang)) + "</strong>："
                         + _e(_fmt(km, 2, "km")) + "</li>")
        if kcal is not None:
            items.append("<li><strong>" + _e(_t("累计热量", "Total energy", lang)) + "</strong>："
                         + _e(_fmt(kcal, 0, "kcal")) + "</li>")
        body.append("<ul>" + "".join(items) + "</ul>")
        body.append(_workout_table(ctx, summary))
    return _section("s-activity", _t("六、活动与训练", "6. Activity and workouts", ctx.lang),
                    "".join(body))


def _sec_rules(ctx: _Ctx) -> str:
    lang = ctx.lang
    trends = ctx.trends
    body: List[str] = []

    # ── 体成分与体重（与 Markdown 版同一口径：跨来源的首末差值不写进结论）
    body.append("<h3>" + _e(_t("体成分与体重", "Body composition and weight", lang)) + "</h3>")
    lines: List[str] = []
    skipped: List[str] = []
    for metric in _BODY_METRICS:
        trend = trends.get(metric)
        if trend is None or str(_g(trend, "kind", "")) != "trend":
            continue
        if len(_g(trend, "sources", None) or []) > 1:
            skipped.append(_cn_of(metric, trend))
            continue
        first, last = _g(trend, "first", None), _g(trend, "last", None)
        if first is None or last is None:
            continue
        spec = _spec_of(metric)
        prec = _precision_of(metric, spec)
        unit = _unit_of(metric, trend, spec)
        v_first = _jnum(_g(first, "value", None))
        v_last = _jnum(_g(last, "value", None))
        if v_first is None or v_last is None:
            continue
        delta = _jnum(_g(trend, "delta", None))
        if delta is None:
            delta = v_last - v_first
        pct = _jnum(_g(trend, "pct", None))
        if pct is None and v_first:
            pct = (v_last - v_first) / v_first * 100.0
        d1 = str(_g(first, "date", "") or "")
        d2 = str(_g(last, "date", "") or "")
        parts = [_fmt(delta, prec, unit, signed=True), _pct_str(pct)]
        detail = "，".join(p for p in parts if p)
        if d1 and d2:
            date_txt = d1 + " → " + d2
            detail = (detail + "；" + date_txt) if detail else date_txt
        lines.append("<li><strong>" + _e(_cn_of(metric, trend)) + "</strong>：{0} → {1}（{2}）</li>".format(
            _e(_fmt(v_first, prec, unit)), _e(_fmt(v_last, prec, unit)), _e(detail)))
    if lines:
        body.append("<ul>" + "".join(lines) + "</ul>")
    else:
        body.append("<p>" + _e(_t("没有形成趋势的体成分指标（数据点不足或时间跨度太短）。",
                                  "No body-composition metric has enough data to form a trend yet.",
                                  lang)) + "</p>")
    if skipped:
        body.append('<div class="callout"><p>' + _t(
            "⚠️ {0} 的首末差值没有列出：这些指标的读数来自多个来源，"
            "跨来源的差值不能当作真实变化（见第一节）。要看趋势，请固定用同一台设备。".format(
                "、".join("<strong>" + _e(n) + "</strong>" for n in skipped)),
            "⚠️ First-to-last deltas for {0} are omitted: their readings come from more than "
            "one source.".format("、".join(_e(n) for n in skipped)), lang) + "</p></div>")

    # ── 超出参考区间
    body.append("<h3>" + _e(_t("超出参考区间的指标", "Metrics outside their reference range", lang))
                + "</h3>")
    oor = _out_of_ref(ctx.analysis, trends)
    concerning: List[Any] = []
    benign: List[Any] = []
    for trend in oor:
        if _is_benign_side(_g(trend, "ref", None), _g(trend, "verdict", "")):
            benign.append(trend)
        else:
            concerning.append(trend)
    if not oor:
        body.append("<p>" + _e(_t("没有指标超出参考区间。",
                                  "No metric is outside its reference range.", lang)) + "</p>")
    else:
        items = []
        if not concerning:
            items.append("<li>" + _e(_t("没有需要留意的越界项。",
                                        "Nothing here needs attention.", lang)) + "</li>")
        for trend in concerning:
            metric = str(_g(trend, "metric", "") or "")
            spec = _spec_of(metric)
            prec = _precision_of(metric, spec)
            unit = _unit_of(metric, trend, spec)
            ref = _g(trend, "ref", None)
            kind = _ref_kind(trend, ref)
            last = _g(trend, "last", None)
            latest = _jnum(_g(last, "value", None)) if last is not None else None
            src = _ref_source(trend, ref, spec)
            note = "<li><strong>{0}</strong>：{1}，{2}（参考区间 {3}".format(
                _e(_cn_of(metric, trend)), _e(_fmt(latest, prec, unit)),
                _e(_verdict_text(_g(trend, "verdict", ""), kind, lang,
                                 _g(trend, "verdict_note", ""))),
                _e(_ref_label(ref)))
            if kind:
                note += "，" + _e(_KIND_CN.get(kind, _t("参考区间", "reference range", lang)))
            if src:
                note += "，" + _e(_t("来源：", "source: ", lang)) + _e(src)
            note += "）</li>"
            items.append(note)
        if benign:
            bits = []
            for trend in benign:
                metric = str(_g(trend, "metric", "") or "")
                spec = _spec_of(metric)
                prec = _precision_of(metric, spec)
                unit = _unit_of(metric, trend, spec)
                last = _g(trend, "last", None)
                latest = _jnum(_g(last, "value", None)) if last is not None else None
                bits.append("<strong>{0}</strong>（{1} {2}）".format(
                    _e(_cn_of(metric, trend)), _e(_fmt(latest, prec, unit)),
                    _e(_t("落在不需要留意的一侧", "on the side that is not a concern", lang))))
            items.append("<li>" + _t(
                "以下指标也落在参考区间外，但<strong>落在不需要留意的那一侧</strong>，"
                "不作为异常提示，只当背景信息（原因见第一节）：" + "、".join(bits) + "。",
                "The following are also outside their range but on the side that is not a "
                "concern: " + "、".join(bits) + ".", lang) + "</li>")
        body.append("<ul>" + "".join(items) + "</ul>")

    # ── 不足以判断
    body.append("<h3>" + _e(_t("不足以判断的部分", "Not enough data to judge", lang)) + "</h3>")
    insuff = _insufficient(ctx.analysis, trends)
    if not insuff:
        body.append("<p>" + _e(_t("当前没有因数据点不足而无法判断的指标。",
                                  "No metric is currently blocked by a lack of data points.",
                                  lang)) + "</p>")
    else:
        items = []
        for trend in insuff:
            metric = str(_g(trend, "metric", "") or "")
            n = _jint(_g(trend, "n", 0))
            days = _jint(_g(trend, "days", 0))
            items.append(_t(
                "<li><strong>{0}</strong>：只有 {1} 个数据点（{2} 天），不足以判断趋势；"
                "需要更多同条件下的测量。</li>".format(_e(_cn_of(metric, trend)), n, days),
                "<li><strong>{0}</strong>: only {1} data point(s) over {2} day(s).</li>".format(
                    _e(_cn_of(metric, trend)), n, days), lang))
        body.append("<ul>" + "".join(items) + "</ul>")

    # ── 跨设备可比性
    handover_trends = [t for t in trends.values() if _g(t, "handovers", None)]
    if handover_trends:
        handover_trends.sort(key=lambda t: (-len(_g(t, "handovers", None) or []),
                                           str(_g(t, "metric", ""))))
        body.append("<h3>" + _e(_t("跨设备可比性", "Cross-device comparability", lang)) + "</h3>")
        body.append("<p>" + _t(
            "以下指标的时间线里存在<strong>设备接替</strong>（Apple 不标注这件事）。"
            "不同设备甚至同一设备换算法版本后，对同一个生理量的基线读数并不相同，"
            "所以曲线在换设备处的台阶很可能是<strong>测量变了</strong>而不是身体变了。"
            "读这些趋势时，只在同一台设备的时段内比较；趋势图上的竖直虚线就是这些切换点。",
            "The following metrics have a device handover in their timeline; compare trends only "
            "within a single device's period.", lang) + "</p>")
        items = []
        for t in handover_trends[:8]:
            new, old, day = (list(_g(t, "handovers", None)) + [("", "", "")])[0][:3]
            extra = _t("（共 {0} 处）".format(len(_g(t, "handovers", None) or [])),
                       " ({0} handovers)".format(len(_g(t, "handovers", None) or []))
                       ) if len(_g(t, "handovers", None) or []) > 1 else ""
            items.append(_t(
                "<li><strong>{0}</strong>：{1} 前后从「{2}」换到「{3}」{4}</li>".format(
                    _e(_cn_of(str(_g(t, "metric", "")), t)), _e(day), _e(ctx.src(old)),
                    _e(ctx.src(new)), _e(extra)),
                "<li><strong>{0}</strong>: switched around {1}{2}</li>".format(
                    _e(_cn_of(str(_g(t, "metric", "")), t)), _e(day), _e(extra)), lang))
        body.append("<ul>" + "".join(items) + "</ul>")
        if len(handover_trends) > 8:
            body.append("<p>" + _e(_t("另有 {0} 个指标同理。".format(len(handover_trends) - 8),
                                      "{0} more metrics are affected.".format(
                                          len(handover_trends) - 8), lang)) + "</p>")

    body.append('<p class="small">' + _e(_t(
        "以上结论全部来自确定性计算，可以逐条核对第二节的数值。",
        "Every conclusion above comes from deterministic computation.", lang)) + "</p>")
    return _section("s-rules", _t("七、规则层结论", "7. Rule-based conclusions", ctx.lang),
                    "".join(body))


def _sec_ai(ctx: _Ctx, ai_text: str, ai_model: str, ai_note: str) -> str:
    lang = ctx.lang
    text = (ai_text or "").strip()
    note = (ai_note or "").strip()
    body: List[str] = []
    if text:
        model = _e(ai_model) if ai_model else _e(_t("未注明", "not specified", lang))
        body.append('<p class="small">' + _t(
            "模型：{0}。以下内容由语言模型生成，仅解释上面的数值，不参与计算，"
            "也不改变第一到第七节的结论；其中的数字由 llm.py 做过溯源校验，"
            "本页原样呈现，不再重新计算。".format(model),
            "Model: {0}. The text below was generated by a language model; it explains the "
            "numbers above but does not compute anything.".format(model), lang) + "</p>")
        body.append(_md_to_html(text, lang))
        if note:
            body.append("<blockquote><p>" + _e(note) + "</p></blockquote>")
        return _section("s-ai", _t("八、AI 解读", "8. AI interpretation", ctx.lang), "".join(body))

    body.append("<p>" + _e(_t("本节为空：没有生成 AI 解读，所以这里没有任何文字。",
                              "This section is empty: no AI interpretation was generated.",
                              lang)) + "</p>")
    if note:
        body.append("<blockquote><p>" + _e(note) + "</p></blockquote>")
    else:
        body.append("<p>" + _e(_t(
            "原因通常是：没有配置模型接口（默认完全离线运行），或者后端连不上、超时。",
            "Usually because no model endpoint is configured (the default is fully offline), or "
            "the backend was unreachable.", lang)) + "</p>")
    body.append("<p>" + _e(_t("开启方法（可选，纯本地运行不需要联网）：",
                              "How to enable it (optional; fully local use needs no network):",
                              lang)) + "</p>")
    body.append("<ul>"
                "<li>" + _t("设置环境变量 <code>HEALTH_REPORT_API_KEY</code>（必需，填你自己的接口密钥）；",
                            "Set <code>HEALTH_REPORT_API_KEY</code>;", lang) + "</li>"
                "<li>" + _t("可选 <code>HEALTH_REPORT_BASE_URL</code>（OpenAI 兼容接口地址，"
                            "默认 <code>api.openai.com/v1</code>，填什么就用什么，末尾斜杠会被去掉）；",
                            "optionally <code>HEALTH_REPORT_BASE_URL</code>;", lang) + "</li>"
                "<li>" + _t("可选 <code>HEALTH_REPORT_MODEL</code>（默认 <code>gpt-4o-mini</code>）"
                            "与 <code>HEALTH_REPORT_TIMEOUT</code>。",
                            "optionally <code>HEALTH_REPORT_MODEL</code> and "
                            "<code>HEALTH_REPORT_TIMEOUT</code>.", lang) + "</li>"
                "</ul>")
    body.append('<div class="callout note"><p>' + _t(
        "<strong>不开启也是完整的。</strong>第一节到第七节的每一个数字和每一句结论都由本地"
        "确定性计算得出。AI 解读只是在它们之上多写一段白话，外加一节「下一步」——"
        "而那一节被限制在「改善数据可比性」的范围内（固定设备、统一测量条件、增加数据点），"
        "不给医疗或营养上的具体方案。关掉不影响报告的正确性与可用性。",
        "<strong>The report is complete without it.</strong> Sections 1-7 come from local "
        "deterministic computation; the AI section only adds plain-language explanation.",
        lang) + "</p></div>")
    return _section("s-ai", _t("八、AI 解读", "8. AI interpretation", ctx.lang), "".join(body))


def _appendix_a(ctx: _Ctx) -> str:
    """附录 A：方法与口径。

    正文**复用 `report._method_section()`**：口径这种话只应该有一个来源，
    复制一份到 HTML 里早晚会和 Markdown 版说不到一起去。
    """
    bullets = _md_bullets(_method_section(ctx.lang))
    body = ['<p class="lede">' + _e(_t(
        "这一节说明报告里的数字是怎么来的，以及哪些地方刻意不下结论。",
        "How the numbers are produced, and where the report deliberately refuses to conclude.",
        ctx.lang)) + "</p>", bullets]
    body.append('<div class="callout danger"><p>' + _e(DISCLAIMER) + "</p></div>")
    return _section("s-methods", _t("附录 A：方法与口径", "Appendix A: Methods and conventions",
                                    ctx.lang), "".join(body))


def _appendix_b(ctx: _Ctx) -> str:
    ids = [m for m in ctx.hidden_ids if m in ctx.trends]
    body = ['<p class="lede">' + _e(_t(
        "这里包含主表没有列出的两类指标：生物电阻抗（设备原始读数，它的漂移会解释体成分读数的漂移）"
        "与设备建议与评分（设备自算结果，不参与范围判定）。它们不画趋势图："
        "斜率在这里没有解释意义，明细表足够。",
        "Two categories excluded from the main table are listed here; they are not charted.",
        ctx.lang)) + "</p>"]
    if ids:
        body.append(_metric_table(ctx, [ctx.trends[m] for m in ids]))
    else:
        body.append(_nodata(_t("本次没有设备原始读数或设备建议类指标。",
                               "no bioimpedance or device-generated metrics in this dataset.",
                               ctx.lang), ctx.lang))
    body.append('<div class="callout danger"><p>' + _e(DISCLAIMER) + "</p></div>")
    return _section("s-appendix-b", _t("附录 B：全部指标明细", "Appendix B: All metrics in detail",
                                       ctx.lang), "".join(body))


# ──────────────────────────────────────────────────────────── 入口


def build_html(dataset: Dataset, analysis: Any, findings: list, *,
               lang: str = "zh",
               ai_text: str = "", ai_model: str = "", ai_note: str = "",
               title: str = "健康数据报告", include_appendix: bool = True,
               generated_at: Optional[str] = None,
               mask_sources: bool = True) -> str:
    """产出一份**自包含**的 HTML 报告（完整文档字符串，`<!DOCTYPE html>` 开头）。

    参数
    ----
    dataset / analysis / findings
        与 `report.build_report` 完全相同的输入。这里不重新计算任何结论。
    lang
        `"zh"`（默认）或 `"en"`：切换小节标题、表头与判定措辞（判定措辞同样走
        `report._verdict_text`，所以两种语言的区分口径一致）。
    ai_text / ai_model / ai_note
        AI 解读。`ai_text` 为空时明确写出「没有生成」以及可能的原因；
        `ai_note` 无论哪种情况都会显示出来。
    title
        封面标题，同时作为 `<title>`。
    include_appendix
        是否输出**附录 B：全部指标明细**（与 Markdown 版的同名参数一致）。
        附录 A（方法与口径）与免责声明永远输出。
    generated_at
        **输出里唯一非确定性的内容**。默认取当前时间（`report._now()`）；
        测试与 CI 传一个固定值，就能让两次调用逐字节相同。
        这一行单独占一个 `<p class="generated">`，方便按行剔除或替换。
    mask_sources
        是否对来源名再过一次 `privacy.mask_source_name()`（默认 True，幂等）。
        解析器默认已经脱敏；这里是防线：手工构造的 `Dataset` 或 `--show-source-names`
        调试时，也不会把「某某的 Apple Watch」这类含机主姓名的设备名写进要分享的 HTML。
        需要原样显示时（仅本地调试）传 False。

    为什么把生成时间做成参数而不是常量：报告的价值之一是**可 diff**。
    Markdown 版用 `grep -v '^生成时间：'` 剔除这一行后比对，HTML 版同样可以
    （这一行单独成行），但更省事的做法是 `generated_at=` 直接把它钉死。
    """
    ds = dataset if dataset is not None else Dataset()
    trends = _trends_of(analysis)
    fs = _findings_of(findings)
    order = _ordered_metrics(analysis, trends)
    main_ids = [m for m in order
                if _category_of(m, trends.get(m)) not in HIDDEN_CATEGORIES]
    hidden_ids = [m for m in order
                  if _category_of(m, trends.get(m)) in HIDDEN_CATEGORIES]
    span = _data_span(ds, analysis)
    days = _span_days(ds, analysis, span)
    ctx = _Ctx(
        ds=ds, analysis=analysis, findings=fs, trends=trends, order=order,
        main_ids=main_ids, hidden_ids=hidden_ids,
        lang=str(lang or "zh"), mask=bool(mask_sources),
        span=span, span_days=days,
        by_metric=_by_metric(ds), cache={},
    )
    generated = str(generated_at) if generated_at is not None else _now()
    print_hint = ('<div class="screen-only">{0}</div>\n').format(_e(_t(
        "打印成 PDF：Ctrl+P → 目标「另存为 PDF」→ 纸张 A4、边距默认、勾选「背景图形」；"
        "页码用浏览器的「页眉和页脚」选项（样式表里的页码规则只有分页引擎会读）。"
        "本提示只在屏幕上显示，不会出现在打印结果里。",
        "Print to PDF: Ctrl+P, destination \"Save as PDF\", A4, default margins, background "
        "graphics on. This hint is screen-only.", ctx.lang)))

    parts = [
        '<!DOCTYPE html>\n<html lang="{0}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>{1}</title>\n<style>{2}</style>\n</head>\n<body>\n'.format(
            "en" if str(ctx.lang).lower().startswith("en") else "zh", _e(title), _CSS),
        print_hint,
        _cover(ctx, title, _GEN_TOKEN),
        _sec_credibility(ctx),
        _sec_overview(ctx),
        _sec_sleep(ctx),
        _sec_recovery(ctx),
        _sec_assoc(ctx),
        _sec_activity(ctx),
        _sec_rules(ctx),
        _sec_ai(ctx, ai_text, ai_model, ai_note),
        _appendix_a(ctx),
    ]
    if include_appendix:
        parts.append(_appendix_b(ctx))
    parts.append("</body>\n</html>")
    # 最后一道隐私防线：把「日期 + 时分秒」一律降级成日期（生成时间那行用占位符挡着）。
    text = _TS_SCRUB.sub(r"\1", "".join(parts))
    return text.replace(_GEN_TOKEN, generated).rstrip()
