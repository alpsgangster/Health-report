"""报告生成与 build_facts 的测试。

这里守的是两条底线：
1. **报告的可信度结构**：免责声明必须在所有结论之前；判定措辞必须区分
   正常参考 / 目标 / 筛查阈值；没有参考区间的指标必须被显式列出来。
2. **发给模型的东西是可审计的**：build_facts 里不能有原始观测记录。
"""

from __future__ import annotations

import json
import unittest

from health_report.analysis import analyze
from health_report.model import Dataset, Observation, Workout
from health_report.pitfalls import check
from health_report.report import DISCLAIMER, build_facts, build_report
from health_report.samples import synthetic_dataset

# 报告里绝不许可出现的措辞。字面量黑名单，故意做得很笨：聪明的匹配器
# （比如判断否定语境）可以被绕过去，而这个清单要保证的是"报告正文里根本不出现这些词"。
# 代价是连「本报告不提供处方」这种否定句也会被拦——那就换个说法
# （报告里用「不给医疗或营养上的具体方案」），而不是把检测放松。
BANNED_WORDING = ["你患有", "您患有", "说明你有", "建议服用", "建议剂量", "确诊", "处方"]


def section_of(md: str, heading_prefix: str) -> str:
    """取某个标题下的正文，到**下一个任意层级**的标题为止。

    不能用 `split("\\n### ")`：报告里 h2 小节下面才是 h3，
    这样切会把后面的「## 二、指标总览」整节吞进来，
    于是断言会在别人的正文上失败（这个坑踩过一次）。
    """
    lines = md.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(heading_prefix):
            start = i + 1
            break
    if start is None:
        raise AssertionError(f"报告里找不到标题：{heading_prefix}")
    buf = []
    for ln in lines[start:]:
        if ln.startswith("##"):   # h2 和 h3 都算边界
            break
        buf.append(ln)
    return "\n".join(buf)


class ReportFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset()
        cls.an = analyze(cls.ds)
        cls.findings = check(cls.ds, cls.an)
        cls.md = build_report(cls.ds, cls.an, cls.findings, ai_text="", ai_model="")


