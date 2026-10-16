"""可选的 AI 解读层：把结构化事实讲成人话，并**硬校验**模型有没有编造数字。

设计原则
--------
* **只用标准库**（`urllib.request` / `subprocess` / `json`），不引入 openai / requests，
  这样「克隆下来立刻可用」这条承诺不会被依赖打破。
* **默认不联网**：没有可用的后端时 `narrate()` 直接返回空字符串，
  报告照常生成，只是第六节给出开启方法。
* **失败一律静默降级**：网络不通、超时、返回体不是 JSON、结构不符……
  全部返回空字符串，绝不让报告生成过程崩掉；日志里只记一条不含密钥的警告。
* **安全边界写在 system prompt 里**：模型只能引用 `facts` 里出现过的数字，
  不许自己计算，不许下诊断，`verdict` 为 `na` 的指标只能谈趋势。

四类后端
--------
`detect_backend()` 会按固定优先级挑一个后端，供 `narrate_ex()` 使用：

1. `http` 环境变量配置的 OpenAI 兼容端点（现有逻辑，`LLMConfig.from_env()`）；
2. `http` **本机** HTTP 模型：`127.0.0.1` 上的 Ollama / LM Studio / llama.cpp 等。
   这一类数据**不出本机**，所以 `Backend.is_local` 为 True。
3. `cli` 客户电脑上**已经装好并登录过**的编码 agent（DSH / Claude Code / Codex）。
   复用客户已有的登录，零配置——这是它存在的唯一理由。
4. `none` 什么都没有，报告退回纯规则版本。

⚠️ 一个容易被含糊掉的概念（所以它被写进了返回字段）
--------------------------------------------------
DSH / Claude Code / Codex 是「**程序在本地、模型在云端**」：装在客户机器上的是
客户端与命令行外壳，推理发生在厂商的服务器上。**调用它们就等于把 facts 发给
对应厂商**，和「本机跑一个 Ollama」完全不是一回事。因此：

* 每个 `Backend` 都必须带 `sends_to`（数据会发给谁）与 `is_local`（是否真的不出机器），
  CLI 后端一律 `is_local=False`，`sends_to` 写厂商名（"DeepSeek" / "Anthropic" / "OpenAI"）；
* 代码、注释、帮助文本里**不允许**把 CLI 后端描述成「本地 AI」——
  只有 `127.0.0.1` 上的 HTTP 模型才是本地的。
* `docs/PRIVACY.md` 里那句「发给谁由你定；发了什么你可以先看」靠的就是这些字段：
  调用前能把「发什么（facts）」和「发给谁（sends_to）」都摊开给用户看。

调用 CLI agent 的三道闸门
------------------------
这些工具是**编码 agent**（设计上会读写文件、执行命令），所以调用它们时：

1. **提示词**：把它的文件能力限制成「只读当前目录下那一份 facts.json」，
   并禁止执行命令、写代码、输出过程叙述；
2. **工作目录**：`tempfile.mkdtemp()` 的空目录，里面只有一份 facts.json，
   绝不放进客户的原始导出或报告——万一它真去读文件，也只能读到这一份；
3. **数字溯源校验**（`verify_numbers`）：事后逐个数检查，编造就整段丢弃。

另外两条踩过的坑，都写进了实现：
* **facts 走文件，不走命令行**：真实 facts.json 有 47 KB，塞进 argv 会撞上
  Windows 的 ~32767 字命令行上限（`The command line is too long.`），
  所以命令行里只有一段短提示词，并且有 `_CLI_ARGV_LIMIT` 护栏先挡一道。
* **stdout/stderr 用文件重定向，不用管道**：目标环境带沙箱，命名管道可能被拒。

`_SYSTEM_PROMPT` 里的硬性约束任何一条都不能删，只能追加；本模块在其之后又追加了
三条「只读 facts.json / 只输出 Markdown 正文 / 空手回复视为无效」的补充，
编号顺延为 13~15（见 `_CLI_NOTE`）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

_LOG = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 60.0

# CLI agent 的默认超时：这些是编码 agent，冷启动本身就要几秒到十几秒
# （实测 `dsh headless` 用 Electron 的 Node runtime 启动，光是启动到报错就花了十几秒），
# 再叠加一次云端推理，180 秒是合理下限。
DEFAULT_CLI_TIMEOUT = 180.0

# 命令行长度护栏。Windows 的命令行上限约 32767 字符（CreateProcess 限制），
# 实测真实数据的 facts.json 有 47 KB，直接塞进 argv 会报
# `The command line is too long.`。这里自己先挡：宁可主动放弃命令行方式，
# 也不要在用户的机器上撞一个看不懂的操作系统错误。
# 8000 字符留了很大余量（不同 Windows 版本、是否经 shell 转发都会影响上限）。
_CLI_ARGV_LIMIT = 8000

# 短提示词 + facts 副本的总长超过这个值就不再附 facts，只让 agent 读文件。
# 比 `_CLI_ARGV_LIMIT` 小很多，保证「附了 facts」的情况也远低于命令行上限。
_PROMPT_INLINE_LIMIT = 6000

# 本机 HTTP 模型的探测超时：本机回环连接要么立刻成功要么立刻 ECONNREFUSED，
# 0.5 秒足够；探测三个端口最坏也就 1.5 秒，不会拖慢正常报告生成。
_LOCAL_PROBE_TIMEOUT = 0.5

# 本机 HTTP 模型的候选端口（都只连 127.0.0.1）：
#   11434 Ollama ｜ 1234 LM Studio ｜ 8080 llama.cpp / 多数自建 OpenAI 兼容服务
_LOCAL_CANDIDATES: Tuple[Tuple[str, str], ...] = (
    ("http://127.0.0.1:11434/v1", "Ollama"),
    ("http://127.0.0.1:1234/v1", "LM Studio"),
    ("http://127.0.0.1:8080/v1", "llama.cpp / 本机 OpenAI 兼容服务"),
)

# system prompt 是本项目的安全边界：这些约束任何一条都不能放松。
#
# 第 12 条是**真机实测之后补的**：模型自己把「每次训练的时长」加成了「约 704 小时」、
# 把热量加成「约 20.2 万 kcal」、还算出「约 53~56%」——这些数字 facts 里没有，
# 于是整句被数字溯源校验删掉。让它「不自己算」比事后删句子更省事：
# 需要总量/占比时，正确做法是写「数据里没有」，而不是自己算一个。
_SYSTEM_PROMPT = """你是健康数据的解释助手，负责把一份已经算好的结构化事实（facts）写成普通人能读懂的说明，并给出下一步可做的事。

硬性约束，任何一条都不能违反：
1. 你不是医生。不提供诊断、不判断疾病、不推荐药物、不给任何剂量或处方；也不要变相复述这类内容。
2. 只能引用 facts 里出现过的数字。绝对不许自己计算、推算、外推或补一个 facts 里没有的数值；需要某个数字但 facts 里没有时，就写「数据里没有」。
3. facts 里 verdict 为 na 的指标只能谈趋势和方向，不许下正常或异常的判断。
4. 参考区间有三种性质：range 是正常参考区间，goal 是活动量目标，screen 是筛查阈值。措辞必须对应：没走到步数目标只能说「未达目标」，不能说「指标异常」；筛查阈值越界只能说「值得进一步评估」，不能说某人有某种病。
5. **`benign_side` 为 true 的指标不要写进「需要注意」的部分**：那表示该值落在这个指标不需要留意的一侧（例如静息心率偏低、去脂体重偏高、血氧偏高）。这类情况在本工具里只是背景信息，不是问题；把它们写成需要处理的事会造成没必要的焦虑，还会淹没真正该看的提示。可以放在中性描述里，也可以不提。
6. facts 的 findings 里如果有数据可信度问题（severity 为 alert 或 warn），必须先提醒这一点，再谈其它结论。
7. 不确定就明说「数据不足以判断」，不要用推测填补空白，也不要把缺失当成正常。
8. 输出 Markdown，分 3~5 个小节，其中必须有一个「下一步」小节；总字数不超过 600 字。不要重复报告里已有的表格，也不要逐个罗列全部指标。
9. 「下一步」要限定在**改善数据可比性与可解释性**的范围内，这才是这份事实能支撑的建议，例如：固定用同一台设备、统一测量条件（晨起、空腹、排空、同一时段）、增加数据点、把观察窗口拉长到 4 周以上、在相同条件下复测以确认某个变化是否持续。每条都要说清「为什么这么做能让判断更可靠」。
10. 不要给医疗、营养或训练处方：不写具体的热量缺口、蛋白质克数、补剂、药物、剂量，也不要建议做某项具体检查或治疗。遇到这类情形，一律写成「这一点需要专业评估，建议带着原始数据咨询医生」。
11. 结尾必须有一句提醒：本解读不是医学建议，如有持续异常请咨询医生。
12. 只引用事实摘要里**已经存在**的数字。不要自己做加法、求平均、算百分比或换算单位；需要总量/占比时，如果摘要里没有，就不要写那个数字。"""

_USER_PREFIX = ("以下是已经计算好的事实（JSON）。请只依据这些事实写解读（含「下一步」），"
                "不要自己计算任何数字，也不要引入 facts 之外的数值。\n\n")

# 把内联的 facts 明确框起来：实测只说「见下面的 JSON」时，agent 会误以为
# 「这条消息里没有附上数据」而回头向调用方索要。醒目分隔符能让它一眼看到数据在哪。
_FACTS_FENCE = ("<<<FACTS_JSON>>>", "<<<END_FACTS_JSON>>>")

_EN_NOTE = "\n\n输出语言：请用英文回答，上面的约束一条都不变。"

# CLI agent 是「编码助手」而不是「聊天补全接口」：它们默认有读写文件、执行命令的能力。
# 这两条追加约束把这种能力限制在「只读工作目录里那一份 facts.json」：
# 为了不让「请读取 facts.json」和「不要读写任何文件」自相矛盾，措辞上先把
# **唯一允许**的那一次读取说清楚，再排除其它一切。
_CLI_NOTE = """

额外约束（你正在以非交互方式被自动调用，违反即视为本次输出无效）：
13. **数据就在本提示词里**：见下面 `<<<FACTS_JSON>>>` 与 `<<<END_FACTS_JSON>>>`
    之间的 JSON（如果本提示词里没有这段，那么同一份数据在**当前工作目录**的
    `./facts.json` 里，读那一个文件即可）。请立刻开始写解读——**不要向调用方
    索要数据**，不要说「请把 facts 发给我」「等你提供数据」「消息里没有附上数据」
    这类话：本对话没有人在，也不会有人回复你。
    除了读那一个 facts.json 之外，不要读写、列出或搜索任何文件，不要执行任何
    命令、脚本或测试，不要写代码，也不要提议修改任何文件。
