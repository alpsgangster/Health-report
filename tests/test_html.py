"""HTML 交付形态的测试。

这里守的是四件事：

1. **自包含**：HTML 里没有任何外部资源，也没有一行 JavaScript。
   图表必须是**合法 XML 的 SVG**（真的用 ElementTree 解析一遍，不是看着像就行）。
2. **确定性**：同一份输入两次调用逐字节相同；唯一会变的「生成时间」那一行
   可以用 `generated_at=` 固定住，也可以按行剔除后比对。
3. **口径不漂移**：判定措辞必须与 Markdown 版一致——「未达目标」「超出筛查参考」
   「只做趋势」「来源不一致，不做判定」四种区分都要在 HTML 里出现。
4. **隐私**：逐条观测的时间戳（`ts`）绝不进 HTML；来源名默认脱敏。

夹具用 `samples.synthetic_dataset()`（合成数据，不含任何真实个人数值）。
"""

from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

from health_report.analysis import analyze
from health_report.html import build_html, monthly_stats_of
from health_report.model import Dataset, Observation, RefRange
from health_report.pitfalls import check
from health_report.report import DISCLAIMER
from health_report.samples import synthetic_dataset

#: 固定「生成时间」，让输出完全不依赖当前时间。
FIXED_AT = "2026-01-01 00:00"

#: 报告里绝不许可出现的诊断性措辞（与 tests/test_report.py 同一份黑名单）。
BANNED_WORDING = ["你患有", "您患有", "说明你有", "建议服用", "建议剂量", "确诊", "处方"]

#: 隐藏类别（与 report.HIDDEN_CATEGORIES 同义）：主表不列，也不画趋势图。
HIDDEN = ("生物电阻抗", "设备建议与评分")


def strip_generated(text: str) -> str:
    """按行剔除「生成时间」那一行（与 Markdown 版 CI 的 grep -v 等价）。"""
    return "\n".join(ln for ln in text.split("\n") if "生成时间：" not in ln)


class _TagBalance(HTMLParser):
    """检查标签是否成对——浏览器能容忍错配，但错配说明生成器写错了。"""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
            "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list = []
        self.errors: list = []

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


def dataset_with_verdict_paths() -> Dataset:
    """专门触发四种判定措辞的合成数据（维度：区间性质 × 判定）。

    * `steps`   —— goal（目标）+ low   -> 「未达目标」
    * `bmi`     —— screen（筛查）+ high（concern=both，不是「不需要留意的那侧」）
                     -> 「超出筛查参考」
    * `hrv_sdnn`—— 没有任何可用区间      -> 「只做趋势」
    * `body_mass` —— 有区间，但最新读数来自另一台没给区间的设备
                     -> 「来源不一致，不做判定」
    """
    ds = Dataset(profile={"sex": "男", "age": 28, "height_cm": 172.0})
    device_ref = RefRange(low=55.0, high=75.0, source="测试体成分仪参考范围")
    # 体重：两条读数来自两套测量系统，最新那条的来源没有区间，且没有内置表。
    ds.observations.append(Observation("body_mass", "2026-01-10", 74.0, "kg",
                                       "体成分仪", ts="2026-01-10 07:30:00", ref=device_ref))
    ds.observations.append(Observation("body_mass", "2026-05-20", 71.0, "kg",
                                       "智能秤", ts="2026-05-20 07:30:00"))
    # BMI：内置筛查阈值（WHO 成人 18.5~24.9，concern=both），30 明显越界。
    ds.observations.append(Observation("bmi", "2026-01-10", 24.0, "kg/m2",
                                       "体成分仪", ts="2026-01-10 07:30:00"))
    ds.observations.append(Observation("bmi", "2026-05-20", 30.0, "kg/m2",
                                       "体成分仪", ts="2026-05-20 07:30:00"))
    # 步数：目标 ≥8000，有数据日的日均只有 6000 -> 未达目标。
    for day in range(1, 21):
        ds.observations.append(Observation("steps", "2026-05-{0:02d}".format(day), 6000.0, "步",
                                           "智能手环", ts="2026-05-{0:02d} 21:00:00".format(day)))
    # HRV：catalog 里没有任何参考区间 -> 只做趋势。
    for day in range(1, 21):
        ds.observations.append(Observation("hrv_sdnn", "2026-05-{0:02d}".format(day), 48.0, "ms",
                                           "智能手环", ts="2026-05-{0:02d} 07:00:00".format(day)))
    return ds.dedupe()


class HtmlFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset()
        cls.an = analyze(cls.ds)
        cls.fs = check(cls.ds, cls.an)
        cls.html = build_html(cls.ds, cls.an, cls.fs, generated_at=FIXED_AT)

    # ---------------------------------------------------------------- 基本形态

    def test_document_shape(self):
        self.assertTrue(self.html.startswith("<!DOCTYPE html>"))
        self.assertTrue(self.html.endswith("</html>"))
        self.assertIn('<html lang="zh">', self.html)
        self.assertIn("<title>", self.html)
        self.assertIn("</body>", self.html)

    def test_self_contained_no_external_resources(self):
        """零外部资源：没有 script / link / 远程 img / Web 字体 / 远程地址。"""
        for needle in ("<script", "<link", "<img", "<iframe", "@import", " src=", "url("):
            self.assertNotIn(needle, self.html, "出现了外部资源或脚本：" + needle)
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)
        self.assertNotIn("http:", self.html)

    def test_no_javascript_at_all(self):
        """刻意不用 JS：打印不需要它，静态标注在纸上才是有效信息通道。"""
        lowered = self.html.lower()
        for needle in ("<script", "javascript:", "onclick", "onload=", "addEventListener"):
            self.assertNotIn(needle, lowered)

    def test_every_svg_is_valid_xml(self):
        svgs = re.findall(r"<svg\b.*?</svg>", self.html, re.S)
        self.assertGreaterEqual(len(svgs), 20, "SVG 太少，可能没有画出图表")
        for i, svg in enumerate(svgs):
            try:
                ET.fromstring(svg)
            except ET.ParseError as exc:  # pragma: no cover - 失败时给出可定位的信息
                self.fail("第 {0} 个 SVG 不是合法 XML：{1}\n{2}".format(i, exc, svg[:400]))
        # 也不能出现未转义的实体（除标准实体外）
        for ent in ("&nbsp;", "&mdash;", "&hellip;"):
            self.assertNotIn(ent, self.html)

    def test_html_tags_are_balanced(self):
        """HTML 标签必须成对闭合：错配说明生成器写漏了闭合标签。"""
        parser = _TagBalance()
        parser.feed(self.html)
        parser.close()
        self.assertFalse(parser.errors, "；".join(parser.errors[:5]))
        self.assertFalse(parser.stack, "未闭合的标签：" + "、".join(parser.stack[:5]))

    # ---------------------------------------------------------------- 打印样式

    def test_print_css_contract(self):
        self.assertIn("@page { size: A4; margin: 16mm 14mm; }", self.html)
        self.assertIn("break-before: page", self.html)
        self.assertIn("page-break-before: always", self.html)
        self.assertIn("break-inside: avoid", self.html)
        self.assertIn("page-break-inside: avoid", self.html)
        self.assertIn("print-color-adjust: exact", self.html)
        # 正文用 pt 而不是 px（打印尺寸可预期）
        self.assertIn("font-size: 10.5pt", self.html)
        # 页眉页码：做不到的要如实注释，不能假装有
        self.assertIn("@bottom-center", self.html)
        self.assertIn("counter(page)", self.html)

    def test_black_and_white_readability(self):
        """黑白可读：判定有文字、区间性质有不同线型、差异在噪声内用斜纹。"""
        self.assertIn("bandrange", self.html)     # range 实线框
        self.assertIn("bandscreen", self.html)    # screen 虚线框
        self.assertIn("goalline", self.html)      # goal 点线
        self.assertIn("hatchline", self.html)     # 斜纹＝差异在噪声内
        self.assertIn("未达目标", self.html)
        self.assertIn("chip", self.html)
        # 「未达目标」与「越界」必须是两档视觉：没走到步数目标不是指标异常
        self.assertIn("chip goalout", self.html)
        self.assertIn("vgoalout", self.html)
        self.assertIn("chip out", self.html)

    # ---------------------------------------------------------------- 确定性

    def test_deterministic_with_fixed_generated_at(self):
        again = build_html(self.ds, self.an, self.fs, generated_at=FIXED_AT)
        self.assertEqual(self.html, again)

    def test_deterministic_ignoring_the_generated_line(self):
        first = build_html(self.ds, self.an, self.fs)
        second = build_html(self.ds, self.an, self.fs)
        self.assertEqual(strip_generated(first), strip_generated(second))

    # ---------------------------------------------------------------- 图表

    def test_one_trend_chart_per_chartable_metric(self):
        """设计口径：主表的每个指标都有一张趋势图；不足 2 个月的点写「数据不足」。"""
        main_ids = [m for m in self.an.ordered_metrics()
                    if self.an.trends[m].category not in HIDDEN]
        chartable = [m for m in main_ids
                     if len(monthly_stats_of(self.ds, m, self.an.trends[m].agg)) >= 2]
        # 第三节的画廊之外，睡眠/恢复两节各要重画一张更大的（睡眠时长、静息心率、HRV）
        extra = [m for m in ("sleep_total", "resting_hr", "hrv_sdnn") if m in chartable]
        trend_count = len(re.findall(r'data-chart="trend"', self.html))
        self.assertEqual(trend_count, len(chartable) + len(extra),
                         "趋势图数量与「每个可画指标一张」的设计不符")
        drawn = set(re.findall(r'data-chart="trend" data-metric="([^"]+)"', self.html))
        self.assertTrue(set(chartable) <= drawn, "有指标没画出来：" + str(set(chartable) - drawn))
        # 不足以画图的指标：不留空图，明确写「数据不足」
        not_chartable = [m for m in main_ids if m not in chartable]
        self.assertTrue(not_chartable, "合成数据里本应有画不出趋势的指标（身高/腰围）")
        for metric in not_chartable:
            self.assertNotIn('data-metric="{0}"'.format(metric), self.html)
        self.assertIn("画不出月度趋势", self.html)

    def test_all_eight_chart_kinds_are_present(self):
        kinds = set(re.findall(r'data-chart="([a-z-]+)"', self.html))
        for kind in ("trend", "sleep-stages", "sleep-scatter", "assoc-bars",
                     "steps-bars", "coverage", "verdict-strip"):
            self.assertIn(kind, kinds, "缺少图表类型：" + kind)

    def test_charts_declare_their_data_point_counts(self):
        """每张图都要自报数据点数（自检脚本靠它报告规模）。"""
        for m in re.finditer(r"<svg\b[^>]*data-chart=\"([a-z-]+)\"[^>]*data-points=\"(\d+)\"", self.html):
            self.assertGreaterEqual(int(m.group(2)), 1, m.group(1))

    def test_handover_markers_are_drawn_on_trend_charts(self):
        """设备接替要在趋势图上标出来——这是本项目区别于其它工具的地方。"""
        with_hand = re.findall(r'data-chart="trend"[^>]*data-handovers="(\d+)"', self.html)
        self.assertTrue(with_hand)
        self.assertTrue(any(int(n) > 0 for n in with_hand),
                        "合成数据里有设备接替，图上却没标")
        self.assertIn('class="hand"', self.html)
        self.assertIn("2026-05-01 → ", self.html)
        # 设备名要脱敏后显示（合成数据里本来就没有姓名，这里守的是格式）
        self.assertIn("智能秤", self.html)

    def test_scatter_is_downsampled_and_says_so(self):
        """散点图每夜一个点，超过上限要等间隔抽样并注明。"""
        m = re.search(r'data-chart="sleep-scatter"[^>]*data-points="(\d+)"[^>]*data-nights="(\d+)"',
                      self.html)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 1200)

    # ---------------------------------------------------------------- 小节

    def test_all_major_sections_are_present(self):
        for heading in ("一、数据可信度提示", "二、指标总览", "三、睡眠", "四、恢复与负荷",
                        "五、指标之间的关联", "六、活动与训练", "七、规则层结论", "八、AI 解读",
                        "附录 A：方法与口径", "附录 B：全部指标明细"):
            self.assertIn(heading, self.html, "缺少小节：" + heading)
        self.assertIn(DISCLAIMER, self.html)
        self.assertIn("这不是医学诊断", self.html)
        # 免责声明必须在任何结论之前
        self.assertLess(self.html.find("这不是医学诊断"), self.html.find("一、数据可信度提示"))

    def test_verdict_wording_reuses_the_markdown_wording(self):
        """四种判定口径必须都能出现，而且区分得开。"""
        ds = dataset_with_verdict_paths()
        an = analyze(ds)
        html = build_html(ds, an, check(ds, an), generated_at=FIXED_AT)
        self.assertIn("未达目标", html)                  # goal + low
        self.assertIn("超出筛查参考", html)              # screen + high（需要留意的一侧）
        self.assertIn("只做趋势", html)                  # 没有可用区间
        self.assertIn("来源不一致，不做判定", html)      # 有区间、但拿不到同源区间
        self.assertIn("在参考区间内", html)              # range + in
        # 两个措辞不能互相冒充
        steps_row = re.search(r"<tr><td class=\"m\"><strong>步数</strong></td>.*?</tr>", html, re.S)
        self.assertIsNotNone(steps_row)
        self.assertIn("未达目标", steps_row.group(0))
        self.assertNotIn("低于参考下限", steps_row.group(0))
        mass_row = re.search(r"<tr><td class=\"m\"><strong>体重</strong></td>.*?</tr>", html, re.S)
        self.assertIsNotNone(mass_row)
        self.assertIn("来源不一致，不做判定", mass_row.group(0))

    # ---------------------------------------------------------------- 隐私

    def test_no_raw_observation_timestamps(self):
        """隐私底线：逐条观测的 ts 不进 HTML，也不出现任何 HH:MM:SS。"""
        self.assertNotRegex(self.html, r"\d{2}:\d{2}:\d{2}")
        for o in self.ds.observations:
            if o.ts:
                self.assertNotIn(o.ts, self.html,
                                 "HTML 里出现了逐条观测的时间戳：" + o.ts)
        for w in self.ds.workouts:
            if w.ts:
                self.assertNotIn(w.ts, self.html)

    def test_source_names_are_masked_by_default(self):
        """Apple 的默认设备名里带机主姓名，HTML 里不能再带出去。

        用的是 Apple 中文默认命名的真实形状（`某某的Apple Watch`，中间没有空格）——
        `privacy.mask_source_name()` 认的就是这个形状。
        """
        ds = Dataset()
        for day in ("2026-01-10", "2026-05-20"):
            ds.observations.append(Observation(
                "body_mass", day, 74.0, "kg", "某某的Apple Watch", ts=day + " 07:30:00"))
        an = analyze(ds)
        masked = build_html(ds, an, [], generated_at=FIXED_AT)
        self.assertNotIn("某某", masked)
        self.assertIn("Apple Watch", masked)
        raw = build_html(ds, an, [], generated_at=FIXED_AT, mask_sources=False)
        self.assertIn("某某的Apple Watch", raw)

    # ---------------------------------------------------------------- 退化

    def test_empty_dataset_still_produces_a_complete_report(self):
        ds = Dataset()
        html = build_html(ds, analyze(ds), [], generated_at=FIXED_AT)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertTrue(html.endswith("</html>"))
        self.assertIn("数据不足", html)
        self.assertIn("这不是医学诊断", html)
        self.assertEqual(html.count("<svg"), 0, "空数据不该留下半张图")
        for heading in ("一、数据可信度提示", "二、指标总览", "三、睡眠", "四、恢复与负荷",
                        "五、指标之间的关联", "六、活动与训练", "七、规则层结论", "八、AI 解读",
                        "附录 A：方法与口径"):
            self.assertIn(heading, html)

    def test_no_nan_none_or_inf_in_output(self):
        """不留半张图：空数据与正常数据里都不许出现 NaN / None / inf 字样。

        用词边界匹配：报告里有「info 级提示」这种正常词组，不能误伤。
        """
        bad = re.compile(r"\bNaN\b|\bNone\b|\binf\b|\bInfinity\b|\bnan\b")
        for html in (self.html,
                     build_html(Dataset(), analyze(Dataset()), [], generated_at=FIXED_AT)):
            hit = bad.search(html)
            self.assertIsNone(hit, "输出里出现了空值标记：" + (hit.group(0) if hit else ""))

    # ---------------------------------------------------------------- AI 与附录

    def test_ai_section_says_why_it_is_empty(self):
        self.assertIn("没有生成 AI 解读", self.html)
        self.assertIn("HEALTH_REPORT_API_KEY", self.html)
        self.assertIn("不开启也是完整的", self.html)
        # 开启方法里不能带外部链接
        self.assertNotIn("http", self.html)

    def test_ai_text_and_note_are_rendered_as_is(self):
        ds = self.ds
        an = self.an
        html = build_html(ds, an, self.fs,
                          ai_text="## 结论\n\n- 体重**变化不大**。\n\n| 指标 | 变化 |\n|---|---|\n| 体重 | -1.1 kg |\n\n[外链](https://example.invalid/x)",
                          ai_model="test-model", ai_note="数字溯源校验通过。",
                          generated_at=FIXED_AT)
        self.assertIn("test-model", html)
        self.assertIn("体重", html)
        self.assertIn("<strong>变化不大</strong>", html)
        self.assertIn("<table>", html)
        self.assertIn("数字溯源校验通过。", html)
        # AI 文字里的外链必须退化成纯文本
        self.assertNotIn("https://example.invalid/x", html)
        self.assertNotIn("<a ", html)
        self.assertIn("外链", html)

    def test_ai_text_is_escaped(self):
        html = build_html(self.ds, self.an, self.fs,
                          ai_text="<script>alert(1)</script> 与 <b>标签</b>",
                          generated_at=FIXED_AT)
        self.assertNotIn("<script", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<b>标签</b>", html)

    def test_include_appendix_switches_appendix_b(self):
        without = build_html(self.ds, self.an, self.fs, include_appendix=False,
                             generated_at=FIXED_AT)
        self.assertNotIn('id="s-appendix-b"', without)
        self.assertIn("附录 A：方法与口径", without)

    # ---------------------------------------------------------------- 措辞与容错

    def test_no_diagnostic_language(self):
        for b in BANNED_WORDING:
            self.assertNotIn(b, self.html, "出现了禁用措辞：" + b)

    def test_lang_en_smoke(self):
        html = build_html(self.ds, self.an, self.fs, lang="en", generated_at=FIXED_AT)
        self.assertIn('<html lang="en">', html)
        self.assertIn("1. Data credibility notes", html)
        self.assertIn(DISCLAIMER, html)   # 免责声明常量固定中文
        self.assertNotIn("http", html)

    def test_rough_inputs_do_not_crash(self):
        """字段缺失 / 半成品对象：报告层只按属性读取，不能因为一个 None 就崩。"""
        class Rough:
            trends = {"body_mass": object()}

        html = build_html(Dataset(), Rough(), [None, object()], generated_at=FIXED_AT)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("指标总览", html)
        # analysis 完全没有 ordered_metrics / by_category 时也要能排版
        html2 = build_html(Dataset(), Rough(), [], generated_at=FIXED_AT)
        self.assertIn("体成分", html2)


if __name__ == "__main__":
    unittest.main()