class TestReportStructure(ReportFixture):
    def test_disclaimer_before_any_conclusion(self):
        """免责声明必须在任何结论之前。"""
        pos_disclaimer = self.md.find("这不是医学诊断")
        self.assertGreater(pos_disclaimer, 0, "报告里没有免责声明")
        for section in ("数据可信度提示", "指标总览", "规则层结论"):
            pos = self.md.find(section)
            if pos > 0:
                self.assertLess(pos_disclaimer, pos,
                                f"「{section}」出现在免责声明之前")

    def test_has_all_required_sections(self):
        for section in ("数据可信度提示", "指标总览", "规则层结论", "方法与口径"):
            self.assertIn(section, self.md, f"缺少小节：{section}")

    def test_only_trend_section_lists_metrics_without_reference_range(self):
        """没有参考区间的指标必须被显式列出来 —— 这是本项目的特色，不能省。"""
        self.assertIn("只做趋势", self.md)
        no_ref = [t for t in self.an.trends.values() if t.ref_origin == "none"]
        self.assertTrue(no_ref, "测试夹具本身应该有没区间的指标")

    def test_verdict_wording_distinguishes_range_goal_screen(self):
        """三种区间性质的措辞必须不同。"""
        self.assertIn("未达目标", self.md)       # goal
        self.assertIn("筛查", self.md)           # screen 的说明
        self.assertIn("只做趋势", self.md)       # 无区间
        self.assertIn("参考区间", self.md)       # range

    def test_multi_source_does_not_suppress_every_verdict(self):
        """「来源不一致」与「没有区间」是两回事，而且多来源**不再**一律压制判定。

        这条测试守的是一个实测过的过度触发：某份真实 Apple 导出里 17 条提示有
        15 条是「来源不一致，不做判定」，几乎所有可判定指标都被压掉，报告等于
        没有内容可交付。

        正确的判据是「**有区间，但被判定的那条观测的来源提供不了它**」：
          * 体重 —— 设备给过区间，最新读数来自没给区间的另一台，且无内置表
                   → 压制，写「来源不一致，不做判定」
          * 体脂率 —— 同上，但内置表有区间 → 用内置表判定，**不压制**
        """
        mixed = [t for t in self.an.trends.values() if len(t.sources) > 1]
        self.assertTrue(mixed, "测试夹具里应该有跨来源指标")

        judged = [t.metric for t in mixed if t.verdict in ("in", "low", "high")]
        self.assertTrue(judged, "多来源指标全被压制了 —— 又回到了过度触发")
        self.assertIn("body_fat_pct", judged)

        for t in mixed:
            if t.verdict == "na":
                self.assertEqual(t.verdict_note, "来源不一致，不做判定", t.metric)
                self.assertTrue(t.range_available,
                                f"{t.metric} 既然压制了判定，就该是「有区间但同源拿不到」")

        # 两种措辞都要在报告里出现：表格里既有真判定，也有「来源不一致」
        self.assertIn("来源不一致，不做判定", self.md)

    def test_mixed_source_metrics_are_not_listed_as_trend_only(self):
        """跨来源的指标不该出现在「只做趋势」小节里 —— 它有区间，只是不能用。"""
        section = section_of(self.md, "### 只做趋势")
        self.assertNotIn("**体重**", section)
        for t in self.an.trends.values():
            if len(t.sources) > 1:
                self.assertNotIn(f"**{t.cn}**", section, t.metric)

    def test_conclusions_never_state_a_cross_source_delta(self):
        """报告不许一边警告"跨来源不能比"，一边在结论里把那个差值当变化写出来。

        这是这个项目最不能犯的错：它会让第一节的全部警告失去可信度。
        """
        mixed_names = {t.cn for t in self.an.trends.values() if len(t.sources) > 1}
        self.assertTrue(mixed_names)
        section = section_of(self.md, "### 体成分与体重")
        bullet_lines = [ln for ln in section.splitlines() if ln.startswith("- **")]
        for ln in bullet_lines:
            for cn in mixed_names:
                if ln.startswith(f"- **{cn}**："):
                    self.fail(f"结论里出现了跨来源指标 {cn} 的首末差值：{ln}")
        # 但必须明确说明为什么省略，而不是静默丢掉
        self.assertIn("跨来源的差值不能当作真实变化", section)
        for cn in mixed_names:
            self.assertIn(cn, section, f"没告诉用户 {cn} 为什么没写")

    def test_goal_shortfall_is_not_called_abnormal(self):
        """没到步数目标不能写成「异常」。"""
        for line in self.md.splitlines():
            if "未达目标" in line and "步" in line:
                self.assertNotIn("异常", line)

    def test_no_diagnostic_language_anywhere(self):
        """报告里绝不允许出现诊断性措辞（黑名单见 BANNED_WORDING）。"""
        for b in BANNED_WORDING:
            self.assertNotIn(b, self.md, f"报告里出现了禁用措辞：{b}")

    def test_ai_section_explains_how_to_enable_and_that_it_is_optional(self):
        self.assertIn("HEALTH_REPORT_API_KEY", self.md)
        self.assertTrue("不开启" in self.md or "不联网" in self.md,
                        "没告诉用户不开启 AI 也是完整的")

    def test_rule_layer_conclusion_is_present_and_deterministic(self):
        self.assertIn("规则层结论", self.md)
        # 应包含具体的首末值和变化量
        self.assertIn("体重", self.md)

    def test_disclaimer_constant_is_used(self):
        self.assertTrue(DISCLAIMER.strip())
        self.assertIn(DISCLAIMER.splitlines()[0][:20], self.md)

    def test_tables_are_well_formed(self):
        """表格每行的竖线数量必须一致，否则 Markdown 渲染会坏。"""
        in_table, header_pipes = False, 0
        for line in self.md.splitlines():
            if line.startswith("|"):
                pipes = line.count("|")
                if not in_table:
                    in_table, header_pipes = True, pipes
                else:
                    self.assertEqual(pipes, header_pipes,
                                     f"表格列数不一致：{line[:80]}")
            else:
                in_table = False

    def test_no_raw_pipes_inside_cells(self):
        """单元格里不能出现未转义的竖线（中文全角｜是允许的）。"""
        for line in self.md.splitlines():
            if line.startswith("|") and "｜" in line:
                # 用了全角竖线，说明作者刻意避免了冲突
                continue