14. 只输出 Markdown 正文本身：不要解释你的工作过程，不要汇报你读了什么、
    不要输出工具调用记录或思考过程，也不要用 ``` 代码围栏把全文包起来。
15. 即使数据不全或口径有冲突，也要基于**已经拿到的内容**写出解读，并在正文里
    说明哪一项缺数据。空手回复、只给写作提纲、或只提问，都视为本次输出无效。"""


@dataclass
class LLMConfig:
    """一次 AI 解读所需的连接配置。

    注意 `describe()` 会把配置渲染成人看的字符串，**永远不包含完整密钥**，
    只显示前 4 位加 `***`（密钥过短时全部打码）。
    """

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT
    max_tokens: int = 2000
    temperature: float = 0.2

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """从环境变量读配置。

        读取顺序：
          key      HEALTH_REPORT_API_KEY → OPENAI_API_KEY
          base_url HEALTH_REPORT_BASE_URL → OPENAI_BASE_URL → 默认 https://api.openai.com/v1
          model    HEALTH_REPORT_MODEL → OPENAI_MODEL → 默认 gpt-4o-mini
          timeout  HEALTH_REPORT_TIMEOUT（浮点，解析失败用默认）

        base_url 只做「去掉末尾斜杠」这一种处理：用户填什么就用什么。
        不自作聪明补 `/v1`——DeepSeek 的兼容端点是 `https://api.deepseek.com/chat/completions`，
        而 OpenAI 必须保留 `https://api.openai.com/v1`。
        """
        key = _first_env("HEALTH_REPORT_API_KEY", "OPENAI_API_KEY")
        base = _first_env("HEALTH_REPORT_BASE_URL", "OPENAI_BASE_URL") or DEFAULT_BASE_URL
        model = _first_env("HEALTH_REPORT_MODEL", "OPENAI_MODEL") or DEFAULT_MODEL
        timeout = DEFAULT_TIMEOUT
        raw_timeout = _first_env("HEALTH_REPORT_TIMEOUT")
        if raw_timeout:
            try:
                parsed = float(raw_timeout)
                if parsed > 0:
                    timeout = parsed
            except (TypeError, ValueError):
                timeout = DEFAULT_TIMEOUT
        return cls(api_key=key, base_url=_normalize_base(base), model=model, timeout=timeout)

    def available(self) -> bool:
        """三项都有才能发请求：密钥、接口地址、模型名。"""
        return bool(str(self.api_key or "").strip()
                    and str(self.base_url or "").strip()
                    and str(self.model or "").strip())

    def describe(self) -> str:
        """给人看的单行配置摘要，绝不包含完整密钥（CLI 会把它整行塞进提示里）。"""
        return (
            "模型：{0} ｜ 接口：{1} ｜ 密钥：{2} ｜ 超时：{3} 秒 ｜ 最大输出：{4} tokens ｜ 温度：{5}".format(
                str(self.model or "（未设置）"),
                _normalize_base(self.base_url) or "（未设置）",
                _mask_key(self.api_key),
                self.timeout,
                self.max_tokens,
                self.temperature,
            )
        )


def _first_env(*names: str) -> str:
    """按顺序取第一个非空环境变量。"""
    for name in names:
        value = os.environ.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_base(base_url: str) -> str:
    """只去掉末尾斜杠，不加也不删任何路径段。"""
    base = str(base_url or "").strip()
    while base.endswith("/"):
        base = base[:-1]
    return base


def _mask_key(api_key: str) -> str:
    """密钥打码：足够长时显示前 4 位 + ***，短密钥全部打码。"""
    key = str(api_key or "").strip()
    if not key:
        return "（未配置）"
    if len(key) < 8:
        return "***（已配置，长度不足 8 位，已全部打码）"
    return key[:4] + "***"


def _redact(text: str, api_key: str) -> str:
    """兜底：万一服务端把请求原样回显在错误体里，也不能让密钥进日志。"""
    s = str(text or "")
    key = str(api_key or "").strip()
    if key and key in s:
        s = s.replace(key, _mask_key(key))
    return s


def _endpoint(base_url: str) -> str:
    """拼接 chat/completions 端点。保留用户给的路径，只去尾部斜杠。"""
    return _normalize_base(base_url) + "/chat/completions"


def build_messages(facts: Dict[str, Any], lang: str = "zh") -> list:
    """构造 messages（方便测试与复用）。

    system 消息是安全边界；user 消息里放 `json.dumps(facts, ensure_ascii=False, indent=1)`，
    并明确说明这些是**已经算好的事实**，模型只需据此写解读。
    """
    system = _SYSTEM_PROMPT
    if str(lang or "zh").lower().startswith("en"):
        system = system + _EN_NOTE
    try:
        payload = json.dumps(facts if facts is not None else {}, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        payload = "{}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _USER_PREFIX + payload},
    ]


# ══════════════════════════════════════════════════════════════ 后端描述


@dataclass
class Backend:
    """一个可用的解读后端，以及**数据会去哪**。

    kind      "http" 走 HTTP 接口 ｜ "cli" 本机已装的命令行 agent ｜ "none" 什么都不用
    name      人看的后端名："Ollama" / "DSH" / "Claude Code" / "Codex" / "OpenAI 兼容端点" / "无"
    detail    人看的细节：模型名、命令路径或 base_url
    is_local  True 表示数据不出本机（只有 127.0.0.1 上的 HTTP 模型才是）
    sends_to  数据会发给谁："本机（不出机器）" / "DeepSeek" / "Anthropic" / "OpenAI" / "你配置的端点"
    model     HTTP 后端：模型名；CLI 后端留空
    base_url  HTTP 后端：接口地址
    command   CLI 后端：可执行文件路径
    """

    kind: str
    name: str
    detail: str
    is_local: bool
    sends_to: str
    model: str = ""
    base_url: str = ""
    command: str = ""


# CLI agent 的静态元数据。vendor 是**真正会收到数据的那家厂商**，不是「本地」：
# 这些工具装在客户机器上，但推理在厂商的云端，调用它们 = 把 facts 发出去。
# 这一点必须在 UI 与文档里说清，不能含糊成「本地 AI」。
_CLI_AGENTS: Dict[str, Dict[str, str]] = {
    "dsh": {
        "name": "DSH",
        "vendor": "DeepSeek",
        "note": "DeepSeek Harness（本机命令行客户端，模型在 DeepSeek 云端）",
    },
    "claude": {
        "name": "Claude Code",
        "vendor": "Anthropic",
        "note": "Claude Code（本机命令行客户端，模型在 Anthropic 云端）",
    },
    "codex": {
        "name": "Codex",
        "vendor": "OpenAI",
        "note": "Codex CLI（本机命令行客户端，模型在 OpenAI 云端）",
    },
}

_PREFER_ALIASES: Dict[str, str] = {
    "auto": "auto",
    "none": "none",
    "off": "none",
    "http": "http",
    "api": "http",
    "cli": "cli",
    "dsh": "dsh",
    "claude": "claude",
    "codex": "codex",
}

# Backend.name 是给人看的名字，回查 agent 键时用这张表。
_AGENT_BY_DISPLAY_NAME: Dict[str, str] = {
    meta["name"]: key for key, meta in _CLI_AGENTS.items()
}

_NO_BACKEND = Backend(kind="none", name="无", detail="没有可用的 AI 后端",
                      is_local=False, sends_to="不发送任何数据")


def _normalize_prefer(prefer: Any) -> str:
    """把 `prefer` 归一化成内部取值；非法值抛带中文说明的 ValueError。"""
    raw = str(prefer if prefer is not None else "auto").strip().lower()
    if not raw:
        raw = "auto"
    if raw not in _PREFER_ALIASES:
        raise ValueError(
            "prefer 取值非法：{0!r}。可选值：auto（自动探测）、none（不探测、纯规则报告）、"
            "http（只用环境变量配置的端点）、cli（只用本机已装的 AI 工具）、"
            "dsh / claude / codex（指定某一个工具）。".format(prefer)
        )
    return _PREFER_ALIASES[raw]


def _host_of(url: str) -> str:
    """从 base_url 里取主机名（取不到就原样返回，方便写进 sends_to）。"""
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    return host or str(url or "")


def _is_loopback_url(url: str) -> bool:
    """只认 127.0.0.0/8、::1、localhost：本机模型探测绝不碰局域网地址。"""
    host = _host_of(url).strip().strip("[]").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _local_http_backend(name: str, base_url: str, model: str) -> Backend:
    """本机 HTTP 模型：唯一一种 `is_local=True` 的后端。"""
    label = (model + " ｜ " if model else "") + base_url
    return Backend(kind="http", name=name, detail=label, is_local=True,
                   sends_to="本机（不出机器）", model=model, base_url=base_url)


def _http_backend(config: LLMConfig, base_url: str, model: str) -> Backend:
    """环境变量配置的端点。

    `is_local` 恒为 False：用户显式配置了「我要把数据发到这里」，
    即便他填的是 localhost，也要如实说明这不是本工具自动挑的本机模型。
    """
    label = (model + " ｜ " if model else "") + base_url
    return Backend(kind="http", name="OpenAI 兼容端点", detail=label, is_local=False,
                   sends_to=_host_of(base_url) or "你配置的端点", model=model, base_url=base_url)


def _probe_local_http(url: str, timeout: float = _LOCAL_PROBE_TIMEOUT) -> Optional[str]:
    """探测一个本机 OpenAI 兼容端点，返回模型名（探不到返回 None）。

    只看 `GET {url}/models`；**只允许连回环地址**，避免把健康数据探测请求
    意外发到局域网或公网。任何异常都当成「没有」，绝不抛出。
    """
    if not _is_loopback_url(url):
        return None
    request = urllib.request.Request(_normalize_base(url) + "/models", method="GET",
                                     headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if isinstance(data, dict):
        items = data.get("data")
        if not isinstance(items, list) and isinstance(data.get("models"), list):
            items = data["models"]
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    name = item.get("id") or item.get("name") or item.get("model")
                    if isinstance(name, str) and name.strip():
                        return name.strip()
    return ""


def _probe_candidates(candidates: Sequence[Tuple[str, str]] = _LOCAL_CANDIDATES
                      ) -> Optional[Tuple[str, str, str]]:
    """并发探测本机 HTTP 模型，返回 `(base_url, 显示名, 模型名)`；都没有返回 None。

    为什么要并发：三个候选**串行**探测时，一个都没装的情况下要等
    3 × 0.5 = 1.5 秒，而 `health-report report` 在纯规则模式下也会走这条探测
    （用户只是想生成报告，却先卡 1.5 秒）。并发之后最坏情况是 0.5 秒。

    并发是安全的：只有三个线程、每个线程只做一次 0.5 秒超时的本机 HTTP 请求，
    不共享可变状态；结果按 `_LOCAL_CANDIDATES` 的顺序收集，所以**优先级仍然确定**
    （Ollama → LM Studio → llama.cpp），不会因为线程调度快慢而变。
    """
    if not candidates:
        return None
    results: List[Optional[str]] = [None] * len(candidates)
    threads: List[threading.Thread] = []

    def _work(index: int, url: str) -> None:
        try:
            results[index] = _probe_local_http(url)
        except Exception:  # 兜底：探测线程绝不允许把异常带出来
            results[index] = None

    for index, (url, _name) in enumerate(candidates):
        thread = threading.Thread(target=_work, args=(index, url), daemon=True,
                                  name="health-report-probe-{0}".format(index))
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=_LOCAL_PROBE_TIMEOUT * 3)
    for index, (url, name) in enumerate(candidates):
        if results[index] is not None:
            return url, name, results[index]
    return None


def _search_dirs() -> List[str]:
    """额外探测用的目录：PATH 之外常见的手工安装位置。

    这些都是**只读检查**，不会创建任何东西。前几项按平台填：
    Windows 用 %LOCALAPPDATA%\\Programs，类 Unix 用 ~/.local/bin、/usr/local/bin、~/.npm-global/bin。
    最后一项覆盖「装了 DSH 桌面版但我不知道它装在哪」的情形（见 `_dsh_bin_dirs`）。
    """
    dirs: List[str] = []
    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA") or ""
        appdata = os.environ.get("APPDATA") or ""
        home = os.path.expanduser("~")
        for base in (local_appdata, appdata):
            if base:
                dirs.append(os.path.join(base, "Programs"))
        dirs.append(os.path.join(home, "AppData", "Local", "Programs"))
    else:
        dirs.extend([
            os.path.join(os.path.expanduser("~"), ".local", "bin"),
            "/usr/local/bin",
            "/usr/bin",
            os.path.join(os.path.expanduser("~"), ".npm-global", "bin"),
        ])
    dirs.extend(_dsh_bin_dirs())
    return dirs


def _drive_roots() -> List[str]:
    """枚举「可能装着程序的盘」上的 Program Files 目录。

    为什么不能只看 `%ProgramFiles%` / `%ProgramFiles(x86)%`：它们只指向**系统盘**
    （实测本机都指 C:），而 DSH 装在 `D:\\Program Files (x86)\\...`，于是
    「可执行文件明明在、探测却说没有」。这里的做法是：从 PATH 与几个已知环境变量里
    收集出现过的盘符，再对每个盘符拼出两个常见安装根目录。

    全程只做 `os.path.isdir`，不枚举磁盘、不递归，代价可以忽略。
    """
    drives: List[str] = []
    samples: List[str] = []
    for name in ("PATH", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA",
                 "ProgramData", "SystemRoot", "USERPROFILE", "DSH_HOME", "DSH_PROFILE_DIR"):
        value = str(os.environ.get(name) or "")
        if value:
            samples.append(value)
    pattern = re.compile(r"(?:^|;)([A-Za-z]):[\\/]")
    for sample in samples:
        for match in pattern.finditer(";" + sample):
            drive = match.group(1).upper() + ":"
            if drive not in drives:
                drives.append(drive)
    # 当前进程自己所在盘通常就是「程序装在这台机器的哪个盘」的答案。
    for path in (sys.executable, os.getcwd()):
        drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
        if drive and drive not in drives:
            drives.append(drive)
    roots: List[str] = []
    for drive in drives:
        roots.append(drive + "\\Program Files")
        roots.append(drive + "\\Program Files (x86)")
    return roots


def _dsh_bin_dirs(max_entries: int = 120) -> List[str]:
    """找 DSH 的 `resources\\runtime\\cli\\bin` 目录。

    为什么需要它：实测本机上 `Get-Command dsh` / `shutil.which("dsh")` **都找不到**
    `dsh`（它不在 PATH 里），而可执行文件确实躺在一个完全猜不到的位置
    （`D:\\Program Files (x86)\\DEEPSEEK HARNESS\\resources\\runtime\\cli\\bin\\dsh.cmd`
    —— 注意在 **D 盘**，所以 %ProgramFiles% / %ProgramFiles(x86)% 那两个环境变量
    根本指不到它）。所以这里用「**不依赖安装目录名与盘符**」的判据：

    在若干常见安装根目录下扫一层子目录，谁含有 `resources\\runtime\\cli\\bin`
    这个结构，就认为它是 DSH 的安装目录。再叠一层候选根目录：

      * `DSH_HOME` / `DSH_INSTALL_DIR` / `DSH_APP_DIR` 往上找两级的父目录；
      * `%ProgramFiles%` / `%ProgramFiles(x86)%` / `%LOCALAPPDATA%` / `%APPDATA%`
        各自所在盘的 `<盘>:\\Program Files` 与 `<盘>:\\Program Files (x86)`
        （这样 D 盘上的安装也能被发现）；
      * 当前进程可执行文件所在目录的祖先链（DSH 打包成 Electron 时，
        它的 runtime 往往就在 Electron 安装目录旁边）。

    扫描是**有界**的：只看一层子目录、总条目数设上限、每个 `os.listdir` 都容错，
    所以最坏情况也只是几十次 `os.path.isdir`，不会拖慢报告生成。
    """
    found: List[str] = []
    seen: set = set()

    def _consider(root: str) -> None:
        """检查一个候选安装根目录：root 本身或它的一层子目录里有没有 cli/bin。"""
        if not root or not os.path.isdir(root) or root in seen:
            return
        seen.add(root)
        candidates = [root]
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            entries = []
        for name in entries[:max_entries]:
            if name.lower() in ("windows", "$recycle.bin", "system volume information"):
                continue
            sub = os.path.join(root, name)
            if os.path.isdir(sub):
                candidates.append(sub)
        for base in candidates:
            marker = os.path.join(base, "resources", "runtime", "cli", "bin")
            if os.path.isdir(marker) and marker not in found:
                found.append(marker)

    roots: List[str] = []
    for env_name in ("DSH_HOME", "DSH_INSTALL_DIR", "DSH_APP_DIR", "DSH_DESKTOP_DIR"):
        base = str(os.environ.get(env_name) or "").strip()
        if base:
            roots.append(os.path.dirname(os.path.dirname(base)))
            roots.append(base)
    for pf in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), "C:\\Program Files"):
        if pf:
            roots.append(pf)
    roots.extend(_drive_roots())
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")):
        if base:
            roots.append(base)
    # 当前进程的祖先链：打包成桌面应用时 runtime 通常就在旁边。
    here = os.path.dirname(os.path.abspath(sys.executable or os.getcwd()))
    for _ in range(4):
        roots.append(here)
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    for root in roots:
        _consider(root)
    return found


def _path_entries() -> List[str]:
    """PATH 拆成目录列表。"""
    return [p for p in str(os.environ.get("PATH") or "").split(os.pathsep) if p.strip()]


def _which(name: str) -> str:
    """找一个可执行文件：先 `shutil.which`，再扫 PATH 目录与已知安装位置。

    返回绝对路径（找不到返回空串）。`shutil.which` 在 Windows 上认 PATHEXT
    （所以 `dsh.cmd` / `codex.cmd` 都能命中）；手工扫描时两种后缀都试。
    """
    found = shutil.which(name)
    if found:
        return os.path.abspath(found)
    exts = (".cmd", ".bat", ".exe", ".ps1", "") if os.name == "nt" else ("",)
    for directory in _path_entries() + _search_dirs():
        if not directory:
            continue
        for ext in exts:
            candidate = os.path.join(directory, name + ext)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
    return ""


# ───────────────────────────────────────────────── CLI agent 的凭据检查
# 「文件在」不等于「能用」：三个 agent 都需要**事先登录/配好密钥**才能非交互调用。
# 只检查可执行文件存在就宣布可用，客户会得到一次莫名其妙的超时。
# 所以这里额外看凭据；缺凭据时**继续往下探测别的后端**，而不是停在这一条上。


def _dsh_home() -> str:
    """DSH 的配置目录：默认 ~/.dsh，可用 DSH_HOME 覆盖（环境变量已设置时不做改动）。"""
    return str(os.environ.get("DSH_HOME") or "").strip() or os.path.join(os.path.expanduser("~"), ".dsh")


def _dsh_credential_state() -> Tuple[bool, str]:
    """判断 DSH 是否已经配好模型凭据。

    判据（DSH 的凭据由它的 web/desktop 界面写入，不看环境变量也通常成立）：
      * 环境里有 DEEPSEEK_API_KEY —— 这是 DSH 自己给出的另一条路；
      * 或 `$DSH_HOME` 下存在凭据存储（`.credentials.yaml` / `auth.json` /
        `credentials.json`），DSH 的 credentials service 就写在这些名字下。

    注意：这里**只读**，不修改 `DSH_HOME`。凭据存在真实的 `DSH_HOME` 里，
    所以调用 `dsh` 时绝不能把 `DSH_HOME` 指到临时目录——那样反而找不到凭据。
    """
    if _first_env("DEEPSEEK_API_KEY"):
        return True, "环境变量 DEEPSEEK_API_KEY"
    home = _dsh_home()
    for name in (".credentials.yaml", "credentials.json", "auth.json"):
        if os.path.isfile(os.path.join(home, name)):
            return True, os.path.join(home, name)
    return False, home


def _config_credential_state(paths: Sequence[str], env_names: Sequence[str]) -> Tuple[bool, str]:
    """通用的「配置目录里有没有登录痕迹 / 环境里有没有密钥」检查。"""
    for env_name in env_names:
        if _first_env(env_name):
            return True, "环境变量 " + env_name
    for path in paths:
        if os.path.isfile(path):
            return True, path
    return False, paths[0] if paths else ""


def _agent_credential_state(agent: str) -> Tuple[bool, str]:
    """某个 CLI agent 的凭据状态。找不到痕迹就当作「不可用」，继续探测别的后端。"""
    home = os.path.expanduser("~")
    if agent == "dsh":
        return _dsh_credential_state()
    if agent == "claude":
        # Claude Code：登录态在 ~/.claude/ 下（凭据文件或凭证存储），
        # 也接受 ANTHROPIC_API_KEY / CLAUDE_CONFIG_DIR 指向的位置。
        config_dir = _first_env("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")
        return _config_credential_state(
            [os.path.join(config_dir, ".credentials.json"),
             os.path.join(config_dir, "credentials.json"),
             os.path.join(config_dir, "auth.json")],
            ("ANTHROPIC_API_KEY",),
        )
    # Codex CLI：登录态在 ~/.codex/auth.json（或 CODEX_HOME 指向的位置）；
    # OpenAI 也支持直接用 OPENAI_API_KEY 跑非交互模式。
    codex_home = _first_env("CODEX_HOME") or os.path.join(home, ".codex")
    return _config_credential_state(
        [os.path.join(codex_home, "auth.json"),
         os.path.join(codex_home, "credentials.json")],
        ("OPENAI_API_KEY",),
    )


def _find_agent(name: str) -> str:
    """定位一个 CLI agent 的可执行文件（PATH + 已知安装位置）。"""
    return _which(name)


def _unconfigured_cli_backend(agent: str, command: str) -> Backend:
    """可执行文件在、但没配凭据：如实标注，并让调用方继续往下探测。"""
    meta = _CLI_AGENTS[agent]
    return Backend(
        kind="cli",
        name=meta["name"],
        detail="{0} ｜ 命令：{1} ｜ 未检测到已配置的模型凭据，可能失败".format(meta["note"], command),
        is_local=False,
        sends_to=meta["vendor"],
        command=command,
    )


def _cli_backend(agent: str, command: str) -> Backend:
    """可执行文件在、凭据也在：这个后端可用。"""
    meta = _CLI_AGENTS[agent]
    return Backend(
        kind="cli",
        name=meta["name"],
        detail="{0} ｜ 命令：{1}".format(meta["note"], command),
        is_local=False,
        sends_to=meta["vendor"],
        command=command,
    )


# ────────────────────────────────────────────────────────── detect_backend


def _explicit_backend(prefer: str, config: Optional[LLMConfig],
                      command_override: str = "") -> Optional[Backend]:
    """`prefer` 显式指定时的结果。

    指定了某个后端但本机没有它 → 返回 None，由调用方降级为「无」，
    **不会**偷偷换成别的后端（用户说的是「就用这个」，不是「优先这个」）。
    """
    if prefer == "none":
        return Backend(kind="none", name="无", detail="已显式关闭 AI 解读（prefer=none）",
                       is_local=False, sends_to="不发送任何数据")
    if prefer == "http":
        if config is not None and config.available():
            base = _normalize_base(config.base_url)
            return _http_backend(config, base, str(config.model or ""))
        found = _probe_candidates()
        if found is not None:
            base, name, model = found
            return _local_http_backend(name, base, model)
        return None
    if prefer == "cli":
        return _detect_cli(command_override)
    if prefer in _CLI_AGENTS:
        if command_override:
            return _probe_command_override(command_override, prefer)
        command = _find_agent(prefer)
        if command:
            available, _where = _agent_credential_state(prefer)
            if available:
                return _cli_backend(prefer, command)
            return _unconfigured_cli_backend(prefer, command)
        return None
    return None


def _detect_cli(command_override: str = "") -> Optional[Backend]:
    """按 dsh → claude → codex 的顺序找本机已装的 AI 工具。

    凭据齐全的第一个直接返回；只有「装了但没配凭据」的，记下第一个作为兜底
    （它仍然返回 kind="cli"，但 detail 里写明未配置凭据、可能失败），
    同时**继续往下探测**——也许后面的 Claude/Codex 才是配好的那个。

    `command_override` 非空时只认这一个可执行文件（对应 CLI 的 `--ai-command`）。
    """
    if command_override:
        agent = _command_override_agent(command_override, "")
        if agent:
            return _cli_backend(agent, command_override)
        return None
    fallback: Optional[Backend] = None
    for agent in ("dsh", "claude", "codex"):
        command = _find_agent(agent)
        if not command:
            continue
        available, _where = _agent_credential_state(agent)
        if available:
            return _cli_backend(agent, command)
        if fallback is None:
            fallback = _unconfigured_cli_backend(agent, command)
    return fallback


def _command_override_agent(path: str, hint: str = "") -> str:
    """从用户给的 `--ai-command` 路径判断这是哪个 agent。

    判据只看 basename：`claude.cmd` → claude，`dsh.cmd` → dsh，`codex` → codex。
    认不出来就返回空串——调用方会降级成「无」，**不会**随便猜一个。
    （`hint` 是 `prefer`，比如用户同时给了 `--ai-command X --ai-prefer codex`
    这种显式指定，那就直接用 hint，不用猜。）

    ⚠️ **不能用 `os.path.basename`**：它只认当前平台的分隔符。
    在 Linux/macOS 上 `os.path.basename(r"D:\\x\\bin\\dsh.cmd")` 会把**整条路径**
    原样返回（因为那里的分隔符是 `/`），于是认不出这是 dsh——用户显式指定的
    命令被静默忽略，代码继续去探测 PATH。

    这就是第一次跑 CI 时 7 个 POSIX job 全红的根因（4 个用例失败），
    而 Windows 上全绿：**`os.path` 的行为随平台变，写成跨平台的自测就漏了。**
    用户手里的路径可能来自任何平台（配置、文档、脚本），所以这里同时按
    `/` 和 `\\` 切，与运行平台无关。
    """
    if hint in _CLI_AGENTS:
        return hint
    text = str(path or "").strip().strip('"').strip("'")
    # 反斜杠与正斜杠都当分隔符：不依赖 os.path 的平台行为
    base = text.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    for ext in (".cmd", ".bat", ".exe", ".ps1", ".sh"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    for agent in _CLI_AGENTS:
        if base == agent or base.startswith(agent + "-") or base.startswith(agent + "_"):
            return agent
    return ""


def _probe_command_override(command_override: str, prefer: str) -> Optional[Backend]:
    """处理 `command_override`：指定了就用它，认不出是哪个 agent 就放弃。

    `prefer="http"` / `"none"` 与命令行覆盖是冲突的两种意图，直接忽略覆盖
    （用户显式说了「走 HTTP」），不做「猜他想干嘛」这种事。

    认不出是哪个工具时返回 None，让调用方继续按正常流程走：`prefer="auto"`
    下会退回到自动探测（可能挑到别的可用后端），`prefer="dsh"` 之类下则变成
    「无」。两种都不会拿这个陌生路径去拼命令。
    """
    path = str(command_override or "").strip().strip('"')
    if not path:
        return None
    if prefer in ("none", "http"):
        return None
    agent = _command_override_agent(path, prefer if prefer in _CLI_AGENTS else "")
    if not agent:
        _LOG.warning("AI 解读：--ai-command 指向的可执行文件认不出是哪个工具（%s），已忽略",
                     os.path.basename(path))
        return None
    resolved = os.path.abspath(path) if os.path.isfile(path) else path
    return _cli_backend(agent, resolved)


def detect_backend(config: LLMConfig, prefer: str = "auto",
                   command_override: str = "",
                   allow_remote: bool = False) -> Backend:
    """挑一个 AI 解读后端。

    顺序（`prefer` 非法时抛 ValueError）：
      1. `prefer` 显式指定：
         - auto   自动探测（下面的 2 → 3 → 4）
         - none   **不探测任何东西**，直接 kind="none"（用于「我要纯规则报告」）
         - http   只用 HTTP：先环境变量配置的端点，再本机 HTTP 模型
         - cli    只用本机已装的 AI 工具
         - dsh / claude / codex  只要指定那一个（没装就 kind="none"，不会换别的）
      2. **环境变量配置的端点**（`HEALTH_REPORT_API_KEY` 等，现有逻辑）：
         kind="http"、`sends_to` = 实际 base_url 的主机名、`is_local=False`；
      3. **本机 HTTP 模型**：依次探测 127.0.0.1 上 11434（Ollama）、1234（LM Studio）、
         8080（llama.cpp）的 `/models`，每个超时 0.5 秒，只连回环地址；
         命中则 `is_local=True`、`sends_to="本机（不出机器）"`；
      4. **本机已装的 AI 工具**：dsh → claude → codex，`shutil.which` 加已知安装位置，
         并且要求**凭据也在**（见 `_agent_credential_state`）；命中则 kind="cli"，
         `sends_to` 写厂商名、`is_local=False`；
      5. 都没有 → kind="none"。

    ⚠️ **`prefer="auto"` 绝不会自动选中第 4 类（会外发的 CLI 工具）。**

    这是一条隐私上的硬约束，不是遗漏。第 4 类的程序虽然装在本机，**推理在厂商云端**：
    用它就等于把你的健康摘要发给 DeepSeek / Anthropic / OpenAI。

    早期版本让 auto 一路探测到底，后果是实测踩到的：客户机器上装了 DSH 时，
    `health-report report 导出.xml`（**既没配 key、也没加 `--llm`**）会**静默地把摘要发出去**。
    这与本项目的默认承诺「什么都不发出去」直接冲突——`docs/PRIVACY.md` 当时那张
    「什么时候会联网」的表里，`report 文件` 那一行写的是"不"。

    现在的分工是：

    | `prefer` | 允许的后端 | 为什么 |
    |---|---|---|
    | `auto`（默认） | 环境变量配置的端点、**本机模型** | 前者是你显式配置 = 知情；后者不出本机 |
    | `llm` / `cli` / `dsh` / `claude` / `codex` | 含会外发的 CLI 工具 | 你显式要求了 AI |

    也就是说：**要外发，必须显式开口。** 本机模型不需要，因为它不出机器。

    `command_override`（CLI 的 `--ai-command`）用来指定一个**没进 PATH** 的可执行
    文件路径，比如 `D:\\...\\resources\\runtime\\cli\\bin\\dsh.cmd`。给了它本身
    就是一种显式意图，所以 `prefer="auto"` 下也会采用（认不出是哪个工具时降级为
    kind="none"，**不会**去猜子命令）。
    """
    normalized = _normalize_prefer(prefer)
    override = _probe_command_override(command_override, normalized)
    if override is not None:
        return override

    # `command_override` 给了、但没被采用（认不出是哪个工具）。这时怎么走取决于 prefer：
    #   * `none` / `http`  → 用户明确说了不走 CLI，**忽略 override**，继续下面那条 prefer。
    #   * `auto`           → **就此收手，不探测 PATH**。用户显式指定了「用这个命令」，
    #                        静默换成别的后端（例如本机装好的 DSH）违背他的意图——
    #                        这正是 `--ai-command` 存在的意义。契约有测试钉着：
    #                        「给了 override 就不该再探测 PATH」。
    #   * 其它显式 prefer   → 交回下面的显式分支（用户同时说了「走 CLI」和「用这个命令」）。
    #
    # 早期版本没有这一层，会继续往下探测。Windows 上侥幸没暴露
    # （`os.path.basename` 认得反斜杠，override 总会命中），到了 Linux/macOS 上
    # 立刻变成 4 个用例失败——**第一次跑 CI，7 个 POSIX job 全红就是这个原因**。
    if str(command_override or "").strip() and normalized == "auto":
        return _NO_BACKEND

    if normalized != "auto":
        backend = _explicit_backend(normalized, config)
        return backend if backend is not None else _NO_BACKEND
    if config is not None and config.available():
        base = _normalize_base(config.base_url)
        return _http_backend(config, base, str(config.model or ""))
    found = _probe_candidates()
    if found is not None:
        base, name, model = found
        return _local_http_backend(name, base, model)
    # 走到这里说明只有「会外发的 CLI 工具」可选。auto 不替用户做这个决定——
    # 除非调用方明确说了 allow_remote（对应 CLI 的 --llm）。
    cli = _detect_cli()
    if cli is not None and not cli.is_local:
        if allow_remote:
            return cli
        _LOG.info("AI 解读：检测到本机已装的 %s（数据会发往 %s），"
                  "但自动模式不会使用会外发的后端。要启用请加 --llm 或 "
                  "--ai-backend dsh/claude/codex。", cli.name, cli.sends_to)
    return _NO_BACKEND


# ══════════════════════════════════════════════ 数字溯源校验（硬约束）


# 白名单里额外放行的数字，以及**为什么**放行它们。
# 这是一份「宁可放过、也要少误杀」的清单：白名单越大，漏抓的可能越多，
# 但误杀（把一段完全正确的解读丢掉）的代价更大——所以只放行那些**结构性、
# 非测量值**的数字。
#   1~12     小节序号（"第 2 点"）、月份、以及少量「常识常数」级别的计数
#   7/14/28/30/90/180/365  天/周/月/年这些窗口长度，是建议里的口径
#             （"把观察窗口拉长到 4 周以上"）而不是任何测量结果
#   *        100/1000 是百分数与千分位换算的常数，60（秒/分）也是常见换算
_NUMBER_WHITELIST: frozenset = frozenset(
    [float(n) for n in range(1, 13)]
    + [7.0, 14.0, 28.0, 30.0, 90.0, 180.0, 365.0, 60.0, 100.0, 1000.0]
)

# 允许出现的数字形状。注意顺序：先匹配千分位/小数/整数。
# 外层那对括号**是必需的**：它把「纯数字部分」固定成 group(1)，
# `_iter_numbers` 就靠它把 1.08 与后缀「万」分开（不要把整段 match 丢给 float）。
#
# 三种「一整组数字」的写法，按优先级排列：
#   1. 逗号千分位 `1,910`；
#   2. **空格千分位** `11 534`（真机实测：模型就是这么写的，而 facts 里是 11534，
#      于是整句被当成编造删掉）。判据刻意收得很紧——`1~3 位 + 一个或多个
#      「空格 + 恰好 3 位」`，所以「近 7 天」「2 台设备」这种正常断开的数字
#      不会被粘成一个数。结尾的 `(?!\d)` 再挡一层：`1 2000` 拆成 1 与 2000，
#      而不是硬拼成 12000。
#   3. 普通整数/小数。
_NUM_TOKEN = (r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?"
              r"|\d{1,3}(?:[ \u00a0\u202f\u2009]\d{3})+(?!\d)(?:\.\d+)?"
              r"|\d+(?:\.\d+)?)")
_NUM_RE = re.compile(
    r"(" + _NUM_TOKEN + r")"
    + r"(?:\s*(?P<mult>[万千亿]|[KkMmBb](?![A-Za-z])))?"
    + r"(?:\s*(?P<sci>[eE][+-]?\d+))?"
)

# 全角数字 / 全角小数点千分位 → 半角。中文文本里"１.４４"这种很常见。
_FULLWIDTH = {ord("０") + i: ord("0") + i for i in range(10)}
_FULLWIDTH.update({ord("．"): ord("."), ord("，"): ord(","), ord("＋"): ord("+"), ord("－"): ord("-")})

# 单位乘数：把 "1.08 万" 这种写法乘开再比（10800）。
_MULTIPLIERS: Dict[str, float] = {
    "万": 1e4, "千": 1e3, "亿": 1e8,
    "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6, "b": 1e9, "B": 1e9,
}

# 「千」/「万」后面跟这些字时，它们不是数量前缀而是**单位词的一部分**：
# 「1,910 千卡」是 1910 kcal，不是 1910000；「1500 千米」是 1500 km。
# 这一类误乘会把正确的解读判成编造（正是最该避免的误杀），所以单独兜住。
#
# 这份表刻意**不含「步」**：需求明确要求「1.08 万步」按 1.08 × 10000 = 10800 处理，
# 而「万步」本身又确实是个常用量词（日行万步）。两者冲突时按需求走——
# 「1.08 万」这种写法乘开，是这个项目最常见的那一种。
# 代价：「1.08 万步」若与 facts 里的 10862753 相比会判为不通过（10800 ≠ 10862753）；
# 这属于「宁可判错也不放过」的一侧，且只可能发生在真正写了「万步」的句子里。
_UNIT_CHARS_AFTER_CJK_MULT = frozenset("卡米克瓦帕焦升伏欧安赫兹秒吨度位")


def _effective_multiplier(match: "re.Match", normalized: str) -> str:
    """决定这一处「万/千/亿」到底是数量前缀还是单位词的一部分。

    规则：只有紧跟在乘数后面的那个字是单位字（`1,910 千卡`、`1500 千米`）时，
    「千」才按单位处理、不参与乘法；其余情况（`1.08 万步`、`累计 1.2 万`）照常乘开。
    千/万/亿后面**直接**跟单位字是极常见的写法，而这个判断只影响紧邻一个字，
    所以它能消掉绝大多数误乘，又不会漏掉真正的数量前缀。
    """
    mult = match.group("mult")
    if not mult:
        return ""
    # ⚠️ 只豁免「千」，**不豁免「万」**。
    #
    # 「千米 / 千克 / 千瓦 / 千卡 / 千焦」都是真正的单位名（km / kg / kW / kcal / kJ），
    # 所以「1500 千米」不该乘成 1500000。
    # 而「万米 / 万克 / 万瓦」并不是单位名——中文里「1.08 万米」就是 10800 米。
    # 早期版本把「万」也一并豁免，于是「走了 1.08 万米」被误判成无法溯源
    # （facts 里是 10800），又是一次误杀。实测发现后收窄。
    if mult == "千":
        # 注意 group(0) 里可能带着乘数前的空白（"1.08 万"），先去干净再往后看一个字。
        end = match.start() + len(match.group(0).rstrip())
        following = normalized[end:end + 1]
        if following and following in _UNIT_CHARS_AFTER_CJK_MULT:
            return ""
    return mult

# 括号里的内容多数是出处/别名/单位换算（"体重(kg)"、"BMI（身体质量指数）"），
# 但也可能藏着真正的数值（"静息心率（58.2）"）。所以括号里的数字**照样校验**，
# 只有「一个数字都没有」的括号才被忽略——这样不会因为出处里的年份/序号误杀。
_BRACKET_RE = re.compile(r"[(（][^()（）]*[)）]")

# 「上标」可选支持：² / ³ 是 BMI、体表面积里常见的单位后缀，不是数值本身。
_SUPERSCRIPT_RE = re.compile(r"[²³¹]")

# Markdown 的**小节号 / 列表序号**：真机实测里 `### 2.2 步数日均 5687 步…` 被判成
# 「数字 2.2 无法溯源」，整句被删——而 2.2 是小节号，不是任何测量值。
#
# 这个正则在行首抓「一个可能是编号的数字」，**是不是编号由 `_is_section_number`
# 决定**（正则只负责把候选连同它前面的 `#` / `**` 标记一起抓出来）：
#   * `#{1,6}` 标题：`## 2.2 步数`、`### 1. 概述`、`#### 3.1.4 细节`；
#   * 行首加粗小节号：`**2.2** 覆盖率`；
#   * 行首列表编号：`1. 我先把数据按周汇总`、`2) 静息心率`、`3、血氧`。
#
# ⚠️ 判据刻意收得很紧，**没有放宽过头**：正文里的 `2.2`（「上升了 2.2」）不在行首，
# 行首的裸数字（`5687 步`、`58.2 次/分`）不算编号，`**20.2 万 kcal**` 也不算——
# 它们全都照常走溯源校验。
_SECTION_NUMBER_RE = re.compile(
    r"(?m)^(?P<lead>[ \t]*(?:(?P<hashes>#{1,6})[ \t]*)?(?P<bold>\*\*)?)"
    r"(?P<num>\d{1,4}(?:[.．]\d{1,4})*)(?P<mark>[.、)）]?)(?=[ \t]|\*\*|$)"
)

#: 紧跟在数字后面的**量级后缀**：`20.2 万 kcal`、`1.08 万步`、`3.2千步`。
#: 有小节号嫌疑的数字后面跟着它时，一律按测量值处理、照常校验。
_MAGNITUDE_CHARS = "万千亿kKmMbB"

# 相对误差 1e-6 或绝对误差 1e-6 —— facts 里可能有 0.30000000000000004 这种浮点尾巴，
# 用它比就足够宽松；同时又远小于任何有意义的测量精度（0.1 级），不会把
# "58.2" 和 "58.25" 当成同一个数。
_REL_TOL = 1e-6
_ABS_TOL = 1e-6

#: 遍历 facts 收集「容器长度」时的节点上限，防止异常大的结构把校验拖慢。
#: 真实 facts 只有几百个节点，这个上限留了两个数量级的余量。
_MAX_WALK_NODES = 20000


def _normalize_digits(text: str) -> str:
    """全角数字/标点转半角。"""
    return str(text or "").translate(_FULLWIDTH)


def _clean_token(number: str) -> str:
    """去掉千分位（逗号、空格、窄空格）后的纯数字文本。

    `1,910` / `11 534` / `11\u00a0534` 都归一成 `1910` / `11534` / `11534`，
    这样 `_text_decimals` 与有效数字位数看到的都是干净的写法。
    """
    return re.sub(r"[,\s\u00a0\u202f\u2009]", "", str(number or ""))


def _is_section_number(match: "re.Match", text: str) -> bool:
    """这个行首候选到底是不是小节号/列表序号（是的话不参与数值校验）。

    判据逐条对应实测到的写法，宁缺毋滥：
      * `#` 标题里：多段编号（`2.2`、`3.1.4`）或带列表标点（`1.`、`2)`）才算；
        后面紧跟量级后缀的（`## 20.2 万 kcal`）按测量值处理；
      * 行首加粗：只有数字被 `**` **整个包住**（`**2.2**`）才算，
        `**20.2 万 kcal**` 里数字后面还有内容，不算；
      * 行首列表编号：必须带 `.` / `、` / `)` / `）` 标点（`1. ` 才算，
        `5687 步` 不算）。
    """
    number = match.group("num")
    mark = match.group("mark") or ""
    multi_part = ("." in number) or ("．" in number)
    if not (multi_part or mark):
        return False  # 单个整数/小数（5687、58.2）不是编号
    if match.group("hashes") or match.group("bold"):
        # 标题 / 加粗小节号：后面紧跟量级后缀的是测量值，照常校验。
        if _followed_by_magnitude(match, text):
            return False
        if match.group("hashes"):
            return True
        # 行首加粗：数字必须被 `**` 整个包住才算小节号。
        return text.startswith("**", match.end())
    return bool(mark)  # 行首列表编号：1. / 2) / 3、


def _followed_by_magnitude(match: "re.Match", text: str) -> bool:
    """数字后面（允许空白）紧跟着 `万/千/亿/K/M/B` 吗。"""
    tail = text[match.end():].lstrip(" \t")
    return bool(tail) and tail[0] in _MAGNITUDE_CHARS


def _blank_section_numbers(text: str) -> str:
    """把小节号/列表序号替换成等长空格，让它们不进数值校验。

    用空格而不是删除：位置保持不变，排查时看到的偏移与原文一致。
    """
    def _replace(match: "re.Match") -> str:
        if not _is_section_number(match, text):
            return match.group(0)
        number = match.group("num")
        mark = match.group("mark") or ""
        return match.group("lead") + " " * (len(number) + len(mark))

    return _SECTION_NUMBER_RE.sub(_replace, text)


def _prepare_for_verify(text: str) -> str:
    """校验前的文本归一：全角转半角 → 抹掉小节号/列表序号 → 抹掉无数值的括号。"""
    normalized = _normalize_digits(text)
    normalized = _blank_section_numbers(normalized)
    return _BRACKET_RE.sub(
        lambda m: "" if not re.search(r"\d", m.group(0)) else m.group(0),
        normalized,
    )


def _parse_number_token(number: str, mult: str, sci: str) -> Optional[float]:
    """把匹配到的一段数字文本解析成 float；解析不了返回 None。

    注意 `number` 必须是**纯数字部分**（`1.08`），不含 `万` / `e5` 这类后缀
    —— 后缀已经拆进 `mult` / `sci` 两个参数。整段 match 直接丢给 `float()`
    会得到 None，`1.08 万` 就静默变成「文本里没有数字」了。

    千分位既可能是逗号（`1,910`）也可能是空格（`11 534`），两种都先归一掉。
    """
    body = _clean_token(number)
    if not body or body in (".", "+", "-"):
        return None
    try:
        value = float(body)
    except (TypeError, ValueError):
        return None
    if mult:
        value *= _MULTIPLIERS.get(mult, 1.0)
    if sci:
        try:
            value *= 10.0 ** int(sci[1:])
        except (TypeError, ValueError, OverflowError):
            return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _iter_number_hits(text: str) -> List[Tuple[str, float, str]]:
    """抽出 `(原样数字文本, 数值, 量级后缀)` 列表。

    比 `_iter_number_tokens` 多带回**量级后缀**（`万` / `千` / `K` / `M`…），
    因为「按有效数字舍入」那条通道只在写了量级后缀时才启用（见
    `_matches_significant_rounding`）。
    """
    normalized = _SUPERSCRIPT_RE.sub("", _normalize_digits(text))
    out: List[Tuple[str, float, str]] = []
    for match in _NUM_RE.finditer(normalized):
        # group(1) 是正则里第一段字面括号：纯数字部分（见 `_NUM_RE` 的构造）。
        number = match.group(1)
        mult = _effective_multiplier(match, normalized)
        value = _parse_number_token(number, mult, match.group("sci"))
        if value is not None:
            out.append((_clean_token(number), value, mult or ""))
    return out


def _iter_number_tokens(text: str) -> List[Tuple[str, float]]:
    """抽出 `(原样数字文本, 数值)` 列表。

    比 `_iter_numbers` 多带回原文本：`verify_numbers` 需要知道模型**写了几位小数**
    （`6.29` 两位）才能判断它是不是把 facts 里的 `6.28937...` 正常舍入了。
    原样文本保持半角、不含千分位（`1,910` / `11 534` 都会归一成 `1910` / `11534`）、
    不含乘数后缀（`1.08 万` 会返回 `("1.08", 10800.0)`）。
    """
    return [(token, value) for token, value, _mult in _iter_number_hits(text)]


def _iter_numbers(text: str) -> List[float]:
    """抽出文本里所有数字（已做全角归一、千分位归一、万/千乘开）。"""
    return [value for _token, value in _iter_number_tokens(text)]


def _facts_numbers(facts: Any) -> List[float]:
    """允许集：facts 的 JSON 文本里出现过的所有数字 + 容器长度。

    组成见 `_facts_literal_numbers`（字面数字）与 `_facts_length_numbers`
    （容器长度）。判「模型有没有真的读到 facts」时只用前者，见
    `_traceable_number_count`。
    """
    return _facts_literal_numbers(facts) + _facts_length_numbers(facts)


def _facts_literal_numbers(facts: Any) -> List[float]:
    """允许集里**字面出现**的那部分：JSON 文本里的所有数字（含键名里的数字）。

    键名也一起扫：`days_90`、`window_7d` 这种键本身就是口径的一部分，
    它出现在文本里（"近 7 天"）不该被判成编造。
    """
    try:
        payload = json.dumps(facts if facts is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        try:
            payload = json.dumps(str(facts), ensure_ascii=False)
        except (TypeError, ValueError):
            payload = "{}"
    numbers = _iter_numbers(payload)
    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", payload):
        for part in re.findall(r"\d+", match.group(0)):
            try:
                numbers.append(float(part))
            except ValueError:
                continue
    return numbers


def _facts_length_numbers(facts: Any) -> List[float]:
    """允许集里**容器长度**那部分。

    **容器长度也要放行**（实测踩到）：模型很自然地会写「本次报告共纳入 40 个指标」，
    这个 40 是它数 `metrics` 数组得到的**合法**事实，但 `40` 并不字面出现在 JSON 里
    ——只按字面扫会把这类正确输出整段丢掉。所以把 list/tuple/dict 的长度也算作可溯源。

    注意这里**只取容器的长度，不取字符串的长度**：字符串长度（比如设备名
    "Apple Watch" = 11）不是任何"事实"，把它放进允许集等于凭空多出几十个
    小整数（11、18、23……），既没有依据，也纯粹是在放大漏抓面。

    代价：若某处容器长度恰好等于一个被编造的数值，那一个数字会漏过。
    这与小整数白名单是同一类取舍，可接受——这道校验的目标是**编造的测量值**。
    """
    numbers: List[float] = []
    stack: List[Any] = [facts]
    seen = 0
    while stack and seen < _MAX_WALK_NODES:
        node = stack.pop()
        seen += 1
        if isinstance(node, (list, tuple)):
            numbers.append(float(len(node)))
            stack.extend(node)
        elif isinstance(node, dict):
            numbers.append(float(len(node)))
            stack.extend(node.values())
    return numbers


def _text_decimals(token: str) -> int:
    """文本里这个数字写了**几位小数**（`6.29` → 2，`56` → 0，`1,910.50` → 2）。"""
    body = str(token or "").split("e")[0].split("E")[0]
    if "." in body:
        return len(body.split(".", 1)[1])
    return 0


def _precision_family(values: Sequence[float]) -> "set":
    """把允许集展开成 `(按 k 位小数四舍五入后的值, k)` 的集合，k 最多到 6 位。

    这是为了**接受模型的正常舍入**，同时不接受编造：

    * 真实 facts 里是 `6.289373745308546`，模型写「均值 6.29 h」——这是把事实
      舍入到两位小数，属于正确解读，不能判成编造（实测真实数据上就会踩到）；
    * 但模型写「静息心率 58.2」而 facts 里是 `61.0` 时，`61.0` 舍入到一位小数
      仍然不等于 `58.2`，照样会被抓住。

    换句话说：**允许「把 facts 里的数按它写的位数舍入」，不允许「凭空生成一个数」**。
    比单纯放大容差精确得多——放大到 0.05 会连 58.2/58.25 这种真实差异都放过。
    """
    family = set()
    for value in values:
        if value != value or value in (float("inf"), float("-inf")):
            continue
        for digits in range(0, 7):
            try:
                family.add((round(value, digits), digits))
            except (OverflowError, ValueError):
                continue
    return family


def _nearly_equal(left: float, right: float) -> bool:
    """两个数在「相对/绝对误差 1e-6」之内算同一个数。

    容差远小于任何有意义的测量精度（0.1 级），所以它只吃掉 JSON 往返产生的
    浮点尾巴（`0.30000000000000004`），不会把 `58.2` 与 `58.25` 抹成同一个数。
    """
    return abs(left - right) <= max(_ABS_TOL, _REL_TOL * max(abs(left), abs(right)))


def _close_enough(token: str, value: float, allowed: Sequence[float], family: "set") -> bool:
    """判断文本里的 `value` 能不能溯源到 `allowed` / `family`。

    三条通道，任一命中即可：
      1. **精确**：与允许集里的某个数相等（覆盖 5687 这类大整数、
         `0.30000000000000004` 这类浮点尾巴）；
      2. **相对/绝对误差 1e-6**：容忍 JSON 往返产生的极小误差；
      3. **按位舍入**：与允许集里的某个数「按文本写的位数舍入」后相等
         （见 `_precision_family`）——这条是实测补上的，没有它，
         真实数据下模型写 6.29（真值 6.2893...）会被误判成编造、整段丢弃。

    ⚠️ 「按有效数字舍入」（`1086 万` 对 `10862753`）**不在**这里，它由
    `_matches_significant_rounding` 单独处理：那条通道只在识别出量级后缀时启用。
    """
    if value == 0.0 and 0.0 in allowed:
        return True
    index = bisect_left(allowed, value)
    for position in (index - 1, index, index + 1):
        if 0 <= position < len(allowed):
            other = allowed[position]
            if other == value:
                return True
            if _nearly_equal(value, other):
                return True
    return (value, _text_decimals(token)) in family


def _significant_digits(token: str) -> int:
    """文本里的这个数字写了几位**有效数字**。

    `1086` → 4、`20.2` → 3、`1.08` → 3、`0.75` → 2、`0.00800` → 3。
    只看文本怎么写：整数的尾随零算有效位（`1080` 是 4 位），
    因为把「1080 万」读成 1 位有效数字会平白放大放行范围。
    """
    body = str(token or "").strip().lstrip("+-").replace(".", "").lstrip("0")
    return len(body)


def _round_significant(value: float, digits: int) -> Optional[float]:
    """把 `value` 四舍五入到 `digits` 位有效数字（`10862753`、4 → `10860000.0`）。

    数量级过大/过小时返回 None（`10.0 ** 400` 会溢出），调用方按「不匹配」处理。
    """
    if value == 0.0 or digits <= 0:
        return value
    try:
        exponent = math.floor(math.log10(abs(value))) - (digits - 1)
        factor = 10.0 ** exponent
        return round(value / factor) * factor
    except (ValueError, OverflowError):
        return None


def _matches_significant_rounding(value: float, token: str, allowed: Sequence[float]) -> bool:
    """「按有效数字舍入」通道：允许模型做**保留 k 位有效数字**的量级改写。

    为什么需要：真机实测 facts 里是 `steps_total = 10862753`，模型写
    「步数 1086 万」——`10862753` 保留 4 位有效数字就是 `10860000`，与
    「1086 万」相符。这是合理的量级改写，不是编造，可原来的校验把它当编造，
    整句被删。判据：**允许集里存在某个数，按文本写的 k 位有效数字舍入后
    正好等于文本里的值**。

    仍然抓得住编造（这是这条通道没有被放宽过头的证据）：
      * facts 里是 `10862753`，「900 万」（=9000000）舍不出来——`10862753`
        按 3 位有效数字是 10900000，按 1 位是 10000000，都不等于 9000000 ✗；
      * facts 里是 `10800`，「1.08 万」（=10800）本来就精确命中；
      * facts 里**只有** `10862753` 时，文本写「1.08 万」（=10800）——这条通道
        查找的区间只有 10750~10850，够不到 10862753，所以现有那条
        「1.08 万不能对 10862753」的行为一个字都没变 ✗。

    ⚠️ 只在**已经识别出量级后缀**（万/千/亿/K/M/B）时启用：纯大数 `10860000`
    不走这条通道，免得把「随手写一个近似大数」也放进来。

    实现是「先算舍入后的目标区间再二分」而不是「对允许集逐个展开」：
    每个候选数只需 O(log n)，不会因为多了一条通道就让真机上的校验变慢。
    """
    digits = _significant_digits(token)
    if digits <= 0 or value == 0.0:
        return False
    try:
        half_unit = (10.0 ** (math.floor(math.log10(abs(value))) - (digits - 1))) / 2.0
    except (ValueError, OverflowError):
        return False
    if half_unit != half_unit or half_unit in (float("inf"), float("-inf")):
        return False
    low, high = value - half_unit, value + half_unit
    index = bisect_left(allowed, low)
    while index < len(allowed) and allowed[index] <= high:
        rounded = _round_significant(allowed[index], digits)
        if rounded is not None and _nearly_equal(rounded, value):
            return True
        index += 1
    return False


def verify_numbers(text: str, facts: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """校验 `text` 里每个数字都能在 `facts` 里找到出处。

    返回 `(是否通过, 无法溯源的数字列表)`；列表里是**原样字符串**（去掉千分位），
    方便直接打进日志或提示里。

    规则
    ----
    * 允许集 = `json.dumps(facts, ensure_ascii=False)` 里出现的所有数字，**加上**
      `_NUMBER_WHITELIST`（窗口天数 7/14/28/30/90/180/365、小节序号 1~12、
      以及 60/100/1000 这类换算常数）。
    * 格式归一：`1,910` 与 `1910` 同一个数；`11 534`（**空格**千分位）也是 11534；
      `1.44` 就是 `1.44`；`44%` 里的 `44` 照常比对；`1.08 万` / `3.2千`
      乘开成 10800 / 3200 再比；全角数字（`１.４４`）先转半角。
    * **Markdown 小节号与列表序号不算数值**：`### 2.2 步数…` 里的 `2.2`、
      `**4.1** 覆盖率`、行首的 `1. ` / `2)` 都是编号，不参与比对
      （见 `_SECTION_NUMBER_RE` 与 `_is_section_number`）。正文里的 `2.2`
      （「上升了 2.2」）与行首的裸数字（`58.2 次/分`）照常校验。
    * 数值比较用**相对误差 1e-6 或绝对误差 1e-6**，因为 facts 里可能有
      `0.30000000000000004` 这种浮点尾巴。
    * **接受模型把 facts 里的数按位舍入**：真值 `6.289373745308546` 写成
      「均值 6.29 h」算通过（这正是人写摘要的方式，实测真实数据上必然会遇到）。
      判据是「允许集里的某个数按文本写的位数舍入后是否等于它」，
      所以 `61.0` 舍入不出 `58.2`，编造的测量值照样会被抓住。
    * **接受「量级改写 + 保留 k 位有效数字」**：facts 里的 `10862753` 写成
      「1086 万」（=10860000）算通过（见 `_matches_significant_rounding`）。
      这条只在识别出量级后缀（万/千/亿/K/M/B）时启用，而且是**精确的舍入比对**，
      不是放大容差——`900 万` 对 `10862753` 仍然判为编造。
    * 中文夹数字、全角数字、上标（`kg/m²` 里的 `²`）都能处理。
    * 「千」/「万」后面紧跟单位字（`1,910 千卡`、`1500 千米`）时按**单位词**处理，
      不做乘法——否则 1910 千卡会被乘成 1910000，把正确的解读误判成编造。

    ⚠️ 明确的取舍（不假装它能抓一切）
    -----------------------------
    小整数（1~12）被白名单放行，所以「编造 3 次训练」这类**计数**可能漏过；
    同理 7/14/30/90/180/365 这些窗口天数、"第 2 点"这类序号也一律放行。
    这道校验的目标是**编造的测量值**——"你的静息心率 58.2"、"血氧 99%"、
    "体脂率 18.7" 这种具体读数，而不是"训练了 3 次"这种结构性叙述。
    放行小整数的理由：不这么做，"分 3 个小节""4 周以上""近 90 天"都会误杀，
    而误杀会把一段完全正确的解读整段丢掉。宁可漏抓少量计数，也不能天天误杀。

    还有几类**已知会漏**的情形，同样如实写在这里：
    * facts 里出现的任何数字都可以被模型拿去用在错误的指标上
      （把 A 指标的读数说成 B 指标的），本函数不校验**数字与指标是否对应**，
      那需要语义比对，不是纯文本校验能做的；
    * 大数值的容差是相对的（1e-6）：facts 里有 10862753 时，文本写 `10862760`
      （相差 7，落在相对容差 10.8 之内）也会被放过。这是「宁可放过少量大数近似、
      也不误杀」的必然代价，真正的测量值都在小数位上有明显差异；
    * `1.08 万步` 这类写法：需求要求「万」乘开（10800），而「万步」本身也是
      常见量词。冲突时按需求走，所以它不会通过 10862753 的比对。
    """
    allowed = _facts_numbers(facts) + list(_NUMBER_WHITELIST)
    normalized_allowed = sorted(set(allowed))
    family = _precision_family(normalized_allowed)
    prepared = _prepare_for_verify(text)
    bad: List[str] = []
    for token, value, mult in _iter_number_hits(prepared):
        if _close_enough(token, value, normalized_allowed, family):
            continue
        if mult and _matches_significant_rounding(value, token, normalized_allowed):
            continue
        bad.append(_format_number(value))
    return (not bad), bad


def _format_number(value: float) -> str:
    """把数字渲染回给人看的短字符串（整数不带小数点）。"""
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


# ═════════════════════════════════════════════ CLI 后端的提示词与运行


def build_cli_prompt(facts: Dict[str, Any], *, lang: str = "zh") -> str:
    """给 CLI agent 的提示词——**刻意很短，不含 facts 内容**。

    为什么 facts 不进命令行：实测真实数据的 `facts.json` 有 47 KB，把它整个塞进
    `dsh headless "<提示词>"` 必然撞上 Windows 的命令行长度上限（约 32767 字符），
    报错是 `The command line is too long.`。所以 facts 完全通过**文件**传递：
    `_cli_narrate` 会在临时工作目录里写一份 `facts.json`，提示词只让 agent 去读它。
    这样命令行长度与 facts 大小无关，几百字符到几千字符都稳。

    返回的是一段**独立可用**的提示词（内含安全约束 + 读取说明），参数 `facts`
    保留只是为了签名兼容；真正发送时 `_cli_prompt_payload()` 会在它后面追一份
    facts 副本，给「懒得读文件」的 agent 留一条退路（受长度护栏保护）。
    """
    system = _SYSTEM_PROMPT + _CLI_NOTE
    if str(lang or "zh").lower().startswith("en"):
        system = system + _EN_NOTE
    # 语言说明属于约束块，必须放在读取说明之前：约束一散开，模型对它的重视程度
    # 会明显下降（经典的长上下文「中间遗失」问题）。
    return system


def _cli_prompt_payload(facts: Dict[str, Any], *, lang: str = "zh") -> str:
    """实际发送的提示词 = 短提示词 + facts 内容。

    facts 一共给了两条路，**优先直接贴进提示词**：
      1. 贴进提示词，并用 `<<<FACTS_JSON>>>` 明确框起来（真实数据的 facts 太大时
         见下面「为什么不只留文件」的说明）；
      2. 工作目录里的 `facts.json`（`_cli_narrate` 会写）。

    为什么**不**只留文件那条路：实测 DSH 拿到「请读取当前目录下的 facts.json」
    之后，输出是「这条消息里没有附上 facts 数据，请把数据发给我」——它对
    「自己去读文件」这个自主决定并不总是执行。把数据直接放进提示词、
    并用醒目分隔符标出来，才是可靠的。

    为什么不能「永远直接贴」：真实数据的 `facts.json` 有 27~47 KB，而 Windows
    的命令行上限约 32767 字符，整段塞进 argv 会报 `The command line is too long.`
    （实测踩过）。所以超过 `_PROMPT_INLINE_LIMIT` 时只能退回文件那条路。
    """
    prompt = build_cli_prompt(facts, lang=lang)
    payload = _facts_json(facts)
    if len(prompt) + len(payload) + len(_USER_PREFIX) + len(_FACTS_FENCE) <= _PROMPT_INLINE_LIMIT:
        return (prompt + "\n\n" + _USER_PREFIX
                + _FACTS_FENCE[0] + "\n" + payload + "\n" + _FACTS_FENCE[1])
    return prompt


def _facts_json(facts: Dict[str, Any]) -> str:
    """facts → JSON 文本（失败时退化成 `{}`，绝不抛异常）。"""
    try:
        return json.dumps(facts if facts is not None else {}, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        return "{}"


def _cli_argv(kind: str, command: str, prompt: str) -> List[str]:
    """拼命令与参数（抽成独立函数，方便测试断言，不用真的启动 agent）。"""
    if kind == "dsh":
        # `dsh headless "<提示词>"` —— 帮助里写的是「answer one task, print the result, and exit」。
        # 副作用：第一次调用会在 `$DSH_HOME/profiles/` 下物化一个 headless profile
        # （实测：目录不可写时报 EPERM mkdir '...\\.dsh\\profiles\\headless'）。
        return [command, "headless", prompt]
    if kind == "claude":
        return [command, "-p", prompt, "--output-format", "json"]
    if kind == "codex":
        # 官方语法：`codex exec [OPTIONS] [PROMPT]`，题面给的是 `codex exec "<提示词>"`。
        return [command, "exec", prompt]
    return [command, prompt]


def _needs_shell(command: str) -> bool:
    """Windows 上 .cmd / .bat 必须经 shell 才能启动（CreateProcess 不认 PATHEXT）。

    这正是实测中 `dsh.cmd` 的情形。走 shell 的风险被两点限制住：
    命令与路径都由本模块给出、提示词是单个 argv 元素；而且 stdout/stderr
    都是**文件重定向**，不经过 shell 的管道。
    """
    return os.name == "nt" and command.lower().endswith((".cmd", ".bat"))


def _run_agent_to_files(argv: Sequence[str], workdir: str, timeout: float,
                        stdout_path: str, stderr_path: str) -> Tuple[int, str, str, str]:
    """执行一个 CLI agent，stdout/stderr **重定向到文件**（不用管道），返回
    `(退出码, stdout 文本, stderr 文本, 错误说明)`。

    为什么必须重定向到文件而不是 `capture_output=True`：本项目的目标运行环境
    带沙箱，命名管道可能被直接拒绝（Windows 沙箱下 `subprocess` 的默认
    `stdio="pipe"` 会 EPERM）。文件重定向没有这个问题，所以这里显式用
    `stdout=open(...)`。错误说明为空串表示「命令确实跑完了」。

    任何异常都在这里被吃掉，绝不向上抛。
    """
    shell = _needs_shell(argv[0])
    command: Any = subprocess.list2cmdline(list(argv)) if shell else list(argv)
    try:
        with open(stdout_path, "w", encoding="utf-8", errors="replace") as out_handle, \
                open(stderr_path, "w", encoding="utf-8", errors="replace") as err_handle:
            try:
                # cwd 固定为临时目录：CLI agent 会读写文件，绝不能在客户的原始数据目录里跑。
                # 环境变量整体继承（凭据就靠它），**不额外改动、尤其不碰 DSH_HOME**。
                completed = subprocess.run(command, cwd=workdir, timeout=float(timeout),
                                           shell=shell, stdin=subprocess.DEVNULL,
                                           stdout=out_handle, stderr=err_handle, check=False)
                code = int(completed.returncode or 0)
                note = ""
            except subprocess.TimeoutExpired:
                code = -1
                note = "已超过 {0:.0f} 秒仍未返回，已放弃（CLI agent 冷启动较慢，可用 timeout 调大）".format(float(timeout))
            except OSError as exc:
                code = -1
                note = "无法启动：{0}: {1}".format(exc.__class__.__name__, exc)
    except OSError as exc:
        return -1, "", "", "无法创建输出重定向文件：{0}".format(exc)
    out_text = _read_text(stdout_path)
    err_text = _read_text(stderr_path)
    if not note and code != 0:
        note = "命令退出码为 {0}".format(code)
    return code, out_text, err_text, note


def _read_text(path: str, limit: int = 400000) -> str:
    """读一个文本文件（失败返回空串）；超长只保留开头，日志/提示里够用。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


