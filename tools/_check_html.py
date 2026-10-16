#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTML 交付形态的自检脚本（标准库，无第三方依赖）。

用法：
    cd 06_HealthReport
    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    python tools/_check_html.py              # 合成数据 + 空数据 + 真实导出（存在就跑）
    python tools/_check_html.py --no-real    # 只跑合成数据与空数据（几秒）

退出码 0 = 全部通过；非零 = 有失败项，失败项列在末尾。

它检查什么
----------
1. **合成数据**（`samples.synthetic_dataset()`，不含任何真实个人数值）：
   文档形态、自包含（无 script/link/img/http）、每个 `<svg>` 都能被
   `ElementTree.fromstring` 解析、图表清点（每张图的类型与数据点数）、
   各小节存在、四种判定措辞、打印样式契约、确定性（两次调用逐字节相同）。
2. **空数据**：`Dataset()` 也要出一份完整 HTML，且明确写「数据不足」、
   不留半张空图、不出现 NaN / None / inf。
3. **真实导出**（只读，默认 `..\\apple_health_export\\导出.xml`）：
   计时解析与生成，报告 HTML 字节数、`<svg>` 数量、每张图的数据点数、
   各小节是否存在、是否含逐条观测时间戳（隐私底线）。
   顺便把真实数据的 HTML 写到 `out/health_report.html`（out/ 已在 .gitignore 里）。