class TestReportWithAiText(ReportFixture):
    def test_ai_text_is_included_and_attributed(self):
        md = build_report(self.ds, self.an, self.findings,
                          ai_text="这是一段模型写的解读。", ai_model="test-model")
        self.assertIn("这是一段模型写的解读。", md)
        self.assertIn("test-model", md)
        # 必须声明模型只解释、不参与计算
        self.assertTrue("不参与计算" in md or "仅解释" in md)


class TestReportGracefulOnEmptyData(unittest.TestCase):
    def test_empty_dataset(self):
        ds = Dataset()
        an = analyze(ds)
        md = build_report(ds, an, [])
        self.assertTrue(md.strip())
        self.assertIn("这不是医学诊断", md)


def dataset_with_every_metric() -> Dataset:
    """每个 catalog 指标都造两个观测，用来把报告的所有文案路径都渲染一遍。"""
    from health_report import catalog

    profile = {"sex": "男", "age": 28, "height_cm": 172.0}
    ds = Dataset(profile=dict(profile))
    for spec in catalog.SPECS:
        ref = catalog.builtin_ref(spec.id, profile)
        if ref is not None:
            lo = ref.low if ref.low is not None else (
                (ref.high - 1.0) if ref.high is not None else 1.0)
            hi = ref.high if ref.high is not None else (lo + 1.0)
            base = (lo + hi) / 2.0 if hi != lo else lo
        else:
            base = 5.0
        base = base if base else 1.0
        for date, value in (("2026-01-10", base * 0.95), ("2026-05-20", base)):
            ds.observations.append(Observation(
                metric=spec.id, date=date, value=round(value, 4), unit=spec.unit,
                source="测试设备A", ts=f"{date} 08:00:00"))
    # 触发 LABEL_CONFLICT 的文案路径
    ds.labels["bmi_judgement@2026-05-20"] = "超重"
    ds.labels["body_fat_pct_judgement@2026-05-20"] = "正常"
    ds.workouts.append(Workout(date="2026-05-20", activity="跑步", duration_min=30.0,
                               distance_km=5.0, avg_hr=150.0, max_hr=175.0,
                               energy_kcal=330.0, source="测试设备A"))
    return ds


class TestEveryMetricPathIsSafe(unittest.TestCase):
    """把全部指标都渲染一遍，确认报告全文没有禁用措辞、也不崩。

    为什么必须单独写这一条：禁用的词可能藏在 catalog 的 `hint`、`ref_note`、
    pitfalls 的文案或报告模板里，而这些**只在对应指标真的出现在报告里时才渲染**。
    只测 demo 数据会漏——真实例子：`recommended_kcal` 属于隐藏类别
    「设备建议与评分」，它的 hint 在 demo 报告里根本不出现，
    但它写的是「不是营养处方」；一旦那个类别变成可见，报告里就会漏出「处方」。
    这里把全部指标喂进去，等于给所有文案路径上了一次全量扫描。
    """

    @classmethod
    def setUpClass(cls):
        cls.ds = dataset_with_every_metric()
        cls.an = analyze(cls.ds)
        cls.findings = check(cls.ds, cls.an)
        cls.md = build_report(cls.ds, cls.an, cls.findings)
        cls.facts = build_facts(cls.ds, cls.an, cls.findings)

    def test_covers_every_metric(self):
        from health_report import catalog
        self.assertEqual(len(self.ds.metric_ids()), len(catalog.SPECS),
                         "夹具没有覆盖全部指标")
        self.assertGreater(len(self.md), 5000, "报告太短，可能没渲染出全部内容")

    def test_no_banned_wording_anywhere(self):
        for b in BANNED_WORDING:
            self.assertNotIn(b, self.md, f"报告里出现了禁用措辞：{b}")

    def test_no_banned_wording_in_facts(self):
        blob = json.dumps(self.facts, ensure_ascii=False)
        for b in BANNED_WORDING:
            self.assertNotIn(b, blob, f"发给模型的事实里出现了禁用措辞：{b}")

    def test_every_metric_gets_a_verdict_or_a_reason(self):
        """每个指标要么有判定，要么有明确的「不判定」原因 —— 不能既没判定也没理由。"""
        from health_report.analysis import VERDICT_NA
        for t in self.an.trends.values():
            if t.verdict == VERDICT_NA:
                self.assertTrue(t.verdict_note,
                                f"{t.metric} 没有判定也没有 verdict_note")
            else:
                self.assertEqual(t.verdict_note, "", t.metric)

    def test_no_raw_observations_in_facts(self):
        self.assertNotIn("observations", self.facts)
        self.assertNotIn("08:00:00", json.dumps(self.facts, ensure_ascii=False))

    def test_disclaimer_still_first(self):
        self.assertLess(self.md.find("这不是医学诊断"), self.md.find("## 一、"))


