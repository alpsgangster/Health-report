"""陷阱回归测试 —— 这个文件是这个项目最重要的测试。

`samples.synthetic_dataset()` 里的合成数据**故意埋了陷阱**，每一处都对应
`docs/PITFALLS.md` 里的一条规则。这里的断言是：那些陷阱必须真的被抓到。

为什么值得单独写一个文件：这类规则的价值全在"该报的时候报、不该报的时候不报"。
如果哪天有人调整了分析口径，让 `HYDRATION_AMPLIFIES` 静默失效，
这个测试会红——否则这个项目的核心卖点会悄悄消失。
"""

from __future__ import annotations

import unittest

from health_report.analysis import analyze
from health_report.model import Dataset, Observation
from health_report.pitfalls import check
from health_report.samples import synthetic_dataset


class FindingsFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset()
        cls.analysis = analyze(cls.ds)
        cls.findings = check(cls.ds, cls.analysis)
        cls.codes = {f.code for f in cls.findings}

    def finding(self, code):
        for f in self.findings:
            if f.code == code:
                return f
        self.fail(f"没有触发 {code}；实际触发：{sorted(self.codes)}")


class TestTrapsAreCaught(FindingsFixture):
    def test_src_mixed(self):
        """体重同时来自智能秤与 InBody。"""
        f = self.finding("SRC_MIXED")
        self.assertIn("body_mass", f.metrics)
        self.assertEqual(f.severity, "warn")

    def test_same_day_conflict(self):
        f = self.finding("SAME_DAY_CONFLICT")
        self.assertEqual(f.severity, "warn")
        self.assertTrue(f.evidence)

    def test_hydration_amplifies(self):
        """身体总水分超出参考上限 —— 最该被抓住的一条。"""
        f = self.finding("HYDRATION_AMPLIFIES")
        self.assertIn("total_body_water", f.metrics)
        self.assertEqual(f.severity, "warn")

    def test_impedance_shift(self):
        """下肢阻抗 +30%。"""
        f = self.finding("IMPEDANCE_SHIFT")
        self.assertIn("z_left_leg_20k", f.metrics)
        self.assertIn("z_right_leg_20k", f.metrics)

    def test_pct_baseline_caveat_always_fires(self):
        """只要有节段百分比就该给 caveat —— 永远成立的提醒。"""
        f = self.finding("PCT_BASELINE_CAVEAT")
        self.assertEqual(f.severity, "info")

    def test_pct_baseline_shift_fires_on_real_drift(self):
        """腿部百分比的隐含标准值漂移 7~8%，超过 5% 阈值 → 升级为 warn。"""
        f = self.finding("PCT_BASELINE_SHIFT")
        self.assertEqual(f.severity, "warn")

    def test_missing_days(self):
        """步数只有约 2/3 的日子有数据。"""
        f = self.finding("MISSING_DAYS")
        self.assertIn("steps", f.metrics)

    def test_label_conflict(self):
        """BMI 24.1 在设备区间内却标「正常」，实际已超出上限 23.0。"""
        f = self.finding("LABEL_CONFLICT")
        self.assertEqual(f.severity, "warn")

    def test_label_conflict_is_not_raised_by_another_devices_value(self):
        """文字判定只能和**说出这句话的那台设备**的数值比。

        合成数据里 2026-05-20 同一天有两条体脂率：InBody270 = 16.9%（区间 10~20，
        判定「正常」，自洽）和智能秤 = 21.2%（无区间）。如果实现时按时间戳取当天最后
        一条，就会拿智能秤的 21.2 去比 InBody 的区间，凭空造出一条假冲突——
        这个假阳性真的发生过，所以这里钉住它。
        """
        labels = self.ds.labels
        self.assertIn("body_fat_pct_judgement@2026-05-20", labels)
        hits = [f for f in self.findings
                if f.code == "LABEL_CONFLICT" and "body_fat_pct" in f.metrics]
        self.assertEqual(hits, [], f"体脂率被误报为判定冲突：{[h.title for h in hits]}")

    def test_label_conflict_count_matches_the_real_conflict(self):
        """合成数据里只有一个真正的判定冲突（BMI），改口径时别悄悄多报。"""
        hits = [f for f in self.findings if f.code == "LABEL_CONFLICT"]
        self.assertEqual([f.metrics for f in hits], [["bmi"]],
                         f"LABEL_CONFLICT 数量不对：{[(f.metrics, f.title) for f in hits]}")

    def test_trend_too_short(self):
        """腰围只有 1 个点。"""
        f = self.finding("TREND_TOO_SHORT")
        self.assertIn("waist_circumference", f.metrics)

    def test_range_out_uses_goal_wording_for_steps(self):
        """步数是 goal 性质：低于目标不能说成「异常」，而且这条分支必须真的可达。

        这条断言存在的理由：catalog 里如果 goal 类指标被标成 verdict=False，
        「未达目标」就永远不会出现——报告会静默吞掉「你没走到目标」这件事。
        所以这里断言 finding 必须存在，不能只写个空的 for 循环。
        """
        step_findings = [f for f in self.findings
                         if f.code == "RANGE_OUT" and "steps" in f.metrics]
        self.assertTrue(step_findings,
                        "步数低于目标却没有触发 RANGE_OUT；"
                        "检查 catalog 里 steps 的 verdict 是否为 True")
        for f in step_findings:
            self.assertEqual(f.severity, "info", f.title)
            self.assertIn("目标", f.title)
            self.assertNotIn("异常", f.title)

    def test_goal_metrics_are_judged_at_all(self):
        """所有 ref_kind 是 goal 的指标都应当参与判定，否则措辞分支是死代码。"""
        from health_report import catalog

        for spec in catalog.SPECS:
            if catalog.builtin_ref(spec.id, {}) is not None:
                ref = catalog.builtin_ref(spec.id, {})
                if ref.kind == "goal":
                    self.assertTrue(spec.verdict,
                                    f"{spec.id} 是 goal 性质但 verdict=False，"
                                    f"「未达目标」永远不会出现在报告里")