# CLI agent 的失败信息分类：客户看到的必须是「下一步该做什么」，不是一段堆栈。
# 这些判据来自本机实测（DSH 的 EPERM mkdir 与 MISSING_CREDENTIAL 两条）。
_PERMISSION_MARKERS = ("EPERM", "EACCES", "operation not permitted", "permission denied",
                       "access is denied", "不可写", "拒绝访问")
_CREDENTIAL_MARKERS = ("MISSING_CREDENTIAL", "no api key", "api key", "not logged in",
                       "unauthorized", "authentication", "login", "credentials")
_CREDENTIAL_HINTS: Dict[str, str] = {
    "dsh": "请在 DSH 里配好模型（或用它的 Models 页面填一次密钥，"
           "也可以在启动环境里设置 DEEPSEEK_API_KEY）",
    "claude": "请先在本机运行一次 `claude` 完成登录（或在环境里设置 ANTHROPIC_API_KEY）",
    "codex": "请先在本机运行一次 `codex login` 完成登录（或在环境里设置 OPENAI_API_KEY）",
}


def _classify_cli_failure(kind: str, code: int, note: str, err_text: str,
                          elapsed: float = -1.0) -> str:
    """把 CLI agent 的失败翻译成带中文可操作提示的原因。

    `elapsed` 会附在结尾：CLI agent 冷启动就要十几秒，用户需要靠耗时区分
    「等超时了」和「一启动就报错」——后者往往意味着凭据或权限问题。
    """
    name = _CLI_AGENTS.get(kind, {}).get("name", kind)
    haystack = (str(err_text or "") + "\n" + str(note or "")).lower()
    suffix = "（耗时 {0:.1f} 秒）".format(elapsed) if elapsed >= 0 else ""
    if "已超过" in note:
        return "{0} 调用超时：{1}{2}".format(name, note, suffix)
    if any(marker.lower() in haystack for marker in _CREDENTIAL_MARKERS):
        hint = _CREDENTIAL_HINTS.get(kind, "请先完成该工具的登录/密钥配置")
        return "{0} 已安装但未配置模型凭据：{1}{2}".format(name, hint, suffix)
    if any(marker.lower() in haystack for marker in _PERMISSION_MARKERS):
        if kind == "dsh":
            return ("DSH 需要在 {0} 下创建 headless profile，但该目录不可写"
                    "（首次调用会在该配置目录里物化一个 headless profile）；"
                    "请确认该目录可写，或改用其它后端。{1}".format(_dsh_home(), suffix))
        return ("{0} 运行时被拒绝访问（可能是沙箱或权限限制，它需要在自己的配置目录里写入状态）"
                "；请确认该工具的配置目录可写，或改用其它后端。{1}".format(name, suffix))
    detail = (str(err_text or "").strip() or str(note or "").strip())[:200]
    return "{0} 调用失败（{1}）{2}{3}".format(
        name,
        detail or "没有输出",
        ("；" + note) if note and note not in detail else "",
        suffix,
    )