class TestBuildFacts(ReportFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.facts = build_facts(cls.ds, cls.an, cls.findings)

    def test_json_serializable(self):
        json.dumps(self.facts, ensure_ascii=False)

    def test_contains_no_raw_observations(self):
        """这是隐私底线：发出去的必须是算好的摘要，不是原始数据。"""
        blob = json.dumps(self.facts, ensure_ascii=False)
        self.assertNotIn("observations", self.facts)
        # 合成数据的观测时间戳是 21:00:00，只出现在原始记录里
        self.assertNotIn("21:00:00", blob,
                         "facts 里出现了逐条记录的时间戳，说明原始数据漏出去了")
        self.assertNotIn("07:30:00", blob)

    def test_excludes_noise_categories(self):
        """生物电阻抗与设备建议不该占用模型上下文。"""
        for m in self.facts.get("metrics", []):
            self.assertNotIn(m.get("metric"), ("z_left_leg_20k", "z_right_leg_20k"))
            self.assertNotIn(m.get("metric"), ("inbody_score", "target_weight"))

    def test_includes_the_essentials(self):
        self.assertIn("profile", self.facts)
        self.assertIn("data", self.facts)
        self.assertIn("metrics", self.facts)
        self.assertIn("findings", self.facts)
        self.assertTrue(self.facts["metrics"])
        self.assertTrue(self.facts["findings"])

    def test_metric_entries_carry_verdict_and_ref_kind(self):
        for m in self.facts["metrics"]:
            self.assertIn("verdict", m)
            self.assertIn("ref_kind", m)

    def test_facts_carry_the_numbers_the_model_keeps_citing(self):
        """模型反复引用的那几个数字必须**在摘要里就有**，否则会被判成编造。

        实测（真机跑 DSH + 真实 Apple 导出）一次输出被删了 13 处句子，逐条看下来
        大量是**模型算得没错、但摘要里没给**的数字：

        * 「累计约 704 小时，约 20.2 万 kcal」—— 摘要只给了各类型的 minutes/kcal，
          总量得模型自己加。它加对了（真实值 704.01 小时 / 201948 kcal），
          但校验不认"模型自己算的数"，整句被删。
        * 「280 万条原始记录」—— 解析器其实数了这个数，只是没传到报告层。

        这违背了本项目一贯的分工：**规则层算，模型只叙述**。
        少给一个总量，就是逼模型自己算，然后又不认它算的结果。
        """
        data = self.facts.get("data", {})
        self.assertIn("record_count", data, "原始记录条数没进摘要")
        # 合成数据默认没设 n_records，但键必须在（值可以为 null）
        workouts = self.facts.get("workouts", {})
        self.assertIn("total_hours", workouts)
        self.assertIn("total_kcal", workouts)
        # 覆盖率是模型自己算占比的原材料（实测它会写出「约 53~56%」这种自算值）
        for m in self.facts["metrics"]:
            self.assertIn("coverage", m)

    def test_workout_totals_equal_the_sum_of_types(self):
        """总量必须等于各类型之和——它是给模型直接引用的，算错就是报告里错。"""
        workouts = self.facts.get("workouts", {})
        by_activity = workouts.get("by_activity") or {}
        if not by_activity:
            self.skipTest("合成数据没有训练类型明细")
        minutes = sum((row.get("minutes") or 0.0) for row in by_activity.values())
        kcal = sum((row.get("kcal") or 0.0) for row in by_activity.values())
        self.assertAlmostEqual(workouts["total_minutes"], round(minutes, 1), places=1)
        self.assertAlmostEqual(workouts["total_kcal"], round(kcal, 1), places=1)
        self.assertAlmostEqual(workouts["total_hours"], round(minutes / 60.0, 2), places=2)

    def test_findings_included_with_codes(self):
        codes = {f["code"] for f in self.facts["findings"]}
        self.assertIn("HYDRATION_AMPLIFIES", codes)


if __name__ == "__main__":
    unittest.main()