class TestFindingsAreWellFormed(FindingsFixture):
    def test_sorted_by_severity_then_code(self):
        rank = {"alert": 0, "warn": 1, "info": 2}
        keys = [(rank[f.severity], f.code) for f in self.findings]
        self.assertEqual(keys, sorted(keys))

    def test_severity_values_are_legal(self):
        for f in self.findings:
            self.assertIn(f.severity, ("info", "warn", "alert"), f.code)

    def test_titles_contain_numbers(self):
        """没有数字的结论没有信息量 —— 这里强制要求 title 带数字。

        例外：纯口径提醒类（caveat）允许没有数字。
        """
        allow_no_number = {"PCT_BASELINE_CAVEAT"}
        for f in self.findings:
            if f.code in allow_no_number:
                continue
            self.assertTrue(
                any(ch.isdigit() for ch in f.title),
                f"{f.code} 的 title 里没有具体数字：{f.title}",
            )

    def test_no_diagnostic_language(self):
        """绝不允许出现诊断性措辞。"""
        banned = ["你患有", "您患有", "说明你有", "建议服用", "建议剂量",
                  "确诊", "处方", "用药"]
        for f in self.findings:
            blob = f.title + f.detail
            for b in banned:
                self.assertNotIn(b, blob, f"{f.code} 出现了禁用措辞：{b}")

    def test_evidence_is_json_serializable(self):
        import json
        for f in self.findings:
            json.dumps(f.evidence, ensure_ascii=False)

    def test_details_are_substantive(self):
        """detail 必须解释「为什么」和「该怎么读」，不能是一句话敷衍。"""
        for f in self.findings:
            self.assertGreater(len(f.detail), 40, f"{f.code} 的 detail 太短")