# ─────────────────────────────────────── 识别「它在要数据 / 只给了提纲」

# 实测踩到：DSH 拿到「请读取 ./facts.json」的提示词，输出却是
# 「请把这份已经算好的结构化 facts 贴给我……我拿到之后会按下面的结构输出」。
# 这种**没有任何解读内容**的回复如果进了报告，比没有 AI 那一段更糟（用户会以为
# 那就是 AI 的解读）。所以这里把它识别出来、当成一次失败降级。
#
# 判据刻意保守（宁可漏判也不误杀真解读）：
#   * 出现「向调用方索要数据」的明确措辞，并且**全文里没有一个 facts 里的数字**；
#   * 或者全文只在一路讲「我将会怎么写」，没有一个数字。
# 「数据里没有这一项」这种 rule 2 要求的措辞**不在**触发词里——那是合法输出。
_DATA_REQUEST_MARKERS: Tuple[str, ...] = (
    "贴给我", "发给我", "请提供", "提供一下", "麻烦提供", "等你提供", "等待你提供",
    "我还没有收到", "没有收到数据", "没有看到数据", "没看到数据", "无法开始", "无法着手",
    "拿到数据之后", "拿到之后", "收到数据后", "收到之后", "请补充", "需要你提供",
    "没有收到任何", "没有收到 facts", "请把那份", "放进工作区",
    "please provide", "send me the", "waiting for the data", "i don't have access",
    "i do not have access", "no data was provided",
    # 实测：受限环境里 agent 读不到工作目录时，会写一份「权限受阻」的说明，
    # 里面全是错误码却不是解读。「完全权限」这一组**只在内容单薄时才用**——
    # 一段真正的解读里也可能出现「请把本会话切到完全权限」这种自我叙述。
    "切到「完全权限」", "切换到「完全权限」", "完全权限", "full access",
    "工作区文件权限受阻", "权限受阻", "审批通道", "审批不可用",
)

