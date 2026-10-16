"""分析层的口径测试。

analysis.py 定义了整个项目的语义（什么算「变化」、用哪个参考区间、什么时候不判定）。
这里的用例逐条钉住那些口径——因为改动它们等于改动全项目的结论，
而结论错在健康数据上比崩溃更危险。
"""

from __future__ import annotations

import datetime
import unittest

from health_report.analysis import (
    NOTE_MIXED_SOURCES,
    NOTE_NO_REF,
    NOTE_NOT_JUDGED,
    analyze,
    ref_for_observation,
    source_structure,
)
from health_report.model import Dataset, Observation, RefRange


def head(low=18.5, high=23.0, source="设备报告参考范围"):
    return RefRange(low=low, high=high, source=source, text=f"{low}~{high}")


def obs(metric, date, value, source="InBody270", unit="kg/m2", ref=None, ts=""):
    return Observation(metric=metric, date=date, value=value, unit=unit,
                       source=source, ref=ref, ts=ts or f"{date} 08:00:00")


class TestReferenceRangeMustBeSameSource(unittest.TestCase):
    """拿 A 设备的读数去套 B 设备的区间，是本项目最要防的读错。

    这正是把"体脂率 21.2%（智能秤）"和"InBody 的 10~20%"配在一起的那种输出，
    它会得出一个看起来专业、实际无意义的"高于参考上限"。
    """

    def setUp(self):
        self.series = [
            obs("body_fat_pct", "2026-01-10", 22.3, source="InBody270",
                unit="%", ref=RefRange(low=10.0, high=20.0, source="设备报告参考范围")),
            obs("body_fat_pct", "2026-06-30", 21.2, source="智能秤", unit="%", ref=None),
        ]

    def test_does_not_borrow_ref_from_another_source(self):
        last = self.series[-1]
        ref, origin = ref_for_observation("body_fat_pct", last, self.series, {"sex": "男"})
        # 智能秤没有自己的区间 -> 必须退到内置人群表，绝不能借用 InBody 的
        self.assertEqual(origin, "builtin")
        self.assertNotEqual(ref.source, "设备报告参考范围")
        self.assertEqual((ref.low, ref.high), (10.0, 20.0))

    def test_same_source_ref_is_used(self):
        first = self.series[0]
        ref, origin = ref_for_observation("body_fat_pct", first, self.series, {})
        self.assertEqual(origin, "device")
        self.assertEqual(ref.source, "设备报告参考范围")

    def test_own_ref_wins(self):
        own = RefRange(low=1, high=2, source="自带")
        o = obs("body_mass", "2026-05-01", 74.0, ref=own)
        ref, origin = ref_for_observation("body_mass", o, [o], {})
        self.assertEqual(origin, "device")
        self.assertEqual(ref.source, "自带")