class TestBenignSideIsNotAWarning(unittest.TestCase):
    """落在「不需要留意的那一侧」时不能发 warn。

    实测依据：一份 1336 天的真实 Apple 导出里，静息心率中位 56、78.7% 的天数
    低于 60。旧口径会把几乎每一天都报成「超出筛查参考」——发这种告警的代价是
    真正该看的那条提示被淹没。

    另一个真实例子：InBody 印的「去脂体重 46.7~57.0」会把 58.3 判成"高于上限"，
    而肌肉量大的人高于这个以一般人群为基准的上限恰恰是好事。所以「哪一侧值得
    留意」必须由 catalog 按指标决定，**不能被设备给的区间边界带跑**。
    """

    def _range_out(self, ds):
        an = analyze(ds)
        found = [f for f in check(ds, an) if f.code == "RANGE_OUT"]
        return an, {f.metrics[0]: f for f in found}

    @staticmethod
    def _obs(metric, date, value, unit, ref=None, source="Apple Watch"):
        return Observation(metric=metric, date=date, value=value, unit=unit,
                           source=source, ref=ref, ts=f"{date} 08:00:00")

    def test_resting_hr_low_is_info_not_warn(self):
        ds = Dataset(observations=[
            self._obs("resting_hr", "2026-01-05", 62, "次/分"),
            self._obs("resting_hr", "2026-01-20", 54, "次/分"),
        ])
        an, hits = self._range_out(ds)
        self.assertEqual(an.trends["resting_hr"].verdict, "low")
        self.assertIn("resting_hr", hits)
        self.assertEqual(hits["resting_hr"].severity, "info")
        self.assertIn("不视为异常", hits["resting_hr"].title)

    def test_resting_hr_high_is_still_warn(self):
        """高那一侧才是需要留意的 —— 不能因为放宽低侧就把高侧也放掉。"""
        ds = Dataset(observations=[
            self._obs("resting_hr", "2026-01-05", 62, "次/分"),
            self._obs("resting_hr", "2026-01-20", 108, "次/分"),
        ])
        _, hits = self._range_out(ds)
        self.assertEqual(hits["resting_hr"].severity, "warn")
        self.assertIn("超出筛查参考范围", hits["resting_hr"].title)

    def test_device_range_does_not_override_the_concern_side(self):
        """设备给的区间只提供边界，不决定「哪一侧值得留意」。"""
        from health_report.model import RefRange
        dev = RefRange(low=46.7, high=57.0, source="设备报告参考范围")
        ds = Dataset(observations=[
            self._obs("lean_body_mass", "2026-01-05", 58.5, "kg", ref=dev),
            self._obs("lean_body_mass", "2026-01-20", 58.3, "kg", ref=dev),
        ])
        an, hits = self._range_out(ds)
        self.assertEqual(an.trends["lean_body_mass"].ref.concern, "low",
                         "catalog 的 concern 没有覆盖设备区间的默认值")
        self.assertEqual(hits["lean_body_mass"].severity, "info")
        self.assertIn("肌肉量充足", hits["lean_body_mass"].detail)

    def test_protein_high_is_info(self):
        from health_report.model import RefRange
        dev = RefRange(low=9.2, high=11.2, source="设备报告参考范围")
        ds = Dataset(observations=[
            self._obs("protein_mass", "2026-01-05", 11.5, "kg", ref=dev),
            self._obs("protein_mass", "2026-01-20", 11.6, "kg", ref=dev),
        ])
        _, hits = self._range_out(ds)
        self.assertEqual(hits["protein_mass"].severity, "info")

    def test_hydration_over_upper_limit_stays_warn(self):
        """身体总水分超标是真信号，必须留在 warn —— 这是 HYDRATION 规则的入口。"""
        from health_report.model import RefRange
        dev = RefRange(low=35.5, high=43.4, source="设备报告参考范围")
        ds = Dataset(observations=[
            self._obs("total_body_water", "2026-01-05", 42.5, "L", ref=dev),
            self._obs("total_body_water", "2026-01-20", 44.0, "L", ref=dev),
        ])
        _, hits = self._range_out(ds)
        self.assertEqual(hits["total_body_water"].severity, "warn")

    def test_bmi_low_is_not_treated_as_benign(self):
        """BMI 偏低是真需要留意的一侧（偏瘦），不能被降调。"""
        ds = Dataset(observations=[
            self._obs("bmi", "2026-01-05", 20.0, "kg/m2"),
            self._obs("bmi", "2026-01-20", 17.0, "kg/m2"),
        ])
        _, hits = self._range_out(ds)
        self.assertEqual(hits["bmi"].severity, "warn")

    def test_titles_are_readable(self):
        """标题里「下限」和数字之间要有空格（这里踩过一次：写成 `低于下限60`）。"""
        ds = Dataset(observations=[
            self._obs("resting_hr", "2026-01-05", 62, "次/分"),
            self._obs("resting_hr", "2026-01-20", 54, "次/分"),
        ])
        _, hits = self._range_out(ds)
        self.assertIn("低于下限 60", hits["resting_hr"].title)
        self.assertNotIn("下限60", hits["resting_hr"].title)


class TestGracefulOnEmptyData(unittest.TestCase):
    def test_empty_dataset_reports_nothing_and_does_not_crash(self):
        from health_report.model import Dataset

        ds = Dataset()
        an = analyze(ds)
        self.assertEqual(check(ds, an), [])
        self.assertEqual(an.trends, {})

    def test_single_metric_single_point(self):
        from health_report.model import Dataset, Observation

        ds = Dataset(observations=[
            Observation(metric="body_mass", date="2026-05-01", value=74.0, unit="kg", source="秤"),
        ])
        an = analyze(ds)
        findings = check(ds, an)
        codes = {f.code for f in findings}
        self.assertIn("TREND_TOO_SHORT", codes)
        self.assertNotIn("SRC_MIXED", codes)          # 只有一个来源
        self.assertNotIn("SAME_DAY_CONFLICT", codes)  # 没有同日冲突


if __name__ == "__main__":
    unittest.main()