# 这些措辞**本身就足以判定**「它在要数据 / 它在报告环境受阻」。
# 刻意只留最不可能出现在正经解读里的那几条：
#   * 「贴给我 / 发给我 / 请把数据 / 等你提供」——要数据；
#   * 「没有收到任何 / 拿到数据之后 / 无法开始」——承认自己没拿到数据；
#   * 「工作区文件权限受阻 / 审批通道 / 审批不可用」——报告环境故障。
# **不含**「完全权限」：实测一段 6267 字、引用了 312 个事实的真解读里也出现了
# 「请把本会话切到完全权限」这种自我叙述，把它当铁证会误杀真解读。
_UNAMBIGUOUS_PLACEHOLDER_MARKERS: Tuple[str, ...] = (
    "贴给我", "发给我", "把数据给我", "请把数据", "等你提供", "等待你提供",
    "我还没有收到", "没有收到任何", "没有收到数据", "没有收到 facts", "无法开始", "无法着手",
    "拿到数据之后", "拿到之后", "收到数据后", "收到之后",
    "工作区文件权限受阻", "审批通道", "审批不可用",
    "please provide", "send me the", "waiting for the data", "i don't have access",
    "i do not have access", "no data was provided",
)

# 只有在「输出里一个数字都没有」时才用这些判据：真解读不可能通篇没有数字
# （facts 全是数值），所以这条与下面「提到的数字必须能在 facts 里溯源」是同一类
# 保守设计。
_OUTLINE_MARKERS: Tuple[str, ...] = (
    "我会按下面的结构", "我拿到之后", "拿到数据之后", "我将输出", "我会输出",
    "无法开始", "无法着手", "未做任何修改", "没有生成诊断报告",
)


