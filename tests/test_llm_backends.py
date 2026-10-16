# -*- coding: utf-8 -*-
"""llm.py 的后端探测、CLI 调用与数字溯源校验测试（标准库 unittest）。

设计原则
--------
* **绝不真的联网、绝不真的跑 agent**：探测函数（`_probe_local_http`）与运行函数
  （`_run_agent_to_files`）都用 `unittest.mock.patch` 换掉，断言的是
  「拼出来的命令 / 临时目录里放了什么 / stdout 是不是文件重定向 / 清理了没有」。
* 少数几处用真实的**本机进程**（`sys.executable` 跑一个一行的临时脚本）来验证
  「文件重定向 + cwd + 退出码 + 超时」这条链路本身，因为那正是踩过的坑
  （沙箱下管道可能被拒），不能只靠 mock 证明。
* 每个测试都自带环境变量隔离：`LLMConfig.from_env()` 读的是进程环境，
  所以 setUp/tearDown 会把相关变量清干净再还原，测试可以反复跑。
* 不依赖网络、不留临时目录（`addCleanup` 兜底删除）。
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from health_report import llm  # noqa: E402
from health_report.llm import (  # noqa: E402
    DEFAULT_BASE_URL,
    LLMConfig,
    NarrateResult,
    _CLI_AGENTS,
    _NUMBER_WHITELIST,
    build_cli_prompt,
    build_messages,
    clean_cli_output,
    detect_backend,
    narrate,
    narrate_ex,
    verify_numbers,
)

_ENV_KEYS = (
    "HEALTH_REPORT_API_KEY", "OPENAI_API_KEY",
    "HEALTH_REPORT_BASE_URL", "OPENAI_BASE_URL",
    "HEALTH_REPORT_MODEL", "OPENAI_MODEL",
    "HEALTH_REPORT_TIMEOUT",
    "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    "DSH_HOME", "DSH_INSTALL_DIR", "DSH_APP_DIR", "DSH_DESKTOP_DIR",
)


class _EnvIsolated(unittest.TestCase):
    """把 llm 关心的环境变量清空/还原，保证测试可反复跑。"""

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _ENV_KEYS}
        for key in _ENV_KEYS:
            os.environ.pop(key, None)
        # 有些用例故意走「校验失败 / 运行出错」的路径，那是预期行为，
        # 不需要把 warning 打到测试输出里（CRITICAL 以上仍然会显示）。
        logging.disable(logging.ERROR)
        self.addCleanup(logging.disable, logging.NOTSET)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _no_agents(self) -> None:
        """让「本机装了哪个 CLI agent」变成确定性的「都没装」。"""
        patcher = mock.patch.object(llm, "_find_agent", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _no_local_http(self) -> None:
        """让「本机有没有 HTTP 模型」变成确定性的「都没有」。

        ⚠️ 这一条是**硬要求**，不是省事：真实的探测会去连 127.0.0.1 的三个端口，
        每个 0.5 秒超时——一个没屏蔽的用例就能让整套测试慢 1.5 秒，
        而且会随运行环境（本机到底装没装 Ollama）产生不同结果。
        单元测试里不允许任何真实网络调用。
        """
        patcher = mock.patch.object(llm, "_probe_local_http", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pin_backend(self, backend: llm.Backend) -> None:
        """把整个「挑后端」这一步钉死成一个给定后端。

        只替换 `_detect_cli` 是不够的：`detect_backend` 会先看环境变量、
        再真的去探三个本机端口，所以只钉 CLI 的话每个用例都要等 1.5 秒超时。
        这里直接替换 `detect_backend`，把网络探测整段跳过去。
        """
        patcher = mock.patch.object(llm, "detect_backend", return_value=backend)
        patcher.start()
        self.addCleanup(patcher.stop)


# ═══════════════════════════════════════════ auto 绝不自作主张把数据发出去


class TestAutoNeverLeaksData(_EnvIsolated):
    """`auto`（默认）不得自动选用**会离开本机**的后端。

    这是一条**隐私默认值**，不是探测顺序问题，所以单独钉一组测试。

    实测踩到：客户机器上装了 DSH 时，`health-report report 导出.xml`
    （**既没配 key、也没加 `--llm`**）会静默把健康摘要发给 DeepSeek——
    终端上确实打印了「数据将发送至：DeepSeek」，但对一个自称"隐私优先、
    默认什么都不发出去"的工具来说，**默认静默外发是不能接受的**。
    `docs/PRIVACY.md` 那张「什么时候会联网」的表里，`report 文件` 写的是"不"，
    所以那条承诺必须由代码兑现，而不是靠用户去读探测顺序的文档。

    分工：`auto` 只用**不出本机**的东西（本机模型）或者**用户已显式配置**的东西
    （环境变量里的端点）；会外发的 CLI 工具必须显式开口（`--llm` 或
    `--ai-backend dsh/claude/codex`）。
    """

    def _all_three_agents_available(self) -> None:
        """三个 CLI agent 都装好、都有凭据——对 auto 来说诱惑最大。"""
        find = mock.patch.object(llm, "_find_agent",
                                 side_effect=lambda name: "/usr/local/bin/" + name)
        creds = mock.patch.object(llm, "_agent_credential_state",
                                  return_value=(True, "creds"))
        find.start()
        creds.start()
        self.addCleanup(creds.stop)
        self.addCleanup(find.stop)

    def test_auto_refuses_a_cloud_cli_agent(self) -> None:
        """三个 agent 全装好、全有凭据，auto 也必须放弃。"""
        self._no_local_http()
        self._all_three_agents_available()
        backend = detect_backend(LLMConfig.from_env(), "auto")
        self.assertEqual(backend.kind, "none",
                         "auto 选中了会外发的后端，等于默认偷偷上传健康数据")
        self.assertEqual(backend.sends_to, "不发送任何数据")

    def test_allow_remote_is_what_unlocks_it(self) -> None:
        """`--llm`（= allow_remote）才解锁。"""
        self._no_local_http()
        self._all_three_agents_available()
        backend = detect_backend(LLMConfig.from_env(), "auto", "", allow_remote=True)
        self.assertEqual(backend.kind, "cli")
        self.assertEqual(backend.sends_to, "DeepSeek")

    def test_explicit_prefer_needs_no_allow_remote(self) -> None:
        """显式点名 `--ai-backend dsh` 本身就是同意，不用再叠 `--llm`。"""
        self._no_local_http()
        self._all_three_agents_available()
        backend = detect_backend(LLMConfig.from_env(), "dsh")
        self.assertEqual(backend.kind, "cli")
        self.assertEqual(backend.sends_to, "DeepSeek")

    def test_auto_still_uses_a_local_model(self) -> None:
        """本机模型不出机器，所以 auto 照用——不能因为收紧而把它也挡掉。"""
        with mock.patch.object(llm, "_probe_local_http", return_value="qwen2.5:7b"):
            with mock.patch.object(llm, "_find_agent", return_value=r"C:\x\dsh.cmd"):
                with mock.patch.object(llm, "_agent_credential_state", return_value=(True, "creds")):
                    backend = detect_backend(LLMConfig.from_env(), "auto")
        self.assertEqual(backend.kind, "http")
        self.assertTrue(backend.is_local)
        self.assertEqual(backend.sends_to, "本机（不出机器）")

    def test_auto_still_uses_an_explicitly_configured_endpoint(self) -> None:
        """环境变量里配了 key = 用户知情，auto 照用（这是原来的行为）。"""
        self._no_local_http()
        self._all_three_agents_available()
        with mock.patch.object(llm, "_probe_local_http", return_value=None):
            config = LLMConfig(api_key="sk-test", base_url="https://example.invalid/v1",
                               model="m")
            backend = detect_backend(config, "auto")
        self.assertEqual(backend.kind, "http")
        self.assertFalse(backend.is_local)
        self.assertIn("example.invalid", backend.sends_to)


# ═══════════════════════════════════════════════════ detect_backend 优先级


class TestDetectBackendPriority(_EnvIsolated):
    """优先级：显式 prefer > 环境变量 > 本机 HTTP > CLI > none。"""

    def test_nothing_available_is_none(self) -> None:
        self._no_agents()
        self._no_local_http()
        backend = detect_backend(LLMConfig.from_env())
        self.assertEqual(backend.kind, "none")
        self.assertEqual(backend.name, "无")
        self.assertFalse(backend.is_local)
        self.assertEqual(backend.sends_to, "不发送任何数据")

    def test_env_endpoint_beats_local_and_cli(self) -> None:
        """环境变量配置的端点优先于本机 HTTP 与 CLI。"""
        os.environ["HEALTH_REPORT_API_KEY"] = "sk-test-1234567890"
        os.environ["HEALTH_REPORT_BASE_URL"] = "https://api.deepseek.com"
        os.environ["HEALTH_REPORT_MODEL"] = "deepseek-chat"
        probe = mock.patch.object(llm, "_probe_local_http",
                                  side_effect=AssertionError("不该探测本机 HTTP"))
        probe.start()
        self.addCleanup(probe.stop)
        find = mock.patch.object(llm, "_find_agent",
                                 side_effect=AssertionError("不该探测 CLI"))
        find.start()
        self.addCleanup(find.stop)

        backend = detect_backend(LLMConfig.from_env())
        self.assertEqual(backend.kind, "http")
        self.assertEqual(backend.name, "OpenAI 兼容端点")
        self.assertEqual(backend.model, "deepseek-chat")
        self.assertEqual(backend.base_url, "https://api.deepseek.com")
        self.assertFalse(backend.is_local)
        self.assertEqual(backend.sends_to, "api.deepseek.com")
        self.assertIn("deepseek-chat", backend.detail)

    def test_local_http_beats_cli(self) -> None:
        """本机 HTTP 模型优先于 CLI agent，且是唯一 is_local=True 的后端。"""
        self._no_agents()
        with mock.patch.object(llm, "_probe_local_http", return_value="qwen2.5:7b") as probe:
            backend = detect_backend(LLMConfig.from_env())
        self.assertEqual(probe.call_args_list[0].args[0], "http://127.0.0.1:11434/v1")
        self.assertEqual(backend.kind, "http")
        self.assertEqual(backend.name, "Ollama")
        self.assertEqual(backend.model, "qwen2.5:7b")
        self.assertTrue(backend.is_local)
        self.assertEqual(backend.sends_to, "本机（不出机器）")
        self.assertIn("127.0.0.1:11434", backend.base_url)

    def test_local_http_candidates_are_loopback_only(self) -> None:
        """三个候选端口都只连 127.0.0.1，集合固定（顺序由优先级决定，不靠线程先后）。"""
        probed = []

        def fake_probe(url, timeout=0.0):
            probed.append((url, timeout))
            return None

        self._no_agents()
        with mock.patch.object(llm, "_probe_local_http", side_effect=fake_probe):
            backend = detect_backend(LLMConfig.from_env())
        self.assertEqual(sorted(url for url, _ in probed), [
            "http://127.0.0.1:11434/v1",
            "http://127.0.0.1:1234/v1",
            "http://127.0.0.1:8080/v1",
        ])
        self.assertTrue(all(url.startswith("http://127.0.0.1:") for url, _ in probed))
        self.assertEqual(len(probed), 3, "三个候选各探测一次")
        self.assertEqual(backend.kind, "none")

    def test_local_http_priority_is_deterministic(self) -> None:
        """并发探测不影响优先级：Ollama 优先于 LM Studio 优先于 llama.cpp。"""
        def fake_probe(url, timeout=0.0):
            return {"http://127.0.0.1:11434/v1": None,
                    "http://127.0.0.1:1234/v1": "lmstudio-model",
                    "http://127.0.0.1:8080/v1": "llamacpp-model"}.get(url)

        self._no_agents()
        with mock.patch.object(llm, "_probe_local_http", side_effect=fake_probe):
            backend = detect_backend(LLMConfig.from_env())
        self.assertEqual(backend.name, "LM Studio")
        self.assertEqual(backend.model, "lmstudio-model")

    def test_local_http_probe_is_concurrent_not_serial(self) -> None:
        """串行探测在一个都没装时要等 3 × 0.5 秒；并发后最坏 0.5 秒。

        这是实测出来的体验问题：`health-report report` 即便走纯规则模式，
        也会因为自动探测而卡 1.5 秒。这里用「每个探测都睡 0.3 秒」来验证
        三个候选确实是同时跑的（总耗时远小于 0.9 秒）。
        """
        def slow_probe(url, timeout=0.0):
            time.sleep(0.3)
            return None

        self._no_agents()
        started = time.time()
        with mock.patch.object(llm, "_probe_local_http", side_effect=slow_probe):
            backend = detect_backend(LLMConfig.from_env())
        elapsed = time.time() - started
        self.assertEqual(backend.kind, "none")
        self.assertLess(elapsed, 0.6, "三个候选应当并发探测（实测 {0:.2f} 秒）".format(elapsed))

    def test_cli_beats_none_and_is_not_local(self) -> None:
        """CLI agent 装在客户机器上，但模型在云端：is_local 必须是 False。

        用 `prefer="cli"` 显式指定——因为 `auto` **刻意不会**自动选用会外发的后端
        （见 `TestAutoNeverLeaksData`）。这条测的是"选中之后的字段语义"。
        """
        self._no_local_http()
        with mock.patch.object(llm, "_find_agent",
                               side_effect=lambda name: r"C:\x\bin\{0}.cmd".format(name) if name == "dsh" else ""):
            with mock.patch.object(llm, "_agent_credential_state", return_value=(True, "creds")):
                backend = detect_backend(LLMConfig.from_env(), "cli")
        self.assertEqual(backend.kind, "cli")
        self.assertEqual(backend.name, "DSH")
        self.assertFalse(backend.is_local)
        self.assertEqual(backend.sends_to, "DeepSeek")
        self.assertEqual(backend.command, r"C:\x\bin\dsh.cmd")
        self.assertIn("dsh.cmd", backend.detail)

    def test_cli_order_is_dsh_then_claude_then_codex(self) -> None:
        """按 dsh → claude → codex 的顺序检测，先命中的先用（`prefer="cli"`）。"""
        calls = []

        def fake_find(name):
            calls.append(name)
            return "/usr/local/bin/" + name

        self._no_local_http()
        with mock.patch.object(llm, "_find_agent", side_effect=fake_find):
            with mock.patch.object(llm, "_agent_credential_state", return_value=(True, "creds")):
                backend = detect_backend(LLMConfig.from_env(), "cli")
        self.assertEqual(calls, ["dsh"])
        self.assertEqual(backend.name, "DSH")

        calls.clear()
        with mock.patch.object(llm, "_find_agent",
                               side_effect=lambda n: "" if n == "dsh" else "/usr/local/bin/" + n):
            with mock.patch.object(llm, "_agent_credential_state", return_value=(True, "creds")):
                backend = detect_backend(LLMConfig.from_env(), "cli")
        self.assertEqual(calls, [])
        self.assertEqual(backend.name, "Claude Code")
        self.assertEqual(backend.sends_to, "Anthropic")

        with mock.patch.object(llm, "_find_agent",
                               side_effect=lambda n: "/usr/local/bin/" + n if n == "codex" else ""):
            with mock.patch.object(llm, "_agent_credential_state", return_value=(True, "creds")):
                backend = detect_backend(LLMConfig.from_env(), "cli")
        self.assertEqual(backend.name, "Codex")
        self.assertEqual(backend.sends_to, "OpenAI")

    def test_cli_without_credentials_keeps_probing(self) -> None:
        """装了 dsh 但没配凭据 → 继续往下探测，命中已登录的 claude。"""
        def fake_find(name):
            return {"dsh": r"C:\dsh\dsh.cmd", "claude": "/usr/bin/claude"}.get(name, "")

        def fake_creds(agent):
            return (agent == "claude"), ("ok" if agent == "claude" else r"C:\Users\x\.dsh")

        self._no_local_http()
        with mock.patch.object(llm, "_find_agent", side_effect=fake_find):
            with mock.patch.object(llm, "_agent_credential_state", side_effect=fake_creds):
                backend = detect_backend(LLMConfig.from_env(), "cli")
        self.assertEqual(backend.name, "Claude Code")
        self.assertNotIn("未检测到", backend.detail)

    def test_cli_without_any_credentials_reports_it(self) -> None:
        """全都装了但都没凭据：返回第一条并如实标注「未配置凭据」，不当成可用。"""
        self._no_local_http()
        with mock.patch.object(llm, "_find_agent",
                               side_effect=lambda name: r"C:\bin\{0}.cmd".format(name)):
            with mock.patch.object(llm, "_agent_credential_state", return_value=(False, r"C:\Users\x\.dsh")):
                backend = detect_backend(LLMConfig.from_env(), "cli")
        self.assertEqual(backend.kind, "cli")
        self.assertEqual(backend.name, "DSH")
        self.assertIn("未检测到已配置的模型凭据", backend.detail)
        self.assertEqual(backend.sends_to, "DeepSeek")


class TestDetectBackendPrefer(_EnvIsolated):
    """`prefer` 显式指定与非法值。"""

    def test_prefer_none_skips_all_probing(self) -> None:
        os.environ["HEALTH_REPORT_API_KEY"] = "sk-test-1234567890"
        probe = mock.patch.object(llm, "_probe_local_http",
                                  side_effect=AssertionError("prefer=none 不该探测"))
        probe.start()
        self.addCleanup(probe.stop)
        find = mock.patch.object(llm, "_find_agent",
                                 side_effect=AssertionError("prefer=none 不该探测"))
        find.start()
        self.addCleanup(find.stop)
        backend = detect_backend(LLMConfig.from_env(), prefer="none")
        self.assertEqual(backend.kind, "none")
        self.assertEqual(backend.sends_to, "不发送任何数据")
        self.assertIn("显式关闭", backend.detail)

    def test_invalid_prefer_raises_value_error_in_chinese(self) -> None:
        # 注意：首尾空白会被 __normalize_prefer 忽略（那是宽容的），所以这里
        # 用的是「归一化之后仍然非法」的取值。
        for bad in ("gpt", "openai", "auto2", "本地", "http api", "claude code"):
            with self.subTest(prefer=bad):
                with self.assertRaises(ValueError) as ctx:
                    detect_backend(LLMConfig.from_env(), prefer=bad)
                message = str(ctx.exception)
                self.assertIn("prefer 取值非法", message)
                self.assertIn("auto", message)

    def test_prefer_is_case_insensitive_and_accepts_off(self) -> None:
        self._no_agents()
        self._no_local_http()
        with mock.patch.object(llm, "_agent_credential_state", return_value=(False, "")):
            self.assertIsInstance(detect_backend(LLMConfig.from_env(), prefer="AUTO"), llm.Backend)
        self.assertEqual(detect_backend(LLMConfig.from_env(), prefer="OFF").kind, "none")

    def test_prefer_dsh_returns_none_when_not_installed(self) -> None:
        self._no_agents()
        backend = detect_backend(LLMConfig.from_env(), prefer="dsh")
        self.assertEqual(backend.kind, "none")
        self.assertNotIn("DSH", backend.name)

    def test_prefer_explicit_agent_does_not_fall_back_to_another(self) -> None:
        """指定 codex 时不会偷偷换成已装的 dsh——用户说的是「就用这个」。"""
        probe = mock.patch.object(llm, "_probe_local_http",
                                  side_effect=AssertionError("指定后端时不该探测本机 HTTP"))
        probe.start()
        self.addCleanup(probe.stop)
        with mock.patch.object(llm, "_find_agent",
                               side_effect=lambda name: r"C:\bin\dsh.cmd" if name == "dsh" else ""):
            backend = detect_backend(LLMConfig.from_env(), prefer="codex")
        self.assertEqual(backend.kind, "none")

    def test_prefer_http_uses_local_model_when_no_env_key(self) -> None:
        self._no_agents()
        with mock.patch.object(llm, "_probe_local_http", return_value="llama3.1:8b"):
            backend = detect_backend(LLMConfig.from_env(), prefer="http")
        self.assertEqual(backend.name, "Ollama")
        self.assertTrue(backend.is_local)
        self.assertEqual(backend.sends_to, "本机（不出机器）")

    def test_prefer_cli_uses_first_available_agent(self) -> None:
        self._no_local_http()
        with mock.patch.object(llm, "_find_agent", side_effect=lambda n: "/usr/local/bin/" + n):
            with mock.patch.object(llm, "_agent_credential_state", return_value=(True, "creds")):
                backend = detect_backend(LLMConfig.from_env(), prefer="cli")
        self.assertEqual(backend.kind, "cli")
        self.assertEqual(backend.name, "DSH")


class TestCommandOverride(_EnvIsolated):
    """`command_override`（CLI 的 `--ai-command`）：显式指定没进 PATH 的可执行文件。"""

    def test_override_wins_over_auto_detection(self) -> None:
        self._no_local_http()
        find = mock.patch.object(llm, "_find_agent",
                                 side_effect=AssertionError("给了 override 就不该再探测 PATH"))
        find.start()
        self.addCleanup(find.stop)
        backend = detect_backend(LLMConfig(), command_override=r"D:\x\bin\dsh.cmd")
        self.assertEqual(backend.kind, "cli")
        self.assertEqual(backend.name, "DSH")
        self.assertEqual(backend.command, r"D:\x\bin\dsh.cmd")
        self.assertEqual(backend.sends_to, "DeepSeek")
        self.assertFalse(backend.is_local)

    def test_override_existing_file_is_made_absolute(self) -> None:
        workdir = tempfile.mkdtemp(prefix="health-report-override-")
        self.addCleanup(shutil.rmtree, workdir, True)
        path = os.path.join(workdir, "claude.cmd")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("@echo off\n")
        backend = detect_backend(LLMConfig(), prefer="claude", command_override=path)
        self.assertEqual(backend.name, "Claude Code")
        self.assertEqual(backend.command, os.path.abspath(path))
        self.assertEqual(backend.sends_to, "Anthropic")

    def test_override_with_unrecognizable_name_is_ignored(self) -> None:
        """认不出是哪个工具就不猜子命令：降级为 none，而不是乱拼命令。"""
        self._no_agents()
        self._no_local_http()
        backend = detect_backend(LLMConfig(), command_override=r"D:\x\my-agent.cmd")
        self.assertEqual(backend.kind, "none")
        self.assertEqual(backend.sends_to, "不发送任何数据")

    def test_override_with_unrecognizable_name_but_explicit_prefer(self) -> None:
        """路径认不出来，但用户同时说了是 codex —— 那就按 codex 走。"""
        backend = detect_backend(LLMConfig(), prefer="codex",
                                 command_override=r"D:\x\my-agent.cmd")
        self.assertEqual(backend.kind, "cli")
        self.assertEqual(backend.name, "Codex")
        self.assertEqual(backend.command, r"D:\x\my-agent.cmd")

    def test_override_is_ignored_for_http_and_none(self) -> None:
        os.environ["HEALTH_REPORT_API_KEY"] = "sk-test-1234567890"
        os.environ["HEALTH_REPORT_BASE_URL"] = "https://api.deepseek.com"
        backend = detect_backend(LLMConfig.from_env(), prefer="http",
                                 command_override=r"D:\x\dsh.cmd")
        self.assertEqual(backend.kind, "http")
        self.assertEqual(backend.name, "OpenAI 兼容端点")
        backend = detect_backend(LLMConfig(), prefer="none",
                                 command_override=r"D:\x\dsh.cmd")
        self.assertEqual(backend.kind, "none")

    def test_agent_recognition_from_basename(self) -> None:
        cases = {
            r"C:\x\bin\dsh.cmd": "dsh",
            r"C:\x\bin\DSH.CMD": "dsh",
            "/usr/local/bin/claude": "claude",
            "/usr/local/bin/claude.exe": "claude",
            "/usr/local/bin/codex": "codex",
            "/usr/local/bin/my-agent": "",
            "": "",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(llm._command_override_agent(path), expected)

    def test_agent_recognition_does_not_depend_on_the_platform(self) -> None:
        """识别 `--ai-command` 的路径**不能依赖 `os.path` 的平台行为**。

        这是第一次跑 CI 时 7 个 POSIX job 全红的根因。原来的实现用
        `os.path.basename(path)`：在 Linux/macOS 上分隔符是 `/`，于是
        `basename(r"D:\\x\\bin\\dsh.cmd")` **把整条路径原样返回**，认不出这是 dsh。
        后果是「用户显式指定的命令」被静默忽略、继续去探测 PATH——
        在 Windows 上永远看不到（那边 basename 认得反斜杠），一到 POSIX 就炸。

        用户手里的路径可能来自任何平台（配置、文档、脚本），所以两种分隔符都要认。
        这里显式把 `os.path.basename` 换成 POSIX 实现来复现 Linux 行为——
        不这么做，这条测试在 Windows 上永远是绿的，等于没测。
        """
        original = os.path.basename
        os.path.basename = posixpath.basename      # 模拟 Linux/macOS
        try:
            for path, expected in (
                (r"D:\x\bin\dsh.cmd", "dsh"),
                (r"C:\Users\x\AppData\Local\claude.exe", "claude"),
                (r"\\server\share\codex.bat", "codex"),
                ("/usr/local/bin/dsh", "dsh"),
                (r"D:\x\my-agent.cmd", ""),
            ):
                with self.subTest(path=path):
                    self.assertEqual(llm._command_override_agent(path), expected)
        finally:
            os.path.basename = original

    def test_unrecognised_override_does_not_fall_back_to_probing_path(self) -> None:
        """给了 `--ai-command` 但认不出时，**不许**改去探测 PATH 找别的后端。

        行为上的理由：用户显式说了「用这个命令」，静默换成别的后端
        （例如本机装好的 DSH）违背他的意图——那正是 `--ai-command` 的意义。
        这条在 CI 上是被 `_find_agent` 的副作用断言出来的，这里单独写明。
        """
        probed: List[str] = []

        def record(name: str) -> str:
            probed.append(name)
            return ""

        with mock.patch.object(llm, "_find_agent", side_effect=record):
            backend = llm.detect_backend(llm.LLMConfig(),
                                         command_override=r"D:\x\my-agent.cmd")
        self.assertEqual(backend.kind, "none")
        self.assertEqual(probed, [], "认不出 override 时不该去探测 PATH")

    def test_override_is_ignored_when_prefer_says_no_cli(self) -> None:
        """`--ai-command` 与 `--ai-backend http/none` 同时给：**以 prefer 为准**。

        用户说了「不走 CLI」，那就忽略那个命令，而不是被它挡住。
        """
        for prefer in ("none", "http"):
            with self.subTest(prefer=prefer):
                backend = llm.detect_backend(llm.LLMConfig(), prefer,
                                             r"D:\x\bin\dsh.cmd")
                self.assertEqual(backend.kind, "none",
                                 "prefer 说了不走 CLI，却仍然采用了 override")

    def test_narrate_ex_forwards_command_override(self) -> None:
        seen = {}

        def fake_narrate(agent, command, facts, *, lang, timeout):
            seen["agent"] = agent
            seen["command"] = command
            return "你的步数是 5687。", ""

        with mock.patch.object(llm, "_cli_narrate", side_effect=fake_narrate):
            result = narrate_ex({"steps": 5687}, LLMConfig(),
                                command_override=r"D:\x\bin\codex.cmd")
        self.assertEqual(seen["agent"], "codex")
        self.assertEqual(seen["command"], r"D:\x\bin\codex.cmd")
        self.assertEqual(result.text, "你的步数是 5687。")


class TestBackendFieldSemantics(_EnvIsolated):
    """`sends_to` / `is_local` 对每种后端的取值（PRIVACY.md 依赖这两个字段）。"""

    def test_every_cli_agent_declares_its_vendor(self) -> None:
        expected = {"dsh": "DeepSeek", "claude": "Anthropic", "codex": "OpenAI"}
        self.assertEqual({k: v["vendor"] for k, v in _CLI_AGENTS.items()}, expected)
        for agent, meta in _CLI_AGENTS.items():
            backend = llm._cli_backend(agent, "/x/" + agent)
            self.assertEqual(backend.sends_to, meta["vendor"])
            self.assertFalse(backend.is_local, "CLI agent 不是本机模型，不能标 is_local")
            self.assertEqual(backend.kind, "cli")

    def test_image_style_detail_never_says_local_ai(self) -> None:
        """CLI 后端的 detail 必须写明「模型在云端」，不能含糊成本地 AI。"""
        for agent in ("dsh", "claude", "codex"):
            backend = llm._cli_backend(agent, "/x/" + agent)
            self.assertIn("云端", backend.detail)
            self.assertNotIn("不出机器", backend.detail)

    def test_local_http_backend_is_local(self) -> None:
        backend = llm._local_http_backend("LM Studio", "http://127.0.0.1:1234/v1", "m")
        self.assertTrue(backend.is_local)
        self.assertEqual(backend.sends_to, "本机（不出机器）")

    def test_env_endpoint_is_not_marked_local(self) -> None:
        config = LLMConfig(api_key="k", base_url="http://127.0.0.1:9999/v1", model="m")
        backend = llm._http_backend(config, config.base_url, config.model)
        self.assertFalse(backend.is_local)
        self.assertEqual(backend.sends_to, "127.0.0.1")

    def test_sends_to_falls_back_to_configured_endpoint(self) -> None:
        self.assertEqual(llm._host_of(""), "")
        backend = llm.detect_backend(LLMConfig(api_key="", base_url="", model=""), prefer="none")
        self.assertEqual(backend.sends_to, "不发送任何数据")


# ═══════════════════════════════════════════════════════ 数字溯源校验


class TestVerifyNumbers(unittest.TestCase):
    """`verify_numbers` 的正例与负例。"""

    def test_facts_number_is_accepted(self) -> None:
        facts = {"metrics": [{"metric": "steps", "latest": 5687}]}
        ok, bad = verify_numbers("你今天一共走了 5687 步，比昨天多一点。", facts)
        self.assertTrue(ok, bad)
        self.assertEqual(bad, [])

    def test_number_absent_from_facts_is_rejected(self) -> None:
        facts = {"metrics": [{"metric": "resting_heart_rate", "latest": 61.0}]}
        ok, bad = verify_numbers("你的静息心率 58.2，属于偏低。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["58.2"])

    def test_thousands_separator_is_normalized(self) -> None:
        ok, bad = verify_numbers("总消耗 1,910 千卡。", {"active_energy": 1910})
        self.assertTrue(ok, bad)

    def test_wan_multiplier_is_expanded(self) -> None:
        """1.08 万 → 10800，要和 facts 里的 10800 对得上。"""
        ok, bad = verify_numbers("累计约 1.08 万步。", {"steps_total": 10800})
        self.assertTrue(ok, bad)
        ok, bad = verify_numbers("累计约 3.2千步。", {"steps_total": 3200})
        self.assertTrue(ok, bad)

    def test_wan_multiplier_does_not_hide_a_fabrication(self) -> None:
        """1.08 万 与 facts 里的 10862753 对不上——不能因为乘开了就放过。"""
        ok, bad = verify_numbers("累计约 1.08 万步。", {"steps_total": 10862753})
        self.assertFalse(ok)

    def test_cjk_multiplier_exemption_only_covers_qian(self) -> None:
        """「千」可以组成单位名，「万」不可以——这条分界线很容易搞错。

        * `千米 / 千克 / 千瓦 / 千卡 / 千焦` 都是**真正的单位名**，
          所以「1500 千米」是 1500 km，乘成 1500000 会把正确解读判成编造。
        * `万米 / 万克 / 万瓦` 都**不是**单位名，中文里「1.08 万米」就是 10800 米。

        实测踩到：早期版本把「万」也一并豁免，于是「走了 1.08 万米」被误判成
        无法溯源（facts 里正是 10800），整段正确的解读被丢掉——误杀比漏抓更糟。
        """
        # 「千」+ 单位字：不乘
        for text, facts in (
            ("距离 1500 千米。", {"distance_km": 1500.0}),
            ("活动能量 1,910 千卡。", {"active_energy": 1910.0}),
            ("体重 68 千克。", {"body_mass": 68.0}),
        ):
            ok, bad = verify_numbers(text, facts)
            self.assertTrue(ok, f"{text} 被误乘了：{bad}")

        # 「万」+ 同一个单位字：要乘
        ok, bad = verify_numbers("走了 1.08 万米。", {"distance_m": 10800.0})
        self.assertTrue(ok, f"「万米」没有乘开：{bad}")
        ok, bad = verify_numbers("走了 1.08 万米。", {"distance_m": 99999.0})
        self.assertFalse(ok, "乘开之后仍然对不上，就该判为编造")
        self.assertEqual(bad, ["10800"])

    def test_window_days_are_whitelisted(self) -> None:
        ok, bad = verify_numbers("近 90 天里数据比较稳，建议把窗口拉长到 4 周以上（28 天）。", {})
        self.assertTrue(ok, bad)

    def test_percent_sign_keeps_the_digits(self) -> None:
        facts = {"metrics": [{"metric": "body_fat", "latest": 18.7}]}
        ok, bad = verify_numbers("体脂率 18.7%，比上次低。", facts)
        self.assertTrue(ok, bad)
        ok, bad = verify_numbers("血氧 99% 正常。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["99"])

    def test_fullwidth_digits_are_handled(self) -> None:
        facts = {"metrics": [{"metric": "weight", "latest": 70.5}]}
        ok, bad = verify_numbers("体重是 ７０.５ 公斤。", facts)
        self.assertTrue(ok, bad)
        ok, bad = verify_numbers("体重是 ７０.９ 公斤。", facts)
        self.assertFalse(ok)

    def test_chinese_text_with_embedded_numbers(self) -> None:
        facts = {"profile": {"height_cm": 178, "age": 41}}
        ok, bad = verify_numbers("身高178厘米，41岁，参考区间按这两项算。", facts)
        self.assertTrue(ok, bad)

    def test_superscript_and_units_are_ignored(self) -> None:
        """kg/m² 里的 ² 是单位的一部分，不是数值。"""
        facts = {"metrics": [{"metric": "bmi", "latest": 22.9}]}
        ok, bad = verify_numbers("BMI 是 22.9 kg/m²。", facts)
        self.assertTrue(ok, bad)

    def test_float_tail_in_facts_is_tolerated(self) -> None:
        """facts 里可能是 0.30000000000000004，相对/绝对误差 1e-6 内算同一个数。"""
        facts = {"metrics": [{"metric": "x", "latest": 0.30000000000000004}]}
        ok, bad = verify_numbers("比值 0.3。", facts)
        self.assertTrue(ok, bad)

    def test_relative_error_does_not_swallow_real_differences(self) -> None:
        ok, bad = verify_numbers("58.25", {"value": 58.2})
        self.assertFalse(ok)
        self.assertEqual(bad, ["58.25"])

    def test_small_integers_are_whitelisted_by_design(self) -> None:
        """明确的取舍：小整数（1~12）放行，「编造 3 次训练」这类计数会漏过。"""
        ok, bad = verify_numbers("本周完成 3 次训练，分 3 个小节说明。", {})
        self.assertTrue(ok, bad)
        self.assertIn(3.0, _NUMBER_WHITELIST)
        self.assertIn(90.0, _NUMBER_WHITELIST)

    def test_keys_are_part_of_the_allowed_set(self) -> None:
        """键名里的数字（days_90 之类）也算口径的一部分。"""
        ok, bad = verify_numbers("近 90 天有 42 个数据点。", {"days_90": {"n": 42}})
        self.assertTrue(ok, bad)

    def test_multiple_fabrications_are_all_listed(self) -> None:
        ok, bad = verify_numbers("静息心率 58.2，血氧 97，体温 36.4。", {})
        self.assertFalse(ok)
        self.assertEqual(bad, ["58.2", "97", "36.4"])

    def test_container_length_is_traceable(self) -> None:
        """「共 29 个指标」是数 metrics 数组得到的合法事实，不能判成编造。"""
        facts = {"metrics": [{} for _ in range(29)], "data": {"sources": ["a", "b", "c"]}}
        ok, bad = verify_numbers("本次报告共纳入 29 个指标，来自 3 台设备。", facts)
        self.assertTrue(ok, bad)
        # 27 不是任何容器的长度，也不在白名单里，仍然要被抓住
        ok, bad = verify_numbers("本次报告共纳入 27 个指标。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["27"])
        # 容器长度属于「允许集」，但**不算**「模型读到了 facts」的证据
        self.assertIn(29.0, llm._facts_numbers(facts))
        self.assertNotIn(29.0, llm._facts_literal_numbers(facts))
        self.assertIn(29.0, llm._facts_length_numbers(facts))

    def test_string_length_is_not_treated_as_a_fact(self) -> None:
        """设备名字符串的长度不是事实，不能进允许集。"""
        facts = {"data": {"sources": ["Apple Watch"]}}  # "Apple Watch" 长 11，但 11 不在允许集里
        ok, bad = verify_numbers("Apple Watch。", facts)
        self.assertTrue(ok, "文本里的数字来自别处才算数")
        # 11 本身在白名单（小整数）里，所以换一个不在白名单、也不是任何容器长度的数
        ok, bad = verify_numbers("这条记录长 11 个字符，另外还有 23 条。", facts)
        self.assertFalse(ok)
        self.assertIn("23", bad)

    def test_rounded_value_is_traceable(self) -> None:
        """模型把 facts 里的数按位舍入是正常写法（实测真实数据必踩）。"""
        facts = {"mean_sleep_hours": 6.289373745308546, "latest": 8.533055555555555}
        ok, bad = verify_numbers("均值 6.29 h、最新 8.53 h。", facts)
        self.assertTrue(ok, bad)
        ok, bad = verify_numbers("均值 6.3 h。", facts)
        self.assertTrue(ok, bad)
        # 但舍入通道不能掩盖真正的编造
        ok, bad = verify_numbers("均值 6.5 h。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["6.5"])

    def test_rounding_does_not_swallow_a_different_measurement(self) -> None:
        """舍入容差是按位算的，不是固定放大 0.05：61 舍入不出 58.2。"""
        ok, bad = verify_numbers("静息心率 58.2", {"resting_heart_rate": 61.0})
        self.assertFalse(ok)
        self.assertEqual(bad, ["58.2"])

    def test_integer_rounding_of_decimal_facts(self) -> None:
        ok, bad = verify_numbers("静息心率均值 56。", {"mean": 56.4})
        self.assertTrue(ok, bad)
        ok, bad = verify_numbers("静息心率均值 57。", {"mean": 56.4})
        self.assertFalse(ok)
        self.assertEqual(bad, ["57"])

    def test_empty_values_no_numbers(self) -> None:
        ok, bad = verify_numbers("", {})
        self.assertTrue(ok)
        self.assertEqual(bad, [])
        ok, bad = verify_numbers("数据里没有这一项。", {"a": 1})
        self.assertTrue(ok, bad)

    def test_unserializable_facts_do_not_raise(self) -> None:
        class Weird:
            pass

        ok, bad = verify_numbers("2.5", {"bad": Weird()})
        self.assertFalse(ok)
        self.assertEqual(bad, ["2.5"])


class TestVerifyNumbersMisKillFixes(unittest.TestCase):
    """真机实测抓出来的四类**误杀**：逐条用当时的**原文**做正例。

    背景：真机上一次输出被删了 13 处句子，逐条看绝大多数是**合法写法**
    （Markdown 小节号、空格千分位、合理的量级改写、facts 里已有的总量），
    不是编造。这里的每一条都是「之前通不过、现在必须通过」。

    这道校验的核心能力（编造的测量值一律拦下）原样保留，
    反面用例集中在 `TestVerifyNumbersNotLoosened`。
    """

    def test_markdown_heading_number_is_not_a_value(self) -> None:
        """误杀 1-a：`### 2.2 …` / `### 4.1 …` 里的 `2.2`、`4.1` 是**小节号**。

        真机原文（一字不改）：
            ### 2.2 步数日均 5687 步，低于常见建议的 ≥8000 步
            ### 4.1 覆盖率：很多指标其实是「半年有、半年没有」
        facts 里当然没有 2.2，于是整句被删——而它根本不是测量值。
        """
        facts = {"metrics": [{"metric": "steps", "latest": 5687, "ref_low": 8000,
                              "ref_kind": "goal"}]}
        ok, bad = verify_numbers("### 2.2 步数日均 5687 步，低于常见建议的 ≥8000 步", facts)
        self.assertTrue(ok, bad)
        ok, bad = verify_numbers("### 4.1 覆盖率：很多指标其实是「半年有、半年没有」", {})
        self.assertTrue(ok, bad)

    def test_bold_and_list_section_numbers_are_not_values(self) -> None:
        """误杀 1-b：`**2.2**` 这种加粗小节号、以及行首的 `1.` `2)` `3、` 列表编号。"""
        facts = {"steps": 5687, "resting_heart_rate": 61.0}
        for text in ("**2.2** 步数日均 5687 步",
                     "## **4.1** 覆盖率",
                     "### 3.1.4 单点读数与趋势的区别",
                     "1. 步数 5687 步",
                     "2) 静息心率 61 次/分",
                     "3、这一小节没有数字"):
            with self.subTest(text=text):
                ok, bad = verify_numbers(text, facts)
                self.assertTrue(ok, f"{text} 被误判：{bad}")

    def test_space_thousands_separator_is_one_number(self) -> None:
        """误杀 2：`11 534` 是**空格千分位**的 11534，与逗号千分位同等对待。

        真机原文（一字不改）：`则是 11 534 km（**1.43 倍**）`
        facts 里是 11534（或 11534.x）。
        """
        facts = {"distance_km": 11534.0, "ratio": 1.43}
        ok, bad = verify_numbers("则是 11 534 km（**1.43 倍**）", facts)
        self.assertTrue(ok, bad)
        self.assertEqual(llm._iter_numbers("11 534"), [11534.0])
        ok, bad = verify_numbers("总距离 11 534.5 km", {"distance_km": 11534.5})
        self.assertTrue(ok, bad)

    def test_wan_with_significant_digit_rounding(self) -> None:
        """误杀 3：`1086 万` / `1563 万` 是 facts 里大数**保留 4 位有效数字**的写法。

        真机原文（一字不改）：`步数 1086 万 vs 1563 万（**1.44 倍**）`
        facts 里是 10862753 与 15634979：
            10862753 保留 4 位有效数字 = 10860000 → 「1086 万」✓
            15634979 保留 4 位有效数字 = 15630000 → 「1563 万」✓
        这是合理的量级改写，不是编造。
        """
        facts = {"steps_a": 10862753, "steps_b": 15634979, "ratio": 1.44}
        ok, bad = verify_numbers("步数 1086 万 vs 1563 万（**1.44 倍**）", facts)
        self.assertTrue(ok, bad)

    def test_training_totals_row_is_traceable(self) -> None:
        """误杀 4：训练总量那一行——`20.2 万 kcal` 必须乘成 202000。

        真机原文（一字不改）：`| 训练记录 | 1385 次，累计约 704 小时，约 20.2 万 kcal |`
        1385 在 facts 里；704 与 202000 由报告侧补进 facts，
        所以这里锁的是**「万」乘开**这条通道本身。
        """
        facts = {"workouts": {"count": 1385, "total_hours": 704.0, "total_kcal": 202000}}
        ok, bad = verify_numbers("| 训练记录 | 1385 次，累计约 704 小时，约 20.2 万 kcal |", facts)
        self.assertTrue(ok, bad)
        ok, bad = verify_numbers("约 20.2 万 kcal", {"kcal": 202000.0})
        self.assertTrue(ok, bad)
        # 乘开之后照样要校验：facts 里没有这个总量时仍然判为编造
        ok, bad = verify_numbers("约 20.2 万 kcal", {"workouts": {"count": 1385}})
        self.assertFalse(ok)
        self.assertEqual(bad, ["202000"])


class TestVerifyNumbersNotLoosened(unittest.TestCase):
    """「必须继续抓住」的编造，以及「没有放宽过头」的边界。

    这道校验存在的理由就是前三条：真机实测里模型写过「约 20.2 万 kcal」
    「280 万条原始记录」这类无法溯源的数字，校验把含这些数字的句子删掉了。
    四类误杀修好之后，这些反面例子必须一条不松。
    """

    def test_fabricated_resting_heart_rate_is_still_rejected(self) -> None:
        """facts 里 resting_hr = 61.0，文本写 58.2 → 必须判为编造。"""
        facts = {"metrics": [{"metric": "resting_heart_rate", "latest": 61.0,
                              "metric_cn": "静息心率"}]}
        ok, bad = verify_numbers("你的静息心率是 58.2 次/分，比上月明显升高。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["58.2"])
        # 小节号豁免**不能**顺手放过行首的裸小数：`58.2 次/分` 不是编号
        ok, bad = verify_numbers("58.2 次/分是这次的静息心率读数。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["58.2"])

    def test_wan_of_the_wrong_magnitude_is_still_rejected(self) -> None:
        """facts 里 steps_total = 10862753 时，「万」写错量级照样是编造。"""
        facts = {"steps_total": 10862753}
        ok, bad = verify_numbers("累计约 1.08 万步。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["10800"])
        # 「900 万」= 9000000；10862753 按 3 位有效数字是 10900000，
        # 按 1 位是 10000000 —— 两种读法都对不上，所以这条通道没被放宽过头。
        ok, bad = verify_numbers("步数约 900 万。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["9000000"])
        # 「1080 万」= 10800000：差在有效数字上，也要被抓住
        ok, bad = verify_numbers("步数约 1080 万。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["10800000"])

    def test_fabricated_record_count_is_still_rejected(self) -> None:
        """facts 里没有记录条数时，「280 万条原始记录」必须判为编造。"""
        facts = {"steps_total": 10862753, "resting_heart_rate": 61.0}
        ok, bad = verify_numbers("共整理 280 万条原始记录。", facts)
        self.assertFalse(ok)
        self.assertEqual(bad, ["2800000"])

    def test_body_number_that_looks_like_a_section_number_is_still_checked(self) -> None:
        """没有放宽过头：**正文里**的 2.2 / 4.1 仍然要校验（它们不是小节号）。"""
        ok, bad = verify_numbers("步数上升了 2.2。", {})
        self.assertFalse(ok)
        self.assertEqual(bad, ["2.2"])
        for text in ("这一段的斜率是 2.2。",
                     "覆盖率从 4.1 掉到 3.8。",
                     "日均 5687 步，比上月高了 4.1 个百分点。"):
            with self.subTest(text=text):
                ok, bad = verify_numbers(text, {})
                self.assertFalse(ok, f"{text} 不该被放行：{bad}")

    def test_space_separated_numbers_are_not_glued(self) -> None:
        """没有放宽过头：正常用空格断开的数字不会被粘成一个大数。"""
        self.assertEqual(llm._iter_numbers("近 7 天里有 2 台设备，共 534 条记录。"),
                         [7.0, 2.0, 534.0])
        self.assertEqual(llm._iter_numbers("1 2000"), [1.0, 2000.0])
        ok, bad = verify_numbers("共 23 台设备，534 条记录。", {"devices": 23, "records": 534})
        self.assertTrue(ok, bad)
        # 反向证明它没被粘成 23534：facts 里只有 23534 时，两个独立数字各自都不通过
        ok, bad = verify_numbers("共 23 台设备，534 条记录。", {"total": 23534})
        self.assertFalse(ok)
        self.assertEqual(bad, ["23", "534"])

    def test_significant_digit_channel_needs_a_magnitude_suffix(self) -> None:
        """量级改写通道只在识别出量级后缀（万/千/亿/K/M/B）时启用。

        facts 里是 10862753 时，纯大数 `10860000`（没有「万」）不走这条通道，
        免得把「随手写一个近似大数」也放进来。
        """
        ok, bad = verify_numbers("10860000", {"steps_total": 10862753})
        self.assertFalse(ok)
        self.assertEqual(bad, ["10860000"])
        ok, bad = verify_numbers("1086 万", {"steps_total": 10862753})
        self.assertTrue(ok, bad)

    def test_heading_like_measurement_is_still_checked(self) -> None:
        """没有放宽过头：紧跟着量级后缀的加粗/标题数字仍按**测量值**校验。

        `**20.2 万 kcal**` 里的 20.2 不是小节号（它后面还有内容），
        `## 20.2 万 kcal` 同理。
        """
        ok, bad = verify_numbers("**20.2 万 kcal**", {})
        self.assertFalse(ok)
        self.assertEqual(bad, ["202000"])
        ok, bad = verify_numbers("## 20.2 万 kcal", {})
        self.assertFalse(ok)
        self.assertEqual(bad, ["202000"])


# ═══════════════════════════════════════════════════════ CLI 调用与清洗


class TestCliCommandAssembly(unittest.TestCase):
    """命令 + 参数的字符串拼装。"""

    def test_dsh_argv(self) -> None:
        argv = llm._cli_argv("dsh", r"C:\x\bin\dsh.cmd", "提示词")
        self.assertEqual(argv, [r"C:\x\bin\dsh.cmd", "headless", "提示词"])

    def test_claude_argv_requests_json(self) -> None:
        argv = llm._cli_argv("claude", "/usr/bin/claude", "提示词")
        self.assertEqual(argv[:3], ["/usr/bin/claude", "-p", "提示词"])
        self.assertIn("--output-format", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")

    def test_codex_argv(self) -> None:
        argv = llm._cli_argv("codex", "/usr/bin/codex", "提示词")
        self.assertEqual(argv, ["/usr/bin/codex", "exec", "提示词"])

    def test_prompt_is_a_single_argument(self) -> None:
        """整段提示词作为一个 argv 元素，不做 shell 拼接。"""
        prompt = build_cli_prompt({"a": 1}, lang="zh")
        argv = llm._cli_argv("codex", "/usr/bin/codex", prompt)
        self.assertEqual(len(argv), 3)
        self.assertIs(argv[2], prompt)

    def test_cmd_suffix_needs_shell_only_on_windows(self) -> None:
        self.assertEqual(llm._needs_shell("dsh.cmd"), os.name == "nt")
        self.assertFalse(llm._needs_shell("/usr/bin/claude"))
        self.assertFalse(llm._needs_shell("codex.exe"))


class TestCliPrompt(unittest.TestCase):
    """提示词必须明令禁止读文件、跑命令、写代码，并且只输出 Markdown 正文。"""

    def test_prompt_contains_all_rules(self) -> None:
        """system prompt 的 1~12 条 + CLI 追加的 13~15 条，一条都不能少。"""
        prompt = build_cli_prompt({"metrics": [{"metric": "steps", "latest": 5687}]}, lang="zh")
        for index in range(1, 16):
            self.assertIn("{0}. ".format(index), prompt)

    def test_prompt_is_short_and_does_not_embed_facts(self) -> None:
        """facts 走文件，不进命令行——否则撞 Windows 的 ~32767 字上限。

        实测真实 facts.json 有 47 KB，塞进 `dsh headless "<提示词>"` 直接报
        `The command line is too long.`，所以提示词必须与 facts 大小无关。
        """
        small = build_cli_prompt({"steps": 5687}, lang="zh")
        huge = build_cli_prompt({"metrics": [{"i": i, "v": i * 1.5} for i in range(4000)]}, lang="zh")
        self.assertEqual(small, huge)
        self.assertLess(len(huge), 4000)
        self.assertNotIn("5687", small)

    def test_prompt_tells_agent_where_the_data_is(self) -> None:
        prompt = build_cli_prompt({}, lang="zh")
        # 数据内联在本提示词里；没有内联时说明它就在工作目录的 facts.json
        self.assertIn("<<<FACTS_JSON>>>", prompt)
        self.assertIn("`./facts.json`", prompt)
        # 必须明确禁止「向调用方索要数据」——实测 agent 真的会这么干
        self.assertIn("不要向调用方", prompt)
        self.assertIn("索要数据", prompt)

    def test_prompt_forbids_file_access_and_commands(self) -> None:
        prompt = build_cli_prompt({}, lang="zh")
        # 「读 facts.json」与「不要读写任何文件」不能写成自相矛盾的话
        self.assertIn("除了读那一个 facts.json 之外，不要读写、列出或搜索任何文件", prompt)
        self.assertIn("不要执行任何", prompt)
        self.assertIn("不要写代码", prompt)
        self.assertIn("只输出 Markdown 正文", prompt)
        self.assertIn("视为本次输出无效", prompt)

    def test_prompt_english_note_is_appended_not_replaced(self) -> None:
        zh = build_cli_prompt({}, lang="zh")
        en = build_cli_prompt({}, lang="en")
        note = "\n\n输出语言：请用英文回答，上面的约束一条都不变。"
        self.assertIn("请用英文回答", en)
        self.assertEqual(en, zh + note)
        for index in range(1, 16):
            self.assertIn("{0}. ".format(index), en)

    def test_inline_payload_is_dropped_when_facts_are_huge(self) -> None:
        """护栏：facts 太大时不再附带内容，只让 agent 读文件。"""
        prompt = build_cli_prompt({}, lang="zh")
        huge = {"metrics": [{"i": index, "v": index * 1.5} for index in range(4000)]}
        payload = llm._cli_prompt_payload(huge, lang="zh")
        self.assertEqual(payload, prompt)
        self.assertNotIn("3999", payload)
        self.assertLess(len(payload), llm._CLI_ARGV_LIMIT)

    def test_inline_payload_included_for_small_facts(self) -> None:
        payload = llm._cli_prompt_payload({"steps": 5687}, lang="zh")
        self.assertIn("5687", payload)
        self.assertIn("以下是已经计算好的事实", payload)

    def test_system_prompt_has_not_lost_any_rule(self) -> None:
        """12 条约束一条都不能删（只能追加）。"""
        system = build_messages({}, "zh")[0]["content"]
        for index in range(1, 13):
            self.assertIn("{0}. ".format(index), system)
        self.assertEqual(llm._SYSTEM_PROMPT.count("\n"), 14)

    def test_prompt_forbids_doing_arithmetic_on_facts(self) -> None:
        """真机实测补的第 12 条：模型自己加出了「约 704 小时」「约 20.2 万 kcal」
        「约 53~56%」——这些总量 facts 里没有，于是整句被数字溯源校验删掉。

        「不自己算」比事后删句子省事，所以这条约束必须留在提示词里；
        它同时对 HTTP 后端生效（`build_messages` 用的就是 `_SYSTEM_PROMPT`）。
        """
        system = build_messages({}, "zh")[0]["content"]
        self.assertIn("12. ", system)
        self.assertIn("只引用事实摘要里**已经存在**的数字", system)
        self.assertIn("不要自己做加法、求平均、算百分比或换算单位", system)
        self.assertIn("如果摘要里没有，就不要写那个数字", system)
        # CLI 追加的三条约束编号顺延成 13~15，内容一条都没丢
        prompt = build_cli_prompt({}, lang="zh")
        for index in (13, 14, 15):
            self.assertIn("{0}. ".format(index), prompt)
        self.assertIn("不要向调用方", prompt)
        self.assertIn("只输出 Markdown 正文", prompt)
        self.assertIn("视为本次输出无效", prompt)


class TestCleanCliOutput(unittest.TestCase):
    """输出清洗：保守——宁多留一句，不切正文。"""

    def test_strips_leading_tool_narration(self) -> None:
        raw = ("我先读取了 facts.json，然后开始写解读。\n\n"
               "## 结论\n\n你的静息心率是 61。\n\n本解读不是医学建议。")
        cleaned = clean_cli_output(raw)
        self.assertTrue(cleaned.startswith("## 结论"))
        self.assertIn("静息心率是 61", cleaned)

    def test_unwraps_whole_output_fence(self) -> None:
        raw = "```markdown\n## 结论\n\n体重稳定。\n```"
        cleaned = clean_cli_output(raw)
        self.assertEqual(cleaned, "## 结论\n\n体重稳定。")

    def test_picks_longest_fence_over_short_echo(self) -> None:
        raw = ("```json\n{\"a\": 1}\n```\n\n"
               "```markdown\n## 结论\n\n这是一段足够长的正文，描述数据里看到的东西。\n```")
        cleaned = clean_cli_output(raw)
        self.assertIn("这是一段足够长的正文", cleaned)
        self.assertNotIn('"a": 1', cleaned)

    def test_keeps_body_when_it_merely_starts_with_i_have(self) -> None:
        """保守边界：没有提到文件/命令的句子不能被当成过程叙述删掉。"""
        raw = "我已确认这是稳定趋势，不需要额外关注。\n\n本解读不是医学建议。"
        cleaned = clean_cli_output(raw)
        self.assertTrue(cleaned.startswith("我已确认"), cleaned)

    def test_keeps_all_body_lines(self) -> None:
        raw = ("## 结论\n\n"
               "1. 我先把数据按周汇总，看趋势。\n"
               "2. 读取的窗口是 90 天。\n"
               "3. 下一步：固定晨起测量。\n")
        cleaned = clean_cli_output(raw)
        self.assertIn("1. 我先把数据按周汇总", cleaned)
        self.assertIn("2. 读取的窗口是 90 天", cleaned)
        self.assertIn("3. 下一步", cleaned)

    def test_strips_trailing_narration_only(self) -> None:
        raw = "## 结论\n\n体重稳定。\n我已读取 facts.json 并完成分析。"
        cleaned = clean_cli_output(raw)
        self.assertEqual(cleaned, "## 结论\n\n体重稳定。")

    def test_parses_claude_json_envelope(self) -> None:
        raw = json.dumps({"type": "result", "subtype": "success",
                          "result": "## 结论\n\n体重稳定。"}, ensure_ascii=False)
        self.assertEqual(clean_cli_output(raw), "## 结论\n\n体重稳定。")

    def test_parses_codex_agent_message_event(self) -> None:
        raw = "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "## 结论\n\n体重稳定。"}}),
        ])
        self.assertEqual(clean_cli_output(raw), "## 结论\n\n体重稳定。")

    def test_empty_input(self) -> None:
        for raw in ("", "   \n\n", None):
            self.assertEqual(clean_cli_output(raw), "")

    def test_does_not_truncate_a_long_body(self) -> None:
        body = "## 结论\n\n" + "这是一段正文。" * 40
        self.assertEqual(clean_cli_output(body), body.strip())


class TestCliNarrateWorkdir(_EnvIsolated):
    """临时目录、文件重定向、超时降级、清理。"""

    def test_runs_in_empty_temp_dir_with_only_facts_json(self) -> None:
        observed = {}

        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            observed["argv"] = list(argv)
            observed["workdir"] = workdir
            observed["timeout"] = timeout
            observed["entries"] = sorted(os.listdir(workdir))
            observed["stdout_path"] = stdout_path
            observed["stderr_path"] = stderr_path
            with open(stdout_path, "w", encoding="utf-8") as handle:
                handle.write("## 结论\n\n一切都好。")
            return 0, "## 结论\n\n一切都好。", "", ""

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("dsh", r"C:\x\dsh.cmd",
                                            {"steps": 5687}, lang="zh", timeout=42.0)

        self.assertEqual(text, "## 结论\n\n一切都好。")
        self.assertEqual(reason, "")
        # 临时目录里只有 facts.json 与两个重定向文件，没有原始导出、没有报告
        self.assertIn("facts.json", observed["entries"])
        self.assertTrue(0 < len(observed["entries"]) <= 3, observed["entries"])
        # cwd 必须落在系统临时目录里，而不是客户的数据目录
        self.assertTrue(os.path.dirname(os.path.abspath(observed["workdir"])) ==
                        os.path.abspath(tempfile.gettempdir()))
        # timeout 一路传到运行函数
        self.assertEqual(observed["timeout"], 42.0)
        # 命令拼装正确
        self.assertEqual(observed["argv"][1], "headless")
        # 临时目录用完即删
        self.assertFalse(os.path.exists(observed["workdir"]))
        # stdout/stderr 走文件路径（不是管道对象）
        self.assertIsInstance(observed["stdout_path"], str)
        self.assertTrue(observed["stdout_path"].endswith(".txt"))

    def test_facts_json_content_is_the_facts(self) -> None:
        captured = {}

        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            with open(os.path.join(workdir, "facts.json"), encoding="utf-8") as handle:
                captured["facts"] = json.load(handle)
            return 0, "正文：5687", "", ""

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            llm._cli_narrate("claude", "/usr/bin/claude", {"steps": 5687}, lang="zh", timeout=5.0)
        self.assertEqual(captured["facts"], {"steps": 5687})

    def test_temp_dir_is_removed_even_when_runner_raises(self) -> None:
        seen = {}

        def boom(argv, workdir, timeout, stdout_path, stderr_path):
            seen["workdir"] = workdir
            raise RuntimeError("模拟运行函数炸了")

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=boom):
            text, reason = llm._cli_narrate("codex", "/usr/bin/codex", {}, lang="zh", timeout=1.0)
        self.assertEqual(text, "")
        self.assertIn("出错", reason)
        self.assertFalse(os.path.exists(seen["workdir"]))

    def test_timeout_is_silent_degradation(self) -> None:
        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            return -1, "", "", "已超过 180 秒仍未返回，已放弃"

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("dsh", r"C:\x\dsh.cmd", {}, lang="zh", timeout=180.0)
        self.assertEqual(text, "")
        self.assertIn("超时", reason)
        self.assertIn("DSH", reason)

    def test_nonzero_exit_is_silent_degradation(self) -> None:
        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            return 3, "", "boom", "命令退出码为 3"

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("claude", "/usr/bin/claude", {}, lang="zh", timeout=10.0)
        self.assertEqual(text, "")
        self.assertIn("命令退出码为 3", reason)

    def test_missing_credential_error_gets_actionable_chinese_hint(self) -> None:
        stderr = ('dsh: MISSING_CREDENTIAL: llm-deepseek: no API key for provider route '
                  '"deepseek-official"; store DEEPSEEK_API_KEY through the credentials service')

        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            with open(stderr_path, "w", encoding="utf-8") as handle:
                handle.write(stderr)
            return 1, "", stderr, "命令退出码为 1"

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("dsh", r"C:\x\dsh.cmd", {}, lang="zh", timeout=180.0)
        self.assertEqual(text, "")
        self.assertIn("未配置模型凭据", reason)
        self.assertIn("DEEPSEEK_API_KEY", reason)

    def test_permission_error_names_the_dsh_profile_dir(self) -> None:
        stderr = ("Error: EPERM: operation not permitted, mkdir "
                  "'C:\\Users\\x\\.dsh\\profiles\\headless'")

        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            with open(stderr_path, "w", encoding="utf-8") as handle:
                handle.write(stderr)
            return 1, "", stderr, "命令退出码为 1"

        os.environ["DSH_HOME"] = r"C:\Users\x\.dsh"
        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("dsh", r"C:\x\dsh.cmd", {}, lang="zh", timeout=180.0)
        self.assertEqual(text, "")
        self.assertIn("headless profile", reason)
        self.assertIn(r"C:\Users\x\.dsh", reason)
        self.assertIn("不可写", reason)

    def test_empty_output_is_reported(self) -> None:
        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            return 0, "", "", ""

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("codex", "/usr/bin/codex", {}, lang="zh", timeout=5.0)
        self.assertEqual(text, "")
        self.assertIn("没有输出任何内容", reason)

    def test_prompt_sent_on_command_line_does_not_contain_facts(self) -> None:
        """命令行里只有短提示词：真实 facts 有 47 KB，塞 argv 会超 Windows 上限。"""
        seen = {}

        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            seen["argv"] = list(argv)
            return 0, "正文 10800", "", ""

        huge = {"metrics": [{"i": index, "v": index * 1.5} for index in range(4000)]}
        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("dsh", r"C:\x\dsh.cmd", huge, lang="zh", timeout=5.0)
        self.assertEqual(reason, "")
        self.assertEqual(text, "正文 10800")
        argv_length = sum(len(part) + 1 for part in seen["argv"])
        self.assertLess(argv_length, llm._CLI_ARGV_LIMIT)
        self.assertNotIn("3999", seen["argv"][2])
        self.assertIn("facts.json", seen["argv"][2])

    def test_overlong_argv_is_refused_before_launching(self) -> None:
        """长度护栏：宁可自己先挡，也不要撞操作系统那个看不懂的报错。"""
        launched = []

        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            launched.append(argv)
            return 0, "不该跑到这里", "", ""

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            with mock.patch.object(llm, "_cli_prompt_payload",
                                   return_value="x" * (llm._CLI_ARGV_LIMIT + 10)):
                text, reason = llm._cli_narrate("dsh", r"C:\x\dsh.cmd", {}, lang="zh", timeout=5.0)
        self.assertEqual(text, "")
        self.assertEqual(launched, [], "超长命令行不允许真的启动进程")
        self.assertIn("提示词过长", reason)
        self.assertIn("已跳过命令行方式", reason)

    def test_failure_reason_contains_elapsed_time(self) -> None:
        """CLI agent 冷启动十几秒，用户要靠耗时区分「超时」和「一启动就报错」。"""
        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            return 1, "", "boom", "命令退出码为 1"

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("claude", "/usr/bin/claude", {}, lang="zh", timeout=10.0)
        self.assertEqual(text, "")
        self.assertIn("耗时", reason)
        self.assertIn("秒", reason)

    def test_no_retry_on_failure(self) -> None:
        """失败不重试：每次十几秒，重试会让用户以为卡死。"""
        calls = []

        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            calls.append(argv)
            return 1, "", "boom", "命令退出码为 1"

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            llm._cli_narrate("dsh", r"C:\x\dsh.cmd", {}, lang="zh", timeout=10.0)
        self.assertEqual(len(calls), 1, "只允许调用一次，不许重试")

    def test_answer_written_to_a_file_is_picked_up(self) -> None:
        """实测踩到：DSH 把说明写成 `健康数据说明.md`，stdout 只剩一句过程叙述。

        只读 stdout 会让 AI 那一节变成「已完成，写成文件 xxx.md」这种废话。
        """
        def fake_run(argv, workdir, timeout, stdout_path, stderr_path):
            with open(os.path.join(workdir, "健康数据说明.md"), "w", encoding="utf-8") as handle:
                handle.write("## 结论\n\n你的步数是 5687，这段时间的数据完整。\n\n"
                             "本解读不是医学建议。")
            # 顺手写的日志（应当被忽略：它对不上几个事实数字）
            with open(os.path.join(workdir, "acl-report-1.jsonl"), "w", encoding="utf-8") as handle:
                handle.write('{"path": "C:/x", "win32": 5}\n')
            return 0, "已完成。工作区里有一份 facts.json，我把它翻译成了 health.md。", "", ""

        with mock.patch.object(llm, "_run_agent_to_files", side_effect=fake_run):
            text, reason = llm._cli_narrate("dsh", r"C:\x\dsh.cmd",
                                            {"steps": 5687}, lang="zh", timeout=10.0)
        self.assertEqual(reason, "")
        self.assertIn("你的步数是 5687", text)
        self.assertNotIn("已完成", text)

    def test_only_top_level_text_files_are_considered(self) -> None:
        """只收顶层、带正文后缀、且不是符号链接的普通文件。"""
        workdir = tempfile.mkdtemp(prefix="health-report-llm-test-")
        self.addCleanup(shutil.rmtree, workdir, True)
        for name in ("facts.json", "stdout.txt", "stderr.txt", "notes.md", "raw.bin"):
            with open(os.path.join(workdir, name), "w", encoding="utf-8") as handle:
                handle.write("x")
        os.makedirs(os.path.join(workdir, "subdir"))
        with open(os.path.join(workdir, "subdir", "deep.md"), "w", encoding="utf-8") as handle:
            handle.write("不该被读到")
        found = llm._collect_agent_files(workdir)
        self.assertEqual([os.path.basename(path) for path in found], ["notes.md"])

    def test_file_with_fabricated_numbers_is_rejected(self) -> None:
        """文件里的数字也要过溯源校验：全是编造的文件不能当正文。"""
        workdir = tempfile.mkdtemp(prefix="health-report-llm-test-")
        self.addCleanup(shutil.rmtree, workdir, True)
        messy = os.path.join(workdir, "aaa-log.txt")
        clean = os.path.join(workdir, "zzz-report.md")
        with open(messy, "w", encoding="utf-8") as handle:
            handle.write("错误码 9991、错误码 9992、错误码 9993、错误码 9994。")
        with open(clean, "w", encoding="utf-8") as handle:
            handle.write("## 结论\n\n步数 5687，共 92 天数据。")
        picked = llm._agent_output_from_files({"steps": 5687, "days": 92}, [messy, clean])
        self.assertIn("步数 5687", picked)
        # 只剩一份编造的产物时：返回空串（调用方按失败处理），而不是硬塞进报告
        self.assertEqual(llm._agent_output_from_files({"steps": 5687}, [messy]), "")

    def test_answer_written_to_a_file_must_still_be_traceable(self) -> None:
        """文件路径同样受数字校验约束：编造的数值不会因为换了条路就放过。"""
        workdir = tempfile.mkdtemp(prefix="health-report-llm-test-")
        self.addCleanup(shutil.rmtree, workdir, True)
        path = os.path.join(workdir, "report.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("## 结论\n\n你的步数是 5687，比上月多 120 步。")  # 120 是编造的
        picked = llm._agent_output_from_files({"steps": 5687}, [path])
        self.assertEqual(picked, "")


    def test_dsh_workdir_never_touches_the_dsh_home_on_the_normal_path(self) -> None:
        """正常路径下工作目录必须在**系统临时目录**，绝不碰 `$DSH_HOME`。

        这是用一次真实的挂死换来的约束。早期设计对 DSH 优先用
        `$DSH_HOME/tmp/`，理由是"受限环境里 DSH 读不到系统临时目录"。
        但实测踩到：一旦该目录处于异常状态，沙箱对它的拦截会让 `mkdir`
        **挂住而不是报错**，于是整个测试套件从 1.5 秒变成永不结束
        （挂住的栈是 `_agent_workdir` → `tempfile.mkdtemp`）。

        一个可能永久挂死的测试套件，比"AI 偶尔读不到数据"严重得多——
        后者会优雅降级并在报告里说明，前者会让 CI 永远红着排队。

        所以顺序反过来：系统临时目录优先，`$DSH_HOME/tmp` 只在
        系统临时目录失败时兜底（见下一条测试）。
        """
        home = tempfile.mkdtemp(prefix="health-report-dshhome-")
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["DSH_HOME"] = home
        workdir = llm._agent_workdir("dsh")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.assertFalse(
            os.path.abspath(workdir).startswith(os.path.abspath(home)),
            "工作目录落进了 $DSH_HOME —— 一次测试运行就可能挂死")
        self.assertTrue(os.path.isdir(workdir))
        # 用户的配置目录必须原封不动
        self.assertEqual(os.listdir(home), [],
                         "正常路径不该往 $DSH_HOME 里写任何东西")
        with open(os.path.join(workdir, "facts.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.assertEqual(sorted(os.listdir(workdir)), ["facts.json"])

    def test_dsh_home_is_only_a_fallback(self) -> None:
        """系统临时目录不可用时，才退到 `$DSH_HOME/tmp` 兜底。

        用 monkeypatch 让第一次 `mkdtemp` 失败来模拟"系统临时目录不可用"，
        避免真的去动 `$DSH_HOME`。
        """
        home = tempfile.mkdtemp(prefix="health-report-dshhome-")
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["DSH_HOME"] = home

        real_mkdtemp = tempfile.mkdtemp
        calls: List[str] = []

        def flaky_mkdtemp(*args: object, **kwargs: object) -> str:
            calls.append(str(kwargs.get("dir", "")))
            if not kwargs.get("dir"):
                raise OSError("模拟：系统临时目录不可用")
            return real_mkdtemp(*args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(llm.tempfile, "mkdtemp", side_effect=flaky_mkdtemp):
            workdir = llm._agent_workdir("dsh")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.assertTrue(os.path.abspath(workdir).startswith(os.path.abspath(home)))
        self.assertTrue(os.path.isdir(workdir))

    def test_other_agents_stay_in_the_system_temp_dir(self) -> None:
        workdir = llm._agent_workdir("claude")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.assertEqual(os.path.dirname(os.path.abspath(workdir)),
                         os.path.abspath(tempfile.gettempdir()))

    def test_dsh_workdir_falls_back_when_config_dir_is_unwritable(self) -> None:
        """配置目录不可写时退回系统临时目录，绝不抛异常。"""
        os.environ["DSH_HOME"] = os.path.join(tempfile.gettempdir(), "readonly-home")
        with mock.patch.object(llm.os, "makedirs", side_effect=OSError("denied")):
            with mock.patch.object(tempfile, "mkdtemp", return_value="/tmp/fallback"):
                self.assertEqual(llm._agent_workdir("dsh"), "/tmp/fallback")


class TestRunAgentToFiles(_EnvIsolated):
    """`_run_agent_to_files` 的行为：文件重定向、cwd、退出码、超时。

    **不启动任何真实子进程**：`subprocess.run` 被换成一个假的，
    它按真实语义把内容写进传进来的文件对象里，然后返回一个假的完成对象。
    这样既能断言「stdout/stderr 是文件对象而不是管道」，又不依赖网络/子进程，
    整套测试可以反复快速跑。真实的子进程链路由 tools/_check_ai.py 负责验证。
    """

    def setUp(self) -> None:
        super().setUp()
        self.workdir = tempfile.mkdtemp(prefix="health-report-llm-test-")
        self.addCleanup(shutil.rmtree, self.workdir, True)

    def _fake_run(self, out_text="", err_text="", code=0, raises=None):
        def runner(command, cwd=None, timeout=None, shell=False, stdin=None,
                   stdout=None, stderr=None, check=False):
            if raises is not None:
                raise raises
            if out_text:
                stdout.write(out_text)
            if err_text:
                stderr.write(err_text)
            return mock.Mock(returncode=code)
        return runner

    def test_stdout_and_stderr_are_files_not_pipes(self) -> None:
        calls = {}

        def runner(command, cwd=None, timeout=None, shell=False, stdin=None,
                   stdout=None, stderr=None, check=False):
            calls["command"] = command
            calls["cwd"] = cwd
            calls["timeout"] = timeout
            calls["stdout"] = stdout
            calls["stderr"] = stderr
            calls["stdin"] = stdin
            # 真实语义：写进文件
            stdout.write("正文 5687")
            stderr.write("warn-line")
            # 此刻文件里就应该有内容（说明是文件而不是管道）
            self.assertTrue(stdout.name.endswith(".txt"))
            return mock.Mock(returncode=0)

        paths = (os.path.join(self.workdir, "o.txt"), os.path.join(self.workdir, "e.txt"))
        with mock.patch.object(subprocess, "run", side_effect=runner):
            code, out_text, err_text, note = llm._run_agent_to_files(
                ["agent", "headless", "提示词"], self.workdir, 42.0, *paths)
        self.assertEqual(code, 0)
        self.assertEqual(note, "")
        self.assertEqual(out_text, "正文 5687")
        self.assertEqual(err_text, "warn-line")
        self.assertEqual(calls["cwd"], self.workdir)
        self.assertEqual(calls["timeout"], 42.0)
        self.assertIs(calls["stdin"], subprocess.DEVNULL)
        self.assertFalse(calls["stdout"] is subprocess.PIPE)
        self.assertFalse(calls["stderr"] is subprocess.PIPE)
        self.assertTrue(hasattr(calls["stdout"], "write"))
        # 重定向文件确实被创建了
        self.assertTrue(os.path.isfile(paths[0]))
        self.assertTrue(os.path.isfile(paths[1]))
        # cwd 必须落在这个临时目录（绝不在客户的原始数据目录里跑）
        self.assertEqual(calls["cwd"], self.workdir)

    def test_nonzero_exit_code_is_returned_with_note(self) -> None:
        paths = (os.path.join(self.workdir, "o.txt"), os.path.join(self.workdir, "e.txt"))
        with mock.patch.object(subprocess, "run",
                               side_effect=self._fake_run(out_text="partial", code=4)):
            code, out_text, _err, note = llm._run_agent_to_files(
                ["agent"], self.workdir, 10.0, *paths)
        self.assertEqual(code, 4)
        self.assertIn("退出码为 4", note)
        self.assertEqual(out_text, "partial")

    def test_timeout_is_swallowed_and_reported(self) -> None:
        paths = (os.path.join(self.workdir, "o.txt"), os.path.join(self.workdir, "e.txt"))
        expired = subprocess.TimeoutExpired(cmd="agent", timeout=1.5)
        with mock.patch.object(subprocess, "run", side_effect=self._fake_run(raises=expired)):
            code, out_text, _err, note = llm._run_agent_to_files(
                ["agent"], self.workdir, 1.5, *paths)
        self.assertEqual(code, -1)
        self.assertIn("已超过", note)
        self.assertIn("已放弃", note)
        self.assertEqual(out_text, "")

    def test_oserror_is_swallowed_and_reported(self) -> None:
        paths = (os.path.join(self.workdir, "o.txt"), os.path.join(self.workdir, "e.txt"))
        with mock.patch.object(subprocess, "run",
                               side_effect=self._fake_run(raises=OSError("无法启动"))):
            code, _out, _err, note = llm._run_agent_to_files(
                ["no-such-agent"], self.workdir, 5.0, *paths)
        self.assertEqual(code, -1)
        self.assertIn("无法启动", note)
        self.assertIn("OSError", note)

    def test_unwritable_redirect_path_is_reported_not_raised(self) -> None:
        bad_dir = os.path.join(self.workdir, "no-such-dir")
        code, _out, _err, note = llm._run_agent_to_files(
            ["agent"], self.workdir, 5.0,
            os.path.join(bad_dir, "o.txt"), os.path.join(bad_dir, "e.txt"))
        self.assertEqual(code, -1)
        self.assertIn("无法创建输出重定向文件", note)

    def test_cmd_files_use_shell_so_windows_can_start_them(self) -> None:
        """Windows 上 .cmd 必须经 shell 才能启动（CreateProcess 不认 PATHEXT）。"""
        seen = {}

        def runner(command, cwd=None, timeout=None, shell=False, stdin=None,
                   stdout=None, stderr=None, check=False):
            seen["shell"] = shell
            seen["command"] = command
            return mock.Mock(returncode=0)

        paths = (os.path.join(self.workdir, "o.txt"), os.path.join(self.workdir, "e.txt"))
        with mock.patch.object(subprocess, "run", side_effect=runner):
            llm._run_agent_to_files([r"C:\x\bin\dsh.cmd", "headless", "p"],
                                    self.workdir, 5.0, *paths)
        self.assertEqual(seen["shell"], os.name == "nt")
        if os.name == "nt":
            # 走 shell 时必须是一条拼好的命令行字符串，而不是 list
            self.assertIsInstance(seen["command"], str)
        else:
            self.assertIsInstance(seen["command"], list)


# ═══════════════════════════════════════════════════════ narrate_ex / narrate


class TestNarrateEx(_EnvIsolated):
    """narrate_ex 的编排：校验失败丢弃、失败降级、none 不调用。"""

    def test_none_backend_does_not_attempt(self) -> None:
        result = narrate_ex({"a": 1}, LLMConfig(), prefer="none")
        self.assertIsInstance(result, NarrateResult)
        self.assertFalse(result.attempted)
        self.assertFalse(result.dropped)
        self.assertEqual(result.text, "")
        self.assertEqual(result.backend.kind, "none")
        self.assertIn("显式关闭", result.drop_reason)

    def test_good_cli_output_passes_verification(self) -> None:
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        with mock.patch.object(llm, "_cli_narrate",
                               return_value=("你的步数是 5687。", "")):
            result = narrate_ex({"steps": 5687}, LLMConfig())
        self.assertTrue(result.attempted)
        self.assertFalse(result.dropped)
        self.assertEqual(result.text, "你的步数是 5687。")
        self.assertEqual(result.backend.sends_to, "DeepSeek")

    def test_fabricated_number_drops_whole_text(self) -> None:
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        with mock.patch.object(llm, "_cli_narrate",
                               return_value=("你的静息心率 58.2，建议关注。", "")):
            result = narrate_ex({"steps": 5687}, LLMConfig())
        self.assertTrue(result.dropped)
        self.assertEqual(result.text, "")
        self.assertEqual(result.numbers, ["58.2"])
        self.assertIn("找不到出处", result.drop_reason)
        self.assertIn("58.2", result.drop_reason)

    def test_cli_failure_degrades_without_dropping(self) -> None:
        self._pin_backend(llm._cli_backend("claude", "/usr/bin/claude"))
        with mock.patch.object(llm, "_cli_narrate",
                               return_value=("", "Claude Code 调用超时：已超过 180 秒仍未返回")):
            result = narrate_ex({}, LLMConfig())
        self.assertFalse(result.dropped)
        self.assertEqual(result.text, "")
        self.assertTrue(result.attempted)
        self.assertIn("超时", result.drop_reason)

    def test_http_backend_is_used_when_configured(self) -> None:
        os.environ["HEALTH_REPORT_API_KEY"] = "sk-test-1234567890"
        os.environ["HEALTH_REPORT_BASE_URL"] = "https://api.deepseek.com"
        os.environ["HEALTH_REPORT_MODEL"] = "deepseek-chat"
        with mock.patch.object(llm, "_http_narrate",
                               return_value=("你的步数是 5687。", "")) as http:
            result = narrate_ex({"steps": 5687}, LLMConfig.from_env())
        self.assertEqual(result.text, "你的步数是 5687。")
        self.assertEqual(result.model, "deepseek-chat")
        self.assertEqual(http.call_args.kwargs["base_url"], "https://api.deepseek.com")
        self.assertFalse(result.backend.is_local)

    def test_cli_timeout_argument_is_forwarded(self) -> None:
        self._pin_backend(llm._cli_backend("codex", "/usr/bin/codex"))
        with mock.patch.object(llm, "_cli_narrate", return_value=("", "x")) as run:
            narrate_ex({}, LLMConfig(), timeout=7.5)
        self.assertEqual(run.call_args.kwargs["timeout"], 7.5)
        self.assertEqual(run.call_args.args[0], "codex")

    def test_offending_sentence_is_dropped_and_the_rest_is_kept(self) -> None:
        """实测踩到：一次真实输出里 4 个数字对不上，原来**整段丢弃**。

        真机（装了 DSH 的机器上跑真实导出）实测：AI 一次输出被判定
        `2800000、2.3、2.3、2000` 四个数字无法溯源，于是整段解读被丢掉，
        客户什么都没拿到。一票否决太浪费——一句编造不该让另外几十句
        正确的解读一起作废。

        现在按**整句**删除，其余保留，并在 `removed` 里交代删了什么。
        """
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        answer = (
            "## 睡眠与恢复\n\n"
            "本次报告共纳入 2 个指标。\n\n"
            "你的静息心率是 58.2 次/分，比上月明显升高。\n\n"
            "### 该看什么\n\n"
            "把注意力放在趋势而不是单点读数上。单次测量落进参考区间之外，"
            "在多数指标上并不罕见；真正值得关心的是连续多天朝同一个方向走。\n\n"
            "同一指标如果来自多台设备，跨设备的差值不能当作身体变化，"
            "换设备造成的台阶要先排除，再谈趋势。\n\n"
            "睡眠时长与作息规律性是两件事：时长可以靠某一天补回来，"
            "作息的漂移补不回来，所以两者要分开看。\n"
        )
        facts = {"metrics": [{"latest": 54.5}, {"latest": 7.5}]}
        with mock.patch.object(llm, "_cli_narrate", return_value=(answer, "")):
            result = narrate_ex(facts, LLMConfig())

        # 编造的那一句必须消失，且那一段不能以任何形式留在报告里
        self.assertNotIn("58.2", result.text)
        self.assertFalse(result.dropped, "剩下的内容足够多，不该整段丢弃")
        self.assertEqual(len(result.removed), 1)
        self.assertIn("58.2", result.removed[0])
        self.assertIn("该看什么", result.text)
        self.assertIn("58.2", result.drop_reason)
        # 被删的句子本身绝不能出现在保留正文里
        self.assertNotIn("比上月明显升高", result.text)

    def test_real_world_miskilled_sentences_now_survive(self) -> None:
        """真机实测的 4 类误杀，在 narrate_ex 这一层也必须**整句保留**。

        下面每一行都是当时被删掉的**原文**（一字不改）：小节号 `2.2` / `4.1`、
        空格千分位 `11 534`、量级改写 `1086 万` / `1563 万`、训练总量那一行。
        facts 侧补齐了距离、步数总量与训练总量（报告侧会加），
        所以它们全都应当原样留下——`removed` 必须是空的。
        """
        answer = (
            "## 结论\n\n"
            "### 2.2 步数日均 5687 步，低于常见建议的 ≥8000 步\n\n"
            "### 4.1 覆盖率：很多指标其实是「半年有、半年没有」\n\n"
            "则是 11 534 km（**1.43 倍**）\n\n"
            "步数 1086 万 vs 1563 万（**1.44 倍**）\n\n"
            "| 训练记录 | 1385 次，累计约 704 小时，约 20.2 万 kcal |\n"
        )
        facts = {
            "metrics": [{"metric": "steps", "latest": 5687, "ref_low": 8000}],
            "distance_km": 11534.0,
            "steps_a": 10862753,
            "steps_b": 15634979,
            "ratio_a": 1.43,
            "ratio_b": 1.44,
            "workouts": {"count": 1385, "total_hours": 704.0, "total_kcal": 202000},
        }
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        with mock.patch.object(llm, "_cli_narrate", return_value=(answer, "")):
            result = narrate_ex(facts, LLMConfig())
        self.assertFalse(result.dropped)
        self.assertEqual(result.removed, [], "这些是合法写法，一句都不该被删")
        self.assertEqual(result.text, answer)
        for line in ("### 2.2 步数日均 5687 步",
                     "### 4.1 覆盖率",
                     "则是 11 534 km",
                     "步数 1086 万 vs 1563 万",
                     "| 训练记录 | 1385 次"):
            self.assertIn(line, result.text)

    def test_dropping_a_sentence_preserves_paragraph_structure(self) -> None:
        """删句之后必须**保住换行与段落结构**。

        实测踩到：句子切分把 `\\n` 当分隔符吃掉了，于是保留下来的正文全部
        挤成一行——「## 睡眠与恢复本次报告共纳入 29 个指标…### 该看什么第一，…」，
        Markdown 标题和小节全部失效。切分必须用零宽断言。
        """
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        answer = ("## 睡眠与恢复\n\n"
                  "第一句正确。\n\n"
                  "这句编造 2800000 条记录。\n\n"
                  "### 小节\n\n"
                  "第三句正确。这一句要写得足够长，好让删掉上面那句之后剩下的正文"
                  "仍然超过保留阈值——否则测试会走到「整段丢弃」那条路上去，"
                  "而这条测试要守的是**换行结构必须保住**，不是阈值行为。\n\n"
                  "第四句也正确，讲的是同一件事的另一个侧面，用作段落结构的第二块内容。\n")
        with mock.patch.object(llm, "_cli_narrate", return_value=(answer, "")):
            result = narrate_ex({"metrics": [{"latest": 1}]}, LLMConfig())

        self.assertFalse(result.dropped)
        self.assertIn("## 睡眠与恢复", result.text)
        self.assertIn("### 小节", result.text)
        # 标题必须自成一行：如果换行被吃掉，`## 睡眠与恢复第一句正确。` 会连在一起
        for line in result.text.splitlines():
            self.assertTrue(line == line.strip(), f"出现了粘连的行：{line!r}")
        self.assertIn("\n\n", result.text, "段落空行被吃掉了")
        self.assertNotIn("2800000", result.text)

    def test_too_little_left_means_drop_everything(self) -> None:
        """删完剩下的太单薄，就整段丢弃——一段残缺的解读比没有更糟。

        用户看到两行字会以为那就是全部解读；明确说"没生成"反而更诚实。
        """
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        answer = "## 结论\n\n你的静息心率是 58.2。\n"
        with mock.patch.object(llm, "_cli_narrate", return_value=(answer, "")):
            result = narrate_ex({"metrics": [{"latest": 54.5}]}, LLMConfig())
        self.assertTrue(result.dropped)
        self.assertEqual(result.text, "")
        self.assertIn("所剩内容过少", result.drop_reason)

    def test_asking_for_data_is_treated_as_failure(self) -> None:
        """实测踩到：agent 输出「请把 facts 贴给我」而不是解读。

        这种文字绝不能进报告——用户会以为那就是 AI 的解读。判据是
        「命中索要数据的措辞」且「没有一个数字能在 facts 里溯源」。
        """
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        answer = ("请把这份已经算好的结构化 facts 贴给我（JSON、表格或纯文本都行）。\n\n"
                  "我拿到之后会按下面的结构输出一份普通人能读懂的说明：\n"
                  "1. 一句话总结 2. 逐项解读 3. 需要留意的地方")
        # facts 为空 → 一个数字都溯源不了（正如真实那份「请把数据给我」的回复）
        with mock.patch.object(llm, "_cli_narrate", return_value=(answer, "")):
            result = narrate_ex({}, LLMConfig())
        self.assertEqual(result.text, "")
        self.assertFalse(result.dropped)
        self.assertIn("索要数据", result.drop_reason)

    def test_environment_blocked_report_is_treated_as_failure(self) -> None:
        """实测踩到（第二次）：agent 读不到工作目录时，写了一份「权限受阻」说明。

        它里面**有数字**——错误码 `Win32 5`、`610`、`32`——所以「有没有数字」
        不能当判据；真正的判据是「有没有一个数字能在 facts 里溯源」。
        """
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        answer = ("我停在这里报告，因为既没有可用的授权通道，工作区也确实读不到。\n\n"
                  "## 当前状态：工作区文件权限受阻，未做任何修改\n\n"
                  "**现象**：列目录失败，报错发生在设置文件权限的阶段"
                  "（`SetNamedSecurityInfoW failed (Win32 5)`，即拒绝访问）。\n\n"
                  "## 需要你做的两件事\n\n"
                  "1. **把这个会话临时切到「完全权限」(full access)**。\n"
                  "2. 把这个会话作为反馈发出去。\n\n"
                  "## 关于你交给我的任务本身\n\n"
                  "但目前我手上**没有收到任何 facts 数据**，所以无法开始。\n"
                  "权限恢复后，请任选一种方式把数据给我：直接贴进对话，或告诉我文件名。")
        with mock.patch.object(llm, "_cli_narrate", return_value=(answer, "")):
            result = narrate_ex({"steps": 5687}, LLMConfig())
        self.assertEqual(result.text, "", "「我读不到文件」不能当成 AI 解读进报告")
        self.assertIn("降级", result.drop_reason)

    def test_outline_only_reply_is_treated_as_failure(self) -> None:
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        answer = "我会按下面的结构输出：总体情况、逐项解读、下一步。"
        with mock.patch.object(llm, "_cli_narrate", return_value=(answer, "")):
            result = narrate_ex({}, LLMConfig())
        self.assertEqual(result.text, "")
        self.assertIn("提纲", result.drop_reason)

    def test_legitimate_reply_mentioning_missing_data_is_kept(self) -> None:
        """「数据里没有这一项」是 rule 2 要求的措辞，不能被误判成索要数据。"""
        self._pin_backend(llm._cli_backend("dsh", r"C:\x\dsh.cmd"))
        answer = "你的步数是 5687。血氧这项数据里没有，所以不做判断。"
        with mock.patch.object(llm, "_cli_narrate", return_value=(answer, "")):
            result = narrate_ex({"steps": 5687}, LLMConfig())
        self.assertEqual(result.text, answer)
        self.assertFalse(result.dropped)

    def test_real_reply_with_numbers_is_never_flagged_as_placeholder(self) -> None:
        """有实质内容的长回复，即使措辞里带「请提供」也不能误判。"""
        long_reply = ("## 结论\n\n你的步数是 5687。这一段是足够长的正文，" * 8)
        self.assertEqual(llm._asks_for_data_instead_of_answering(long_reply, {"steps": 5687}), "")
        self.assertEqual(llm._asks_for_data_instead_of_answering(
            "请提供更多数据。不过先看现有数据：步数 5687，" + "这一段继续说明。" * 30,
            {"steps": 5687}), "")
        self.assertEqual(llm._asks_for_data_instead_of_answering("", {}), "")

    def test_short_legitimate_reply_is_not_flagged(self) -> None:
        """短但没有任何索要/提纲措辞的回复照常保留。"""
        self.assertEqual(llm._asks_for_data_instead_of_answering("## 结论\n\n步数 5687。", {}), "")

    def test_whitelist_numbers_do_not_make_a_request_look_traceable(self) -> None:
        """回归：白名单（1~12 等）不能参与「它引用了事实」的判断。

        实测踩到：那份「权限受阻」说明里的数字是错误码（`Win32 5`、`610`、`32`）
        和列表序号（`1.` `2.`），如果白名单也算数，它们就会全部"命中"，
        判定立刻失效、这类回复会长驱直入进报告。
        """
        facts = {"metrics": [{"metric": "steps", "latest": 5687}]}
        # 这些数字都能在白名单/facts 里找到，但没有一个是真正在解读数据
        blocked = ("工作区文件权限受阻，未做任何修改。请把数据给我。"
                   "报错 SetNamedSecurityInfoW failed (Win32 5)。")
        self.assertLess(llm._traceable_number_count(blocked, facts),
                        llm._MIN_TRACEABLE_NUMBERS)
        self.assertTrue(llm._asks_for_data_instead_of_answering(blocked, facts))
        # 再确认「白名单不参与计数」：只有列表序号和小整数的文本，计数必须是 0
        self.assertEqual(llm._traceable_number_count(
            "1. 总结 2. 逐项解读 3. 下一步 4. 边界 5. 结语", facts), 0)

    def test_unambiguous_marker_beats_the_length_guard(self) -> None:
        """铁证级措辞与文本长短无关：实测那句「请把 facts 贴给我」有 230+ 字。

        内容长得像正经解读也没用——「贴给我」本身就是让调用方补数据的信号。
        """
        factual = "睡眠均值 6.29 h，静息心率 56，训练 1385 次。" * 12
        long_request = ("请把这份已经算好的结构化 facts 贴给我（JSON、表格或纯文本都行）。\n\n"
                        "我拿到之后会按下面的结构输出一份普通人能读懂的说明：\n" + factual)
        self.assertGreater(len(re.sub(r"\s+", "", long_request)), llm._PLACEHOLDER_MIN_CHARS)
        self.assertTrue(llm._asks_for_data_instead_of_answering(long_request, {}))


class TestNarrateCompatibility(_EnvIsolated):
    """`narrate` / `build_messages` / `LLMConfig` 的签名与行为不能变。"""

    def test_narrate_returns_empty_without_key_and_probes_nothing(self) -> None:
        probe = mock.patch.object(llm, "_probe_local_http",
                                  side_effect=AssertionError("没配 key 时不该探测"))
        probe.start()
        self.addCleanup(probe.stop)
        self.assertEqual(narrate({"a": 1}, LLMConfig()), "")
        self.assertEqual(narrate({"a": 1}, None), "")

    def test_narrate_is_a_thin_wrapper_of_narrate_ex(self) -> None:
        os.environ["HEALTH_REPORT_API_KEY"] = "sk-test-1234567890"
        with mock.patch.object(llm, "narrate_ex", return_value=NarrateResult(text="正文")) as wrapped:
            self.assertEqual(narrate({"a": 1}, LLMConfig.from_env()), "正文")
        self.assertEqual(wrapped.call_args.kwargs.get("lang"), "zh")

    def test_narrate_positional_and_keyword_signature(self) -> None:
        import inspect

        signature = inspect.signature(narrate)
        self.assertEqual(list(signature.parameters), ["facts", "config", "lang"])
        self.assertIs(signature.parameters["lang"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(signature.parameters["lang"].default, "zh")

    def test_build_messages_shape_unchanged(self) -> None:
        messages = build_messages({"a": 1}, "zh")
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn('"a": 1', messages[1]["content"])
        self.assertIn("以下是已经计算好的事实", messages[1]["content"])

    def test_config_from_env_and_describe_unchanged(self) -> None:
        os.environ["HEALTH_REPORT_API_KEY"] = "sk-abcdefgh"
        os.environ["HEALTH_REPORT_BASE_URL"] = "https://api.deepseek.com/"
        os.environ["HEALTH_REPORT_TIMEOUT"] = "12.5"
        config = LLMConfig.from_env()
        self.assertTrue(config.available())
        self.assertEqual(config.base_url, "https://api.deepseek.com")
        self.assertEqual(config.timeout, 12.5)
        described = config.describe()
        self.assertIn("sk-a***", described)
        self.assertNotIn("sk-abcdefgh", described)
        self.assertEqual(LLMConfig().base_url, DEFAULT_BASE_URL)


class TestCredentialDetection(_EnvIsolated):
    """凭据检测：只看文件是否存在，不触发任何进程。"""

    def test_dsh_credential_from_env_var(self) -> None:
        os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek"
        available, where = llm._dsh_credential_state()
        self.assertTrue(available)
        self.assertIn("DEEPSEEK_API_KEY", where)

    def test_dsh_credential_from_home_store(self) -> None:
        home = tempfile.mkdtemp(prefix="health-report-dshhome-")
        self.addCleanup(shutil.rmtree, home, True)
        with open(os.path.join(home, ".credentials.yaml"), "w", encoding="utf-8") as handle:
            handle.write("llm-deepseek: {}\n")
        os.environ["DSH_HOME"] = home
        available, where = llm._dsh_credential_state()
        self.assertTrue(available)
        self.assertEqual(where, os.path.join(home, ".credentials.yaml"))

    def test_dsh_without_credentials_is_reported_unavailable(self) -> None:
        home = tempfile.mkdtemp(prefix="health-report-dshhome-")
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["DSH_HOME"] = home
        available, where = llm._dsh_credential_state()
        self.assertFalse(available)
        self.assertEqual(where, home)

    def test_detection_never_creates_anything_in_dsh_home(self) -> None:
        home = tempfile.mkdtemp(prefix="health-report-dshhome-")
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["DSH_HOME"] = home
        llm._dsh_credential_state()
        self.assertEqual(os.listdir(home), [])

    def test_dsh_home_env_is_not_modified(self) -> None:
        os.environ["DSH_HOME"] = r"C:\custom\dsh"
        self._no_local_http()
        with mock.patch.object(llm, "_find_agent", return_value=r"C:\x\dsh.cmd"):
            with mock.patch.object(llm, "_agent_credential_state", return_value=(True, "y")):
                detect_backend(LLMConfig())
        self.assertEqual(os.environ["DSH_HOME"], r"C:\custom\dsh")

    def test_claude_and_codex_credential_paths(self) -> None:
        home = tempfile.mkdtemp(prefix="health-report-agenthome-")
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(os.path.join(home, "claude"))
        os.makedirs(os.path.join(home, "codex"))
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(home, "claude")
        os.environ["CODEX_HOME"] = os.path.join(home, "codex")
        self.assertFalse(llm._agent_credential_state("claude")[0])
        self.assertFalse(llm._agent_credential_state("codex")[0])
        with open(os.path.join(home, "claude", "auth.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(os.path.join(home, "codex", "auth.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.assertTrue(llm._agent_credential_state("claude")[0])
        self.assertTrue(llm._agent_credential_state("codex")[0])

    def test_anthropic_env_var_counts_as_logged_in(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant"
        self.assertTrue(llm._agent_credential_state("claude")[0])


class TestLocalHttpProbe(unittest.TestCase):
    """探测函数的两个硬约束：只连回环地址、任何异常都返回 None。"""

    def test_non_loopback_url_is_never_probed(self) -> None:
        for url in ("http://10.0.0.5:11434/v1", "http://192.168.1.9:1234/v1",
                    "https://api.example.com/v1", "http://example.com:8080/v1"):
            with self.subTest(url=url):
                with mock.patch.object(llm.urllib.request, "urlopen",
                                       side_effect=AssertionError("不该连非回环地址")):
                    self.assertIsNone(llm._probe_local_http(url))

    def test_loopback_detection(self) -> None:
        self.assertTrue(llm._is_loopback_url("http://127.0.0.1:11434/v1"))
        self.assertTrue(llm._is_loopback_url("http://127.5.5.5:8080/v1"))
        self.assertTrue(llm._is_loopback_url("http://localhost:1234/v1"))
        self.assertFalse(llm._is_loopback_url("http://0.0.0.0:8080/v1"))
        self.assertFalse(llm._is_loopback_url("http://api.openai.com/v1"))

    def test_connection_failure_is_none(self) -> None:
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertIsNone(llm._probe_local_http("http://127.0.0.1:11434/v1"))

    def test_model_name_is_extracted(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = json.dumps({"data": [{"id": "qwen2.5:7b"}]}).encode("utf-8")
        response.__enter__ = lambda self: response
        response.__exit__ = lambda self, *args: False
        with mock.patch.object(llm.urllib.request, "urlopen", return_value=response) as called:
            model = llm._probe_local_http("http://127.0.0.1:11434/v1", timeout=0.5)
        self.assertEqual(model, "qwen2.5:7b")
        self.assertEqual(called.call_args.kwargs["timeout"], 0.5)
        self.assertTrue(called.call_args.args[0].full_url.endswith("/models"))


if __name__ == "__main__":
    unittest.main()