class TestMixedSourcesAreNotJudged(unittest.TestCase):
    """两套测量系统混在一个序列里时，不给"正常/异常"。"""

    def setUp(self):
        self.ds = Dataset()
        self.ds.observations.extend([
            obs("body_mass", "2026-05-20", 71.2, source="InBody270", unit="kg",
                ref=RefRange(low=57.0, high=76.0, source="设备报告参考范围")),
            obs("body_mass", "2026-06-30", 73.6, source="智能秤", unit="kg"),
        ])
        self.an = analyze(self.ds)

    def test_verdict_is_na_with_the_more_informative_reason(self):
        """两种原因同时成立时报「来源不一致」。

        因为用户会说"可我报告上明明印着参考范围"——「无可用参考区间」会让人困惑，
        「来源不一致」才解释了为什么不能用那个范围。
        """
        t = self.an.trends["body_mass"]
        self.assertEqual(t.verdict, "na")
        self.assertEqual(t.verdict_note, NOTE_MIXED_SOURCES)
        self.assertEqual(t.ref_kind, "")

    def test_no_range_is_invented_for_the_newer_source(self):
        """智能秤没有自己的区间，就没有区间 —— 绝不借 InBody 的。"""
        t = self.an.trends["body_mass"]
        self.assertIsNone(t.ref, "借用了另一台设备的区间")

    def test_ref_is_exposed_when_the_newest_source_supplied_one(self):
        """被判定的那个来源自带区间时，既要展示、也要正常判定。

        （早期版本在这里放弃判定，理由是"来源不一致"。那是过度触发：
        判定算的是「最新值 vs **同源**区间」，多来源并不妨碍它——
        真正受影响的是跨时间的可比性，那是趋势的事。）
        """
        ds = Dataset(observations=[
            obs("body_mass", "2026-01-10", 74.0, source="智能秤", unit="kg"),
            obs("body_mass", "2026-05-20", 71.2, source="InBody270", unit="kg",
                ref=RefRange(low=57.0, high=76.0, source="设备报告参考范围")),
        ])
        t = analyze(ds).trends["body_mass"]
        self.assertIsNotNone(t.ref)
        self.assertEqual(t.ref.source, "设备报告参考范围")
        self.assertEqual(t.verdict, "in")
        self.assertEqual(t.verdict_note, "")

    def test_suppression_only_when_a_range_exists_but_not_for_this_source(self):
        """唯一该放弃判定的情形：有尺子，却不能拿来量这个读数。

        体重：设备给过 57~76 的区间，但最新读数来自没给区间的智能秤，
        而体重没有内置人群表 → 有区间但同源拿不到 → 「来源不一致，不做判定」。
        """
        ds = Dataset(observations=[
            obs("body_mass", "2026-05-20", 71.2, source="InBody270", unit="kg",
                ref=RefRange(low=57.0, high=76.0, source="设备报告参考范围")),
            obs("body_mass", "2026-06-30", 73.6, source="智能秤", unit="kg"),
        ])
        t = analyze(ds).trends["body_mass"]
        self.assertIsNone(t.ref)
        self.assertTrue(t.range_available, "设备确实给过区间")
        self.assertEqual(t.verdict, "na")
        self.assertEqual(t.verdict_note, NOTE_MIXED_SOURCES)

    def test_builtin_range_restores_the_verdict_for_multi_source(self):
        """设备没给区间、但内置表有 → 用内置表判定，不再压制。

        体脂率：智能秤没给区间，但内置表（男性 10~20）有 → 判定为高。
        这正是修复「过度触发」要恢复的那类判定。
        """
        ds = Dataset(observations=[
            obs("body_fat_pct", "2026-05-20", 16.9, source="InBody270", unit="%",
                ref=RefRange(low=10.0, high=20.0, source="设备报告参考范围")),
            obs("body_fat_pct", "2026-06-30", 21.2, source="智能秤", unit="%"),
        ], profile={"sex": "男"})
        t = analyze(ds).trends["body_fat_pct"]
        self.assertEqual(t.ref_origin, "builtin")
        self.assertEqual(t.verdict, "high")
        self.assertEqual(t.verdict_note, "")

    def test_single_source_is_judged_normally(self):
        ds = Dataset(observations=[
            obs("bmi", "2026-01-10", 25.0, ref=head()),
            obs("bmi", "2026-05-20", 24.1, ref=head()),
        ])
        t = analyze(ds).trends["bmi"]
        # 24.1 高于上限 23.0；设备自带的区间是 range 性质
        self.assertEqual(t.verdict, "high")
        self.assertEqual(t.ref_kind, "range")
        self.assertEqual(t.verdict_note, "")

    def test_no_range_anywhere_means_trend_only(self):
        ds = Dataset(observations=[
            obs("hrv_sdnn", "2026-05-01", 45.0, source="Apple Watch", unit="ms"),
            obs("hrv_sdnn", "2026-06-01", 51.0, source="Apple Watch", unit="ms"),
        ])
        t = analyze(ds).trends["hrv_sdnn"]
        self.assertEqual(t.verdict, "na")
        self.assertEqual(t.verdict_note, NOTE_NO_REF)
        self.assertIsNone(t.ref)

    def test_non_judged_metric_says_so(self):
        ds = Dataset(observations=[
            obs("inbody_score", "2026-01-10", 78.0, unit="分"),
            obs("inbody_score", "2026-05-20", 80.0, unit="分"),
        ])
        t = analyze(ds).trends["inbody_score"]
        self.assertEqual(t.verdict_note, NOTE_NOT_JUDGED)


