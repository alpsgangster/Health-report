"""解析器测试。

不依赖任何真实个人数据：先用 `samples.write_all()` 生成合成示例文件，
再解析它们。这样解析器永远有可复现的夹具，而且夹具不会泄漏任何隐私。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from health_report import catalog
from health_report.parsers import detect_kind, parse_path
from health_report.samples import write_all, synthetic_dataset


class ParserFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="health-report-test-")
        paths = write_all(cls.tmp)
        cls.inbody_csv = os.path.join(cls.tmp, "inbody_sample.csv")
        cls.apple_xml = os.path.join(cls.tmp, "apple_export_sample.xml")
        assert len(paths) == 2

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestDetection(ParserFixture):
    def test_detect_kinds(self):
        self.assertEqual(detect_kind(self.inbody_csv), "inbody")
        self.assertEqual(detect_kind(self.apple_xml), "apple")

    def test_detect_unsupported_raises_with_helpful_message(self):
        p = os.path.join(self.tmp, "notes.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("hello")
        with self.assertRaises(ValueError) as ctx:
            detect_kind(p)
        self.assertTrue(str(ctx.exception))

    def test_parse_missing_file_raises(self):
        with self.assertRaises((FileNotFoundError, ValueError)):
            parse_path(os.path.join(self.tmp, "nope.csv"))


class TestInBody(ParserFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ds = parse_path(cls.inbody_csv)

    def test_profile_extracted(self):
        self.assertEqual(self.ds.profile.get("height_cm"), 172.0)
        self.assertEqual(self.ds.profile.get("age"), 28)
        self.assertEqual(self.ds.profile.get("sex"), "男")

    def test_two_measurement_dates_parsed(self):
        mass = self.ds.series("body_mass")
        self.assertEqual([o.date for o in mass], ["2026-01-10", "2026-05-20"])
        self.assertAlmostEqual(mass[0].value, 74.0)
        self.assertAlmostEqual(mass[1].value, 71.2)

    def test_device_reference_range_attached(self):
        """InBody 报告自带的参考范围必须挂在观测值上 —— 它优先于内置表。"""
        bmi = self.ds.latest("bmi")
        self.assertIsNotNone(bmi.ref, "设备自带的 BMI 区间没被解析出来")
        self.assertAlmostEqual(bmi.ref.low, 18.5)
        self.assertAlmostEqual(bmi.ref.high, 23.0)

    def test_value_not_in_any_column_is_not_invented(self):
        """变化量列必须被跳过，不能被当成一次「测量」。"""
        for o in self.ds.series("body_mass"):
            self.assertNotAlmostEqual(o.value, -2.8)  # 那是变化量列的 -2.8

    def test_text_rows_become_labels_not_observations(self):
        self.assertTrue(any("judgement" in k for k in self.ds.labels),
                        f"没有解析出文字判定：{self.ds.labels}")

    def test_segmental_and_impedance_metrics_present(self):
        for mid in ("lean_left_leg", "lean_pct_left_leg", "fat_trunk", "z_left_leg_20k"):
            self.assertTrue(self.ds.series(mid), f"缺少 {mid}")

    def test_units_normalized_to_catalog_units(self):
        for o in self.ds.observations:
            spec = catalog.get(o.metric)
            if spec is not None:
                self.assertEqual(o.unit, spec.unit, f"{o.metric} 的单位没归一")


class TestAppleHealth(ParserFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ds = parse_path(cls.apple_xml)

    def test_profile_from_me_element(self):
        self.assertEqual(self.ds.profile.get("sex"), "男")
        self.assertTrue(self.ds.profile.get("birth_date"))
        self.assertIsNotNone(self.ds.profile.get("age"))

    def test_daily_metrics_parsed(self):
        self.assertTrue(self.ds.series("steps"))
        self.assertTrue(self.ds.series("resting_hr"))
        self.assertTrue(self.ds.series("body_mass"))
        self.assertTrue(self.ds.series("hrv_sdnn"))

    def test_aggregation_keeps_it_small(self):
        """一天多条记录必须被聚合成一条，否则内存会爆。"""
        per_day = {}
        for o in self.ds.observations:
            per_day.setdefault((o.metric, o.date), 0)
            per_day[(o.metric, o.date)] += 1
        worst = max(per_day.items(), key=lambda kv: kv[1])
        self.assertEqual(worst[1], 1, f"{worst[0]} 一天有多条，聚合没生效")

    def test_step_count_is_summed_not_taken_as_snapshot(self):
        """累加型指标：值应该是全天量级，不能只剩最后一段的几米/几步。"""
        steps = self.ds.series("steps")
        self.assertTrue(steps)
        self.assertGreater(max(o.value for o in steps), 1000)

    def test_sleep_attributed_to_wake_date(self):
        total = self.ds.series("sleep_total")
        self.assertTrue(total, "没有解析出睡眠")
        for o in total:
            self.assertGreater(o.value, 0)
            self.assertLess(o.value, 14)  # 小时量级，不是分钟
        deep = self.ds.series("sleep_deep")
        self.assertTrue(deep)
        for o in deep:
            self.assertLess(o.value, 4)

    def test_sleep_stages_are_summed_not_averaged(self):
        """同一夜的多个分段必须**求和**，不能按段取平均。

        这条测试存在的理由：解析器一度把 `sleep_awake` / `sleep_inbed` 当"按段平均"
        （它们在 catalog 里是 `agg="mean"`，容易被误当成"取平均"）。真实数据上的
        表现是 `sleep_awake` 中位数 **0.033 h（2 分钟/夜）**、`sleep_inbed`
        **0.663 h**——一夜不可能只在床上待 40 分钟。

        夹具里每夜刻意有**两段** Awake 和**两段** InBed，所以求和与求平均给出
        不同的结果，能真正区分这两种实现。
        """
        awake = self.ds.series("sleep_awake")
        inbed = self.ds.series("sleep_inbed")
        self.assertTrue(awake, "没有解析出 sleep_awake")
        self.assertTrue(inbed, "没有解析出 sleep_inbed")

        # 夹具：两段 Awake 各 10 分钟 -> 求和 0.333 h；求平均 0.167 h
        for o in awake:
            self.assertAlmostEqual(o.value, 1.0 / 3.0, places=3)
        # 夹具：两段 InBed（3h40m + 4h35m）-> 求和 8.25 h；求平均 4.125 h
        for o in inbed:
            self.assertAlmostEqual(o.value, 8.25, places=3)

    def test_inbed_is_larger_than_total_sleep(self):
        """卧床时长必须大于睡着时长 —— 后者是前者的子集。

        这是「按段平均把总量摊掉」的另一个独立探针：只要平均值错得离谱，
        这条关系就会被破坏。
        """
        inbed = {o.date: o.value for o in self.ds.series("sleep_inbed")}
        total = {o.date: o.value for o in self.ds.series("sleep_total")}
        common = sorted(set(inbed) & set(total))
        self.assertTrue(common)
        for d in common:
            self.assertGreater(inbed[d], total[d], f"{d} 的卧床时长不大于睡着时长")

    # ── 睡眠时刻（入睡 / 醒来 / 睡眠中点） ────────────────────
    #
    # 夹具（见 samples.write_apple_xml）：当夜 asleep 段是
    #   前一夜 23:10→02:00、02:00→03:05、03:05→04:35、04:35→06:45；
    # 另外还有 2 段 InBed（22:50→02:30、02:30→07:05）与 2 段 Awake（06:45→06:55、06:55→07:05）。
    # 参考点 = **醒来日的前一天 18:00**。

    def test_sleep_clock_first_night_matches_the_fixture(self):
        """这条测试在守：新增的 3 个「睡眠时刻」指标的参考点与取值口径没被改坏。

        期望值按夹具手算（参考点 = 醒来日的前一天 18:00）：
          onset = 最早的 asleep start = 前一夜 23:10 → 5h10m = **310**
          wake  = 最晚的 asleep end   = 当日 06:45 → 12h45m = **765**
                  （注意不是 785：07:05 是 InBed 的结束，而 InBed 不算睡眠本身）
          mid   = (310 + 765) / 2 = **537.5**
        若有人把 InBed 或 Awake 也算进入睡/醒来的范围，wake 会变成 785/785 之外的值，
        这条测试就会红 —— 这正是它存在的意义。
        """
        onset = self.ds.series("sleep_onset_min")
        wake = self.ds.series("sleep_wake_min")
        mid = self.ds.series("sleep_mid_min")
        self.assertTrue(onset and wake and mid, "没有解析出睡眠时刻三个指标")

        self.assertAlmostEqual(onset[0].value, 310.0, places=6)
        self.assertAlmostEqual(wake[0].value, 765.0, places=6)
        self.assertAlmostEqual(mid[0].value, 537.5, places=6)

        # 单位必须等于 catalog 的规范单位（分钟），ts 取该夜**醒来**的时间戳
        for series in (onset, wake, mid):
            spec = catalog.get(series[0].metric)
            self.assertIsNotNone(spec, f"{series[0].metric} 不在 catalog 里")
            self.assertEqual(series[0].unit, spec.unit)
        self.assertEqual(wake[0].ts, "2026-05-01 06:45:00")
        self.assertEqual(onset[0].ts, wake[0].ts)
        self.assertEqual(mid[0].ts, wake[0].ts)

        # 来源必须与同一天的 sleep_total 逐日一致
        total = {o.date: o for o in self.ds.series("sleep_total")}
        for series in (onset, wake, mid):
            self.assertEqual(series[0].source, total[series[0].date].source,
                             f"{series[0].metric} 的来源与 sleep_total 不一致")

    def test_sleep_clock_is_self_consistent_every_night(self):
        """这条测试在守：mid == (onset + wake) / 2，且 wake > onset（逐夜都成立）。

        顺带守住"分钟数"这个口径：如果哪天有人改回"钟点"，跨午夜的那一夜
        会算出负数或接近 1440 的 wake，(onset + wake) / 2 立刻对不上。
        """
        onset = {o.date: o.value for o in self.ds.series("sleep_onset_min")}
        wake = {o.date: o.value for o in self.ds.series("sleep_wake_min")}
        mid = {o.date: o.value for o in self.ds.series("sleep_mid_min")}
        self.assertTrue(onset and wake and mid)
        self.assertEqual(set(onset), set(wake), "入睡与醒来的日期集合不一致")
        self.assertEqual(set(onset), set(mid), "入睡与睡眠中点的日期集合不一致")

        for d in sorted(onset):
            self.assertAlmostEqual(mid[d], (onset[d] + wake[d]) / 2.0, delta=0.5,
                                   msg=f"{d} 的睡眠中点与 (入睡 + 醒来) / 2 不一致")
            self.assertGreater(wake[d], onset[d], f"{d} 的醒来时刻不晚于入睡时刻")

    def test_sleep_clock_values_stay_in_the_monotonic_range(self):
        """这条测试在守：18:00 这个参考点选得对 —— 取值落在 0~900 的**单调区间**里。

        入睡时刻若跑到区间外（负数、或接近 1440），说明参考点选错或口径变成了钟点，
        那样下游算出来的标准差与分位数都是错的。睡眠时长本身也必须在合理范围内。
        """
        onset = {o.date: o.value for o in self.ds.series("sleep_onset_min")}
        self.assertTrue(onset)
        for d, v in onset.items():
            self.assertGreater(v, 0.0, f"{d} 的入睡时刻不是正数：{v}")
            self.assertLess(v, 720.0, f"{d} 的入睡时刻偏晚到像是参考点算错了：{v}")
        for o in self.ds.series("sleep_wake_min"):
            self.assertGreater(o.value, onset[o.date] + 120.0,
                               f"{o.date} 的睡眠时长不足 2 小时：{o.value - onset[o.date]}")
            self.assertLess(o.value, onset[o.date] + 960.0,
                            f"{o.date} 的睡眠时长超过 16 小时：{o.value - onset[o.date]}")

    def test_sleep_clock_does_not_disturb_existing_sleep_metrics(self):
        """这条测试在守：新增的 3 个「时刻」指标不能污染原来的 6 个「时长」指标。

        夹具每晚的段完全固定，逐日数值应当一点不变：
          sleep_total = 2h50m + 1h05m + 1h30m + 2h10m = 7h35m = 7.5833… h
          sleep_core  = 2h50m + 2h10m = 5 h；sleep_deep = 1h05m；sleep_rem = 1h30m
          sleep_awake = 10m + 10m = 1/3 h（不计入总睡眠）
          sleep_inbed = 3h40m + 4h35m = 8.25 h（不计入总睡眠）
        只要"睡眠时刻"的实现顺手改到了聚合路径（塞进 durations、或让它参与
        同日多来源口径、或改了指数的来源选择），这里就会红。
        """
        expected = {
            "sleep_total": 7.0 + 35.0 / 60.0,
            "sleep_core": 5.0,
            "sleep_deep": 1.0 + 5.0 / 60.0,
            "sleep_rem": 1.5,
            "sleep_awake": 1.0 / 3.0,
            "sleep_inbed": 8.25,
        }
        n_days = len(self.ds.series("sleep_total"))
        self.assertGreater(n_days, 0, "没有解析出 sleep_total")
        for metric, want in expected.items():
            series = self.ds.series(metric)
            self.assertEqual(len(series), n_days, f"{metric} 的天数与 sleep_total 不一致")
            for o in series:
                self.assertAlmostEqual(o.value, want, places=6,
                                       msg=f"{o.date} 的 {metric} 变了：{o.value} != {want}")

    def test_workouts_parsed(self):
        self.assertTrue(self.ds.workouts)
        run = [w for w in self.ds.workouts if w.activity == "跑步"]
        self.assertTrue(run, f"运动类型没中文化：{[w.activity for w in self.ds.workouts][:5]}")
        w = run[0]
        self.assertIsNotNone(w.distance_km)
        self.assertIsNotNone(w.avg_hr)
        self.assertIsNotNone(w.duration_min)
        self.assertGreater(w.distance_km, 0)

    def test_notes_mention_the_inventory(self):
        blob = " ".join(self.ds.notes)
        self.assertTrue(blob.strip(), "解析后没有任何提示信息")

    def test_files_are_basenames_only(self):
        """路径不进 Dataset —— 报告会把它展示出来，不该泄漏目录结构。"""
        for f in self.ds.files:
            self.assertNotIn(os.sep, f)


class TestParsersAgreeWithGenerator(ParserFixture):
    """解析出来的结果和生成器直接造的 Dataset 应当在指标覆盖上一致。"""

    def test_same_metrics_roughly(self):
        direct = synthetic_dataset()
        parsed = parse_path(self.inbody_csv)
        direct_inbody = {o.metric for o in direct.observations
                         if o.source == "InBody270"}
        self.assertTrue(direct_inbody.issubset(set(parsed.metric_ids())),
                        f"解析丢了指标：{sorted(direct_inbody - set(parsed.metric_ids()))}")


if __name__ == "__main__":
    unittest.main()
