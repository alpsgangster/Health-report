"""命令行入口。

设计取向：默认**完全离线**。只有显式开启（或环境里有 key 且未加 `--no-llm`）
才会联网，且只把「已经算好的结构化事实」发出去，不发原始数据。

    health-report demo                      # 30 秒看到完整报告长什么样，零配置
    health-report report export.zip -o r.md # 解析并生成报告
    health-report report a.csv b.xml --llm  # 显式开启 AI 解读
    health-report parse export.zip -o out/  # 只做解析，产出结构化文件
    health-report metrics                   # 看看支持哪些指标
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, catalog
from .analysis import analyze
from .llm import LLMConfig, detect_backend, narrate_ex
from .model import Dataset
from .parsers import parse_path
from .pitfalls import check
from .report import build_facts, build_report
from .html import build_html

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_LLM_FAILED = 3

_CSV_COLUMNS = ["指标ID", "指标名称", "日期", "数值", "单位", "来源", "时间戳", "取值方式", "参考区间"]


# ──────────────────────────────────────────────────────────── 公共


def _force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _write_text(path: str, text: str, bom: bool = False) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as f:
        f.write(text)
    return path


def _load_dataset(paths: Sequence[str], args: Optional[argparse.Namespace] = None) -> Dataset:
    """解析并合并多个输入。任何一个输入出错都直接向上抛，让用户看到原因。

    `args` 里的三个开关都转给解析器：
      * `--source-policy`   多来源指标的取值口径（默认 primary，见 parsers/apple.py）
      * `--prefer-source`   指定主设备；可重复
      * `--show-source-names` 关闭设备名脱敏（仅本地调试用）
    """
    policy = getattr(args, "source_policy", None) or "primary"
    prefer = tuple(getattr(args, "prefer_source", None) or ())
    mask = not bool(getattr(args, "show_source_names", False))

    ds = Dataset()
    for p in paths:
        if not os.path.exists(p):
            raise FileNotFoundError(f"找不到输入：{p}")
        part = parse_path(p, source_policy=policy, prefer_sources=prefer,
                          mask_source_names=mask)
        ds.merge(part)
    if not paths:
        raise ValueError("至少要给一个输入文件（或用 `health-report demo`）")
    ds.dedupe()
    return ds


def _emit(text: str, out: Optional[str], bom: bool = False) -> None:
    if out:
        _write_text(out, text, bom=bom)
        print(f"[√] 已写入 {os.path.abspath(out)}  ({len(text) / 1024:.1f} KB)")
    else:
        print(text)


# ──────────────────────────────────────────────────────────── 子命令


def cmd_report(args: argparse.Namespace) -> int:
    ds = _load_dataset(args.inputs, args)
    analysis = analyze(ds)
    findings = check(ds, analysis)

    # ── AI 解读 ────────────────────────────────────────────
    #
    # 后端按「环境变量配置的端点 → 本机 HTTP 模型（Ollama 等）→ 本机已装的
    # AI 工具（dsh / claude / codex）→ 无」的顺序自动探测。
    # **数据会发给谁，必须显式打出来**——"本地装的工具"不等于"模型在本地"。
    ai_text, ai_model, ai_note = "", "", ""
    cfg = LLMConfig.from_env()
    prefer = getattr(args, "ai_backend", None) or "auto"
    command_override = getattr(args, "ai_command", None) or ""
    if args.no_llm:
        if args.llm:
            print("[!] 同时给了 --llm 和 --no-llm，以 --no-llm 为准。", file=sys.stderr)
        prefer = "none"

    facts = build_facts(ds, analysis, findings)

    if prefer != "none":
        try:
            backend = detect_backend(cfg, prefer, command_override,
                                     allow_remote=bool(args.llm))
        except ValueError as e:
            print(f"[×] {e}", file=sys.stderr)
            return EXIT_USAGE

        if backend.kind == "none":
            ai_note = ("没有检测到可用的 AI 后端（已探测：环境变量配置的端点、"
                       "本机模型 127.0.0.1、本机已装的 AI 工具）。报告的第一到第五节"
                       "全部由本地确定性计算得出，不受影响。")
            if args.llm:
                print("[×] 你要求了 AI 解读，但没有找到可用的后端。\n"
                      "    可选做法：设置 HEALTH_REPORT_API_KEY（云端 API）、"
                      "启动本机模型（如 Ollama，监听 127.0.0.1:11434）、"
                      "或确认已装好 dsh / claude / codex 并登录。\n"
                      "    不加 --llm 也能得到完整报告——AI 解读只是可选的一节。",
                      file=sys.stderr)
                return EXIT_USAGE
            print("[i] 未检测到可用的 AI 后端，生成纯规则报告。", file=sys.stderr)
        else:
            # 用户显式给了 --ai-command，就必须用它。**不能悄悄换成别的后端**：
            # 路径认不出是哪个 agent 时，自动探测会退回去挑本机装好的工具
            # （例如真 DSH），那就违背了"我指定了这个命令"的意图。
            if command_override and backend.command != command_override:
                print(f"[×] --ai-command 指定的「{command_override}」没被采用"
                      f"（实际选中：{backend.name or '无'}）。\n"
                      f"    可执行文件名需要能认出是哪个工具（dsh / claude / codex），"
                      f"或者用 --ai-backend 显式指明其中之一。",
                      file=sys.stderr)
                return EXIT_USAGE
            where = backend.sends_to or "未知"
            print(f"[i] 用「{backend.name}」生成解读中……（{backend.detail}）", file=sys.stderr)
            print(f"[i] 数据将发送至：{where}", file=sys.stderr)
            res = narrate_ex(facts, cfg, prefer=prefer, command_override=command_override,
                             allow_remote=bool(args.llm))
            ai_text = res.text or ""
            if ai_text:
                ai_model = backend.name + (f" / {res.model}" if res.model else "")
                if res.removed:
                    # 部分删除：正文还在，但有几句话因含无法溯源的数字被删掉了。
                    # 这件事必须在报告里说明——读者有权知道 AI 的话被编辑过。
                    ai_note = (f"AI 解读里有 {len(res.removed)} 处句子因含无法溯源的数字"
                               f"被删除，其余内容保留。")
                    print(f"[!] 已删除 {len(res.removed)} 处含无法溯源数字的句子：",
                          file=sys.stderr)
                    for line in res.removed[:8]:
                        print(f"      · {line[:160]}", file=sys.stderr)
            else:
                if res.dropped:
                    # ⚠️ 刻意**不把编造的数字写进报告**。说明里只报"有几个数字无法溯源"，
                    # 具体是哪几个只打到 stderr。理由：报告的承诺是"不出现编造数值"，
                    # 如果把 58.2 抄进说明里，一个粗心的读者仍然会在报告里看到它。
                    cnt = len(res.numbers or [])
                    ai_note = (f"⚠️ AI 返回的文字里有 {cnt} 个数字无法在事实摘要里溯源，"
                               f"已**整段丢弃**，改用规则层原文。这是刻意的硬校验："
                               f"报告里宁可没有 AI 那一段，也不能出现一个编造的数值。")
                    print(f"[!] 被判定为无法溯源的数字：{'、'.join(str(x) for x in (res.numbers or [])[:8])}",
                          file=sys.stderr)
                else:
                    ai_note = res.drop_reason or "AI 后端没有返回可用文字。"
                print(f"[!] AI 解读未产出文字，已降级为纯规则报告。原因：{ai_note}",
                      file=sys.stderr)
                if args.llm:
                    return EXIT_LLM_FAILED

    if args.json:
        _write_text(args.json, json.dumps(facts, ensure_ascii=False, indent=2))
        print(f"[√] 结构化事实已写入 {os.path.abspath(args.json)}")

    md = build_report(ds, analysis, findings, ai_text=ai_text, ai_model=ai_model,
                      title=args.title, include_appendix=not args.no_appendix,
                      ai_note=ai_note)

    # ── HTML 交付形态（打印即 PDF）──────────────────────────
    #
    # 这是给客户的形态：自包含、内联 SVG 图表、A4 分页、零外部资源、零 JS。
    # 与 Markdown 版共用同一套口径助手（判定措辞、区间标注、来源列），
    # 所以两份产物说的是同一句话。
    if args.html:
        html_text = build_html(
            ds, analysis, findings,
            ai_text=ai_text, ai_model=ai_model, ai_note=ai_note,
            title=args.title, include_appendix=not args.no_appendix,
            # 解析器默认已脱敏；这里再保一道，且跟随 --show-source-names
            mask_sources=not bool(getattr(args, "show_source_names", False)),
        )
        _write_text(args.html, html_text)
        svg = html_text.count("<svg")
        print(f"[√] HTML 报告已写入 {os.path.abspath(args.html)}  "
              f"({len(html_text) / 1024:.0f} KB / {svg} 张图)")

    if args.out or not args.html:
        _emit(md, args.out)
    else:
        # 给了 --html 又没给 -o：说明要的就是 HTML 交付物，
        # 把 20 多 KB 的 Markdown 倒到终端没有意义。要两份就显式加 -o。
        print("[i] 未写 Markdown（要同时要 Markdown 请加 -o 报告.md）")
    return EXIT_OK


def cmd_parse(args: argparse.Namespace) -> int:
    ds = _load_dataset(args.inputs, args)
    outdir = args.out or "health_out"
    os.makedirs(outdir, exist_ok=True)

    # observations.csv
    spec_of = {s.id: s for s in catalog.SPECS}
    rows: List[Dict[str, Any]] = []
    for o in sorted(ds.observations, key=lambda x: (x.metric, x.sort_key)):
        s = spec_of.get(o.metric)
        rows.append({
            "指标ID": o.metric,
            "指标名称": s.cn if s else o.metric,
            "日期": o.date,
            "数值": o.value,
            "单位": o.unit,
            "来源": o.source,
            "时间戳": o.ts,
            "取值方式": o.kind,
            "参考区间": o.ref.label() if o.ref else "",
        })
    _write_csv(os.path.join(outdir, "observations.csv"), _CSV_COLUMNS, rows)

    # workouts.csv
    wcols = ["日期", "运动类型", "时长_分钟", "距离_km", "平均心率", "最大心率",
             "热量_kcal", "配速_秒每km", "来源"]
    wrows = [{
        "日期": w.date, "运动类型": w.activity, "时长_分钟": w.duration_min,
        "距离_km": w.distance_km, "平均心率": w.avg_hr, "最大心率": w.max_hr,
        "热量_kcal": w.energy_kcal, "配速_秒每km": w.pace_sec_per_km, "来源": w.source,
    } for w in sorted(ds.workouts, key=lambda w: (w.date, w.ts))]
    _write_csv(os.path.join(outdir, "workouts.csv"), wcols, wrows)

    # dataset.json（可被 Dataset.from_dict 读回）
    _write_text(os.path.join(outdir, "dataset.json"),
                json.dumps(ds.to_dict(), ensure_ascii=False, indent=1))

    # summary.txt
    # summary.txt 是给人看的（而且是中文），必须带 BOM。
    # 中文 Windows 上记事本 / PowerShell 的 Get-Content 对无 BOM 的 UTF-8
    # 默认按 GBK 解码，打开就是乱码。
    _write_text(os.path.join(outdir, "summary.txt"), _summary_text(ds), bom=True)

    print(f"[√] 已输出到 {os.path.abspath(outdir)}")
    for fn in ("observations.csv", "workouts.csv", "dataset.json", "summary.txt"):
        p = os.path.join(outdir, fn)
        if os.path.exists(p):
            print(f"    - {fn}  ({os.path.getsize(p) / 1024:.1f} KB)")
    return EXIT_OK


def _write_csv(path: str, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    # Excel 在中文 Windows 上按 GBK 打开无 BOM 的 UTF-8 CSV 会乱码
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _summary_text(ds: Dataset) -> str:
    span = ds.date_span()
    lines = ["health-report 解析结果", "=" * 46]
    lines.append(f"数据来源文件：{', '.join(ds.files) if ds.files else '—'}")
    lines.append(f"数据跨度：{span[0]} → {span[1]}" if span else "数据跨度：—")
    lines.append(f"观测点数：{len(ds.observations)}")
    lines.append(f"训练记录：{len(ds.workouts)}")
    lines.append(f"识别指标数：{len(ds.metric_ids())}")
    lines.append("")
    lines.append("用户信息（来自输入）：")
    for k in ("height_cm", "age", "sex", "birth_date"):
        if ds.profile.get(k) is not None:
            lines.append(f"  {k} = {ds.profile[k]}")
    if not ds.profile:
        lines.append("  —")
    lines.append("")
    lines.append("各指标覆盖情况（指标 / 数据点 / 覆盖天数 / 来源）：")
    for mid in ds.metric_ids():
        s = catalog.get(mid)
        lines.append(
            f"  {s.cn if s else mid:<16} {len(ds.series(mid)):>5} 点 "
            f"{ds.days_covered(mid):>4} 天   {' / '.join(ds.sources_of(mid))}"
        )
    if ds.labels:
        lines.append("")
        lines.append("输入自带的文字判定：")
        for k, v in ds.labels.items():
            lines.append(f"  {k} = {v}")
    if ds.notes:
        lines.append("")
        lines.append("解析提示：")
        for n in ds.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines) + "\n"


def cmd_demo(args: argparse.Namespace) -> int:
    from .samples import synthetic_dataset

    ds = synthetic_dataset()
    analysis = analyze(ds)
    findings = check(ds, analysis)
    md = build_report(ds, analysis, findings,
                      ai_text="", ai_model="",
                      title="健康数据报告（内置合成样例）",
                      include_appendix=not args.no_appendix)
    out = args.out or os.path.join("out", "demo_report.md")
    _write_text(out, md)
    print(f"[√] 合成样例报告已写入 {os.path.abspath(out)}")
    print("    这份数据是编的，用来演示报告结构与数据陷阱提示。")
    print("    换成你自己的数据：health-report report <你的导出文件> -o 报告.md")
    print("    想看全部规则命中情况：health-report demo --findings")

    # ── `demo --html`：给「30 秒看到长什么样」补上客户交付形态那一步 ──
    #
    # 之前只有 `report` 子命令有 `--html`。于是"想看看 HTML 报告长什么样"必须
    # 先去仓库里找一份合成导出、再跑 `report examples/...`——而 `demo` 存在的
    # 全部意义就是省掉这一步。**最该看到交付形态的入口反而看不到它**，
    # 这是实现时按"子命令"分类、而不是按"用户想干什么"分类留下的缺口。
    if args.html:
        html_text = build_html(ds, analysis, findings,
                               ai_text="", ai_model="", ai_note="",
                               title="健康数据报告（内置合成样例）",
                               include_appendix=not args.no_appendix)
        _write_text(args.html, html_text)
        print(f"[√] HTML 版已写入 {os.path.abspath(args.html)}  "
              f"({len(html_text) / 1024:.0f} KB / {html_text.count('<svg')} 张图)")
        print("    用浏览器打开，Ctrl+P 即可另存为 PDF（打印时勾选「背景图形」）。")

    if args.findings:
        print("")
        print(f"{'严重度':<8}{'规则':<26}说明")
        print("-" * 100)
        for f in findings:
            print(f"{f.severity:<8}{f.code:<26}{f.title}")
    return EXIT_OK


def cmd_metrics(args: argparse.Namespace) -> int:
    print(f"health-report 支持 {len(catalog.SPECS)} 个指标。\n")
    for cat in catalog.CATEGORY_ORDER:
        specs = [s for s in catalog.SPECS if s.category == cat]
        if not specs:
            continue
        print(f"── {cat} ── {catalog.CATEGORY_HINT.get(cat, '')}")
        print(f"   {'ID':<26}{'名称':<20}{'单位':<10}{'聚合':<10}{'参考区间'}")
        for s in specs:
            ref = catalog.builtin_ref(s.id, {})
            # 说「无内置区间」而不是「只做趋势」：体重、骨骼肌这些指标并非永远无法判定，
            # 体成分报告自带的参考范围优先级更高。写成「只做趋势」会让用户以为这些
            # 指标根本没有参考范围，从而忽略自己报告上印着的区间。
            ref_txt = f"{ref.label()}（{ref.kind}）" if ref else "—（无内置区间）"
            if s.ref_fn:
                ref_txt += " *需年龄/性别"
            print(f"   {s.id:<26}{s.cn:<20}{s.unit:<10}{s.agg:<10}{ref_txt}")
        print("")
    print("说明：参考区间列给的是「内置人群参考表」。输入自带的区间（例如 InBody 报告上")
    print("      印的「报告参考范围」）优先级更高；两者都没有时，报告才写「只做趋势」，")
    print("      不会硬编一个「正常」出来。")
    print("      verdict=False 的指标（生物电阻抗、设备建议与评分等）永远不参与判定。")
    return EXIT_OK


def cmd_inputs(args: argparse.Namespace) -> int:
    print("""支持的输入
