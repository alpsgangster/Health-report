# 发布检查清单

给维护者（也就是你）自己看的。**这个文件不面向用户，但留在仓库里无妨**——
它顺便告诉潜在贡献者这个项目对隐私是认真的。

> ⚠️ 这份清单本身**不许出现任何真实个人数值**。下面凡是要填搜索关键词的地方，
> 都用占位符示意——一旦写成真实值，这份本该保护隐私的文件自己就变成了泄漏点。

---

## 首次推送到 GitHub 之前

### 1. 占位符（已完成，换仓库名时才需要重做）

`CHANGE_ME` 已经全部替换成 `alpsgangster`，出现在：

| 文件 | 位置 |
|---|---|
| `README.md` | CI 徽章链接、安装命令、克隆地址 |
| `README.en.md` | 同上 |
| `pyproject.toml` | `[project.urls]` 的 Homepage 与 Issues |

如果你换了仓库名，重做一遍并复查：

```powershell
Select-String -Path (Get-ChildItem -Recurse -File -Include *.md,*.toml,*.yml).FullName -Pattern CHANGE_ME
```

顺便确认 `pyproject.toml` 里的 `description`、`keywords` 和你的仓库描述一致。

### 2. 搜真实姓名（这一步抓到过真问题，别跳过）

**实测教训**：首次提交前自查时发现**真实姓名出现在 16 处源码里**——
`privacy.py` 的 docstring 示例、`test_privacy.py` 的夹具、`_check_multisource.py`
的十几处断言。仓库一公开就跟着出去了，而且这些地方**看起来完全无害**（谁能想到
一个脱敏函数的注释里会写着真名）。

```powershell
# 换成你自己的姓、名、拼音、邮箱、常用昵称
$names = "姓", "名", "拼音", "邮箱前缀", "昵称"
Get-ChildItem -Recurse -File -Include *.py,*.md,*.toml,*.yml,*.csv,*.xml |
  Where-Object { $_.FullName -notmatch '\\out\\|\\\.git\\|__pycache__' } |
  Select-String -Pattern $names
```

**期望 0 命中。** 要保留示例的地方一律用占位符（本项目用 `某某`，
英文文档用 `Example Name`）。

> 顺带一条：这一段检查要**在提交之后再用 git 自己的视角复查一遍**，
> 因为工作区干净不代表提交历史干净：
> ```powershell
> git grep -n "你的姓名"
> ```

### 3. 确认 `tools/` 下的自测脚本里没有真实个人数值

六个自检脚本（`_check_analysis.py` / `_check_report.py` / `_check_parsers.py` /
`_check_multisource.py` / `_check_sleep_metrics.py` / `_check_ai.py`）**已经脱敏**：
例子用的都是 `samples.py` 里那套合成数值，个人数值只存在于运行时内存。
你的真实数值只作为**设计动机**留在 `docs/PITFALLS.md`——那里用的是合成值。

**但这只是当前状态，不是永久保证。** 你以后拿真实数据调试时很容易顺手把数值写进脚本：

```powershell
# 换成你自己真实数据里的关键词（体重、身高、体脂、日期、设备序列号…）
$mine = "身高数值", "体重数值", "体测日期", "设备序列号"
Get-ChildItem -Recurse -File -Include *.py,*.md,*.csv,*.xml |
  Where-Object { $_.FullName -notmatch '\\out\\|\\__pycache__\\' } |
  Select-String -Pattern $mine
```

命中的每一行都要看清楚：是**格式示例里的巧合数字**，还是**真的从你报告里抄下来的**。
后者一律换成合成值。

> 另外注意 `out/` 目录（所有解析产物与真实报告）已被 `.gitignore` 挡住。
> **别用 `git add -f` 去加它。**

### 4. 确认没有真实数据被 `git add` 过

```powershell
git ls-files | Select-String -Pattern '\.(zip|gpx)$|export\.xml|workout-routes|^InBody'
```

预期：`examples/` 下只有 `inbody_sample.csv`、`apple_export_sample.xml`、
`expected_report.md` 三个合成文件。CI 里的 `隐私守卫` job 也会挡这一层
（它跑的就是上面这条模式，注意模式锚定了行首，所以 `examples/inbody_sample.csv`
不会误伤），但**推送前自己看一眼比 CI 报错早**。

### 5. 生成示例文件与预期报告

```powershell
$env:PYTHONPATH="src"
python tools/make_sample_data.py                       # 等价于 health-report sample-data -o examples
python -m health_report demo -o examples/expected_report.md
```

`examples/expected_report.md` 是别人判断"这东西对我有没有用"的最快途径，必须进仓库。

**CI 现在会比对它**（剔掉「生成时间」那一行后逐行 diff），所以改了 `samples.py`
或 `report.py` 之后忘了重新生成，push 会直接红。同理 `examples/*.csv|xml`
会被 `cmp` 逐字节比对。

### 6. 跑一遍完整测试

```powershell
$env:PYTHONPATH="src"
python -m unittest discover -s tests        # 当前基线：328 项，约 1.5 秒
python tools/_check_multisource.py          # 另有 5 个 _check_*.py 同系列，全部应 exit 0
python -m health_report demo --findings
```

### 7. 建仓库时的建议

- **描述**（Description）：`Turn an Apple Health export into an auditable health report, entirely on your machine. Local-first, zero dependencies, no API key required.`
- **Topics**：`apple-health`、`health-data`、`self-tracking`、`local-first`、`privacy`、
  `quantified-self`、`python`、`data-quality`、`sleep-tracking`
- 第一次 push 后再去看 CI 是否绿。红了先修 CI——**一个红徽章比没有徽章更劝退。**
- Release 先发 `v0.1.0`，把 `demo` 的输出（或 HTML 版的截图）贴上。

---

## 每个版本发布前

- [ ] `python -m unittest discover -s tests` 全绿
- [ ] `tools/_check_*.py` 六个脚本全部 exit 0
- [ ] `health-report demo` 能跑（CI 会验，但要本地确认一次输出好看）
- [ ] 新增的规则同步写进了 `docs/PITFALLS.md`（含总表那一行）
- [ ] 新增的指标区间的 `ref_source` 写了出处（`tests/test_catalog.py` 会强制）
- [ ] `README.md` 与 `README.en.md` 的内容没有分叉
- [ ] 没有把 `examples/expected_report.md` 忘在旧版本（CI 会挡，但别依赖它）
- [ ] 第 2 条（真实姓名）复查过一遍

---

## 关于这个文件本身

如果你想在正式发布时删掉它，删掉本章节以上的内容即可——
但**第 2 条（真实姓名）与第 3 条（`tools/` 里的真实数值）在推送前必须处理完**。