class TestAggregationSemantics(unittest.TestCase):
    def test_sum_metrics_have_no_first_last_delta(self):
        """把两天的步数相减没有意义 —— delta 必须是 None。"""
        ds = Dataset(observations=[
            obs("steps", "2026-05-01", 12000, source="Apple Watch", unit="步"),
            obs("steps", "2026-05-02", 8000, source="Apple Watch", unit="步"),
        ])
        t = analyze(ds).trends["steps"]
        self.assertIsNone(t.delta)
        self.assertIsNone(t.pct)
        self.assertIsNone(t.per_30d)
        self.assertIsNotNone(t.daily_mean)

    def test_daily_mean_counts_missing_days_as_zero(self):
        """缺数据的日子按 0 算（偏保守），并同时暴露覆盖率。"""
        ds = Dataset(observations=[
            obs("steps", "2026-05-01", 10000, source="W", unit="步"),
            obs("steps", "2026-05-02", 10000, source="W", unit="步"),
            # 05-03、05-04 缺失 -> 跨度 4 天
            obs("steps", "2026-05-05", 10000, source="W", unit="步"),
        ])
        t = analyze(ds).trends["steps"]
        self.assertAlmostEqual(t.daily_mean, 30000 / 4, places=2)
        self.assertAlmostEqual(t.coverage, 3 / 4, places=3)

    def test_snapshot_metrics_do_have_delta(self):
        ds = Dataset(observations=[
            obs("body_mass", "2026-05-01", 74.0, source="秤", unit="kg"),
            obs("body_mass", "2026-05-31", 73.1, source="秤", unit="kg"),
        ])
        t = analyze(ds).trends["body_mass"]
        self.assertAlmostEqual(t.delta, -0.9, places=6)
        self.assertAlmostEqual(t.per_30d, -0.9, places=6)


class TestMassBalance(unittest.TestCase):
    """体成分仪的恒等式：水分 + 蛋白质 + 无机盐 + 体脂肪 == 体重。

    真实报告总是闭合的（这是设备自检），所以规则的**不报**和**报**都要测。
    """

    def _ds(self, fat):
        d = "2026-05-20"
        return Dataset(observations=[
            obs("total_body_water", d, 44.0, unit="L"),
            obs("protein_mass", d, 11.6, unit="kg"),
            obs("mineral_mass", d, 3.6, unit="kg"),
            obs("body_fat_mass", d, fat, unit="kg"),
            obs("body_mass", d, 71.2, unit="kg"),
        ])

    def test_balanced_data_reports_nothing(self):
        from health_report.pitfalls import check
        ds = self._ds(12.0)          # 44.0+11.6+3.6+12.0 = 71.2 ✓
        findings = check(ds, analyze(ds))
        self.assertNotIn("MASS_BALANCE", {f.code for f in findings})

    def test_unbalanced_data_is_flagged(self):
        from health_report.pitfalls import check
        ds = self._ds(16.0)          # 44.0+11.6+3.6+16.0 = 75.2 vs 71.2 -> 差 4.0
        findings = check(ds, analyze(ds))
        hit = [f for f in findings if f.code == "MASS_BALANCE"]
        self.assertTrue(hit, "不闭合的数据没有被抓到")
        self.assertEqual(hit[0].severity, "warn")


class TestGracefulDegradation(unittest.TestCase):
    def test_dirty_dates_do_not_leak_into_span(self):
        ds = Dataset(observations=[
            obs("body_mass", "2026-05-01", 74.0, source="秤", unit="kg"),
            obs("body_mass", "不是日期", 73.0, source="秤", unit="kg"),
            obs("body_mass", "2026-05-31", 73.1, source="秤", unit="kg"),
        ])
        an = analyze(ds)
        self.assertEqual(an.span, ("2026-05-01", "2026-05-31"))
        self.assertEqual(ds.date_span(), ("2026-05-01", "2026-05-31"))


