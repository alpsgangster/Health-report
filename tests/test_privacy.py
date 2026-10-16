"""设备名脱敏的测试。

背景（真实数据实测）：Apple 导出的 `sourceName` 默认带机主姓名，例如
「某某的Apple Watch」——某份真实导出里有 14,883 条记录来自这样一个名字。
而本工具会把来源名打印进报告的「数据来源」和每一行的来源列，于是
**一份用来分享的报告会把机主姓名一起带出去**。
"""

from __future__ import annotations

import unittest

from health_report import privacy


class TestMaskSourceName(unittest.TestCase):
    def test_chinese_default_device_name(self):
        self.assertEqual(privacy.mask_source_name("某某的Apple Watch"), "Apple Watch")
        self.assertEqual(privacy.mask_source_name("张三的iPhone"), "iPhone")

    def test_english_possessive(self):
        self.assertEqual(privacy.mask_source_name("Zhang San's iPhone"), "iPhone")
        self.assertEqual(privacy.mask_source_name("Li’s Apple Watch"), "Apple Watch")

    def test_names_without_a_person_prefix_are_untouched(self):
        """用户自己改过的设备名不能被误改 —— 他需要靠它认出哪台是哪台。"""
        for name in ("明成祖钛镶九龙戏珠手镯", "明熹宗玄铝淬九曜手环",
                     "iPhone", "Apple Watch", "智能秤", "InBody270", "血氧"):
            self.assertEqual(privacy.mask_source_name(name), name, name)

    def test_empty_and_none_safe(self):
        self.assertEqual(privacy.mask_source_name(""), "")
        self.assertEqual(privacy.mask_source_name(None), None)  # type: ignore[arg-type]
        self.assertEqual(privacy.mask_source_name("   "), "")

    def test_idempotent(self):
        once = privacy.mask_source_name("某某的Apple Watch")
        self.assertEqual(privacy.mask_source_name(once), once)

    def test_model_name_with_digits_is_not_treated_as_a_person(self):
        """「...Series 7 的...」这种前缀不是姓名，不该被剥掉。"""
        s = "Watch Series 7 的显示屏"
        self.assertEqual(privacy.mask_source_name(s), s)

    def test_detector(self):
        self.assertTrue(privacy.looks_like_personal_name("某某的Apple Watch"))
        self.assertFalse(privacy.looks_like_personal_name("Apple Watch"))
        self.assertFalse(privacy.looks_like_personal_name(""))


class TestAliasSourceName(unittest.TestCase):
    def test_alias_is_stable_and_readable(self):
        self.assertEqual(privacy.alias_source_name("irrelevant", 0), "设备A")
        self.assertEqual(privacy.alias_source_name("irrelevant", 1), "设备B")
        self.assertEqual(privacy.alias_source_name("x", 26), "设备A1")


if __name__ == "__main__":
    unittest.main()