#: 「有实质解读」的最低字数。低于这个长度、又在讲「我将会怎么写 / 请把数据给我」，
#: 就是在要数据或只给提纲，不是对一份数值 facts 的解读。
_PLACEHOLDER_MIN_CHARS = 200

#: 判定「模型到底有没有拿到 facts」的门槛。
#:
#: 一段真解读会引用**大量**具体数值（均值、最新值、变化量、数据点数、窗口天数、
#: 参考区间端点……），实测真实数据的解读里有 13 个以上可溯源数字；
#: 而「我读不到文件 / 请把数据给我」这类回复即使提到几个数字，也远低于这个数。
#: 实测那份「权限受阻」说明里只有 8 个（它引用了报告里的几处数值来说明自己读不到），
#: 所以门槛定在 6——真解读远超它，故障回复够不着。
_MIN_TRACEABLE_NUMBERS = 6


def _traceable_number_count(text: str, facts: Dict[str, Any]) -> int:
    """文本里有多少个数字**能溯源到 facts 本身**。

    这是判断「模型到底有没有拿到 facts」的最强信号：真正的解读一定会引用
    facts 里的具体数值，所以这个数通常有十几个；而「请把数据贴给我」这类
    文字里几乎一个都对不上（它里面的数字是错误码、版本号、列表序号）。

    ⚠️ 这里**故意只算字面数字**（`_facts_literal_numbers`），不算白名单、也不算容器长度：
    白名单里全是 1~12、30、90 这类小整数，容器长度也常常是 1、2、3，而
    "我拿到数据之后会写 1. 总结 2. 逐项解读"里的列表序号正好落在里面——
    把它们算进来会让这类回复看起来"引用了事实"，判定立刻失效（实测就是这么漏掉的）。
    """
    allowed = sorted(set(_facts_literal_numbers(facts)))
    if not allowed:
        return 0
    family = _precision_family(allowed)
    return sum(1 for token, value in _iter_number_tokens(str(text or ""))
               if _close_enough(token, value, allowed, family))


