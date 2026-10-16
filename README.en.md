# health-report

**Turn your Apple Health export and InBody body-composition reports into a health report you can actually audit. Local-first: your data never leaves your machine.**

[中文说明](README.md) ｜ English

[![CI](https://github.com/alpsgangster/health-report/actions/workflows/ci.yml/badge.svg)](https://github.com/alpsgangster/health-report/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](#install)

```bash
health-report demo          # a full sample report in 30 seconds. No config, no API key, no network.
```

> **Don't want to install anything yet?** Read
> [`examples/expected_report.md`](examples/expected_report.md) — it is the complete output of
> the command above (generated from synthetic data). The CSV/XML files in `examples/` are
> synthetic too, so you can try the parsers with them.

---

## The problem

You already have health data everywhere. Apple Watch logs steps, heart rate, sleep, VO₂max.
A body-composition scale or an InBody machine hands you a printed report. Your bathroom
scale tracks weight. Every app draws a chart.

**None of them tell you which numbers must not be compared.**

Real examples, all from auditing actual body-composition reports:

- Your bathroom scale and the InBody both report body mass. Plot them together and you get
  one continuous line — but they are **two different measurement systems**. The "jump" in
  the middle may just be a device change, not a change in your body.
- The report says *"total body water 42.5 L, reference 35.5~43.4."* Nothing warns you that
  elevated water **inflates both body mass and lean mass**, so the concurrent
  *"lost 2.8 kg"* is partly water and can no longer be read as fat loss.
- The report shows *"left-leg fat 1.8 kg / 115.1%"* and *"1.5 kg / 94.3%"*. That second
  number reads like "down to 94% of normal" — but its denominator is the device's
  **internal reference model**, which is not guaranteed to be the same ruler across visits.
- Your weight went from 74.0 to 73.8 kg and you feel good. But a bathroom scale drifts by
  a few hundred grams day to day. **That 0.2 kg is inside the noise. It carries no information.**

`health-report` turns these "computed correctly, read incorrectly" situations into
**executable checks**: 12 rules with stable machine codes. Each one explains, with the
actual numbers, how to read the data. Full rule documentation:
**[docs/PITFALLS.md](docs/PITFALLS.md)** — that file matters more than the code.

---

## How it differs from other health-data tools

### 1. Reference ranges have a priority order — and must come from the same source

```
Device-reported range (printed on the InBody report)  >  built-in population table  >  none
```

Every item on a lab report carries its own reference range. Body-composition devices are
the same — they use their own model and their own population. So a **device-supplied range
wins**, and its provenance is printed in the report so you can check it.

With one precondition: **a range may only be applied to readings from the device that
supplied it.** Judging a bathroom scale's 21.2% body fat against InBody's 10–20% range
produces a professional-looking but meaningless "above reference upper bound" — exactly the
error this project exists to prevent. So:

- A range must come from the **same source** as the value being judged; otherwise the
  built-in population table is used instead.
- When a metric's readings come from **more than one device**, **no verdict is issued at
  all** — the report says `sources disagree, no verdict`. A normal/abnormal verdict on a
  series that mixes two rulers is meaningless.

The third tier is not a failure, it is honesty: **HRV(SDNN) and deep-sleep duration have no
generally accepted normal range** (between-person variation is too large). Those metrics get
`trend only` in the verdict column, and the report has a dedicated section listing them.
**Inventing a "normal" range does far more harm than leaving it blank.**

### 2. It separates `normal reference` from `activity goal` from `screening threshold`

This is the most common mistake in this category of tool: rendering a public-health
recommendation as a medical verdict.

| Kind | Example | Wording when outside |
|---|---|---|
| `range` normal reference | sleep 7–9 h | `above reference upper limit` |
| `screen` screening threshold | waist ≥90 cm (male) | `outside screening reference` (and it says this is not a diagnosis) |
| `goal` activity target | steps ≥8000 | **`target not met`** (never "abnormal") |

You walked 6,500 steps today. That is not an "abnormal metric". It is a day below a
commonly suggested amount.

### 2.5 The two sides of the same range do not mean the same thing

There is a subtler layer, and it decides whether the report becomes the boy who cried wolf:

**For a number of metrics only one side is a real signal.** The other side is common, or even
favourable. A low resting heart rate is normal for endurance athletes; high lean mass, muscle
mass and protein mean plenty of muscle; high SpO₂ is not a problem. Reporting the harmless
side as "abnormal" does not merely add a useless line — **it drowns out the line that matters.**

Measured evidence: in a real 1,336-day Apple export the median resting heart rate was **56**
and **78.7% of days fell below 60**. Under a naive "outside the screening range → warn" rule,
almost every single day would raise an alarm.

So those values are **downgraded**: the wording becomes `low (usually not a concern)`, the
first section explains why, and they stay out of the "needs attention" list.

Key design point: this judgement is **per metric and does not follow the bounds**. Even when
the range is printed by a device — InBody's "lean mass 46.7–57.0" flags 58.3 as above the
upper limit, yet being above a population-based limit is exactly what a muscular person looks
like. **Bounds may come from the device; "is this abnormal" is a statement about the metric**
(implemented in `catalog.REF_CONCERN`).

### 3. The rule layer computes. The model only explains.

**Every numeric conclusion comes from deterministic code. The language model is not allowed
to do arithmetic.**

It receives the structured summary produced by `build_facts()` — every number in it has
already been computed by code. `--json facts.json` dumps exactly that payload so you can
**read it before you send it**.

Consequence: **without an API key the report is still complete**, because every conclusion
already lives in the deterministic "Rule-layer conclusions" section. A missing key, a dead
network or a garbage model response cannot damage the auditable part of the report.

When enabled, it does two things: it translates the numbers into plain language, and it says
**what to do next**. Such advice is **strictly limited to improving data comparability** —
fix on one device, standardise measurement conditions, add data points, extend the observation
window, re-measure to confirm a change — because that is what these facts can support. It will
not give medical, nutritional or training prescriptions: no calorie deficits, protein grams,
supplements or dosages. In those situations it can only say "this needs professional review".

---

## Install

Zero third-party dependencies. The standard library is enough.

```bash
# Recommended: install into an isolated environment from git
pipx install "git+https://github.com/alpsgangster/health-report"

# Or clone and run without installing anything
git clone https://github.com/alpsgangster/health-report
cd health-report
PYTHONPATH=src python -m health_report demo
```

Optional accelerator: for multi-GB `export.xml` files, `lxml` is several times faster.
**It is optional** — without it the parser falls back to the standard library.

```bash
pipx install "health-report[fast] @ git+https://github.com/alpsgangster/health-report"
```

---

## Usage

```bash
# See what it does
health-report demo                       # full report from built-in synthetic data
health-report demo --findings            # also print which rules fired
health-report metrics                    # list the 74 supported metrics and built-in ranges
health-report inputs                     # explain the accepted input formats

# Your own data
health-report report export.zip -o report.md       # Apple Health export
health-report report inbody.csv -o report.md       # InBody long-format CSV
health-report report export.zip inbody.csv -o report.md   # merged
health-report parse export.zip -o health_out/      # parse only: CSV + JSON
health-report report export.zip --llm              # explicitly enable the AI section
health-report report export.zip --json facts.json --no-llm   # inspect the payload first
```

### Bring your own model

Any OpenAI-compatible endpoint. **No provider is assumed, and there is no middleman server.**

```bash
export HEALTH_REPORT_API_KEY="your-key"
export HEALTH_REPORT_BASE_URL="https://api.openai.com/v1"   # optional
export HEALTH_REPORT_MODEL="gpt-4o-mini"                    # optional
```

---

## Supported inputs

| Input | Forms | Notes |
|---|---|---|
| **Apple Health export** | `export.zip` / `export.xml` / `apple_health_export/` | Streaming parser; multi-GB files are fine. Daily metrics, sleep stages, individual workouts |
| **InBody / body-composition report** | long-format CSV | Any number of measurement-date columns; **the device's own reference ranges are parsed and take priority**; fuzzy metric-name matching |
| **Generic long-format CSV** | `date,metric,value,unit,source` | Universal entry point for any device, including hand-transcribed data |

Units are normalized at parse time (lb→kg, mi→km, kJ→kcal, …), so nothing downstream ever
sees mixed units. Details and examples: **[docs/DATA_FORMATS.md](docs/DATA_FORMATS.md)**.

---

## Privacy

Health data is among the most sensitive personal data there is. An Apple Health export
contains not just weight and heart rate but `workout-routes/*.gpx` — **the GPS trace of
every run, precise enough to locate your front door.**

- **Offline by default.** No telemetry, no update check, no crash reporting, no "anonymous
  usage statistics". To guarantee it: pass `--no-llm`.
- **Network access happens only when you explicitly enable the AI section**, and only a
  **computed summary** is sent — never raw records. Use `--json` to inspect it first.
- API keys are read from environment variables only. **Never written to disk, never logged**;
  they are masked as `sk-a***` in any config output.
- `.gitignore` rejects real data files by default, and a CI job checks every push for
  accidentally committed health data.

Full details, including an explicit threat model of what this project does **not** protect
against: **[docs/PRIVACY.md](docs/PRIVACY.md)**.

---

## ⚠️ This is not medical advice

This tool produces descriptive summaries of self-tracked measurements. It is not a medical
device and does not diagnose, treat, or prevent any disease. Its output is not a substitute
for evaluation by a qualified health professional.

Built-in reference ranges are **population screening heuristics with cited sources**,
intended for orientation only — not diagnostic thresholds. Where a measurement device or a
lab report supplies its own reference range, that range takes precedence. Every range in the
report is printed with its source so you can check it.

If you experience unexplained weight or muscle loss, persistent fatigue, or appetite or
endocrine symptoms, **see a doctor**. Do not substitute this tool's explanations for that.

---

## Roadmap

- [x] **v0.1** Apple Health + InBody + generic CSV; 66-metric catalog; 12 data-pitfall rules; Markdown report; optional AI section
- [ ] English report output (`build_report` already takes `lang`; only Chinese is implemented)
- [ ] HTML report with trend charts (plain Markdown cannot carry plots; needs a plotting dependency, which conflicts with "zero dependencies")
- [ ] Body-composition report **photos** as input (the data lives on paper — the most natural entry point; needs OCR/vision)
- [ ] Device-formula forensics (e.g. reverse-engineering Katch-McArdle from lean mass to verify the device's BMR)
- [ ] Cross-metric correlation (sleep duration ↔ next-day resting heart rate) — needs longer series
- [ ] More sources: Huawei Health, Xiaomi, Garmin, Oura

---

## Contributing

**The most valuable contribution is not code — it is a new data pitfall.**

If you hit a case on your own data where "both numbers are right but the conclusion was
wrong", please open an issue describing it (**de-identified** — see [PRIVACY.md](docs/PRIVACY.md)).
That kind of knowledge is worth more than code.

How to add a metric, a data source or a rule: see the "Extension points" section of
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

```bash
PYTHONPATH=src python -m unittest discover -s tests -v   # zero deps, stdlib unittest
```

---

## License

[MIT](LICENSE). Please also read the **NOT MEDICAL ADVICE** paragraph at the end of the LICENSE.
