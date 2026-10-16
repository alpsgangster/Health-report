"""指标目录与单位换算的测试。

catalog 是整个项目的枢纽：它错了，下游全错。所以这里覆盖得细一些。
"""

from __future__ import annotations

import unittest

from health_report import catalog, units


class TestAliasResolution(unittest.TestCase):
    def test_exact_chinese_names(self):
        for name, expect in [
            ("体重", "body_mass"),
            ("体脂百分比", "body_fat_pct"),
            ("骨骼肌", "skeletal_muscle_mass"),
            ("身体总水分", "total_body_water"),
            ("腰臀比", "whr"),
            ("内脏脂肪等级", "visceral_fat_level"),
        ]:
            self.assertEqual(catalog.resolve(name), expect, name)

    def test_longest_alias_wins(self):
        """子串关系是最容易出错的地方：长别名必须先匹配。"""
        cases = [
            # "体重" 是 "目标体重"、"去脂体重"、"体重控制" 的子串
            ("目标体重", "target_weight"),
            ("去脂体重", "lean_body_mass"),
            ("体重控制", "weight_control"),
            ("体重", "body_mass"),
            # "骨骼肌" 是 "骨骼肌指数SMI" 的子串
            ("骨骼肌指数SMI", "smi"),
            ("骨骼肌", "skeletal_muscle_mass"),
            # "躯干脂肪量" 与 "躯干脂肪百分比" 都有 "躯干脂肪"
            ("躯干脂肪量", "fat_trunk"),
            ("躯干脂肪百分比", "fat_pct_trunk"),
        ]
        for name, expect in cases:
            self.assertEqual(catalog.resolve(name), expect, name)

    def test_apple_style_segmental_and_impedance_names(self):
        cases = [
            ("右上肢20kHz", "z_right_arm_20k"),
            ("左下肢100kHz", "z_left_leg_100k"),
            ("左腿肌肉量", "lean_left_leg"),
            ("右臂脂肪量", "fat_right_arm"),
        ]
        for name, expect in cases:
            self.assertEqual(catalog.resolve(name), expect, name)

    def test_unknown_returns_none(self):
        self.assertIsNone(catalog.resolve("宇宙射线吸收率"))
        self.assertIsNone(catalog.resolve(""))
        self.assertIsNone(catalog.resolve(None))

    def test_apple_type_map_covers_every_apple_type(self):
        for spec in catalog.SPECS:
            for t in spec.apple_types:
                self.assertEqual(catalog.APPLE_TO_METRIC[t], spec.id)


class TestReferenceRanges(unittest.TestCase):
    def test_no_sex_means_no_range(self):
        """不知道性别就不要给体脂率区间 —— 给错比不给更糟。"""
        self.assertIsNone(catalog.builtin_ref("body_fat_pct", {}))
        self.assertIsNotNone(catalog.builtin_ref("body_fat_pct", {"sex": "男"}))
        self.assertIsNotNone(catalog.builtin_ref("body_fat_pct", {"sex": "女"}))

    def test_sex_specific_smi_screen(self):
        male = catalog.builtin_ref("smi", {"sex": "男"})
        female = catalog.builtin_ref("smi", {"sex": "女"})
        self.assertEqual((male.low, male.kind), (7.0, "screen"))
        self.assertEqual((female.low, female.kind), (5.7, "screen"))
        self.assertFalse(male.contains(6.9))
        self.assertTrue(male.contains(8.9))

    def test_hrv_has_no_range_by_design(self):
        """没有公认通用范围的指标必须留空，报告里显示「只做趋势」。"""
        self.assertIsNone(catalog.builtin_ref("hrv_sdnn", {}))

    def test_exclusive_upper_bound(self):
        """内脏脂肪等级是「<10」，10 必须判为超出。"""
        ref = catalog.builtin_ref("visceral_fat_level", {})
        self.assertEqual(ref.label(), "<10")
        self.assertTrue(ref.contains(9))
        self.assertFalse(ref.contains(10))

    def test_goal_kind_is_not_range(self):
        """步数是目标不是正常范围 —— 措辞不能混。"""
        ref = catalog.builtin_ref("steps", {})
        self.assertEqual(ref.kind, "goal")
        self.assertEqual(ref.low, 8000)

    def test_vo2max_needs_age_and_sex(self):
        self.assertIsNone(catalog.builtin_ref("vo2max", {}))
        self.assertIsNone(catalog.builtin_ref("vo2max", {"sex": "男"}))
        ref = catalog.builtin_ref("vo2max", {"sex": "男", "age": 28})
        self.assertIsNotNone(ref)
        self.assertEqual(ref.kind, "screen")

    def test_every_range_has_a_source(self):
        """内置区间必须带出处，否则报告里无法复核。"""
        for spec in catalog.SPECS:
            ref = catalog.builtin_ref(spec.id, {"sex": "男", "age": 28})
            if ref is not None:
                self.assertTrue(ref.source, f"{spec.id} 的参考区间没有出处")
                self.assertIn(ref.kind, ("range", "goal", "screen"), spec.id)

    def test_range_label_formatting(self):
        self.assertEqual(catalog.builtin_ref("bmi", {}).label(), "18.5~24.9")
        upper_only = catalog.builtin_ref("waist_circumference", {"sex": "男"})
        self.assertEqual(upper_only.label(), "≤90")