def _asks_for_data_instead_of_answering(text: str, facts: Dict[str, Any]) -> str:
    """判断这段文字是不是「在要数据 / 只给了提纲」，而不是真正的解读。

    返回空串表示「像正常解读」；否则返回中文原因（直接进 `drop_reason`）。

    判据（实测两轮之后定下来的）：**必须先命中明确的索要/受阻措辞**，而且
    能溯源到 facts 的数字**少于 `_MIN_TRACEABLE_NUMBERS` 个**。
    facts 全是数值，真解读一定会引用好几个；而这类失败回复里的数字都是
    错误码、列表序号、版本号（实测 `Win32 5`、`610`、`32`），几乎对不上。

    ⚠️ 为什么不用「输出里有没有数字」当判据：实测那份「工作区权限受阻」的说明
    里带着错误码，有数字但全是无关的——用「有没有数字」判会漏掉这类回复，
    而它进了报告比没有 AI 那一段更糟。

    ⚠️ 为什么还要字数上限：正常解读里可能出现「下一步请把观察窗口拉长到 4 周以上」
    这种含「请把」的句子，多一层「内容单薄」的护栏就不会误杀。
    「数据里没有这一项」是 rule 2 要求的措辞，不在触发词里。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    lowered = body.lower()
    fetchable = _traceable_number_count(body, facts)
    # 顺序很重要：**先看铁证级措辞，再看"像不像真解读"**。
    # 反过来（先要求"引用了足够多事实"再查措辞）会漏判：实测那份「权限受阻」说明
    # 很短，里面几个数字碰巧都能对上 facts，于是"看起来引用了事实"，判定失效。
    for marker in _UNAMBIGUOUS_PLACEHOLDER_MARKERS:
        if marker.lower() in lowered:
            return ("后端没有写解读，而是向调用方索要数据或报告环境受阻（出现「{0}」，"
                    "正文里引用的数字只有 {1} 个来自事实摘要）；已按失败降级，"
                    "避免把一段「请把数据发给我 / 我读不到文件」当成 AI 解读放进报告").format(
                marker, fetchable)
    # 引用了足够多的事实 → 它确实读到并解读了数据，再怎么措辞都不该判成"要数据"
    if fetchable >= _MIN_TRACEABLE_NUMBERS:
        return ""
    # 其余措辞较模糊（可能出现在正经建议或自我叙述里），只在内容单薄时才判定
    if len(re.sub(r"\s+", "", body)) >= _PLACEHOLDER_MIN_CHARS:
        return ""
    for marker in _DATA_REQUEST_MARKERS:
        if marker.lower() in lowered:
            return ("后端没有写解读，而是向调用方索要数据或报告环境受阻（出现「{0}」，"
                    "正文里引用的数字只有 {1} 个来自事实摘要）；已按失败降级，"
                    "避免把一段「请把数据发给我 / 我读不到文件」当成 AI 解读放进报告").format(
                marker, fetchable)
    for marker in _OUTLINE_MARKERS:
        if marker in body:
            return ("后端只给了写作提纲或环境说明、没有写解读（出现「{0}」，"
                    "正文里引用的数字只有 {1} 个来自事实摘要）；已按失败降级").format(
                marker, fetchable)
    return ""


# ─────────────────────────────────────────────── CLI agent 的输出清洗


_NARRATION_PREFIXES: Tuple[str, ...] = (
    "我先", "首先", "现在", "接下来", "根据要求", "让我", "我来",
    "我已", "已经读取", "读取了", "已读取", "我将", "现在读取",
)
_NARRATION_KEYWORDS: Tuple[str, ...] = (
    "facts.json", "读取", "列出", "查看文件", "运行命令", "执行命令", "工具调用",
)


def _looks_like_tool_narration(line: str) -> bool:
    """判断一行是不是「我在读文件 / 我在跑命令」这类过程叙述。

    刻意保守（**宁可留下多余的一句，也不要把正文切掉**）：
    必须以过程性词开头 **且** 提到文件/读取/命令之类的词才算，
    所以 "我目前的判断是……" 这种正文开头不会被误删。
    """
    if not line:
        return False
    if not (line.startswith(_NARRATION_PREFIXES)
            or line.startswith(("- 读取", "- 查看", "- 运行", "* 读取", "* 查看"))):
        return False
    return any(keyword in line for keyword in _NARRATION_KEYWORDS)


def _strip_trailing_process_lines(lines: List[str]) -> List[str]:
    """去掉**结尾**的过程叙述（"以上内容来自 facts.json" 之类）。"""
    while lines and _looks_like_tool_narration(lines[-1]):
        lines.pop()
    return lines


def _clean_dropped_lead(lines: List[str]) -> List[str]:
    """去掉**开头**的过程叙述，遇到第一行正文就停。

    只在「首行确实是过程叙述」时才切，而且切完要求剩余内容够长（见 `clean_cli_output`），
    所以不会把正文里偶尔提到"读取"的一句话整段丢掉。
    """
    index = 0
    while index < len(lines) and _looks_like_tool_narration(lines[index]):
        index += 1
    return lines[index:] if index else lines


def clean_cli_output(raw: str) -> str:
    """清洗 CLI agent 的原始输出：去代码围栏、去过程叙述、去掉尾巴。

    保守原则：**宁可留下多余的一句，也不要把正文切掉**。所以：

    * 如果整段输出被 ``` 围栏包着，只取围栏里的内容（agent 很爱这么干）；
      围栏外有多个块时取**最长**的那块（正文通常最长，而"我刚读了文件"的
      展示块很短）；
    * 只有「首行确实是过程叙述」时才从开头往下切，且切完内容仍然够长（≥ 40 字）；
    * 结尾若正好是过程叙述行，直接删掉（不会动中间的任何一行）。

    输入为空或没有可用的正文时返回空串。
    """
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    # 先试着解析 `claude -p --output-format json` 的结果对象。
    parsed = _extract_cli_json(text)
    if parsed:
        text = parsed
    blocks = [block.strip() for block in re.findall(r"```[^\n]*\n(.*?)(?:```|\Z)", text, re.S)
              if block.strip()]
    if blocks:
        text = max(blocks, key=len).strip()
    lines = [line.rstrip() for line in text.split("\n")]
    lines = _clean_dropped_lead(lines)
    lines = _strip_trailing_process_lines(lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def _extract_cli_json(text: str) -> str:
    """从 CLI 的 JSON 输出里取正文（解析不出来就返回空串，表示「不是 JSON」）。

    * `claude -p --output-format json` → 单个对象，正文在 `result`；
    * 有的版本/包装脚本 → 对象数组或 `stream-json` 事件流，正文在最后一条
      带 `result` 的记录里；
    * Codex 的 `--json` 事件流 → 正文在 `item.text`（type 为 agent_message）。
    """
    stripped = str(text or "").strip()
    if not stripped.startswith(("{", "[")):
        return ""
    try:
        data = json.loads(stripped)
    except (TypeError, ValueError):
        records: List[Any] = []
        for line in stripped.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    records.append(json.loads(line))
                except (TypeError, ValueError):
                    continue
    else:
        records = data if isinstance(data, list) else [data]
    last = ""
    for record in records:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("result"), str) and record["result"].strip():
            last = record["result"].strip()
        item = record.get("item")
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip():
            last = item["text"].strip()
    return last


# ══════════════════════════════════════════════ CLI 后端的一次完整调用


def _agent_workdir(kind: str) -> str:
    """给 CLI agent 建一个**它自己一定读得到**的空工作目录。

    为什么不能只用系统临时目录：实测 DSH 在受限环境里对它自己的工作目录
    (`%TEMP%\\health-report-llm-*`) **列目录与读取都失败**
    （`SetNamedSecurityInfoW failed (Win32 5)` / `ripgrep launch failed`），
    于是它写出一句「工作区被权限挡住、请把数据贴给我」就算完事——AI 那一节
    等于彻底没用。而它在自己的配置目录（`$DSH_HOME`）里读写是正常的：
    凭据、profile 都在那儿，它本来就一直在动。

    ⚠️ **顺序是「系统临时目录优先，DSH 配置目录兜底」，不是反过来。**
    早期版本对 DSH 优先用 `$DSH_HOME/tmp/`，理由是"受限环境里 agent 读不到
    系统临时目录"。那个理由在真机上不成立（同一用户身份运行，%TEMP% 读得到），
    却带来一个不可接受的代价：**只要 `$DSH_HOME/tmp` 处于异常状态，一次测试
    运行就会永久阻塞**——实测踩到过（沙箱对该目录的拦截让 mkdir 挂住而不是
    报错，整个测试套件从 1.5 秒变成永不结束）。一个可能挂死的测试套件
    比"AI 偶尔读不到数据"严重得多，所以顺序调过来。

    非 DSH 的 agent 同样用系统临时目录：claude / codex 没这个问题，
    没必要去动它们的配置目录。
    """
    try:
        return tempfile.mkdtemp(prefix="health-report-llm-")
    except OSError:
        pass

    if kind == "dsh":
        # 兜底：极少数受限环境里系统临时目录不可用，才去碰 DSH 自己的运行时目录。
        # 这是**写进用户配置目录**的副作用，但 ① 只写一个临时子目录、用完即删，
        # ② 该目录本来就是 DSH 的运行时目录（它第一次调用就在里面物化 profile）。
        try:
            home = _dsh_home()
            base = os.path.join(home, "tmp")
            os.makedirs(base, exist_ok=True)
            return tempfile.mkdtemp(prefix="health-report-llm-", dir=base)
        except OSError:
            _LOG.warning("AI 解读：DSH 配置目录下也建不了工作目录")
    raise OSError("没有可用的临时目录来放工作目录")


#: 工作目录里我们自己写的文件：找 agent 额外产物时要排除掉。
_OUR_WORKDIR_FILES = frozenset({"facts.json", "stdout.txt", "stderr.txt"})

#: agent 自己写出来的文件里，这些后缀才可能是正文（并且按这个顺序优先）。
_TEXT_SUFFIXES = (".md", ".markdown", ".txt")


def _collect_agent_files(workdir: str, limit: int = 6) -> List[str]:
    """收集工作目录里 **agent 自己写出来**的文本文件路径（相对路径，稳定排序）。

    ⚠️ **只收普通文件，不收目录，也不递归**：agent 可能在目录里放一堆东西，
    递归进去读会把无关内容甚至它自己的日志当成正文。只认顶层、带正文后缀的文件，
    并且数量设上限。

    另外用 `os.path.isfile` 挡掉符号链接（有的 agent 会放指向工作区外的链接，
    读它就等于越界）。排查问题时这里也是唯一会读 agent 产物的入口。
    """
    found: List[str] = []
    try:
        entries = sorted(os.listdir(workdir))
    except OSError:
        return found
    for name in entries:
        if name in _OUR_WORKDIR_FILES:
            continue
        path = os.path.join(workdir, name)
        if not name.lower().endswith(_TEXT_SUFFIXES):
            continue
        try:
            if os.path.islink(path) or not os.path.isfile(path):
                continue
        except OSError:
            continue
        found.append(path)
        if len(found) >= limit:
            break
    return found


def _agent_output_from_files(facts: Dict[str, Any], paths: Sequence[str]) -> str:
    """从 agent 自己写的文件里取正文（按「能溯源多少数字」打分，取最好的一份）。

    实测踩到：DSH 把说明写成 `健康数据说明.md` 放进工作目录，stdout 只剩一句
    「已完成，写成文件 xxx.md」——只读 stdout 会让 AI 那节变成一句过程叙述。

    打分是 `长度 - 每个无法溯源的数字罚 150 字`：真正的说明几乎每条数字都能溯源，
    而它顺手写的日志/清单（`acl-report-*.jsonl` 之类）对不上几个，会被压下去。
    全部都不合格时返回空串，交给调用方按失败处理（宁可不给，也不给错的）。
    """
    best_text = ""
    best_score = 0.0
    for path in paths:
        candidate = clean_cli_output(_read_text(path))
        if not candidate:
            continue
        _ok, bad = verify_numbers(candidate, facts)
        score = len(candidate) - 150 * len(bad)
        if score > best_score:
            best_score = score
            best_text = candidate
    return best_text


def _cli_narrate(kind: str, command: str, facts: Dict[str, Any], *,
                 lang: str, timeout: float) -> Tuple[str, str]:
    """在临时目录里跑一次 CLI agent，返回 `(清洗后的正文, 失败原因)`。

    安全边界（逐条对应踩过的坑）：
      1. **绝不在客户的原始数据目录里跑**：cwd 是 `tempfile.mkdtemp()` 的空目录，
         里面**只放一份 facts.json**，不放原始导出、不放报告。
      2. 提示词把 agent 的文件能力限制成「只读 facts.json 这一个文件」，
         并禁止执行命令、写代码，只输出 Markdown 正文。
      3. stdout/stderr 用**文件重定向**（不用管道，沙箱下管道可能被拒），设 cwd 与 timeout。
      4. **命令行长度自己先挡**（`_CLI_ARGV_LIMIT`）：facts 主要通过文件传递，
         命令行里只有一段几百到几千字符的提示词，绝不会撞上 Windows 的 ~32K 上限。
      5. 超时/失败一律静默降级：返回空正文 + 中文原因，绝不抛异常，也不重试
         （agent 冷启动要十几秒，重试只会让用户以为卡死）。
      6. 无论成功失败都删掉临时目录。
    """
    workdir = ""
    started = time.time()
    try:
        workdir = _agent_workdir(kind)
        with open(os.path.join(workdir, "facts.json"), "w", encoding="utf-8") as handle:
            handle.write(_facts_json(facts))
        argv = _cli_argv(kind, command, _cli_prompt_payload(facts, lang=lang))
        total = sum(len(part) + 1 for part in argv)
        if total > _CLI_ARGV_LIMIT:
            return "", ("提示词过长（命令行约 {0} 字符，护栏 {1}），已跳过命令行方式；"
                        "请改用 HTTP 端点（可指向本机模型）").format(total, _CLI_ARGV_LIMIT)
        out_path = os.path.join(workdir, "stdout.txt")
        err_path = os.path.join(workdir, "stderr.txt")
        code, out_text, err_text, note = _run_agent_to_files(
            argv, workdir, float(timeout), out_path, err_path)
        elapsed = time.time() - started
        if note:
            return "", _classify_cli_failure(kind, code, note, err_text, elapsed)
        if code != 0:
            return "", _classify_cli_failure(
                kind, code, "命令退出码为 {0}".format(code), err_text, elapsed)
        text = clean_cli_output(out_text)
        # 调试用：设了 HEALTH_REPORT_LLM_DEBUG 就把 stdout/stderr 与工作目录清单
        # 留一份，方便排查「它到底写了什么」。默认完全不落盘。
        debug_path = str(os.environ.get("HEALTH_REPORT_LLM_DEBUG") or "").strip()
        if debug_path:
            try:
                with open(debug_path, "w", encoding="utf-8") as handle:
                    handle.write("===== ls workdir =====\n")
                    handle.write(repr(sorted(os.listdir(workdir))) + "\n")
                    handle.write("===== stdout =====\n" + out_text)
                    handle.write("\n===== stderr =====\n" + err_text)
            except OSError:
                pass
        # agent 常把正文写成文件、stdout 只剩一句「已完成，见 xxx.md」（实测 DSH）。
        # 它自己写在工作目录里的 Markdown/txt 才是真正要的那份。
        extra_files = _collect_agent_files(workdir)
        from_files = _agent_output_from_files(facts, extra_files)
        if from_files:
            _LOG.info("AI 解读：从 %s 自己写的文件里取到正文（stdout 只有 %d 字节）",
                      kind, len(out_text))
            text = from_files
        if not text:
            if err_text.strip():
                return "", _classify_cli_failure(kind, code, "没有可用的正文", err_text, elapsed)
            return "", "{0} 没有输出任何内容（耗时 {1:.1f} 秒）".format(
                _CLI_AGENTS.get(kind, {}).get("name", kind), elapsed)
        return text, ""
    except Exception as exc:  # 兜底：任何意外都降级，绝不让报告生成崩掉
        _LOG.warning("AI 解读：CLI 后端调用出现意外错误，已降级（%s）", exc.__class__.__name__)
        return "", "调用 {0} 时出错：{1}（耗时 {2:.1f} 秒）".format(
            kind, exc.__class__.__name__, time.time() - started)
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════ HTTP 后端


def _extract_content(data: Any) -> str:
    """从响应体里取正文：先 choices[0].message.content，再 choices[0].text。"""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        # 少数服务把正文拆成分段数组
        if isinstance(content, list):
            pieces = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
                elif isinstance(part, str):
                    pieces.append(part)
            joined = "".join(pieces).strip()
            if joined:
                return joined
    text = first.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return ""


def _http_narrate(config: LLMConfig, *, model: str, base_url: str,
                  facts: Dict[str, Any], lang: str) -> Tuple[str, str]:
    """调用 OpenAI 兼容接口，返回 `(正文, 失败原因)`；失败一律静默降级。

    密钥永远不会进日志：错误体先过 `_redact()`。
    """
    url = _endpoint(base_url)
    try:
        body = json.dumps({
            "model": model,
            "messages": build_messages(facts, lang),
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return "", "facts 无法序列化为 JSON（{0}）".format(exc.__class__.__name__)

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = str(config.api_key or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=float(config.timeout)) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        content = _extract_content(data)
        if not content:
            return "", "接口返回体里没有可用的正文（{0}）".format(url)
        return content, ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        _LOG.warning("AI 解读：接口返回 HTTP %s，已降级为空解读（%s）%s",
                     getattr(exc, "code", "?"), url,
                     ("；" + _redact(detail, key)) if detail else "")
        return "", "接口返回 HTTP {0}（{1}）".format(getattr(exc, "code", "?"), url)
    except Exception as exc:  # 包含 URLError / TimeoutError / JSONDecodeError / KeyError 等
        _LOG.warning("AI 解读：请求失败，已降级为空解读（%s：%s）",
                     exc.__class__.__name__, _redact(str(exc), key)[:200])
        return "", "请求失败（{0}：{1}）".format(exc.__class__.__name__,
                                                _redact(str(exc), key)[:200])


# ═════════════════════════════════════════════ narrate_ex / narrate


@dataclass
class NarrateResult:
    """一次 AI 解读的结果，包含「数据发给了谁」与「为什么没有文字」。

    text        解读正文；为空表示这一段没有可用的 AI 内容
    backend     实际使用的后端（`detect_backend` 的结果），带 sends_to / is_local
    dropped     AI 确实返回了文字，但**因数字溯源校验失败被整段丢弃**
    drop_reason 放弃的原因（中文，人可读）；成功时为空
    attempted   是否真的发起过调用（未配置后端、prefer=none 时为 False）
    model       实际使用的模型名（HTTP 后端有；CLI 后端留空）
    numbers     校验发现的、无法溯源的数字列表（没触发校验时为空）
    """

    text: str = ""
    backend: Backend = field(default_factory=lambda: _NO_BACKEND)
    dropped: bool = False
    drop_reason: str = ""
    attempted: bool = False
    model: str = ""
    numbers: List[str] = field(default_factory=list)
    #: 因含无法溯源数字而被**按句删除**的句子（正文部分保留时非空）。
    #: 用途：排查"模型到底编了什么"，以及判断某个数字是真编造还是模糊改述。
    removed: List[str] = field(default_factory=list)


def narrate_ex(facts: Dict[str, Any], config: LLMConfig, *, lang: str = "zh",
               prefer: str = "auto", timeout: float = DEFAULT_CLI_TIMEOUT,
               command_override: str = "",
               allow_remote: bool = False) -> NarrateResult:
    """用自动挑选的后端生成解读，并对输出做**数字溯源硬校验**。

    流程：
      1. `detect_backend(config, prefer, command_override)` 选后端
         （`command_override` 是 CLI 的 `--ai-command`：直接给一个没进 PATH 的
         可执行文件路径）；
      2. `kind="none"` → 直接返回 `attempted=False`、空文字，不产生任何副作用；
      3. HTTP 后端走 OpenAI 兼容接口（本机模型也一样，只是地址在 127.0.0.1）；
      4. CLI 后端在 `tempfile.mkdtemp()` 的空目录里跑，里面只有一份 facts.json，
         提示词里只有一段短说明（facts 走文件，避免撞 Windows 命令行长度上限）；
      5. 拿到文字后跑 `verify_numbers()`：**只要有一个数字在 facts 里找不到出处，
         就丢弃整段 AI 文字**（`text=""`、`dropped=True`），因为报告里出现一个
         编造的数值，比没有 AI 那一段糟糕得多；
      6. 任何失败都静默降级（`text=""`），绝不抛异常、不重试，
         `drop_reason` 写清原因与耗时。

    `timeout` 只作用于 CLI 后端（HTTP 后端沿用 `config.timeout`）。
    """
    backend = detect_backend(config, prefer, command_override, allow_remote)
    result = NarrateResult(backend=backend, model=backend.model)
    if backend.kind == "none":
        result.drop_reason = backend.detail
        return result

    result.attempted = True
    if backend.kind == "cli":
        # Backend 上不存 agent 键（那是给用户看的 name），这里反查回 dsh/claude/codex。
        agent = _AGENT_BY_DISPLAY_NAME.get(backend.name, "dsh")
        text, reason = _cli_narrate(agent, backend.command, facts, lang=lang, timeout=timeout)
    else:
        cfg = config if config is not None else LLMConfig()
        text, reason = _http_narrate(cfg, model=backend.model or cfg.model,
                                     base_url=backend.base_url or cfg.base_url,
                                     facts=facts, lang=lang)

    if not text:
        result.drop_reason = reason or "后端没有返回可用的文字"
        return result

    # 先排除「它在要数据 / 只给了提纲」——这种回复必须降级，绝不能进报告
    placeholder = _asks_for_data_instead_of_answering(text, facts)
    if placeholder:
        result.drop_reason = placeholder
        _LOG.warning("AI 解读：后端没有写解读，已降级（%s）", placeholder)
        return result

    ok, bad_numbers = verify_numbers(text, facts)
    if not ok:
        # ── 按句子丢弃，而不是一票否决整段 ──────────────────────
        #
        # 为什么改：真机实测（装了 DSH 的机器上跑真实导出）AI 一次输出里
        # 有 4 个数字对不上（2800000、2.3、2.3、2000），其中 2800000 明显是
        # 编造的记录条数，但**整段被丢掉**——客户因此什么都拿不到。
        # 一票否决太浪费：一句编造不该让另外几十句正确的解读一起作废。
        #
        # 现在的做法：把含有无法溯源数字的**整句**删掉，其余保留，
        # 并在结果里说明删了几句、删的是哪几句（供排查）。
        # 若删完剩下的内容太单薄，就仍然整段丢弃——一段残缺的解读
        # 比没有更糟（用户会以为那就是全部）。
        kept, removed = _drop_offending_sentences(text, facts)
        if len(kept.strip()) >= _MIN_KEPT_CHARS:
            result.text = kept
            result.numbers = bad_numbers
            result.drop_reason = (
                "AI 输出里有 {0} 个数字在 facts 里找不到出处（{1}），"
                "已删除含这些数字的 {2} 处句子，其余内容保留").format(
                len(bad_numbers), "、".join(bad_numbers[:8]), len(removed))
            result.removed = removed
            _LOG.warning("AI 解读：删除了 %d 处含无法溯源数字的句子（%s）",
                         len(removed), "、".join(bad_numbers[:8]))
            return result

        result.dropped = True
        result.text = ""
        result.numbers = bad_numbers
        result.removed = removed
        result.drop_reason = ("AI 输出里有 {0} 个数字在 facts 里找不到出处（{1}），"
                              "删掉这些句子后所剩内容过少，已整段丢弃以避免编造数值").format(
            len(bad_numbers), "、".join(bad_numbers[:8]))
        _LOG.warning("AI 解读：数字溯源校验未通过，已丢弃整段输出（%s）", result.drop_reason)
        return result
    result.text = text
    return result


#: 按句删除后，剩下的正文至少要有这么长才值得保留（否则整段丢弃）。
#:
#: 取 120 字符的依据：这个阈值要挡的是**碎片**——只剩一个小节标题加半句话，
#: 读者会以为那就是全部解读。实测标定：一个标题 + 3 句实质中文正文约 190 字符，
#: 那是可用的内容；原来定 200 会把它误判成碎片（实测差 7 个字符被丢掉）。
#: 而在 120 以下，剩下的通常已经不成段落。
_MIN_KEPT_CHARS = 120

#: 句子切分：中英文句末标点 + 换行。**两个分支都是零宽断言**，所以标点与换行
#: 本身都留在前一个片段里，`"".join(...)` 之后原文的换行结构不会被破坏。
#:
#: ⚠️ 换行必须写成 `(?<=\n)` 而不是 `| \n`：后者会把换行符**当作分隔符吃掉**，
#: 于是保留下来的正文全部挤成一行（实测踩到：
#: 「## 睡眠与恢复本次报告共纳入 29 个指标…### 该看什么第一，…」）。
#:
#: 刻意不在逗号处切——中文长句里逗号很密，按逗号切会把一句话拆成残片。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])|(?<=\n)")


def _drop_offending_sentences(text: str, facts: Dict[str, Any]
                              ) -> Tuple[str, List[str]]:
    """删掉含「无法溯源数字」的整句，返回 (保留的正文, 被删的句子列表)。

    为什么按**整句**而不是按行：模型经常用一行写很长一段，按行删会连同
    大量正确内容一起丢掉；而按句删的粒度与"一个错误说法"最接近。
    """
    kept: List[str] = []
    removed: List[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if not sentence:
            continue
        if not sentence.strip():
            kept.append(sentence)
            continue
        ok, _bad = verify_numbers(sentence, facts)
        if ok:
            kept.append(sentence)
        else:
            removed.append(sentence.strip())

    body = "".join(kept)
    # 折叠因删除产生的连续空行（最多留一个空行）
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, removed


def narrate(facts: Dict[str, Any], config: LLMConfig, *, lang: str = "zh") -> str:
    """调用 AI 生成解读，返回正文；失败返回空字符串。

    这是 `narrate_ex` 的薄包装：签名与行为保持不变（`report.py` / `cli.py` 在用）。
    默认 `prefer="auto"`：先看环境变量配置的端点，再看本机 HTTP 模型，
    最后看本机已装的 AI 工具（DSH / Claude Code / Codex）。

    注意：为了不改变老行为，`config.available()` 为假时本函数**不做任何探测**，
    直接返回空字符串（老行为就是「没配 key 就什么都不做」）。

    任何失败（未配置、网络不通、超时、HTTP 错误、返回体不是预期结构、
    数字溯源校验不通过）都返回空字符串，**绝不抛异常**；
    日志里只留一条不含密钥的警告。
    """
    if config is None or not config.available():
        return ""
    return narrate_ex(facts, config, lang=lang).text
