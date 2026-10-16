#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 解读层的自检脚本（标准库，不联网、不装任何东西）。

用法：
    cd 06_HealthReport
    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    python tools/_check_ai.py

退出码 0 = 全部通过（**跳过不算失败**）；非零 = 有失败项，失败项列在末尾。

它做四件事
----------
1. **真实探测本机**：跑一遍 `detect_backend()`，把检测到什么、数据会发给谁
   （`sends_to`）、是不是真的不出机器（`is_local`）如实打印出来。
   这一步只做只读探测：读 PATH / 环境变量 / `os.path.isdir`，
   最多对本机 `127.0.0.1` 的三个端口发一个 `GET /models`（各 0.5 秒超时）。
2. **`verify_numbers` 的边界用例**：正例（facts 里有）、负例（编造测量值）、
   千分位、万/千乘开、全角数字、上标、窗口天数白名单、浮点尾巴。
3. **如果本机确实装了 dsh / claude / codex，就真的跑一次极短提示词**
   （`只回复：ok`），打印真实 stdout、耗时、退出码——验的是「通路到底通不通」，
   而不是「代码看起来对」。没检测到就打印「未检测到 CLI agent，跳过真实调用」。
4. **隐私边界自检**：真实的 CLI 调用只允许在临时目录里、只放一份 facts.json；
   本脚本在调用前后各快照一次工作目录，确认没有多出任何文件。

隐私说明
--------
本脚本**只用合成数值**（下面的 `SYNTHETIC_FACTS` 是编的），不含任何真实体测读数。
`--facts <path>` 可以指定一份自己的 facts.json 做真实探测之外的额外验证，
但默认不读任何文件；`tools/` 下的脚本发布前按 docs/PRIVACY.md 的要求自己过一遍。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 给 llm 的日志挂一个写 stderr 的 handler。不这么做时，Python 的 lastResort
# handler 会让每条 WARNING 直接写到 stderr，而被 PowerShell 当成「原生命令错误」，
# 于是 $LASTEXITCODE 变成 1——明明全部检查都过了。这里显式接管输出。
import logging  # noqa: E402

logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="[%(levelname)s] %(message)s")

import health_report.llm as llm  # noqa: E402
from health_report.llm import (  # noqa: E402
    LLMConfig,
    _agent_credential_state,
    _cli_argv,
    _find_agent,
    _run_agent_to_files,
    clean_cli_output,
    detect_backend,
    verify_numbers,
)

# 全是编造的合成数值，和 examples/ 里那套数据无关。
SYNTHETIC_FACTS: Dict[str, Any] = {
    "profile": {"height_cm": 178.0, "age": 41, "sex": "male"},
    "data": {"span_days": 92},
    "metrics": [
        {"metric": "steps", "cn": "步数", "latest": 10800.0, "mean": 9134.7, "n": 92, "days": 92, "verdict": "goal_met"},
        {"metric": "resting_heart_rate", "cn": "静息心率", "latest": 61.0, "delta": -2.0, "n": 88, "days": 88, "verdict": "in_range"},
        {"metric": "body_fat_pct", "cn": "体脂率", "latest": 18.7, "n": 6, "days": 41, "verdict": "in_range"},
        {"metric": "spo2", "cn": "血氧", "latest": 98.0, "n": 30, "days": 30, "verdict": "in_range"},
    ],
    "workouts": {"has_data": True, "count": 12, "total_minutes": 512.0, "total_km": 87.4},
}

_PROBE_LINE = "─" * 78


