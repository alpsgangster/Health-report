# 发布检查清单

给维护者（也就是你）自己看的。**这个文件不面向用户，但留在仓库里无妨**——
它顺便告诉潜在贡献者这个项目对隐私是认真的。

---

## 首次推送到 GitHub 之前

### 1. 把占位符换掉

全仓库搜 `CHANGE_ME`，替换成你的 GitHub 用户名：

```powershell
Select-String -Path (Get-ChildItem -Recurse -File -Include *.md,*.toml,*.yml).FullName -Pattern CHANGE_ME
```

出现在这些地方：

| 文件 | 位置 |
|---|---|
| `README.md` | CI 徽章链接、安装命令、克隆地址 |
| `README.en.md` | 同上 |
| `pyproject.toml` | `[project.urls]` 的 Homepage 与 Issues |

顺便确认 `pyproject.toml` 里的 `description`、`keywords` 和你的仓库描述一致。

### 2. 确认 `tools/` 下的自测脚本里没有真实个人数值

三个自检脚本（`_check_analysis.py` / `_check_report.py` / `_check_parsers.py`）**已经脱敏**：
例子用的都是 `samples.py` 里那套合成数值，`_check_parsers.py` 更是直接从样例文件读期望值、
个人数值只存在于运行时内存。你的真实数值只作为**设计动机**留在 `docs/PITFALLS.md`——
那里用的是合成值。

**但这只是当前状态，不是永久保证。** 你以后拿真实数据调试时很容易顺手把数值写进脚本，
所以推送前跑一遍这条命令：

```powershell
# 把下面这串换成你自己真实数据里的关键词（体重、身高、体脂、日期、设备序列号…）
# 这里故意只放占位符 —— 用真实数值当搜索模式，等于把它们又发布了一遍。
$mine = "身高数值", "体重数值", "体测日期", "设备序列号"
Get-ChildItem -Recurse -File -Include *.py,*.md,*.csv,*.xml |
  Where-Object { $_.FullName -notmatch '\\out\\|\\__pycache__\\' } |
  Select-String -Pattern $mine
```

命中的每一行都要看清楚：是**格式示例里的巧合数字**，还是**真的从你报告里抄下来的**。
后者一律换成合成值。

> 注意：**不要**把这份清单本身写进仓库（也别写进这个文件）。
> 上面用占位符示意就是出于这个原因：一旦写成真实数值，
> 这份本该保护隐私的清单自己就变成了泄漏点。

### 3. 确认没有真实数据被 `git add` 过

```powershell
git status --porcelain
git ls-files | Select-String -Pattern '\.(zip|gpx|png|jpg)$|export\.xml|InBody'
```

预期：`examples/` 下只有 `inbody_sample.csv`、`apple_export_sample.xml`、
`expected_report.md` 三个合成文件。CI 里的 `隐私守卫` job 也会挡这一层，
但**推送前自己看一眼比 CI 报错早**。

### 4. 生成示例文件与预期报告

```powershell
$env:PYTHONPATH="src"
python tools/make_sample_data.py                       # 写 examples/*.csv|xml
python -m health_report demo -o examples/expected_report.md
```

`examples/expected_report.md` 是别人判断"这东西对我有没有用"的最快途径，
**必须进仓库**，而且改动了 `samples.py` 或 `report.py` 之后要重新生成
（CI 会跑 `demo` 但不会比对这份文件，所以这一步靠自觉）。

### 5. 跑一遍完整测试

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests -v
python -m health_report demo --findings
```

### 6. 建仓库时的建议

- **描述**（Description）：`Turn Apple Health exports and InBody reports into an auditable health report. Local-first, BYO API key.`
- **Topics**：`apple-health`、`inbody`、`health-data`、`self-tracking`、`local-first`、
  `quantified-self`、`body-composition`、`python`、`privacy`
- 第一次 push 后再去看 CI 是否绿。红了先修 CI——**一个红徽章比没有徽章更劝退。**
- Release 先发 `v0.1.0`，把 `demo` 的输出截图贴上。

---

## 每个版本发布前

- [ ] `python -m unittest discover -s tests` 全绿
- [ ] `health-report demo` 能跑（CI 会验，但要本地确认一次输出好看）
- [ ] 新增的规则同步写进了 `docs/PITFALLS.md`
- [ ] 新增的指标区间的 `ref_source` 写了出处（`tests/test_catalog.py` 会强制）
- [ ] `README.md` 与 `README.en.md` 的内容没有分叉
- [ ] 没有把 `examples/expected_report.md` 忘在旧版本

---

## 关于这个文件本身

如果你想在正式发布时删掉它，删掉本章节以上的内容即可——
但**第 2 条（`tools/` 里的真实数值）在推送前必须处理完**。
