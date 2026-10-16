"""统一数据模型的测试。"""

from __future__ import annotations

import unittest

from health_report.model import Dataset, Observation, RefRange, Workout


def obs(metric, date, value, source="秤", unit="kg", ts=""):
    return Observation(metric=metric, date=date, value=value, unit=unit,
                       source=source, ts=ts or f"{date} 08:00:00")


class TestRefRange(unittest.TestCase):
    def test_both_bounds(self):
        r = RefRange(low=18.5, high=24.9)
        self.assertTrue(r.contains(22.0))
        self.assertTrue(r.contains(18.5))
        self.assertTrue(r.contains(24.9))
        self.assertFalse(r.contains(18.4))
        self.assertFalse(r.contains(25.0))

    def test_open_bounds(self):
        r = RefRange(high=10, high_inclusive=False)
        self.assertTrue(r.contains(9.99))
        self.assertFalse(r.contains(10))
        self.assertIsNone(r.contains(None))

    def test_no_bounds_is_unknown_not_true(self):
        """空区间必须返回 None（无法判断），不能返回 True（假装正常）。"""
        self.assertIsNone(RefRange().contains(42))

    def test_label(self):
        self.assertEqual(RefRange(low=1, high=2).label(), "1~2")
        self.assertEqual(RefRange(high=10, high_inclusive=False).label(), "<10")
        # 单边区间用 ≥ / ≤ 而不是 >= / <=：这些文字会直接出现在中文报告里
        self.assertEqual(RefRange(low=7).label(), "≥7")
        self.assertEqual(RefRange(high=90).label(), "≤90")
        self.assertEqual(RefRange(text="88~99").label(), "88~99")
        self.assertEqual(RefRange().label(), "—")


class TestDataset(unittest.TestCase):
    def setUp(self):
        self.ds = Dataset()
        self.ds.observations.extend([
            obs("body_mass", "2026-05-03", 73.5),
            obs("body_mass", "2026-05-01", 74.0),
            obs("body_mass", "2026-05-02", 73.8, ts="2026-05-02 21:00:00"),
            obs("body_mass", "2026-05-02", 73.9, ts="2026-05-02 07:00:00"),
            obs("resting_hr", "2026-05-01", 54, unit="次/分"),
            obs("body_mass", "2026-05-02", 73.7, source="InBody270"),
        ])

    def test_series_is_sorted(self):
        """按 (日期, 时间戳) 升序 —— 同一天内也要定序，否则「当日最后一条」会随机。"""
        self.assertEqual([o.date for o in self.ds.series("body_mass")],
                         ["2026-05-01", "2026-05-02", "2026-05-02", "2026-05-02", "2026-05-03"])
        same_day = [o for o in self.ds.series("body_mass") if o.date == "2026-05-02"]
        self.assertEqual([o.ts for o in same_day],
                         ["2026-05-02 07:00:00", "2026-05-02 08:00:00", "2026-05-02 21:00:00"])

    def test_latest(self):
        self.assertIsNotNone(self.ds.latest("body_mass"))
        self.assertIsNone(self.ds.latest("不存在的指标"))

    def test_sources_of_dedupes_and_keeps_order(self):
        self.assertEqual(self.ds.sources_of("body_mass"), ["秤", "InBody270"])
        self.assertEqual(self.ds.sources_of("resting_hr"), ["秤"])

    def test_date_span(self):
        self.assertEqual(self.ds.date_span(), ("2026-05-01", "2026-05-03"))

    def test_date_span_empty(self):
        self.assertIsNone(Dataset().date_span())

    def test_days_covered_counts_distinct_dates(self):
        self.assertEqual(self.ds.days_covered("body_mass"), 3)

    def test_dedupe(self):
        d = Dataset()
        a = obs("body_mass", "2026-05-01", 74.0)
        d.observations.extend([a, obs("body_mass", "2026-05-01", 74.0), a])
        self.assertEqual(len(d.dedupe().observations), 1)

    def test_merge_fills_missing_profile_and_keeps_notes(self):
        a = Dataset(profile={"height_cm": 172.0})
        b = Dataset(profile={"height_cm": 999.0, "age": 28})
        b.add_note("来自 B 的提示")
        a.merge(b)
        self.assertEqual(a.profile["height_cm"], 172.0)  # 已有的不被覆盖
        self.assertEqual(a.profile["age"], 28)
        self.assertIn("来自 B 的提示", a.notes)

    def test_merge_dedupes_files(self):
        a = Dataset(files=["x.xml"])
        a.merge(Dataset(files=["x.xml", "y.csv"]))
        self.assertEqual(a.files, ["x.xml", "y.csv"])

    def test_merge_sums_raw_record_count(self):
        """原始记录条数在 merge 时必须**累加**。

        这条守的是一个真实的漏传：CLI 总是先建一个空 Dataset、再 `merge(part)`，
        如果 merge 不带 `n_records`，解析器辛苦数出来的条数就在这里被丢掉，
        事实摘要里 `record_count` 恒为 null。后果是模型写「280 万条原始记录」
        （真话！真实值 2801761）会被数字溯源校验判成编造、整句删掉。
        """
        a = Dataset(n_records=2801761)
        a.merge(Dataset(n_records=10))
        self.assertEqual(a.n_records, 2801771)

    def test_merge_tolerates_a_source_without_the_field(self):
        """老代码/其它解析器可能不设 n_records，不能因此崩掉。"""
        a = Dataset(n_records=5)
        a.merge(Dataset())          # 默认 0
        self.assertEqual(a.n_records, 5)

    def test_add_note_is_idempotent(self):
        d = Dataset()
        d.add_note("同样的提示")
        d.add_note("同样的提示")
        self.assertEqual(d.notes, ["同样的提示"])


class TestSerialization(unittest.TestCase):
    def test_roundtrip_preserves_everything(self):
        ds = Dataset(profile={"age": 28}, labels={"bmi_judgement@2026-05-01": "正常"},
                     files=["a.csv"])
        ds.add_note("提示")
        ds.observations.append(Observation(
            metric="body_mass", date="2026-05-01", value=74.0, unit="kg",
            source="秤", ts="2026-05-01 08:00:00",
            ref=RefRange(low=57.0, high=76.0, source="设备报告参考范围"),
        ))
        ds.workouts.append(Workout(date="2026-05-01", activity="跑步", distance_km=5.0))

        back = Dataset.from_dict(ds.to_dict())
        self.assertEqual(back.profile, ds.profile)
        self.assertEqual(back.labels, ds.labels)
        self.assertEqual(back.notes, ds.notes)
        self.assertEqual(len(back.observations), 1)
        self.assertEqual(back.observations[0].metric, "body_mass")
        self.assertIsNotNone(back.observations[0].ref)
        self.assertEqual(back.observations[0].ref.label(), "57~76")
        self.assertEqual(back.observations[0].ref.source, "设备报告参考范围")
        self.assertEqual(len(back.workouts), 1)

    def test_to_dict_is_json_serializable(self):
        import json
        ds = Dataset()
        ds.observations.append(Observation(
            metric="body_mass", date="2026-05-01", value=74.0, unit="kg",
            ref=RefRange(low=57, high=76),
        ))
        json.dumps(ds.to_dict(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
