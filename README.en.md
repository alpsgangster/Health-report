# health-report

**From nothing but your own Apple Health export, generate an audit-ready health report on your own machine. No network, no uploads, no API key.**

[中文说明](README.md) ｜ English

[![CI](https://github.com/alpsgangster/Health-report/actions/workflows/ci.yml/badge.svg)](https://github.com/alpsgangster/Health-report/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](#install)

```bash
health-report demo          # a complete report in 30 seconds — zero config, zero keys, no network
```

> **Not ready to install anything?** Read
> [`examples/expected_report.md`](examples/expected_report.md) — it is the complete output of the
> command above (built from synthetic data). The CSV/XML files under `examples/` are synthetic too,
> so you can try the parsers against them first.

---

## The problem it solves

An Apple Watch already records your steps, heart rate, sleep and VO₂max; you may also have a
body-composition report and a bathroom scale. Every app can draw you a chart.

**What none of them tell you is which numbers must not be compared with each other.**

Every item below actually occurred in one real 6-year export covering more than 2,600 days:

- **That step in the curve may be a device change.** One metric can come from several devices over
  the years, and Apple merges them into the same curve **without marking the switch**. In that export
  VO₂max came from **3** different devices, resting heart rate from 3, and heart-rate variability from
  3. How much of those "changes" is device difference cannot be read off the curve alone.
- **Steps were counted twice, once by each device.** iPhone and Apple Watch each record steps, over
  heavily overlapping intervals. Adding them unconditionally inflates the total by about **1.44×**
  (57.4% of days in that export had records from more than one source). Worse, **the error is not
  uniform** — only the single-source days are correct, so "which device was I wearing that day"
  contaminates every day-to-day comparison.
- **A low resting heart rate: alarm every day and you have no alarm at all.** People who have trained
  endurance for years sit in the low 50s. Under a naive "below the screening floor → flag it" rule,
  **most days** in that export would raise an alarm, and the one day that deserves attention drowns.
- **Six hours of sleep is not the same as "asleep at 1 a.m., up at 7."** Duration can be made up on
  another day; a drifting sleep midpoint cannot. Looking only at total duration misses the latter —
  and the latter tracks recovery more reliably.
- **Which day you attribute a night's sleep to decides whether you can see an association at all.**
  Apple files a night's sleep under **the day you woke up**. Get that wrong and a relationship such as
  "slept well → lower heart rate the next day" smears into noise.
- **An Apple export carries almost no reference ranges.** For most metrics the correct default is
  therefore "trend only". **Hard-coding a "normal" does far more harm than leaving it blank.**

`health-report` turns these "computed correctly, read incorrectly" situations into
**executable checks**: **13 rules with stable machine codes**, each of which tells you, with the
actual numbers, how to read the data. Full rule documentation is in
**[docs/PITFALLS.md](docs/PITFALLS.md)** — that file matters more than the code.

---

## Four differences from other health-data tools

### 1. A reference range is not mandatory — but it **must come from the same source as the reading**

```
range shipped with the reading being judged  >  most recent range from the same device  >  built-in population table  >  none
```

An Apple export essentially never carries ranges, so most metrics land on the built-in population
table, or on the final tier. That last tier is not a failure, it is honesty: `HRV(SDNN)` and
deep-sleep duration **have no generally accepted normal range** (between-person variation is far too
large). Those metrics get "trend only" in the verdict column, and the report has a dedicated section
listing them.

If you additionally feed it a body-composition report (InBody and the like), **the ranges printed on
that report are parsed and preferred** — it uses its own model and its own population. With one
precondition:

> **A range may only be applied to readings from the device that supplied it.** Judging a bathroom
> scale's 21.2% body fat against InBody's 10–20% range yields a professional-looking but meaningless
> "above reference upper limit". So when the most recent reading comes from another device that
> supplied no range of its own, the report says `sources disagree, no verdict` instead of forcing a
> range onto it.

### 2. It separates `normal reference`, `activity goal` and `screening threshold`

This is the most common mistake in this class of tool: rendering a public-health recommendation as a
medical verdict.

| Kind | Example | Wording when outside |
|---|---|---|
| `range` normal reference | sleep 7–9 h | `above reference upper limit` |
| `screen` screening threshold | waist ≥90 cm (men) | `outside screening threshold` (with a note that this is not a diagnosis) |
| `goal` activity goal | steps ≥8000 | **`goal not met`** (never "abnormal") |

You walked 6,500 steps today. That is not an "abnormal metric", it is a day short of a commonly
recommended amount.

### 3. The two sides of one range do not mean the same thing

There is a subtler layer here, and it decides whether the report turns into the boy who cried wolf:

**For a group of metrics only one side is a real signal**; landing on the other side is common, or
even good. A low resting heart rate is the norm for long-term endurance athletes; high lean mass,
skeletal muscle mass and protein mean plenty of muscle; high blood oxygen is not a problem. Reporting
the side that needs no attention as "abnormal" costs more than one useless line — **it drowns out the
line that genuinely deserves attention.**

So those out-of-range values are **downgraded**: the wording becomes `low (usually not a concern)`,
the first section explains why, and they stay out of the "needs attention" list.

Key design point: this judgement is **made per metric and does not follow the bounds**. Even when the
bounds are printed by a device — a body-composition scale's "lean mass 46.7–57.0" flags 58.3 as above
the upper limit, yet for someone with a lot of muscle being above a population-based limit is exactly
a good thing. **Bounds may come from the device; "does this count as abnormal" is a judgement about
the metric itself** (implemented in `catalog.REF_CONCERN`).

### 4. The rule layer computes first; the model only narrates

**Every numeric conclusion comes from deterministic code. The language model only explains, and is
not allowed to do arithmetic.**

What it receives is the **structured summary** produced by `build_facts()` — every number in it has
already been computed by code, and even aggregates such as "total training time" and "per-metric
coverage" are computed up front so that it never has to do the work itself. `--json facts.json` saves
that summary verbatim, so **you can read it yourself before any of it is sent**.

This means: **with no API key and fully offline, the report is still complete**, because every
conclusion is already written by code in the "Rule-layer conclusions" section. The AI at most adds a
paragraph of plain language beside it. A missing key, no network, or a garbage model response cannot
damage the auditable part of the report.

When enabled it writes two things: it turns those numbers into plain language, and it says **what to
do next**. But that kind of advice is **strictly confined to improving data comparability** (fix on
one device, standardise measurement conditions, add data points, extend the observation window,
re-measure to confirm), because that is what these facts can support. It will not give medical,
nutritional or training prescriptions — no calorie deficits, protein grams, supplements or dosages;
in those situations all it can say is "this needs professional review".

---

## Install

The core has **zero third-party dependencies**; the standard library is enough.

```bash
# Recommended: install from git into an isolated environment
pipx install "git+https://github.com/alpsgangster/Health-report"

# Or clone it and run it directly, installing nothing
git clone https://github.com/alpsgangster/Health-report
cd health-report
PYTHONPATH=src python -m health_report demo
```

**On Windows**, you do not have to remember `PYTHONPATH` — there is a launcher in the repository
root. Double-click it, or run it from `cmd`:

```cmd
health-report.cmd demo
health-report.cmd report "..\apple_health_export\export.xml" -o my-report.md
```

All it does is put `src/` on `PYTHONPATH` and set the output encoding to UTF-8 (without that,
a Chinese console chokes on the 🟠🔵 characters in the report). **No `pip install` required.**

Optional accelerator: for Apple's multi-GB `export.xml`, `lxml` is several times faster.
**It runs without it too** (falling back to the standard library):

```bash
pipx install "health-report[fast] @ git+https://github.com/alpsgangster/Health-report"
```

---

## Usage

### See what it can do

```bash
health-report demo                          # full report from the built-in synthetic data
health-report demo --findings               # also print which rules fired
health-report metrics                       # list the 77 supported metrics and built-in reference ranges
health-report inputs                        # explain which input formats are supported
```

### Use your own data

An Apple export alone is enough for a complete report — it is the **only required** data source.

```bash
# Apple Health export (iPhone → Health app → your picture → Export All Health Data)
health-report report export.zip -o my_report.md

# Optional: add a body-composition report (transcribe it into long-format CSV; see docs/DATA_FORMATS.md)
health-report report export.zip inbody.csv -o my_report.md

# Parse only, and get structured CSV/JSON to analyse yourself
health-report parse export.zip -o health_out/

# Explicitly enable AI interpretation (optional)
health-report report export.zip --llm

# Package exactly what would be sent to a model, and check it yourself first
health-report report export.zip --json facts.json --no-llm

# The client-facing form: a self-contained HTML report (inline SVG charts) — print it to PDF
health-report report export.zip --html my-report.html
```

### The HTML report: printing it *is* the PDF

`--html` writes a **self-contained single-file** HTML report: inline CSS, **hand-written SVG
charts**, **no external resources, no JavaScript**. Open it in a browser, press `Ctrl+P`, and
"Save as PDF" — that is the finished deliverable.

- **Nine sections**: cover / data-credibility notes / metric overview / sleep / recovery and load /
  associations between metrics / activity and training / rule-layer conclusions / AI interpretation
  (optional), plus the method and full-detail appendices
- **Every chart is hand-written SVG.** No plotting dependency is pulled in — "zero dependencies" is
  the selling point of this project and drawing a chart is not a good reason to break it:
  - monthly-median trend line with an **interquartile band**; reference ranges change *line style*
    by kind (`range` solid box / `screen` dashed box / `goal` a dotted line at the target only)
  - **device-handover markers**: the switch dates from `handovers` are drawn as vertical dashed
    lines labelled with the old and new device names
  - sleep-stage composition, a sleep-regularity scatter (axis labels restored to clock times),
    association comparison bars, steps against the goal, data coverage, and a check-up style
    verdict strip
- **Down-sampling and aggregation**: six years of data are aggregated by month (1,910 days → 79
  monthly points); scatters above 1,200 points are evenly sampled and the caption says so — it never
  tries to draw 2,600 points
- **Readable in black and white**: every piece of information also has text or a line style, never
  colour alone; "difference within noise" uses hatching, and "goal not met" is visually distinct
  from "outside the range"
- Fast: for a real six-year export the HTML step takes **0.07 s** (parsing is the 33-second part)

> ⚠️ When printing, **enable "Background graphics"**, otherwise the browser drops the pale
> backgrounds and hatching (that is a browser print option; CSS cannot force it). For page numbers,
> use the browser's **Headers and footers** option — `@page` margin boxes with `counter(page)` are
> only rendered by paging engines such as Prince/WeasyPrint, and Chrome/Edge ignore them. We do not
> pretend to have page numbers.

### Apple's multi-device records are not silently double-counted

This is the easiest pitfall to hit in an Apple export: **iPhone and Apple Watch each record steps,
over heavily overlapping intervals.** The Health app shows you the de-duplicated value, but the export
file contains both sets of records.

Measured on one real export covering 1,910 days:

| | |
|---|---|
| Days with records from more than one source | **1,096 days (57.4%)** |
| Unconditional summing vs. taking the primary device | **1.44× inflation** |

The cleanest single piece of evidence: on one day, two devices each recorded roughly 45,000 steps;
summed, that reports **90,000 steps**.

> Absolute totals are deliberately omitted here (that is personal information); what matters is the
> **multiplier**, and the fact that it is **not a uniform bias** — only single-source days are correct,
> so "which device was I wearing that day" contaminates day-to-day comparisons.

HealthKit's overlap de-duplication algorithm has **no public specification**, so this project does not
guess at one. Instead it makes the policy an explicit choice and puts the differences in front of you:

```bash
health-report report export.xml                      # default: primary device wins
health-report report export.xml --prefer-source "Apple Watch"   # name the primary device
health-report report export.xml --source-policy per-day-max     # or take each source's daily maximum
health-report report export.xml --source-policy sum             # unconditional sum (inflated; for comparison only)
```

For every affected metric the report adds a note giving the totals and the multiplier under each
policy. See section 1.5 of [docs/PITFALLS.md](docs/PITFALLS.md).

> Note: for snapshot metrics (weight, body-fat percentage) multiple sources mean **two devices giving
> different numbers for the same thing** — that is a disagreement, not a policy question. Those
> metrics are **judged as usual**, but a range is used only when one is available from the same
> source; when it is not, the report says `sources disagree, no verdict`, and that fact is flagged
> explicitly in the report.

### A step in the curve may be a device change (handover detection)

Apple merges data from different devices and different periods into one curve, **without marking the
switch**. Different devices — even the same device after a major algorithm version change — do not
give the same baseline reading for the same physiological quantity.

So there is a `DEVICE_HANDOVER` rule: it cuts the series into "consecutive runs from one source",
merges away runs that are too short, and whenever the source changes between the remaining runs it
records a handover point with the date and the old and new device names (collapsed into **one** note,
ordered by the number of handover points). The "Rule-layer conclusions" section also has a
**cross-device comparability** subsection listing every affected metric.

It reports `info` rather than `warn`: this is **a caveat about how to read a trend**, not a data
error — `warn` has to be kept for health signals that genuinely need attention.

### Sleep regularity, and how sleep relates to next-day recovery

The report computes the **standard deviation of the sleep midpoint** (midway between falling asleep
and waking) — the single best summary of how regular your schedule is, because it covers both "when
you went to bed" and "when you got up". The band thresholds (30 / 60 minutes) are explicitly
labelled in the report as **empirical bands, not diagnostic criteria**, and they describe dispersion
only, without judging whether it is good or bad.

There is also a set of **deterministically computed associations** (correlation, not causation):

- Sleep duration → **next-day** resting heart rate / heart-rate variability
- Daytime steps → that night's sleep duration
- **Training days vs. non-training days**: resting heart rate, HRV, sleep duration

Each one gives a median comparison between the two groups (easier to read than a correlation
coefficient) plus the coefficient itself. The evidence grade looks at **effect size relative to that
metric's measurement noise**, not at the correlation coefficient — so "high correlation, modest
grade" is possible, and it means the relationship is consistent but its magnitude is still within the
device's own repeat-measurement error. The report explains this.

> Why this deserves a section of its own: **regularity is easier to change than duration, and it
> tracks recovery more reliably.**

### Names in device names are redacted

Apple's default device names contain the owner's name ("Example Name's Apple Watch"). This tool prints
source names into the report, and a report is meant to be shared — so by default the personal-name
part is redacted and the device name kept (you need it to tell devices apart):

```
<owner>'s Apple Watch    ->  Apple Watch
(custom device names with no personal-name prefix)  ->  kept as-is
```

For no redaction at all (local debugging only): `--show-source-names`.

### AI interpretation: four backends, **auto-detected, but never sent off-machine on its own**

The AI section is **optional** (the report is complete without it). When enabled, the **backend is
auto-detected**, in this order:

| Order | Backend | Where the data goes | Used automatically? | What you have to do |
|---|---|---|---|---|
| 1 | A **cloud endpoint** configured through environment variables | the address you configured | ✅ (configuring it *is* consent) | provide an API key |
| 2 | A **local model** (Ollama / LM Studio / llama.cpp) | **nowhere — it stays on your machine** | ✅ | just start it (it probes 11434 / 1234 / 8080 on `127.0.0.1` by default) |
| 3 | An **AI tool already installed locally** (DSH / Claude Code / Codex) | the corresponding vendor (DeepSeek / Anthropic / OpenAI) | ❌ **you must ask for it** | `--llm`, or `--ai-backend dsh` |
| 4 | None of the above | —— | —— | a pure rule-based report: mechanical prose, complete content |

```bash
health-report report export.xml                    # only picks local backends, or ones you configured
health-report report export.xml --llm              # explicitly want AI: unlocks row 3 (which leaves your machine)
health-report report export.xml --ai-backend dsh   # or name it directly
health-report report export.xml --ai-backend none  # rule-based report only, fully offline
health-report report export.xml --no-llm           # same thing
```

The cloud endpoint can be any OpenAI-compatible service:

```bash
export HEALTH_REPORT_API_KEY="your-key"
export HEALTH_REPORT_BASE_URL="https://api.deepseek.com"    # optional
export HEALTH_REPORT_MODEL="deepseek-chat"                  # optional
```

> ⚠️ **"An AI tool installed locally" is not the same as "the model runs locally."** DSH / Claude
> Code / Codex are local clients; the model is in the cloud — using them means sending the computed
> summary to the corresponding vendor. So **they are never used by default**: you have to ask for
> them explicitly, and every run prints **"data will be sent to: &lt;vendor&gt;"** in the terminal,
> so you always know where your data went. To keep it truly off your machine, use backend 2 (a local
> model).
>
> Why this restriction exists: an earlier version let the auto-detection run all the way down, so on
> a machine with DSH installed, `health-report report export.xml` (**no API key configured, no
> `--llm`**) would **silently send the summary out**. Silent egress by default is not acceptable for
> something that calls itself privacy-first, so it now requires an explicit opt-in. The regression
> test is `tests/test_llm_backends.py::TestAutoNeverLeaksData`.

### Fabricated numbers cannot get into the report (hard provenance check)

Any model brings one risk: it invents a value. So every number the AI writes is **traced back, one by
one, against `facts`** (the precomputed summary) — if no source can be found, **the entire sentence
containing that number is deleted**, and the report states how many passages were removed and why. If
what remains is too thin, the whole section is dropped and the rule-layer text is used instead.

Better to have no AI section at all than a single fabricated value.

**False positives matter too** — discarding a whole correct interpretation is worse than missing a bad
one. So these **legitimate forms** are accepted: thousands separators (`1,910` or `11 534`),
magnitude shorthand (`1.08万`, the report's own 10,000-unit form), rounding a fact to fewer digits
(`6.28937…` written as `6.29`), and Markdown section numbers (`### 2.2`). But **a fabricated
measurement is still caught** (the summary says `61.0`, you write `58.2` → deleted).

> Trade-off note: small integers (1–12, used for counts and ordinals such as "two devices" or
> "point 2") are whitelisted, so a fabricated **count** can slip through; this check is aimed at
> fabricated **measurements**.

---

## Supported inputs

| Input | Forms | Notes |
|---|---|---|
| **Apple Health export** (the only required one) | `export.zip` / `export.xml` / `apple_health_export/` | Streaming parser; multi-GB files do not blow up memory. Daily metrics, sleep stages, individual workouts |
| **InBody / body-composition report** (optional) | long-format CSV | Any number of measurement-date columns; **the report's own reference ranges are parsed and take priority**; fuzzy metric-name matching |
| **Generic long-format CSV** (optional) | `date,metric,value,unit,source` | The universal entry point for any device, including hand-transcribed data |

Units are normalised at parse time (lb→kg, mi→km, kJ→kcal, …), so nothing downstream ever sees
"pounds here, kilograms there". Details and examples: **[docs/DATA_FORMATS.md](docs/DATA_FORMATS.md)**.

---

## Privacy

Health data is among the most sensitive personal data there is. An Apple export contains not only
weight and heart rate but also `workout-routes/*.gpx` — **the GPS trace of every run, precise enough
to locate your front door.**

- **Fully offline by default.** No telemetry, no update check, no crash reporting, no "anonymous
  statistics". For a 100% guarantee: add `--no-llm`.
- **Network access happens only when you explicitly enable AI interpretation**, and only the
  **computed summary** is sent — never raw records. Use `--json` to look at it yourself first.
- Keys are read from environment variables only — **never written to disk, never printed** — and are
  masked as `sk-a***` in logs.
- Personal names in the report are redacted (see "Names in device names are redacted" above).
- The repository's `.gitignore` rejects all real data files by default, and a `privacy guard` CI job
  checks every push for accidental commits.

Full details — including the threat model of what this project does **not** protect against — are in
**[docs/PRIVACY.md](docs/PRIVACY.md)**.

---

## ⚠️ This is not a medical diagnosis

This tool produces descriptive summaries of **self-tracked measurements**. It is not a medical device;
it does not diagnose, treat or prevent any disease, and its output cannot substitute for evaluation
by a qualified health professional.

The built-in reference ranges are **population screening references with cited sources**, intended for
orientation only — they are not diagnostic thresholds. Where a measurement device or a lab report
supplies its own reference range, that range takes precedence. Every range in the report is printed
with its source so you can check it.

If you experience unexplained weight or muscle loss, persistent fatigue, or appetite- or
endocrine-related symptoms, **see a doctor**; do not substitute this tool's explanations for that.

---

## Roadmap

- [x] **v0.1** Apple Health + body-composition CSV + generic CSV; 77-metric catalog; 13 data-pitfall rules; Markdown report; optional AI interpretation (four backends + hard numeric provenance check)
- [x] Sleep regularity (standard deviation of the sleep midpoint) and cross-metric associations (sleep → next-day recovery, training days vs. non-training days)
- [x] Device-handover detection (cross-device comparability)
- [x] **HTML report + inline SVG trend charts**, printable to PDF straight from the browser (zero dependencies, zero JS, self-contained single file)
- [ ] A portable, install-free exe (a small tkinter window to pick files → get a report) for people who do not install Python
- [ ] English report output (`build_report` already takes a `lang` argument; only Chinese is implemented)
- [ ] Device-formula forensics (e.g. reverse-engineering Katch-McArdle from lean mass to verify a device's BMR)
- [ ] More data sources: Huawei Health, Xiaomi, Garmin, Oura

---

## Contributing

**The most valuable contribution is not code — it is a new data pitfall.**

If you hit a case on real data where "both numbers are right but the conclusion was wrong", please
open an issue describing the phenomenon (**de-identified** — see
[PRIVACY.md](docs/PRIVACY.md)). That kind of knowledge is worth more than code.

How to add a metric, a data source or a rule: see "Extension points" in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v   # zero deps, stdlib unittest (328 tests)
PYTHONPATH=src python tools/_check_analysis.py           # end-to-end self-check script (5 more in the same family)
```

---

## License

[MIT](LICENSE). Please also read the **NOT MEDICAL ADVICE** paragraph at the end of the LICENSE.