class TestSourceStructure(unittest.TestCase):
    """并发 vs 接替的判定，以及设备接替点的启发式发现。

    为什么要区分这两件事：并发影响**数值口径**（同一段时间被两台设备各记一遍，
    累计类会双计，由解析器的 --source-policy 解决），接替影响**跨时间可比性**
    （换设备造成的台阶不是身体变化）。

    实测动机：某份真实 Apple 导出里，最大摄氧量来自 3 台不同设备、
    静息心率来自 3 台、HRV 来自 3 台——而 **Apple 不标注换设备**。
    """

    @staticmethod
    def _mk(source: str, start: str, n: int):
        d0 = datetime.date.fromisoformat(start)
        return [obs("body_mass", (d0 + datetime.timedelta(days=i)).isoformat(),
                    70.0, source=source, unit="kg") for i in range(n)]

    def test_single_source(self):
        overlap, days, hs = source_structure(sorted(self._mk("A", "2026-01-01", 20),
                                                   key=lambda o: o.sort_key))
        self.assertEqual(overlap, "single")
        self.assertEqual((days, hs), (0, []))

    def test_concurrent_has_no_handover(self):
        series = self._mk("A", "2026-01-01", 30) + self._mk("B", "2026-01-11", 20)
        overlap, days, hs = source_structure(sorted(series, key=lambda o: o.sort_key))
        self.assertEqual(overlap, "concurrent")
        self.assertGreaterEqual(days, 7)
        self.assertEqual(hs, [])

    def test_sequential_handover_is_found(self):
        series = self._mk("A", "2026-01-01", 20) + self._mk("B", "2026-01-21", 20)
        overlap, days, hs = source_structure(sorted(series, key=lambda o: o.sort_key))
        self.assertEqual(overlap, "sequential")
        self.assertEqual(hs, [("B", "A", "2026-01-21")])

    def test_short_fragment_does_not_create_a_handover(self):
        """只出现两天的来源是碎片，不是"换了设备"。

        这里踩过一个 bug：把碎片并进前一段之后，前后两段变成同一来源，
        于是造出「A 接替 A」这种假接替点。所以合并之后还要再并一次同名时代。
        """
        series = (self._mk("A", "2026-01-01", 20) + self._mk("B", "2026-01-21", 2)
                  + self._mk("A", "2026-01-23", 20))
        overlap, _, hs = source_structure(sorted(series, key=lambda o: o.sort_key))
        self.assertEqual(hs, [], "短碎片造出了假接替点")
        self.assertTrue(all(h[0] != h[1] for h in hs), "出现了「A 接替 A」")

    def test_three_eras(self):
        series = (self._mk("A", "2026-01-01", 15) + self._mk("B", "2026-01-16", 15)
                  + self._mk("C", "2026-01-31", 15))
        _, _, hs = source_structure(sorted(series, key=lambda o: o.sort_key))
        self.assertEqual(hs, [("B", "A", "2026-01-16"), ("C", "B", "2026-01-31")])

    def test_analysis_exposes_it_on_the_trend(self):
        series = self._mk("A", "2026-01-01", 20) + self._mk("B", "2026-01-21", 20)
        t = analyze(Dataset(observations=series)).trends["body_mass"]
        self.assertEqual(t.source_overlap, "sequential")
        self.assertEqual(t.handovers, [("B", "A", "2026-01-21")])


class TestDeviceHandoverRule(unittest.TestCase):
    """DEVICE_HANDOVER 规则的触发与沉默。"""

    @staticmethod
    def _mk(source: str, start: str, n: int):
        d0 = datetime.date.fromisoformat(start)
        return [obs("body_mass", (d0 + datetime.timedelta(days=i)).isoformat(),
                    70.0, source=source, unit="kg") for i in range(n)]

    def test_fires_on_a_handover(self):
        from health_report.pitfalls import check
        series = self._mk("旧手表", "2026-01-01", 20) + self._mk("新手表", "2026-01-21", 20)
        ds = Dataset(observations=series)
        hit = [f for f in check(ds, analyze(ds)) if f.code == "DEVICE_HANDOVER"]
        self.assertEqual(len(hit), 1, "接替点没有触发，或者触发了多条（应当合并成一条）")
        self.assertEqual(hit[0].severity, "info")
        self.assertIn("旧手表", hit[0].title)
        self.assertIn("新手表", hit[0].title)

    def test_silent_when_only_one_device(self):
        from health_report.pitfalls import check
        ds = Dataset(observations=self._mk("唯一设备", "2026-01-01", 40))
        codes = {f.code for f in check(ds, analyze(ds))}
        self.assertNotIn("DEVICE_HANDOVER", codes)


if __name__ == "__main__":
    unittest.main()