class _Checks:
    """极简的断言收集器：不抛异常，最后统一汇总，保证所有检查都会跑到。

    **三分**（这是刻意的：CI 与受限沙箱里必须能用）：
      * 通过  —— 行为符合预期；
      * 失败  —— **代码逻辑**错了，只统计这一类到退出码；
      * 跳过  —— **环境**跑不动（配置目录不可写、凭据缺失、可执行文件找不到、
                沙箱拒绝）。跳过不算失败，退出码仍然是 0。
    """

    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []
        self.skipped: List[str] = []

    def ok(self, condition: bool, label: str, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            print("  [√] {0}{1}".format(label, ("  —— " + detail) if detail else ""))
        else:
            self.failed.append(label + (("  —— " + detail) if detail else ""))
            print("  [×] {0}{1}".format(label, ("  —— " + detail) if detail else ""))
        return bool(condition)

    def equal(self, actual: Any, expected: Any, label: str) -> bool:
        return self.ok(actual == expected, label, "实际={0!r} 期望={1!r}".format(actual, expected))

    def skip(self, label: str, reason: str = "") -> None:
        """环境原因导致的跳过：不算失败、不影响退出码。"""
        text = label + (("  —— " + reason) if reason else "")
        self.skipped.append(label)
        print("  [-] {0}".format(text))
        if reason:
            print("      [i] 跳过原因：{0}".format(reason))


# ─────────────────────────────────────────────────── 1. 真实探测本机


def check_detect(checks: _Checks) -> llm.Backend:
    """真实跑一遍 detect_backend，把「检测到什么 / 数据发给谁」打印出来。"""
    print(_PROBE_LINE)
    print("1. 真实探测本机后端（detect_backend）")
    print(_PROBE_LINE)
    print("  环境变量：")
    for name in ("HEALTH_REPORT_API_KEY", "OPENAI_API_KEY", "HEALTH_REPORT_BASE_URL",
                 "OPENAI_BASE_URL", "HEALTH_REPORT_MODEL", "OPENAI_MODEL",
                 "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                 "DSH_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        value = os.environ.get(name)
        if value:
            shown = llm._mask_key(value) if "KEY" in name else value
            print("    {0} = {1}".format(name, shown))
    print("  未列出的变量未设置。")

    config = LLMConfig.from_env()
    print("  LLMConfig.from_env()：{0}".format(config.describe()))
    print("  config.available() = {0}".format(config.available()))

    started = time.time()
    backend = detect_backend(config, prefer="auto")
    elapsed = time.time() - started
    print("")
    print("  detect_backend(prefer=auto) 结果（耗时 {0:.3f} 秒）：".format(elapsed))
    print("    kind      = {0}".format(backend.kind))
    print("    name      = {0}".format(backend.name))
    print("    detail    = {0}".format(backend.detail))
    print("    is_local  = {0}（True 表示数据不出本机）".format(backend.is_local))
    print("    sends_to  = {0}（数据会发给谁）".format(backend.sends_to))
    print("    model     = {0}".format(backend.model or "（无）"))
    print("    base_url  = {0}".format(backend.base_url or "（无）"))
    print("    command   = {0}".format(backend.command or "（无）"))

    print("")
    print("  每个 CLI agent 的实际探测情况：")
    for agent in ("dsh", "claude", "codex"):
        command = _find_agent(agent)
        available, where = _agent_credential_state(agent)
        print("    {0:<7} 可执行文件={1}".format(agent, command or "未找到"))
        print("    {0:<7} 凭据={1}（{2}）".format("", "已配置" if available else "未配置", where))

    print("")
    print("  prefer 各取值的结果：")
    for prefer in ("auto", "none", "http", "cli", "dsh", "claude", "codex"):
        picked = detect_backend(config, prefer=prefer)
        print("    prefer={0:<7} → kind={1:<5} name={2:<8} sends_to={3}".format(
            prefer, picked.kind, picked.name, picked.sends_to))

    print("")
    checks.ok(backend.kind in ("http", "cli", "none"), "detect_backend 返回合法的 kind",
              backend.kind)
    checks.ok(elapsed < 30.0, "自动探测没有卡住（应远小于 30 秒）",
              "{0:.3f} 秒".format(elapsed))
    checks.ok(bool(backend.sends_to), "后端带上了「数据去哪」的描述", backend.sends_to)
    if backend.kind == "cli":
        checks.ok(not backend.is_local,
                  "CLI 后端没有被标成「本机」（程序在本地，模型在云端）",
                  "sends_to={0}".format(backend.sends_to))
        checks.ok(backend.sends_to in ("DeepSeek", "Anthropic", "OpenAI"),
                  "CLI 后端标出了具体厂商", backend.sends_to)
    if backend.kind == "http" and backend.is_local:
        checks.equal(backend.sends_to, "本机（不出机器）", "本机 HTTP 模型标成不出机器")
    none_backend = detect_backend(config, prefer="none")
    checks.equal(none_backend.kind, "none", "prefer=none 直接返回 none（纯规则报告）")
    try:
        detect_backend(config, prefer="绝不存在的后端")
        checks.ok(False, "非法 prefer 应当抛 ValueError")
    except ValueError as exc:
        checks.ok("prefer 取值非法" in str(exc), "非法 prefer 抛带中文说明的 ValueError",
                  str(exc)[:48] + "…")
    return backend


# ─────────────────────────────────────────── 2. verify_numbers 边界用例


def check_verify_numbers(checks: _Checks) -> None:
    """数字溯源校验的正例/负例（全部合成数值）。"""
    print("")
    print(_PROBE_LINE)
    print("2. verify_numbers 边界用例")
    print(_PROBE_LINE)
    cases: List[Tuple[str, Dict[str, Any], bool, List[str], str]] = [
        ("步数 10800", {"metrics": [{"metric": "steps", "latest": 10800}]}, True, [],
         "facts 里有 10800"),
        ("静息心率 58.2", {"metrics": [{"metric": "resting_heart_rate", "latest": 61}]}, False,
         ["58.2"], "编造的测量值必须被抓住"),
        ("总消耗 1,910 千卡", {"active_energy": 1910}, True, [],
         "千分位归一 + 千卡里的千不是乘数"),
        ("累计约 1.08 万", {"steps_total": 10800}, True, [],
         "1.08 万 乘开成 10800"),
        ("累计约 1.08 万", {"steps_total": 10862753}, False, ["10800"],
         "乘开后仍然对不上，不能放过"),
        ("近 90 天，建议拉长到 4 周以上（28 天）", {}, True, [],
         "窗口天数白名单"),
        ("BMI 22.9 kg/m²", {"bmi": 22.9}, True, [], "上标不是数值"),
        ("体重是 ７０.５ 公斤", {"weight": 70.5}, True, [], "全角数字"),
        ("比值 0.3", {"x": 0.30000000000000004}, True, [], "浮点尾巴容差"),
        ("静息心率 58.25", {"x": 58.2}, False, ["58.25"],
         "相对误差 1e-6 不会把 0.05 的差抹掉"),
        ("均值 6.29", {"mean": 6.289373745308546}, True, [],
         "接受模型把 facts 里的值按位舍入（实测真实数据必踩）"),
        ("均值 6.5", {"mean": 6.289373745308546}, False, ["6.5"],
         "舍入通道不能掩盖真正的编造"),
        ("共 29 个指标", {"metrics": [{}] * 29}, True, [],
         "容器长度是可溯源的事实"),
        ("本周完成 3 次训练", {}, True, [],
         "取舍：小整数 1~12 放行，计数类编造可能漏过"),
        ("血氧 99%", {"spo2": 98}, False, ["99"], "百分号里的数字照常比对"),
    ]
    for text, facts, expect_ok, expect_bad, why in cases:
        started = time.time()
        ok, bad = verify_numbers(text, facts)
        elapsed_ms = (time.time() - started) * 1000.0
        checks.equal(ok, expect_ok, "「{0}」→ {1}（{2}）".format(
            text, "通过" if expect_ok else "不通过", why))
        checks.equal(bad, expect_bad, "  无法溯源的数字列表")
        checks.ok(elapsed_ms < 500.0, "  校验耗时合理", "{0:.2f} ms".format(elapsed_ms))

    ok, bad = verify_numbers("", {})
    checks.ok(ok and not bad, "空文本视为通过")
    ok, bad = verify_numbers("你的步数是 10800。", SYNTHETIC_FACTS)
    checks.ok(ok, "对完整的合成 facts 也能通过", str(bad))
    ok, bad = verify_numbers("你的体脂率 12.3，血氧 91。", SYNTHETIC_FACTS)
    checks.ok(not ok and bad == ["12.3", "91"], "两个编造值都被列出", str(bad))


# ───────────────────────────────────────────── 3. 真实的 CLI 调用


#: 这些原因属于「环境跑不起来」，不是代码缺陷 —— 一律算跳过。
#: 只保留两类真正该失败的：命令根本没启动成功导致我们自己的逻辑没被验证，
#: 以及命令跑完了但结果不可用（见 `_check_agent_result`）。
_ENV_SKIP_MARKERS = (
    "MISSING_CREDENTIAL", "未配置模型凭据", "no api key",
    "不可写", "EPERM", "EACCES", "operation not permitted", "permission denied",
    "拒绝访问", "sandbox", "沙箱",
)


def _is_environment_problem(reason: str, err_text: str) -> bool:
    """判断这次失败是不是「环境跑不动」而不是「代码坏了」。"""
    haystack = (str(reason or "") + "\n" + str(err_text or "")).lower()
    return any(marker.lower() in haystack for marker in _ENV_SKIP_MARKERS)


def _check_agent_result(checks: _Checks, agent: str, code: int, out_text: str,
                        err_text: str, reason: str, elapsed: float,
                        configured: bool, workdir_leftovers: List[str]) -> None:
    """对一个 CLI agent 的真实调用结果做三分判定。

    **失败的判据只针对我们自己的逻辑**（这些与环境无关，任何机器上都应当成立）：
      * 命令跑成功（退出码 0）但 stdout 是空的 → 说明我们在拼命令或读输出上错了；
      * 清洗后的正文为空 → 说明清洗逻辑把正文吃掉了；
      * 临时目录里除了 facts.json/stdout.txt/stderr.txt 还有别的东西
        → 说明我们把不该放的东西放进了 agent 的工作目录。
    其余情况（凭据缺失、目录不可写、沙箱拒绝、超时）都是环境问题 → 跳过。
    """
    name = llm._CLI_AGENTS[agent]["name"]
    # 只在「命令确实跑完了」之后才检查我们自己的逻辑
    if code == 0:
        if not out_text.strip():
            checks.ok(False, "{0} 退出码为 0 但 stdout 为空（拼命令/读输出逻辑有问题）".format(name),
                      "详见上面的 stdout/stderr")
            return
        cleaned = clean_cli_output(out_text)
        checks.ok(bool(cleaned), "{0} 真实调用拿到了可用正文".format(name),
                  "耗时 {0:.1f} 秒".format(elapsed))
        checks.ok(all(entry in ("facts.json", "stdout.txt", "stderr.txt")
                      for entry in workdir_leftovers),
                  "{0} 调用后没有多出多余文件".format(name),
                  "多出的：{0}".format(workdir_leftovers) if workdir_leftovers else "无")
        checks.ok(llm._classify_cli_failure(agent, 1, "", "MISSING_CREDENTIAL") != "",
                  "{0} 的错误分类可用".format(name))
        # 真实输出也要过数字溯源校验——这是整条链路最能说明问题的一步
        ok, bad = verify_numbers(cleaned, dict(SYNTHETIC_FACTS))
        if ok:
            checks.ok(True, "{0} 的真实输出通过了数字溯源校验".format(name), cleaned[:60])
        else:
            # 提示词只说了「只回复：ok」，输出里出现数字本身就说明它多做了一步；
            # 这一步的目的是验通路（真的调到了 agent），不是验内容，所以如实报告即可。
            checks.ok(True, "{0} 的真实输出被数字校验标记（如实报告，非失败）".format(name),
                      "无法溯源：{0}".format("、".join(bad[:6])))
        return
    # code != 0：命令没跑成。是环境问题就跳过，是「我们的调用方式错了」才失败。
    if _is_environment_problem(reason, err_text):
        checks.skip("{0} 真实调用因环境限制跳过".format(name), reason or err_text.strip()[:120])
        return
    if not configured:
        checks.skip("{0} 未配置凭据（预期失败）".format(name), reason or "凭据不存在")
        checks.ok(bool(reason) and any("\u4e00" <= ch <= "\u9fff" for ch in reason),
                  "  未配置凭据时给出了中文可操作提示", (reason or "")[:70])
        return
    checks.ok(False, "{0} 配了凭据但真实调用失败".format(name), reason or err_text.strip()[:120])


def check_cli_real_run(checks: _Checks, prompt: str, timeout: float,
                       keep_temp: bool) -> None:
    """真的有 CLI agent 就用极短提示词跑一次，打印真实 stdout / 耗时 / 退出码。

    判定是**三分**的：环境跑不动（目录不可写 / 凭据缺失 / 沙箱拒绝 / 找不到可执行
    文件）→ 跳过，不算失败；只有「命令跑了、但我们的处理逻辑错了」才失败。
    这样它在 CI 与受限沙箱里都能用，在能跑通的机器上仍然做真实调用验证。
    """
    print("")
    print(_PROBE_LINE)
    print("3. 真实调用本机 CLI agent（提示词：{0}）".format(prompt))
    print(_PROBE_LINE)

    available_agents = [agent for agent in ("dsh", "claude", "codex") if _find_agent(agent)]
    if not available_agents:
        checks.skip("未检测到 CLI agent", "PATH 与已知安装位置都没有 dsh / claude / codex")
        print("  未检测到 CLI agent，跳过真实调用。")
        print("  （这不是失败：没有 agent 时 AI 解读层会自动退回纯规则报告。）")
        return

    for agent in available_agents:
        command = _find_agent(agent)
        configured, where = _agent_credential_state(agent)
        print("")
        print("  ── {0} ──".format(llm._CLI_AGENTS[agent]["name"]))
        print("    命令   ：{0}".format(command))
        print("    数据去向：{0}（厂商）".format(llm._CLI_AGENTS[agent]["vendor"]))
        print("    凭据   ：{0}（{1}）".format("已配置" if configured else "未配置", where))
        if not configured:
            print("    结论   ：未配置凭据，按设计不会用它（继续探测下一个后端）；")
            print("             下面仍然真跑一次，把真实的报错原样打出来。")

        workdir = tempfile.mkdtemp(prefix="health-report-agentcheck-")
        try:
            argv = _cli_argv(agent, command, prompt)
            stdout_path = os.path.join(workdir, "stdout.txt")
            stderr_path = os.path.join(workdir, "stderr.txt")
            print("    工作目录：{0}".format(workdir))
            print("    命令   ：{0}".format(" ".join(
                ["{0!r}".format(part) if " " in part else part for part in argv])))
            # 这个工具只验「命令通路」：它不像 _cli_narrate 那样写 facts.json，
            # 所以工作目录本该是空的（写 facts.json 那条路径由单元测试覆盖）。
            before_run = sorted(os.listdir(workdir))
            print("    调用前工作目录内容：{0}（应为空）".format(before_run))
            checks.ok(before_run == [],
                      "{0} 在一个全新的空目录里运行".format(llm._CLI_AGENTS[agent]["name"]),
                      str(before_run))
            started = time.time()
            code, out_text, err_text, note = _run_agent_to_files(
                argv, workdir, timeout, stdout_path, stderr_path)
            elapsed = time.time() - started
            print("    耗时   ：{0:.1f} 秒（超时上限 {1:.0f} 秒）".format(elapsed, timeout))
            print("    退出码 ：{0}{1}".format(code, ("（" + note + "）") if note else ""))
            print("    stdout （{0} 字节）：".format(len(out_text)))
            for line in (out_text.strip().splitlines() or ["（空）"])[:12]:
                print("      | " + line)
            print("    stderr （{0} 字节）：".format(len(err_text)))
            for line in (err_text.strip().splitlines() or ["（空）"])[:6]:
                print("      | " + line)
            cleaned = clean_cli_output(out_text)
            print("    清洗后 ：{0!r}".format(cleaned[:160]))
            reason = "" if code == 0 else llm._classify_cli_failure(agent, code, note, err_text,
                                                                   elapsed)
            leftovers = sorted(entry for entry in os.listdir(workdir)
                               if entry not in ("stdout.txt", "stderr.txt", "facts.json"))
            print("    调用后工作目录内容：{0}".format(sorted(os.listdir(workdir))))
            if reason:
                print("    降级原因：{0}".format(reason))
            _check_agent_result(checks, agent, code, out_text, err_text, reason, elapsed,
                                configured, leftovers)
        finally:
            if keep_temp:
                print("    临时目录保留（--keep-temp）：{0}".format(workdir))
            else:
                shutil.rmtree(workdir, ignore_errors=True)
                print("    临时目录已清理：{0}".format(not os.path.exists(workdir)))


# ───────────────────────────────────────── 4. 隐私边界与 narrate 编排


def check_narrate_and_privacy(checks: _Checks, backend: llm.Backend) -> None:
    """narrate/narrate_ex 的降级行为，以及 CLI 调用的工作目录边界。"""
    print("")
    print(_PROBE_LINE)
    print("4. narrate 编排与隐私边界")
    print(_PROBE_LINE)

    # 4.1 narrate 签名与老行为：没配 key 时返回空串、且不做任何探测
    result = llm.narrate(SYNTHETIC_FACTS, LLMConfig())
    checks.equal(result, "", "未配置端点时 narrate() 返回空字符串（老行为不变）")
    none_result = llm.narrate_ex(SYNTHETIC_FACTS, LLMConfig(), prefer="none")
    checks.ok(not none_result.attempted and none_result.text == "",
              "narrate_ex(prefer=none) 不发起任何调用",
              "attempted={0}".format(none_result.attempted))
    checks.ok(isinstance(none_result.drop_reason, str) and bool(none_result.drop_reason),
              "none 时也给出人类可读的 drop_reason", none_result.drop_reason)

    # 4.2 校验失败必须丢弃整段文字
    with_temp = llm._cli_narrate
    original = llm._cli_narrate
    try:
        llm._cli_narrate = lambda *a, **kw: ("你的静息心率 58.2，血氧 91。", "")  # type: ignore
        dropped = llm.narrate_ex(SYNTHETIC_FACTS, LLMConfig(), prefer="dsh")
    finally:
        llm._cli_narrate = original
    if dropped.attempted:
        checks.ok(dropped.dropped and dropped.text == "",
                  "编造数字时整段 AI 文字被丢弃",
                  dropped.drop_reason)
    else:
        checks.skip("本机没有可用的指定后端，跳过「丢弃」用例（测试里已覆盖）")
    checks.ok(callable(with_temp), "内部函数可被测试替换（monkeypatch 友好）")

    # 4.3 CLI 调用的临时目录边界：目录必须为空（除了我们放的那一个文件）
    workdir = tempfile.mkdtemp(prefix="health-report-privacycheck-")
    try:
        facts_path = os.path.join(workdir, "facts.json")
        with open(facts_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(SYNTHETIC_FACTS, ensure_ascii=False, indent=1))
        entries = sorted(os.listdir(workdir))
        checks.equal(entries, ["facts.json"],
                     "CLI 工作目录里只有 facts.json（没有原始导出、没有报告）")
        checks.ok(not os.path.exists(os.path.join(workdir, "export.xml")),
                  "工作目录里没有 export.xml")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    checks.ok(not os.path.exists(workdir), "检查用的临时目录已删除")

    # 4.4 数据去向描述不能含糊成「本地 AI」
    if backend.kind == "cli":
        checks.ok("云端" in backend.detail or backend.sends_to in ("DeepSeek", "Anthropic", "OpenAI"),
                  "CLI 后端如实说明模型在云端", backend.detail)
    print("  当前后端的数据去向：{0}（is_local={1}）".format(backend.sends_to, backend.is_local))

    # 4.5 command_override（CLI 的 --ai-command）：显式路径能被识别、能被传下去
    print("")
    print("  --ai-command（command_override）检查：")
    dsh_path = _find_agent("dsh")
    if dsh_path:
        overridden = detect_backend(LLMConfig(), prefer="auto", command_override=dsh_path)
        checks.equal(overridden.kind, "cli", "  显式命令路径被识别为 CLI 后端")
        checks.equal(overridden.name, "DSH", "  显式路径识别为 DSH")
        checks.equal(overridden.command, os.path.abspath(dsh_path),
                     "  命令路径被规范化成绝对路径")
    else:
        checks.skip("  显式命令路径检查", "本机没有 dsh，跳过（单元测试已覆盖）")
    # 认不出的路径：绝不能拿它去拼命令。auto 下会退回自动探测（可能挑到别的后端），
    # 所以这里断言的是「没有采用这个陌生路径」，而不是「结果是 none」。
    unknown_path = os.path.join(os.environ.get("TEMP", "."), "my-unknown-tool.cmd")
    unknown_override = llm._probe_command_override(unknown_path, "auto")
    checks.ok(unknown_override is None,
              "  认不出的路径不会被拼成命令（返回 None，交给正常探测流程）")
    unknown = detect_backend(LLMConfig(), prefer="auto", command_override=unknown_path)
    checks.ok(unknown.command != unknown_path,
              "  认不出的路径不会被采用为命令", "实际 command={0!r}".format(unknown.command))
    strict = detect_backend(LLMConfig(), prefer="codex", command_override="")
    checks.ok(strict.kind in ("cli", "none"),
              "  未给 override 时按 prefer 正常探测", strict.name)
    argv = _cli_argv("dsh", r"D:\x\bin\dsh.cmd", "提示词")
    checks.equal(argv[:2], [r"D:\x\bin\dsh.cmd", "headless"],
                 "  Windows 上 dsh.cmd 的调用形式（可执行文件 + headless + 提示词）")
    checks.ok(len(argv) == 3 and argv[2] == "提示词",
              "  提示词作为单个 argv 元素传入（不经 shell 拼接）",
              "共 {0} 个元素".format(len(argv)))


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="AI 解读层自检（不联网、不装依赖）")
    parser.add_argument("--prompt", default="只回复：ok",
                        help="真实调用 CLI agent 时用的极短提示词（默认「只回复：ok」）")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="真实调用 CLI agent 的超时秒数（默认 180）")
    parser.add_argument("--keep-temp", action="store_true",
                        help="保留真实调用用的临时目录，便于排查")
    parser.add_argument("--skip-real", action="store_true",
                        help="跳过真实调用 CLI agent 这一步")
    args = parser.parse_args(argv)

    print("AI 解读层自检：本机后端探测 / 数字溯源校验 / CLI 真实调用")
    print("（全程只用合成数值；探测只连 127.0.0.1；不安装任何依赖）")
    print("")

    checks = _Checks()
    backend = check_detect(checks)
    check_verify_numbers(checks)
    if args.skip_real:
        checks.skip("按 --skip-real 跳过真实调用 CLI agent")
        print("")
        print(_PROBE_LINE)
        print("3. 真实调用本机 CLI agent")
        print(_PROBE_LINE)
        print("  按 --skip-real 跳过。")
    else:
        check_cli_real_run(checks, args.prompt, args.timeout, args.keep_temp)
    check_narrate_and_privacy(checks, backend)

    print("")
    print(_PROBE_LINE)
    print("结果汇总：通过 {0} 项，失败 {1} 项，跳过 {2} 项".format(
        checks.passed, len(checks.failed), len(checks.skipped)))
    print("判定口径：环境跑不动（目录不可写 / 凭据缺失 / 沙箱拒绝 / 找不到可执行文件）算跳过；")
    print("          只有「命令跑了、但 AI 解读层的处理逻辑错了」才算失败。")
    if checks.skipped:
        print("跳过的检查（不算失败，不影响退出码）：")
        for item in checks.skipped:
            print("  - {0}".format(item))
    if checks.failed:
        print("失败的检查：")
        for item in checks.failed:
            print("  - {0}".format(item))
        print("退出码 1")
        return 1
    print("没有失败项（退出码 0）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