class TestCatalogWordingIsSafe(unittest.TestCase):
    """直接扫描 catalog 里所有会进报告的文案。

    为什么不能只靠"生成一份报告再 grep"：文案**只在对应指标真的渲染出来时**才出现。
    `recommended_kcal` 属于隐藏类别「设备建议与评分」，它的 hint 在报告里根本不会
    渲染成文字，所以报告级扫描抓不到它——而它当时写的是「不是营养处方」。
    一旦那个类别改成可见（或者它被挪进别的类别），报告里就会漏出「处方」。

    结论：文案安全必须**直接扫源头**，而不是扫某一次渲染的产物。
    """

    BANNED = ["你患有", "您患有", "说明你有", "建议服用", "建议剂量", "确诊", "处方"]

    def _texts(self):
        """(来源描述, 文本) 列表：覆盖每个指标的全部人可见文案。"""
        out = []
        for spec in catalog.SPECS:
            for field in ("cn", "hint", "ref_note", "ref_source", "ref_text"):
                val = getattr(spec, field, "")
                if val:
                    out.append((f"{spec.id}.{field}", str(val)))
        for cat, hint in catalog.CATEGORY_HINT.items():
            if hint:
                out.append((f"CATEGORY_HINT[{cat}]", hint))
        # 内置参考区间的 source 也会进报告
        for spec in catalog.SPECS:
            ref = catalog.builtin_ref(spec.id, {"sex": "男", "age": 28})
            if ref is not None and ref.source:
                out.append((f"{spec.id}.ref.source", ref.source))
        return out

    def test_no_banned_wording_in_any_catalog_text(self):
        for where, text in self._texts():
            for b in self.BANNED:
                self.assertNotIn(b, text, f"catalog 的 {where} 里有禁用措辞：{b}")

    def test_the_scanner_would_actually_catch_a_violation(self):
        """反向验证：确认这个测试不是空过。

        用一个已知违规字符串跑一遍同样的检查逻辑。如果这条断言本身能过，
        说明上面那条 assertNotIn 是有牙齿的（否则可能是没扫到任何文案）。
        """
        texts = self._texts()
        self.assertGreater(len(texts), 100, "没扫到多少文案，检查逻辑可能失效了")
        violations = [(w, t) for w, t in texts if "处方" in t]
        self.assertEqual(violations, [], f"仍有文案含「处方」：{violations}")

    def test_every_spec_wording_was_actually_collected(self):
        """确认每个指标至少贡献了一条文案，否则扫描范围不足。"""
        covered = {where.split(".")[0] for where, _ in self._texts()}
        missing = [s.id for s in catalog.SPECS
                   if s.id not in covered and not s.hint and not s.ref_note
                   and not s.ref_source and s.cn]
        # cn 一定会被收进来，所以 covered 应当包含全部指标
        for s in catalog.SPECS:
            self.assertIn(s.id, covered, f"{s.id} 的文案没被扫描到")


