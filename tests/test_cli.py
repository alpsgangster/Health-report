"""命令行端到端测试。

包括一条**零配置承诺**的测试：`health-report demo` 不联网、不要 key、
不需要任何输入文件，也必须产出一份完整报告。README 里那句"30 秒看到结果"
就是靠这个测试保证的。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from health_report import cli


def run_cli(*argv):
    """跑一次 CLI，返回 (退出码, stdout, stderr)。"""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestDemo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="health-report-cli-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_demo_needs_no_config_and_produces_a_full_report(self):
        for var in ("HEALTH_REPORT_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(var, None)
        out_path = os.path.join(self.tmp, "demo.md")
        code, stdout, stderr = run_cli("demo", "-o", out_path)
        self.assertEqual(code, cli.EXIT_OK, stderr)
        self.assertTrue(os.path.exists(out_path))
        text = open(out_path, encoding="utf-8").read()
        self.assertGreater(len(text), 1500, "报告太短，不像完整报告")
        for section in ("这不是医学诊断", "数据可信度提示", "指标总览",
                        "规则层结论", "方法与口径"):
            self.assertIn(section, text, f"demo 报告缺少：{section}")

    def test_demo_can_list_findings(self):
        code, stdout, _ = run_cli("demo", "-o", os.path.join(self.tmp, "d.md"),
                                  "--findings")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("HYDRATION_AMPLIFIES", stdout)

    def test_demo_html_produces_the_client_facing_deliverable(self):
        """`demo --html` 必须也能出 HTML —— 这是"30 秒看到长什么样"最该给的东西。

        存在的理由：`--html` 一开始只加在 `report` 子命令上，于是想看 HTML 报告
        得先去仓库里找合成导出再跑 `report examples/...`。而 `demo` 存在的全部
        意义就是省掉这一步——**最该看到交付形态的入口反而看不到它**。
        这是按"子命令"分类、而不是按"用户想干什么"分类留下的缺口。
        """
        html_path = os.path.join(self.tmp, "demo.html")
        md_path = os.path.join(self.tmp, "demo.md")
        code, stdout, stderr = run_cli("demo", "-o", md_path, "--html", html_path)
        self.assertEqual(code, cli.EXIT_OK, stderr)
        # 两份产物都要有：--html 是"另存一份"，不该顶掉 Markdown
        self.assertTrue(os.path.exists(md_path), "给了 --html 就不写 Markdown 了")
        self.assertTrue(os.path.exists(html_path))
        self.assertIn("张图", stdout, "没告诉用户生成了多少张图")

        html = open(html_path, encoding="utf-8").read()
        self.assertGreater(len(html), 50_000, "HTML 太短，不像完整报告")
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertTrue(html.rstrip().endswith("</html>"))
        self.assertIn("<svg", html, "一张图都没有")
        self.assertNotIn("<script", html.lower())
        for bad in ('src="http', "src='http", 'href="http', "href='http",
                    "@import", "<link"):
            self.assertNotIn(bad, html, f"HTML 里出现了外部引用：{bad}")

    def test_demo_without_html_does_not_write_html(self):
        """不给 `--html` 时行为必须和以前完全一样（不产生额外文件）。"""
        md_path = os.path.join(self.tmp, "only.md")
        code, stdout, stderr = run_cli("demo", "-o", md_path)
        self.assertEqual(code, cli.EXIT_OK, stderr)
        self.assertNotIn("HTML 版已写入", stdout)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["only.md"])

    def test_demo_default_output_path(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            code, stdout, _ = run_cli("demo")
            self.assertEqual(code, cli.EXIT_OK)
            self.assertTrue(os.path.exists(os.path.join("out", "demo_report.md")))
        finally:
            os.chdir(cwd)


class TestReportCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="health-report-cli-")
        from health_report.samples import write_all
        write_all(self.tmp)
        self.csv = os.path.join(self.tmp, "inbody_sample.csv")
        self.xml = os.path.join(self.tmp, "apple_export_sample.xml")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_report_from_generated_files(self):
        out_path = os.path.join(self.tmp, "r.md")
        code, stdout, stderr = run_cli("report", self.csv, self.xml,
                                       "-o", out_path, "--no-llm")
        self.assertEqual(code, cli.EXIT_OK, stderr)
        text = open(out_path, encoding="utf-8").read()
        self.assertIn("这不是医学诊断", text)
        self.assertIn("规则层结论", text)

    def test_html_flag_writes_a_self_contained_report(self):
        """`--html` 出的是**自包含**文档：无外部资源、无 JS、A4 分页齐备。

        "自包含"是这条交付形态的全部意义：客户拿到一个文件，双击就能在浏览器里
        打印成 PDF，不需要联网、不需要别的资源、不需要装东西。
        所以这里不只断言"有 HTML"，而是断言**没有任何外部引用**——
        一旦有人往模板里加了个 CDN 字体或 `<script src>`，这条测试就该红。
        """
        html_path = os.path.join(self.tmp, "r.html")
        md_path = os.path.join(self.tmp, "r.md")
        code, _, stderr = run_cli("report", self.csv, self.xml, "--no-llm",
                                  "-o", md_path, "--html", html_path)
        self.assertEqual(code, cli.EXIT_OK, stderr)
        text = open(html_path, encoding="utf-8").read()

        self.assertTrue(text.startswith("<!DOCTYPE html>"))
        self.assertTrue(text.rstrip().endswith("</html>"))

        # 无 JS（打印不需要它；静态标签在纸上更可靠）
        self.assertNotIn("<script", text.lower())
        # 无外部资源。注意：SVG 的 xmlns="http://www.w3.org/2000/svg" 是**命名空间
        # 标识符**，不会被网络请求，所以不能一概禁止 http:// ——要看的是"引用"。
        for pattern in ('src="http', "src='http", 'href="http', "href='http",
                        "@import", "<link"):
            self.assertNotIn(pattern, text, f"HTML 里出现了外部引用：{pattern}")

        # 有图，且打印分页规则在
        self.assertIn("<svg", text)
        self.assertIn("@page", text)
        # 两份交付物必须说同一句话（判定措辞来自同一套助手）
        md = open(md_path, encoding="utf-8").read()
        for phrase in ("这不是医学诊断", "规则层结论"):
            self.assertIn(phrase, md)
            self.assertIn(phrase, text)

    def test_html_alone_does_not_dump_markdown(self):
        """只给 `--html` 时不要往终端倒 20 多 KB 的 Markdown。"""
        html_path = os.path.join(self.tmp, "only.html")
        code, stdout, stderr = run_cli("report", self.csv, "--no-llm", "--html", html_path)
        self.assertEqual(code, cli.EXIT_OK, stderr)
        self.assertNotIn("# 健康数据报告", stdout, "Markdown 正文被倒进了终端")
        self.assertIn("未写 Markdown", stdout)

    def test_report_with_json_dump(self):
        facts_path = os.path.join(self.tmp, "facts.json")
        code, _, stderr = run_cli("report", self.csv, "--no-llm",
                                  "-o", os.path.join(self.tmp, "r.md"),
                                  "--json", facts_path)
        self.assertEqual(code, cli.EXIT_OK, stderr)
        facts = json.load(open(facts_path, encoding="utf-8"))
        self.assertIn("metrics", facts)

    def test_no_llm_never_calls_network(self):
        """--no-llm 时不应该有任何联网尝试，即使环境里有 key。"""
        os.environ["HEALTH_REPORT_API_KEY"] = "sk-fake-should-not-be-used"
        try:
            code, stdout, stderr = run_cli("report", self.csv, "--no-llm",
                                           "-o", os.path.join(self.tmp, "r.md"))
            self.assertEqual(code, cli.EXIT_OK, stderr)
            self.assertNotIn("生成 AI 解读中", stderr)
        finally:
            os.environ.pop("HEALTH_REPORT_API_KEY", None)

    def test_llm_flag_without_any_backend_fails_clearly(self):
        """强制走云端后端、又没配 key 时，要给出可操作的中文提示。

        这里必须显式 `--ai-backend http`。原因是后端的探测顺序里，
        **本机已装的 AI 工具（dsh / claude / codex）也算可用后端**——
        在装了这些工具的机器上，「--llm 但没有 key」不再必然失败，
        那正是这个功能想要的效果。所以测试要锁定的是"一个后端都没有"的情形。
        """
        for var in ("HEALTH_REPORT_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(var, None)
        code, stdout, stderr = run_cli("report", self.csv, "--llm", "--ai-backend", "http")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("HEALTH_REPORT_API_KEY", stderr)
        self.assertIn("不加 --llm", stderr)

    def test_invalid_ai_backend_is_rejected(self):
        """非法后端名由 argparse 的 choices 直接挡下（抛 SystemExit(2)）。"""
        with self.assertRaises(SystemExit) as ctx:
            run_cli("report", self.csv, "--ai-backend", "不存在的后端")
        self.assertEqual(ctx.exception.code, 2)

    def test_ai_backend_none_means_pure_rule_report(self):
        """`--ai-backend none` 必须完全不做 AI：报告里说明「没有生成 AI 解读」。"""
        out_path = os.path.join(self.tmp, "none.md")
        code, _, stderr = run_cli("report", self.csv, "--ai-backend", "none", "-o", out_path)
        self.assertEqual(code, cli.EXIT_OK, stderr)
        text = open(out_path, encoding="utf-8").read()
        self.assertIn("没有生成 AI 解读", text)
        self.assertNotIn("数据将发送至", stderr)

    def test_missing_input_file_gives_clean_error(self):
        code, _, stderr = run_cli("report", os.path.join(self.tmp, "nope.csv"))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("找不到输入", stderr)

    def test_bad_input_format_gives_clean_error(self):
        p = os.path.join(self.tmp, "notes.txt")
        open(p, "w", encoding="utf-8").write("hello")
        code, _, stderr = run_cli("report", p)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertTrue(stderr.strip())


class TestParseCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="health-report-cli-")
        from health_report.samples import write_all
        write_all(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_writes_structured_output(self):
        outdir = os.path.join(self.tmp, "out")
        code, stdout, stderr = run_cli("parse", os.path.join(self.tmp, "inbody_sample.csv"),
                                       "-o", outdir)
        self.assertEqual(code, cli.EXIT_OK, stderr)
        for fn in ("observations.csv", "workouts.csv", "dataset.json", "summary.txt"):
            self.assertTrue(os.path.exists(os.path.join(outdir, fn)), f"缺少 {fn}")

    def test_csv_is_excel_friendly(self):
        """中文 Windows 的 Excel 需要 BOM 才不会乱码。"""
        outdir = os.path.join(self.tmp, "out2")
        run_cli("parse", os.path.join(self.tmp, "inbody_sample.csv"), "-o", outdir)
        raw = open(os.path.join(outdir, "observations.csv"), "rb").read(3)
        self.assertEqual(raw, b"\xef\xbb\xbf", "observations.csv 没有 UTF-8 BOM")

    def test_dataset_json_roundtrips(self):
        from health_report.model import Dataset
        outdir = os.path.join(self.tmp, "out3")
        run_cli("parse", os.path.join(self.tmp, "apple_export_sample.xml"), "-o", outdir)
        data = json.load(open(os.path.join(outdir, "dataset.json"), encoding="utf-8"))
        ds = Dataset.from_dict(data)
        self.assertTrue(ds.observations)
        self.assertTrue(ds.workouts)


class TestMiscCommands(unittest.TestCase):
    def test_metrics_lists_catalog(self):
        code, stdout, _ = run_cli("metrics")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("body_mass", stdout)
        self.assertIn("只做趋势", stdout)

    def test_inputs_explains_formats(self):
        code, stdout, _ = run_cli("inputs")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("export.zip", stdout)
        self.assertIn("通用长格式", stdout)

    def test_sample_data_writes_files(self):
        tmp = tempfile.mkdtemp(prefix="health-report-cli-")
        try:
            code, stdout, _ = run_cli("sample-data", "-o", tmp)
            self.assertEqual(code, cli.EXIT_OK)
            self.assertTrue(os.path.exists(os.path.join(tmp, "inbody_sample.csv")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "apple_export_sample.xml")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_command_shows_help_and_exits_nonzero(self):
        code, stdout, _ = run_cli()
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("health-report", stdout)

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            run_cli("--version")
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