隐私：本脚本自身不写死任何真实数值；真实数据只从导出文件里读出来再比对。
"""

from __future__ import annotations

import os
import re
import sys
import time
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

from health_report import privacy  # noqa: E402
from health_report.analysis import analyze  # noqa: E402
from health_report.html import build_html, monthly_stats_of  # noqa: E402
from health_report.model import Dataset, Observation, RefRange  # noqa: E402
# 注意：本文件里的 check() 是断言助手，所以规则层入口改个名字导入。
from health_report.pitfalls import check as run_rules  # noqa: E402
from health_report.report import DISCLAIMER  # noqa: E402
from health_report.samples import synthetic_dataset  # noqa: E402

FIXED_AT = "2000-01-01 00:00"   # 固定生成时间：让整份输出可逐字节比对
HIDDEN = ("生物电阻抗", "设备建议与评分")
SECTIONS = ("一、数据可信度提示", "二、指标总览", "三、睡眠", "四、恢复与负荷",
            "五、指标之间的关联", "六、活动与训练", "七、规则层结论", "八、AI 解读",
            "附录 A：方法与口径", "附录 B：全部指标明细")
BAD_VALUE = re.compile(r"\bNaN\b|\bNone\b|\binf\b|\bInfinity\b")
TS_PATTERN = re.compile(r"\d{2}:\d{2}:\d{2}")

REAL_CANDIDATES = (
    os.path.join(_ROOT, "..", "apple_health_export", "导出.xml"),
    os.path.join(_ROOT, "..", "apple_health_export", "export.xml"),
    os.path.join(_ROOT, "..", "02_Apple健康数据", "样例_export.xml"),
)
OUT_HTML = os.path.join(_ROOT, "out", "health_report.html")

_RESULTS = []


def check(label: str, ok: bool, extra: str = "") -> bool:
    _RESULTS.append((label, bool(ok)))
    tail = ("　→ " + str(extra)[:400]) if (extra and not ok) else ""
    print("  {0} {1}{2}".format("✅" if ok else "❌", label, tail))
    return bool(ok)


class _TagBalance(HTMLParser):
    """HTML 标签成对性检查（浏览器能容忍错配，错配说明生成器写漏了闭合标签）。"""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack:
            self.errors.append("多余的 </{0}>".format(tag))
            return
        if self.stack[-1] != tag:
            self.errors.append("</{0}> 与未关闭的 <{1}> 不匹配".format(tag, self.stack[-1]))
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()


def info(label: str, value: str) -> None:
    print("     · {0}：{1}".format(label, value))


def section(title: str) -> None:
    print()
    print("── " + title)


def svg_inventory(html: str) -> dict:
    """按类型清点 `<svg>`，并逐张记下自报的 data-* 元信息。"""
    out = {}
    for m in re.finditer(r"<svg\b([^>]*)>", html):
        attrs = m.group(1)

        def grab(name: str, default: int = -1) -> int:
            hit = re.search(r'data-{0}="([^"]+)"'.format(name), attrs)
            if not hit:
                return default
            try:
                return int(hit.group(1))
            except ValueError:
                return default

        metric = re.search(r'data-metric="([^"]+)"', attrs)
        kind = re.search(r'data-chart="([^"]+)"', attrs)
        out.setdefault(kind.group(1) if kind else "?", []).append({
            "metric": metric.group(1) if metric else "",
            "points": grab("points"),
            "days": grab("days"),
            "handovers": grab("handovers"),
        })
    return out


def report_inventory(html: str, verbose: bool = True) -> dict:
    inv = svg_inventory(html)
    total = sum(len(v) for v in inv.values())
    print("     · <svg> 总数：{0}".format(total))
    for kind in sorted(inv):
        items = inv[kind]
        pts = [i["points"] for i in items if i["points"] >= 0]
        span = "数据点 {0}~{1}".format(min(pts), max(pts)) if pts else "无数据点标注"
        print("       - {0}：{1} 张（{2}）".format(kind, len(items), span))
    if verbose:
        for kind in sorted(inv):
            for i in inv[kind]:
                extra = ""
                if i["days"] >= 0:
                    extra += " 天数={0}".format(i["days"])
                if i["handovers"] >= 0:
                    extra += " 设备接替={0}".format(i["handovers"])
                print("         [{0}] {1} 数据点={2}{3}".format(
                    kind, i["metric"] or "-", i["points"], extra))
    return inv


def privacy_check(label: str, html: str, ds: Dataset) -> None:
    """隐私底线：逐条观测的 ts 不进 HTML，也不出现任何 HH:MM:SS。"""
    leaked = []
    for item in list(getattr(ds, "observations", None) or []) + \
            list(getattr(ds, "workouts", None) or []):
        ts = str(getattr(item, "ts", "") or "")
        if len(ts) >= 8 and ts in html:
            leaked.append(ts)
            if len(leaked) >= 3:
                break
    hit = TS_PATTERN.search(html)
    check("{0}：HTML 里没有逐条观测的时间戳".format(label), not leaked,
          "泄漏：" + "、".join(leaked[:3]))
    check("{0}：HTML 里没有 HH:MM:SS 形状的时间".format(label), hit is None,
          "命中：" + (hit.group(0) if hit else ""))


def common_checks(label: str, html: str) -> None:
    check("{0}：以 <!DOCTYPE html> 开头".format(label), html.startswith("<!DOCTYPE html>"))
    check("{0}：以 </html> 结尾".format(label), html.endswith("</html>"))
    check("{0}：没有 <script>".format(label), "<script" not in html.lower())
    check("{0}：没有 <link> / <img> / @import".format(label),
          not any(x in html for x in ("<link", "<img", "@import")))
    check("{0}：没有 http:// 与 https://".format(label),
          "http://" not in html and "https://" not in html)
    hit = BAD_VALUE.search(html)
    check("{0}：没有 NaN / None / inf".format(label), hit is None,
          "命中：" + (hit.group(0) if hit else ""))
    bad = []
    svgs = re.findall(r"<svg\b.*?</svg>", html, re.S)
    for i, s in enumerate(svgs):
        try:
            ET.fromstring(s)
        except ET.ParseError as exc:
            bad.append("第 {0} 个：{1}".format(i, exc))
    check("{0}：{1} 个 <svg> 全部是合法 XML".format(label, len(svgs)), not bad,
          "；".join(bad[:3]))
    parser = _TagBalance()
    parser.feed(html)
    parser.close()
    check("{0}：HTML 标签全部成对闭合".format(label),
          not parser.errors and not parser.stack,
          "；".join((parser.errors + ["未闭合：" + "、".join(parser.stack[:5])])[:5]))


def print_css_checks(label: str, html: str) -> None:
    for needle, desc in (
        ("@page { size: A4; margin: 16mm 14mm; }", "A4 页边距"),
        ("break-before: page", "小节分页（新属性）"),
        ("page-break-before: always", "小节分页（旧属性）"),
        ("break-inside: avoid", "块内不断页（新属性）"),
        ("page-break-inside: avoid", "块内不断页（旧属性）"),
        ("print-color-adjust: exact", "浅底色打印保留"),
        ("counter(page)", "页码（分页引擎）"),
        ("bandrange", "range 实线框"),
        ("bandscreen", "screen 虚线框"),
        ("goalline", "goal 点线"),
        ("hatchline", "斜纹＝差异在噪声内"),
    ):
        check("{0}：样式契约「{1}」".format(label, desc), needle in html)


# ──────────────────────────────────────────────────────────── 夹具


def verdict_paths_dataset() -> Dataset:
    """触发四种判定措辞的合成数据（goal / screen / 无区间 / 来源不一致）。"""
    ds = Dataset(profile={"sex": "男", "age": 28, "height_cm": 172.0})
    ref = RefRange(low=55.0, high=75.0, source="合成参考范围")
    ds.observations.append(Observation("body_mass", "2026-01-10", 74.0, "kg", "体成分仪",
                                       ts="2026-01-10 07:30:00", ref=ref))
    ds.observations.append(Observation("body_mass", "2026-05-20", 71.0, "kg", "智能秤",
                                       ts="2026-05-20 07:30:00"))
    ds.observations.append(Observation("bmi", "2026-01-10", 24.0, "kg/m2", "体成分仪",
                                       ts="2026-01-10 07:30:00"))
    ds.observations.append(Observation("bmi", "2026-05-20", 30.0, "kg/m2", "体成分仪",
                                       ts="2026-05-20 07:30:00"))
    for day in range(1, 21):
        ds.observations.append(Observation("steps", "2026-05-{0:02d}".format(day), 6000.0,
                                           "步", "手环", ts="2026-05-{0:02d} 21:00:00".format(day)))
        ds.observations.append(Observation("hrv_sdnn", "2026-05-{0:02d}".format(day), 48.0,
                                           "ms", "手环", ts="2026-05-{0:02d} 07:00:00".format(day)))
    return ds.dedupe()


# ──────────────────────────────────────────────────────────── 三段验证


def verify_synthetic() -> None:
    section("验证 1：合成数据（samples.synthetic_dataset()）")
    ds = synthetic_dataset()
    an = analyze(ds)
    fs = run_rules(ds, an)
    t0 = time.time()
    html = build_html(ds, an, fs, generated_at=FIXED_AT)
    elapsed = time.time() - t0
    check("合成数据：生成 HTML 不抛异常", bool(html))
    size = len(html.encode("utf-8"))
    info("HTML 字节数", "{0:,}（{1:.1f} KB）".format(size, size / 1024.0))
    info("生成耗时", "{0:.3f} 秒".format(elapsed))
    common_checks("合成", html)
    print_css_checks("合成", html)
    for s in SECTIONS:
        check("合成：小节存在「{0}」".format(s), s in html)
    check("合成：含免责声明", DISCLAIMER in html)
    check("合成：免责声明在第一节之前",
          html.find(DISCLAIMER) < html.find("一、数据可信度提示"))
    for word in ("未达目标", "只做趋势", "来源不一致，不做判定"):
        check("合成：判定措辞「{0}」".format(word), word in html)
    privacy_check("合成", html, ds)

    print("     图表清点：")
    inv = report_inventory(html)
    main_ids = [m for m in an.ordered_metrics() if an.trends[m].category not in HIDDEN]
    chartable = [m for m in main_ids
                 if len(monthly_stats_of(ds, m, an.trends[m].agg)) >= 2]
    extra = [m for m in ("sleep_total", "resting_hr", "hrv_sdnn") if m in chartable]
    check("合成：趋势图数量＝可画指标数（{0}）＋睡眠/恢复重画（{1}）".format(
        len(chartable), len(extra)),
        len(inv.get("trend", [])) == len(chartable) + len(extra),
        "实际 {0}".format(len(inv.get("trend", []))))
    check("合成：画不出图的指标不留空图（写「数据不足」）", "画不出月度趋势" in html)
    check("合成：设备接替在趋势图上有标注",
          any(i["handovers"] > 0 for i in inv.get("trend", [])) and 'class="hand"' in html)
    check("合成：七类图表都在",
          all(k in inv for k in ("trend", "sleep-stages", "sleep-scatter", "assoc-bars",
                                 "steps-bars", "coverage", "verdict-strip")))

    ds2 = verdict_paths_dataset()
    an2 = analyze(ds2)
    html2 = build_html(ds2, an2, run_rules(ds2, an2), generated_at=FIXED_AT)
    for word in ("未达目标", "超出筛查参考", "只做趋势", "来源不一致，不做判定"):
        check("合成（判定路径夹具）：措辞「{0}」".format(word), word in html2)
    privacy_check("合成（判定路径夹具）", html2, ds2)

    check("合成：两次调用逐字节相同", html == build_html(ds, an, fs, generated_at=FIXED_AT))
    a = build_html(ds, an, fs)

    def strip(text: str) -> str:
        return "\n".join(ln for ln in text.split("\n") if "生成时间：" not in ln)

    check("合成：剔除「生成时间」行后仍然逐字节相同", strip(a) == strip(build_html(ds, an, fs)))


def verify_empty() -> None:
    section("验证 2：空数据（Dataset()）")
    ds = Dataset()
    an = analyze(ds)
    html = build_html(ds, an, [], generated_at=FIXED_AT)
    check("空数据：不抛异常且是完整文档",
          html.startswith("<!DOCTYPE html>") and html.endswith("</html>"))
    info("HTML 字节数", "{0:,}".format(len(html.encode("utf-8"))))
    check("空数据：明确写「数据不足」", "数据不足" in html)
    check("空数据：不留半张空图（0 个 <svg>）", html.count("<svg") == 0,
          "实际 {0}".format(html.count("<svg")))
    common_checks("空数据", html)
    for s in SECTIONS[:-1]:
        check("空数据：小节存在「{0}」".format(s), s in html)


def verify_real(path: str) -> None:
    section("验证 3：真实导出（只读）")
    info("输入", "{0}（{1:,} 字节）".format(path, os.path.getsize(path)))
    from health_report.parsers import parse_path

    t0 = time.time()
    ds = parse_path(path)
    t_parse = time.time() - t0
    info("解析耗时", "{0:.1f} 秒".format(t_parse))
    info("观测条数", "{0:,}".format(len(ds.observations)))
    info("原始记录条数", "{0:,}".format(int(getattr(ds, "n_records", 0) or 0)))
    info("训练条数", "{0:,}".format(len(ds.workouts)))
    info("数据来源", " / ".join(ds.all_sources()) or "（无）")

    t1 = time.time()
    an = analyze(ds)
    fs = run_rules(ds, an)
    info("分析耗时", "{0:.1f} 秒".format(time.time() - t1))

    t2 = time.time()
    html = build_html(ds, an, fs, title="健康数据报告")
    t_build = time.time() - t2
    size = len(html.encode("utf-8"))
    info("HTML 生成耗时", "{0:.2f} 秒".format(t_build))
    info("HTML 字节数", "{0:,}（{1:.1f} KB）".format(size, size / 1024.0))
    check("真实数据：HTML 生成耗时 < 3 秒", t_build < 3.0, "{0:.2f} 秒".format(t_build))
    common_checks("真实", html)
    print_css_checks("真实", html)
    for s in SECTIONS:
        check("真实：小节存在「{0}」".format(s), s in html)
    privacy_check("真实", html, ds)

    unmasked = [s for s in ds.all_sources()
                if privacy.looks_like_personal_name(s) and str(s) in html]
    check("真实：含机主姓名的来源名没有原样出现在 HTML 里", not unmasked,
          "泄漏：" + "；".join(unmasked[:3]))

    print("     图表清点（每张图的数据点数）：")
    report_inventory(html)

    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    with open(OUT_HTML, "w", encoding="utf-8", newline="") as f:
        f.write(html)
    info("已写出", OUT_HTML)

    check("真实：固定生成时间后两次生成逐字节相同",
          build_html(ds, an, fs, title="健康数据报告", generated_at=FIXED_AT)
          == build_html(ds, an, fs, title="健康数据报告", generated_at=FIXED_AT))


def main(argv: list) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    print("Python {0}".format(sys.version.split()[0]))
    verify_synthetic()
    verify_empty()

    if "--no-real" in argv:
        section("验证 3：真实导出（已用 --no-real 跳过）")
    else:
        real = next((p for p in REAL_CANDIDATES if os.path.exists(p)), "")
        if not real:
            section("验证 3：真实导出")
            print("  [--] SKIP：没有找到真实导出（找过：{0}）".format("；".join(REAL_CANDIDATES)))
        else:
            verify_real(real)

    failed = [name for name, ok in _RESULTS if not ok]
    print()
    print("=" * 72)
    print("共 {0} 项检查，通过 {1} 项，失败 {2} 项".format(
        len(_RESULTS), len(_RESULTS) - len(failed), len(failed)))
    for name in failed:
        print("  ❌ " + name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