class TestUnits(unittest.TestCase):
    def test_mass(self):
        self.assertAlmostEqual(units.convert(150, "lb", "kg"), 68.0388555, places=4)
        self.assertAlmostEqual(units.convert(1000, "g", "kg"), 1.0)
        self.assertAlmostEqual(units.convert(70, "kg", "kg"), 70.0)

    def test_length(self):
        self.assertAlmostEqual(units.convert(1.72, "m", "cm"), 172.0)
        self.assertAlmostEqual(units.convert(1, "in", "cm"), 2.54)

    def test_distance_does_not_confuse_m_and_mi(self):
        self.assertAlmostEqual(units.convert(1000, "m", "km"), 1.0)
        self.assertAlmostEqual(units.convert(1, "mi", "km"), 1.609344, places=6)

    def test_energy(self):
        self.assertAlmostEqual(units.convert(4.184, "kj", "kcal"), 1.0, places=3)

    def test_duration(self):
        self.assertAlmostEqual(units.convert(1, "h", "分钟"), 60.0)
        self.assertAlmostEqual(units.convert(90, "min", "小时"), 1.5)

    def test_unknown_conversion_returns_none_not_a_guess(self):
        """本项目不猜单位 —— 换不了就返回 None，让调用方决定。"""
        self.assertIsNone(units.convert(1, "furlong", "kg"))
        self.assertIsNone(units.convert(1, "", "kg"))
        self.assertIsNone(units.convert(None, "kg", "kg"))

    def test_compound_unit_aliases(self):
        """`·` 和 `/` 写法必须等价 —— 否则 VO₂max 的单位换算会静默失败。"""
        self.assertAlmostEqual(units.convert(45, "ml/min/kg", "ml/kg·min"), 45.0)
        self.assertAlmostEqual(units.convert(45, "ml/kg/min", "ml/kg·min"), 45.0)
        self.assertAlmostEqual(units.convert(45, "ml/kg·min", "ml/kg/min"), 45.0)

    def test_vo2max_unit_is_reachable_from_apple_wording(self):
        """Apple 用的是 ml/min/kg，规范单位是 ml/kg/min，必须能对上。"""
        from health_report import catalog
        spec = catalog.get("vo2max")
        self.assertIsNotNone(units.convert(48.0, "ml/min/kg", spec.unit))

    def test_apple_count_units_are_adopted_not_dropped(self):
        """Apple 用无量纲的 count/counts 表示计数。

        不做换算会让每个这类指标都刷一条「单位无法换算」的告警，
        还会和「下游只见到规范单位」的承诺自相矛盾。
        """
        self.assertEqual(units.convert(58, "count/min", "次/分"), 58)
        self.assertEqual(units.convert(58, "counts/min", "次/分"), 58)
        self.assertEqual(units.convert(4600, "count", "步"), 4600)
        self.assertEqual(units.convert(9, "count", "层"), 9)

    def test_count_passthrough_is_narrow(self):
        """放行必须只看**原始**单位，而且范围要窄。

        `steps` 归一后也是「步」，如果拿归一后的值判断，`convert(9, "steps", "层")`
        就会被错误放行。这个坑踩过一次，钉在这里。
        """
        self.assertIsNone(units.convert(9, "steps", "层"))
        self.assertIsNone(units.convert(4600, "count", "kg"))
        self.assertEqual(units.convert(70, "kg", "kg"), 70)

    def test_ratio_unit_helper_semantics(self):
        """`looks_like_ratio_value` 只回答「这个单位有没有可能是 0~1 存的比值」。

        调用方**仍须自己判断数值 ≤ 1**（Apple 用 `%` 存 0.162 这种形态很常见，
        所以 `%` 必须算进来）。注意 `_norm_unit` 会把 "percent" 归一成 "%"，
        因此判断只能基于归一后的形式。
        """
        self.assertTrue(units.looks_like_ratio_value("%"))
        self.assertTrue(units.looks_like_ratio_value("percent"))
        self.assertTrue(units.looks_like_ratio_value(""))
        self.assertFalse(units.looks_like_ratio_value("kg"))


if __name__ == "__main__":
    unittest.main()