==========
1) Apple 健康导出
   - export.zip（iPhone 上「导出所有健康数据」得到的压缩包）
   - export.xml / 导出.xml
   - apple_health_export 目录
   自动流式解析，几 GB 也没问题（装了 lxml 会快很多）。

2) InBody / 体成分仪报告（长格式 CSV）
   表头形如：分类,指标,单位,2026-01-10 07:30,2026-05-20 07:30,变化量,报告参考范围,备注
   日期列的数量不限（1~5 次测量都行）。
   「报告参考范围」列会被解析成该指标的参考区间，优先级高于内置人群参考表。

3) 通用长格式 CSV（任何设备的万能入口）
   date,metric,value,unit,source
   2026-05-01,body_mass,74.0,kg,智能秤
   metric 支持中文名（体重 / 体脂率 / 静息心率 …）或英文 ID，用 `health-report metrics` 查。

用法示例
========
  health-report demo
  health-report report export.zip -o 报告.md
  health-report report inbody.csv apple.xml -o 报告.md     # 多个来源会合并分析
  health-report report export.zip --llm                    # 显式开启 AI 解读
  health-report report export.zip --json facts.json        # 另存结构化事实
  health-report parse export.zip -o health_out/
  health-report metrics

隐私
====
默认完全离线。只有加了 --llm（或环境里有 key 且没加 --no-llm）才会联网，
且只把「已经算好的结构化摘要」发给你自己配置的模型端点，不发原始数据。
详见 docs/PRIVACY.md。""")
    return EXIT_OK


def cmd_sample_data(args: argparse.Namespace) -> int:
    from .samples import write_all

    outdir = args.out or "examples"
    paths = write_all(outdir)
    for p in paths:
        print(f"[√] {os.path.abspath(p)}  ({os.path.getsize(p) / 1024:.1f} KB)")
    print("\n这些是**合成**数据，不含任何真实个人数据，可以随便提交到仓库。")
    return EXIT_OK


# ──────────────────────────────────────────────────────────── 解析参数


def _add_source_options(p: argparse.ArgumentParser) -> None:
    """多来源口径与设备名脱敏的开关（report / parse 共用）。"""
    p.add_argument(
        "--source-policy", choices=("primary", "per-day-max", "sum"), default="primary",
        help="同一天多台设备都有记录时的取值口径。primary（默认）：按主设备优先，"
             "主设备当天没数据就回退到别的设备；per-day-max：每天取各来源最大值；"
             "sum：无条件累加（**会算高**，实测某份真实数据被放大 1.38 倍，最高一天 2 倍）")
    p.add_argument(
        "--prefer-source", action="append", default=None, metavar="NAME",
        help="指定主设备（可重复，按顺序优先）。匹配是宽松的：写 Apple Watch "
             "能匹配到「某某的Apple Watch」这类带姓名的设备名")
    p.add_argument(
        "--show-source-names", action="store_true",
        help="不做设备名脱敏。默认会隐去 Apple 默认命名里的机主姓名"
             "（「某某的Apple Watch」-> 「Apple Watch」），因为这个报告是要拿去分享的")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="health-report",
        description="只靠你自己的 Apple 健康导出，在本机生成一份能逐条复核的健康报告。"
                    "不联网、不上传、不需要 API key。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不知道该从哪开始？先跑 `health-report demo`。"
               "\nWindows 上不想记 PYTHONPATH？直接跑仓库根目录的 health-report.cmd。",
    )
    ap.add_argument("--version", action="version", version=f"health-report {__version__}")
    sub = ap.add_subparsers(dest="command")

    p = sub.add_parser("report", help="解析输入并生成 Markdown 报告")
    p.add_argument("inputs", nargs="+",
                   help="输入文件：Apple 导出（export.zip / export.xml / apple_health_export/）"
                        "是唯一必需的；体成分 CSV / 通用 CSV 可选")
    p.add_argument("-o", "--out", default=None, help="输出 Markdown 路径（默认打印到终端）")
    p.add_argument("--html", default=None, metavar="PATH",
                   help="另存一份自包含 HTML 报告（内联 SVG 图表，浏览器里打印即 PDF）")
    p.add_argument("--json", default=None, help="另存结构化事实 JSON（就是发给模型的那份内容）")
    p.add_argument("--llm", action="store_true", help="强制开启 AI 解读（没有可用后端就报错退出）")
    p.add_argument("--no-llm", action="store_true", help="强制关闭 AI 解读，完全不联网")
    p.add_argument("--ai-backend", choices=("auto", "none", "http", "cli", "dsh", "claude", "codex"),
                   default="auto",
                   help="AI 解读用哪个后端。auto（默认）按「云端端点 → 本机模型 → 本机已装的 "
                        "AI 工具」探测；none 只要纯规则报告；其余为强制指定")
    p.add_argument("--ai-command", default=None, metavar="PATH",
                   help="直接指定 AI 工具的可执行文件路径（本机工具没进 PATH 时用）")
    p.add_argument("--title", default="健康数据报告", help="报告标题")
    p.add_argument("--no-appendix", action="store_true", help="不输出附录 B（生物电阻抗等明细）")
    _add_source_options(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("parse", help="只做解析，产出结构化文件（CSV / JSON）")
    p.add_argument("inputs", nargs="+", help="同 report")
    p.add_argument("-o", "--out", default="health_out", help="输出目录（默认 health_out/）")
    _add_source_options(p)
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("demo", help="用内置合成数据生成一份示例报告（零配置）")
    p.add_argument("-o", "--out", default=None, help="输出路径（默认 out/demo_report.md）")
    p.add_argument("--html", default=None, metavar="PATH",
                   help="另存一份自包含 HTML（内联 SVG 图表，浏览器里打印即 PDF）")
    p.add_argument("--findings", action="store_true", help="额外打印全部命中的规则")
    p.add_argument("--no-appendix", action="store_true", help="不输出附录 B")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("metrics", help="列出支持的指标与内置参考区间")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("inputs", help="说明支持哪些输入格式")
    p.set_defaults(func=cmd_inputs)

    p = sub.add_parser("sample-data", help="生成合成示例文件（可用于测试解析器）")
    p.add_argument("-o", "--out", default="examples", help="输出目录（默认 examples/）")
    p.set_defaults(func=cmd_sample_data)

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "command", None):
        ap.print_help()
        return EXIT_USAGE
    try:
        return int(args.func(args))
    except FileNotFoundError as e:
        print(f"[×] {e}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as e:
        print(f"[×] 输入有问题：{e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\n[!] 已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
