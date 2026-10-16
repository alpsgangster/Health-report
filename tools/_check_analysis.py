"""临时自测脚本：验证 analysis.py / pitfalls.py 的规则与口径。

本脚本**只用合成数值**：能复用 `src/health_report/samples.py` 的地方就直接复用，
不能复用的（构造特定触发条件所需）也全部是编造的数值，不含任何真实个人数据。

不属于交付物，验证完可删除。运行：

    cd 06_HealthReport
    $env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
    python tools\_check_analysis.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from health_report import catalog, samples  # noqa: E402
from health_report.analysis import NOTE_MIXED_SOURCES, analyze  # noqa: E402
from health_report.model import Dataset, Observation, RefRange, Workout  # noqa: E402
from health_report.pitfalls import MASS_BALANCE, RANGE_OUT, check  # noqa: E402

# ── 复用 samples.py 的合成数值 ────────────────────────────

SRC = samples.INBODY_SOURCE          # "InBody270"
A = samples.INBODY_A_DATE            # 2026-01-10
B = samples.INBODY_B_DATE            # 2026-05-20

#: metric -> (A 值, B 值)，直接从 samples.py 的合成行取。
SAMPLES_PAIRS = {metric: (va, vb) for metric, _ref, va, vb in samples._INBODY_ROWS}


def _device_ref(metric: str) -> RefRange:
    """取 samples.py 里该指标的设备报告参考范围（必须存在）。"""
    ref = _device_ref_or_none(metric)
    if ref is None:
        raise KeyError(f"{metric} 在 samples.py 里没有设备区间")
    return ref


def _device_ref_or_none(metric: str) -> "RefRange | None":
    """取 samples.py 里该指标的设备报告参考范围；没有则 None。"""
    for m, ref, _va, _vb in samples._INBODY_ROWS:
        if m == metric:
            return ref
    raise KeyError(f"samples.py 里没有指标 {metric}")


def ob(metric, date, value, unit, source=SRC, ref=None, ts=""):
    return Observation(metric=metric, date=date, value=value, unit=unit,
                       source=source, ts=ts or f"{date} 07:30:00", ref=ref)


def _inbody_obs(metrics, date_index, extra=None):
    """从 samples.py 取值构造一批观测。

    date_index: 0 取 A 那次数值，1 取 B 那次数值。
    extra: 形如 ((metric, value), ...) 的**可重复迭代**（不要传生成器，
           否则调用一次就被取空了，这里消费两次）。
    """
    overrides = dict(extra or ())
    date = (A, B)[date_index]
    out = []
    for metric in metrics:
        unit = catalog.get(metric).unit if catalog.get(metric) else ""
        value = overrides[metric] if metric in overrides else SAMPLES_PAIRS[metric][date_index]
        out.append(ob(metric, date, value, unit, ref=_device_ref_or_none(metric)))
    return out


# ── 场景夹具 ─────────────────────────────────────────────

#: 场景 2 用到的指标：覆盖 SRC_MIXED / HYDRATION_AMPLIFIES / IMPEDANCE_SHIFT /
#: PCT_BASELINE_CAVEAT / LABEL_CONFLICT 等规则，并验证 MASS_BALANCE 保持沉默。
STABLE_METRICS = [
    "body_mass", "body_fat_pct", "body_fat_mass", "total_body_water",
    "protein_mass", "mineral_mass", "lean_body_mass", "skeletal_muscle_mass",
    "bmr", "bmi", "visceral_fat_level", "smi", "inbody_score", "whr",
    "lean_left_leg", "lean_right_leg", "lean_trunk",
    "lean_pct_left_leg", "lean_pct_right_leg", "lean_pct_trunk",
    "z_left_leg_20k", "z_right_leg_20k", "z_left_arm_20k",
]

#: 场景 2 的节段百分比：**故意让隐含标准值几乎不动**。
#:
#: 这是基线稳定的案例：三个节段的 kg 都在明显变化（绝对值都超过各自噪声），
#: 百分比也在动，但反推出来的基数稳定，所以 PCT_BASELINE_SHIFT 必须**保持沉默**，
#: 而 PCT_BASELINE_CAVEAT 照样给出提示。
#:
#: 百分比不是随手写的：取末次百分比 pct_b，再按目标漂移反解首次百分比
#:     drift = 1 - (kg_b/kg_a) * (pct_a/pct_b)  =>  pct_a = pct_b * (1 - drift) / (kg_b/kg_a)
#: 三个节段的目标漂移分别取 3.5% / 4.0% / 4.5%，都明显低于 5% 的阈值。
_STABLE_PCT_B = {
    "lean_pct_left_leg": 104.0,
    "lean_pct_right_leg": 100.5,
    "lean_pct_trunk": 109.0,
}
_STABLE_PCT_DRIFT_TARGET = {
    "lean_pct_left_leg": 0.035,
    "lean_pct_right_leg": 0.040,
    "lean_pct_trunk": 0.045,
}


def _stable_pct_overrides():
    """按目标漂移反解百分比数值（结果确定性，见上面的公式）。"""
    out = {}
    for pct_metric, pct_b in _STABLE_PCT_B.items():
        kg_metric = "lean_" + pct_metric[len("lean_pct_"):]
        kg_a, kg_b = SAMPLES_PAIRS[kg_metric]
        drift = _STABLE_PCT_DRIFT_TARGET[pct_metric]
        pct_a = pct_b * (1.0 - drift) / (kg_b / kg_a)
        out[pct_metric] = (round(pct_a, 1), pct_b)
    return out


_STABLE_PCT_OVERRIDES = _stable_pct_overrides()

#: 基线稳定的案例里，哪些节段做了百分比覆盖（用于漂移断言）。
STABLE_PCT_METRICS = ("lean_left_leg", "lean_right_leg", "lean_trunk")

#: 只有一个点的指标，用于触发 TREND_TOO_SHORT
STABLE_SINGLE_POINT = "hrv_sdnn"


def build_baseline_stable_dataset() -> Dataset:
    """场景 2：基线稳定的案例。两个日期、两套来源（触发 SRC_MIXED）。"""
    obs = _inbody_obs(STABLE_METRICS, 0,
                      extra=tuple((m, v[0]) for m, v in _STABLE_PCT_OVERRIDES.items()))
    obs += _inbody_obs(STABLE_METRICS, 1,
                       extra=tuple((m, v[1]) for m, v in _STABLE_PCT_OVERRIDES.items()))
    # 第二个来源：家用秤同一天给的体重，与体成分仪差 > 噪声（触发 SAME_DAY_CONFLICT）
    obs.append(ob("body_mass", B, 73.6, "kg", source=samples.SCALE_SOURCE))
    # 只有一个点的指标（触发 TREND_TOO_SHORT）
    obs.append(ob(STABLE_SINGLE_POINT, B, 48.2, "ms"))
    ds = Dataset(
        observations=obs,
        profile=dict(samples.PROFILE),
        # 设备文字判定与数值判定冲突：数值超出上限，文字却写「正常」（触发 LABEL_CONFLICT）
        labels={f"bmi_judgement@{B}": "正常",
                f"body_fat_pct_judgement@{B}": "正常"},
        notes=["本场景使用内置合成样例数值，不含任何真实个人数据。"],
    )
    ds.workouts.append(Workout(date=B, activity="跑步", duration_min=30.0,
                               distance_km=5.0, avg_hr=152, energy_kcal=236, source=SRC))
    ds.workouts.append(Workout(date=B, activity="力量训练", duration_min=45.0,
                               avg_hr=None, energy_kcal=None, source=SRC))
    ds.workouts.append(Workout(date=A, activity="跑步", duration_min=None,
                               distance_km=None, avg_hr=148, source=samples.WATCH_SOURCE))
    return ds


#: 场景 3 用另一组日期，避免与场景 2 的数据混在一起。
SHIFT_A, SHIFT_B = "2024-03-05", "2024-08-12"


def build_baseline_shift_dataset() -> Dataset:
    """场景 3：隐含标准值漂移 > 5%，必须触发 PCT_BASELINE_SHIFT。

    数值全部取自 samples.py 的同名指标（它本身就是纯合成数据）：
    左右腿的 kg 都在下降、百分比下降得更多，反推出来的隐含标准值上升约 7%~8%，
    明显超过 5% 的阈值。具体数字见 show() 打印出的 evidence。
    """
    obs = []
    for kg_metric, pct_metric in (("lean_left_leg", "lean_pct_left_leg"),
                                  ("lean_right_leg", "lean_pct_right_leg")):
        for metric in (kg_metric, pct_metric):
            unit = catalog.get(metric).unit
            va, vb = SAMPLES_PAIRS[metric]
            obs.append(ob(metric, SHIFT_A, va, unit))
            obs.append(ob(metric, SHIFT_B, vb, unit))
    return Dataset(observations=obs, profile=dict(samples.PROFILE),
                   notes=["纯合成数据，用于验证 PCT_BASELINE_SHIFT。"])


def build_aggregate_dataset() -> Dataset:
    """场景 6：按日累计/平均的指标（步数、睡眠），覆盖率低，验证口径 3/7。"""
    from datetime import date, timedelta
    start = date(2024, 1, 1)
    obs = []
    for i in range(20):
        d = (start + timedelta(days=i)).isoformat()
        # 步数只在 8 天有记录，睡眠只在 10 天有记录，覆盖率都低于 70%
        if i % 2 == 0 and i < 16:
            obs.append(Observation(metric="steps", date=d, value=10000.0,
                                   unit="步", source=samples.WATCH_SOURCE))
        if i % 2 == 0:
            obs.append(Observation(metric="sleep_total", date=d, value=7.5,
                                   unit="小时", source=samples.WATCH_SOURCE))
    # 心率是 mean 类：同一天两条，验证按日平均而不是取末条
    obs.append(Observation(metric="heart_rate", date="2024-01-01", value=60.0,
                           unit="次/分", source=samples.WATCH_SOURCE,
                           ts="2024-01-01 08:00:00"))
    obs.append(Observation(metric="heart_rate", date="2024-01-01", value=80.0,
                           unit="次/分", source=samples.WATCH_SOURCE,
                           ts="2024-01-01 20:00:00"))
    return Dataset(observations=obs, profile=dict(samples.PROFILE),
                   notes=["纯合成数据，用于验证 sum/mean 类口径。"])


def build_verdict_kind_dataset() -> Dataset:
    """场景 7：三种区间性质的措辞（range / screen / goal）。

    每个指标只给一个点：`RANGE_OUT` 的判定用最新值，单点就够，
    这样每个指标只会产生一条 RANGE_OUT，断言更准确。
    """
    obs = [
        # sleep_total：内置 7~9（range），6.0 低于下限 -> warn「低于参考下限」
        ob("sleep_total", "2024-01-20", 6.0, "小时", source=samples.WATCH_SOURCE),
        # resting_hr：内置 60~100（screen），108 超出 -> warn「超出筛查参考范围」
        ob("resting_hr", "2024-01-20", 108.0, "次/分", source=samples.WATCH_SOURCE),
        # steps：内置 >=8000（goal，verdict=True），3000 未达目标 -> info「未达目标」
        ob("steps", "2024-01-20", 3000.0, "步", source=samples.WATCH_SOURCE),
    ]
    return Dataset(observations=obs, profile=dict(samples.PROFILE))


def build_imbalanced_dataset() -> Dataset:
    """场景 5：体成分四项加不起来，必须触发 MASS_BALANCE。"""
    obs = [
        ob("total_body_water", B, 42.0, "L", ref=_device_ref("total_body_water")),
        ob("protein_mass", B, 10.8, "kg", ref=_device_ref("protein_mass")),
        ob("mineral_mass", B, 3.50, "kg", ref=_device_ref("mineral_mass")),
        ob("body_fat_mass", B, 9.9, "kg", ref=_device_ref("body_fat_mass")),
        ob("body_mass", B, 71.2, "kg", ref=_device_ref("body_mass")),
    ]
    return Dataset(observations=obs, profile=dict(samples.PROFILE))


# ── 展示与断言 ───────────────────────────────────────────


def show(title, ds):
    print("=" * 78)
    print(title)
    print("=" * 78)
    an = analyze(ds)
    print(f"[analysis] 指标数={len(an.trends)} span={an.span} span_days={an.span_days} "
          f"profile_keys={sorted(an.profile.keys())}")
    print(f"[analysis] by_category={an.by_category()}")
    print(f"[analysis] out_of_ref={[t.metric for t in an.out_of_ref()]}")
    print(f"[analysis] enough_for_trend={[t.metric for t in an.enough_for_trend()]}")
    print(f"[analysis] workout_summary={an.workout_summary}")
    for m in an.ordered_metrics():
        t = an.trends[m]
        print(f"  - {m:24s} n={t.n} days={t.days} span={t.span_days:3d} kind={t.kind:6s} "
              f"insuff={str(t.insufficient):5s} agg={t.agg:8s} delta={t.delta} "
              f"verdict={t.verdict:4s} ref={t.ref_origin}/{t.ref_kind or '-'} "
              f"note={t.verdict_note or '-'} range_avail={t.range_available}")
    findings = check(ds, an)
    print(f"[pitfalls] {len(findings)} 条")
    for f in findings:
        print(f"  {f.code:22s} {f.severity:5s} {f.title}")
        print(f"      detail: {f.detail}")
    return an, findings


def main():
    failures = []

    # ── 场景 1：空 Dataset ───────────────────────────────
    print("=" * 78)
    print("场景 1：空 Dataset")
    print("=" * 78)
    empty_an = analyze(Dataset())
    empty_findings = check(Dataset(), empty_an)
    print(f"[analysis] trends={empty_an.trends} span={empty_an.span} "
          f"span_days={empty_an.span_days} workouts={empty_an.workout_summary}")
    print(f"[pitfalls] {empty_findings}")
    if empty_findings != [] or empty_an.trends != {} or empty_an.span is not None:
        failures.append("空 Dataset 未优雅处理")

    # ── 场景 2：基线稳定的案例（多来源） ─────────────────
    an, findings = show("场景 2：基线稳定的案例（两次数值 + 两套来源，全部合成）",
                        build_baseline_stable_dataset())
    codes = {f.code for f in findings}

    expected = ["SRC_MIXED", "SAME_DAY_CONFLICT", "HYDRATION_AMPLIFIES", "IMPEDANCE_SHIFT",
                "PCT_BASELINE_CAVEAT", "LABEL_CONFLICT", "TREND_TOO_SHORT"]
    for code in expected:
        if code not in codes:
            failures.append(f"场景 2 未触发 {code}")
    if "PCT_BASELINE_SHIFT" in codes:
        failures.append("场景 2 是基线稳定的案例，不该触发 PCT_BASELINE_SHIFT")
    if MASS_BALANCE in codes:
        failures.append("场景 2 各来源内部质量守恒成立，不该触发 MASS_BALANCE")

    # 参考区间必须与读数同源：跨来源的体重不再借用体成分仪的区间
    bm = an.trends["body_mass"]
    print(f"[check] body_mass delta={bm.delta} sources={bm.sources} verdict={bm.verdict} "
          f"note={bm.verdict_note!r} ref={bm.ref} range_available={bm.range_available}")
    if bm.sources != [SRC, samples.SCALE_SOURCE]:
        failures.append(f"body_mass sources 不对：{bm.sources}")
    if bm.verdict != "na" or bm.verdict_note != NOTE_MIXED_SOURCES:
        failures.append(f"跨来源指标应不判定并给出原因：{bm.verdict}/{bm.verdict_note!r}")
    if bm.ref is not None:
        failures.append(f"跨来源指标的 ref 应为 None（不借另一台设备的区间）：{bm.ref}")
    if bm.range_available is not True:
        failures.append("body_mass 确有区间可用，range_available 应为 True")
    if bm.delta is None:
        failures.append("body_mass 首末不同（含跨来源），delta 不应为 None")

    tbw = an.trends["total_body_water"]
    print(f"[check] total_body_water last={tbw.last.value} ref_origin={tbw.ref_origin} "
          f"verdict={tbw.verdict} range_available={tbw.range_available}")
    if tbw.verdict != "high":
        failures.append(f"total_body_water 应为 high：{tbw.verdict}")
    if tbw.last.value <= tbw.ref.high:
        failures.append("HYDRATION_AMPLIFIES 前提不成立：最新水分未超出自身参考上限")

    # 单点且没有公认区间：range_available 说明"到底有没有区间"
    if an.trends[STABLE_SINGLE_POINT].range_available is not False:
        failures.append("HRV 没有公认区间，range_available 应为 False")

    # 逐条核对 evidence
    for f in findings:
        if f.code == "SRC_MIXED" and not f.evidence.get("sources"):
            failures.append("SRC_MIXED evidence 缺 sources")
        if f.code == "HYDRATION_AMPLIFIES":
            ev = f.evidence
            if ev.get("value") != tbw.last.value or ev.get("ref", {}).get("high") != tbw.ref.high:
                failures.append(f"HYDRATION_AMPLIFIES evidence 不对：{ev}")
        if f.code == "IMPEDANCE_SHIFT":
            segs = {s["metric"] for s in f.evidence["segments"]}
            print(f"[check] IMPEDANCE_SHIFT 命中节段={sorted(segs)}")
            if "z_left_leg_20k" not in segs:
                failures.append("IMPEDANCE_SHIFT 未命中 z_left_leg_20k")
            for s in f.evidence["segments"]:
                if not (s["abs_hit"] or s["pct_hit"]):
                    failures.append(f"IMPEDANCE_SHIFT 命中了不超阈值的节段：{s}")
        if f.code == "LABEL_CONFLICT":
            ev = f.evidence
            print(f"[check] LABEL_CONFLICT label={ev['label']!r} value={ev['value']} "
                  f"date={ev['date']} computed={ev['computed_verdict']} "
                  f"label_verdict={ev['label_verdict']}")
            if ev["computed_verdict"] == ev["label_verdict"]:
                failures.append("LABEL_CONFLICT 两边的判定其实一致，不该报")
            if ev["date"] != B:
                failures.append(f"LABEL_CONFLICT 日期取错：{ev['date']}")
        if f.code == "TREND_TOO_SHORT" and STABLE_SINGLE_POINT not in f.metrics:
            failures.append(f"TREND_TOO_SHORT 未包含 {STABLE_SINGLE_POINT}")

    # 质量守恒恒等式在夹具里必须成立（否则 MASS_BALANCE 的"不该报"就没意义）
    four = sum((SAMPLES_PAIRS["total_body_water"][1], SAMPLES_PAIRS["protein_mass"][1],
                SAMPLES_PAIRS["mineral_mass"][1], SAMPLES_PAIRS["body_fat_mass"][1]))
    mass = SAMPLES_PAIRS["body_mass"][1]
    print(f"[check] 夹具质量守恒: 四项合计={four} vs 体重={mass} 残差={abs(four - mass)}")
    if abs(four - mass) > 1e-9:
        failures.append(f"夹具本身不闭合（残差 {abs(four - mass)}），MASS_BALANCE 断言失效")

    # body_fat_pct：设备自带区间，判定应正常
    bf = an.trends["body_fat_pct"]
    print(f"[check] body_fat_pct verdict={bf.verdict} ref_origin={bf.ref_origin} "
          f"note={bf.verdict_note!r}")
    if bf.ref_origin != "device" or bf.verdict not in ("in", "low", "high"):
        failures.append(f"body_fat_pct 应使用同源设备区间并判定：{bf.ref_origin}/{bf.verdict}")

    # 隐含标准值漂移必须 < 5%（这就是"基线稳定"的定义）
    for pct_metric, (pct_a, pct_b) in _STABLE_PCT_OVERRIDES.items():
        kg_metric = "lean_" + pct_metric[len("lean_pct_"):]
        b1 = SAMPLES_PAIRS[kg_metric][0] / (pct_a / 100.0)
        b2 = SAMPLES_PAIRS[kg_metric][1] / (pct_b / 100.0)
        drift = abs(b2 - b1) / b1 * 100.0
        print(f"[check] {kg_metric} 隐含标准值漂移={drift:.2f}%（必须 < 5%）")
        if drift >= 5.0:
            failures.append(f"场景 2 的 {kg_metric} 漂移 {drift:.2f}% 不该达到 5%")

    # ── 场景 3：合成数据，PCT_BASELINE_SHIFT 必须触发 ────
    shift_an, syn_findings = show("场景 3：隐含标准值漂移 > 5%（纯合成）",
                                  build_baseline_shift_dataset())
    syn_codes = {f.code for f in syn_findings}
    if "PCT_BASELINE_SHIFT" not in syn_codes:
        failures.append("场景 3 未触发 PCT_BASELINE_SHIFT")
    if "PCT_BASELINE_CAVEAT" not in syn_codes:
        failures.append("场景 3 未触发 PCT_BASELINE_CAVEAT")
    for f in syn_findings:
        if f.code == "PCT_BASELINE_SHIFT":
            metrics = set(f.metrics)
            if not {"lean_left_leg", "lean_right_leg"} <= metrics:
                failures.append(f"场景 3 漂移指标不全：{metrics}")
            for p in f.evidence["pairs"]:
                print(f"[check] {p['cn']}: b1={p['baseline_a']:.4f} b2={p['baseline_b']:.4f} "
                      f"变化={p['baseline_change_pct']:.2f}%")
                if abs(p["baseline_change_pct"]) <= 5.0:
                    failures.append(f"报出的漂移不足 5%：{p}")
            got = {p["kg_metric"]: abs(p["baseline_change_pct"]) for p in f.evidence["pairs"]}
            if not (7.0 <= got.get("lean_left_leg", 0) <= 10.0):
                failures.append(f"左腿漂移超出预期区间：{got.get('lean_left_leg')}")
            if not (6.0 <= got.get("lean_right_leg", 0) <= 9.0):
                failures.append(f"右腿漂移超出预期区间：{got.get('lean_right_leg')}")
    if shift_an.trends["lean_left_leg"].delta is None:
        failures.append("场景 3 的 lean_left_leg 应有 delta")

    # ── 场景 5：体成分质量守恒 ───────────────────────────
    _, mb_findings = show("场景 5：体成分四项加不起来", build_imbalanced_dataset())
    if MASS_BALANCE not in {f.code for f in mb_findings}:
        failures.append("场景 5 未触发 MASS_BALANCE")
    for f in mb_findings:
        if f.code == MASS_BALANCE:
            rows = f.evidence["rows"]
            expect = abs(rows[0]["water"] + rows[0]["protein"] + rows[0]["mineral"]
                         + rows[0]["fat"] - rows[0]["mass"])
            print(f"[check] MASS_BALANCE 残差={rows[0]['residual']}（应为 {expect}）")
            if abs(rows[0]["residual"] - expect) > 1e-9:
                failures.append(f"MASS_BALANCE 残差算错：{rows[0]['residual']}")
            if rows[0]["residual"] <= f.evidence["tolerance"]:
                failures.append("MASS_BALANCE 报了一条没超阈值的记录")
            if f.severity != "warn":
                failures.append(f"MASS_BALANCE 应为 warn：{f.severity}")
            if "source" not in rows[0]:
                failures.append("MASS_BALANCE 行里应带 source")

    # ── 场景 6：sum / mean 类口径 ────────────────────────
    agg_an, agg_findings = show("场景 6：步数与睡眠（覆盖率不足）+ 同日心率",
                                build_aggregate_dataset())
    if "MISSING_DAYS" not in {f.code for f in agg_findings}:
        failures.append("场景 6 未触发 MISSING_DAYS")
    steps = agg_an.trends["steps"]
    print(f"[check] steps n={steps.n} days={steps.days} span={steps.span_days} "
          f"agg={steps.agg} delta={steps.delta} pct={steps.pct} per_30d={steps.per_30d} "
          f"mean={steps.mean} daily_mean={steps.daily_mean} coverage={steps.coverage} "
          f"kind={steps.kind} insuff={steps.insufficient} verdict={steps.verdict}")
    if steps.agg != "sum" or steps.delta is not None or steps.pct is not None \
            or steps.per_30d is not None:
        failures.append("sum 类的 delta/pct/per_30d 必须为 None")
    if steps.daily_mean is None or abs(steps.daily_mean - 8 * 10000.0 / steps.span_days) > 1e-6:
        failures.append(f"steps daily_mean 应 = 总和/跨度天数：{steps.daily_mean}")
    if steps.coverage is None or abs(steps.coverage - 8 / steps.span_days) > 1e-9:
        failures.append(f"steps coverage 不对：{steps.coverage}")
    if steps.mean is None or abs(steps.mean - 10000.0) > 1e-9:
        failures.append(f"steps mean 不对：{steps.mean}")
    # 步数现在 verdict=True：日均 10000 高于 goal 下限 8000，应判 in
    if steps.verdict != "in":
        failures.append(f"steps 日均 10000 应判 in：{steps.verdict}")
    slp = agg_an.trends["sleep_total"]
    print(f"[check] sleep_total agg={slp.agg} delta={slp.delta} mean={slp.mean} "
          f"daily_mean={slp.daily_mean} coverage={slp.coverage} median={slp.median}")
    if slp.agg != "mean" or slp.delta is not None:
        failures.append("mean 类的 delta 必须为 None")
    if slp.daily_mean is None or abs(slp.daily_mean - 7.5) > 1e-9:
        failures.append(f"sleep daily_mean 应 = 有数据日的平均：{slp.daily_mean}")
    if slp.verdict != "in" or slp.ref_origin != "builtin":
        failures.append(f"sleep_total 判定不对：{slp.ref_origin}/{slp.verdict}")
    hr = agg_an.trends["heart_rate"]
    print(f"[check] heart_rate mean={hr.mean} daily_mean={hr.daily_mean}（同日均值应为 70）")
    if hr.daily_mean is None or abs(hr.daily_mean - 70.0) > 1e-9:
        failures.append(f"mean 类同日多条应按日平均（70）：{hr.daily_mean}")

    # ── 场景 7：三种区间性质的措辞 ───────────────────────
    kind_an, kind_findings = show("场景 7：range / screen / goal 三种区间性质",
                                  build_verdict_kind_dataset())
    kinds = {}
    for f in kind_findings:
        if f.code == RANGE_OUT:
            kinds.setdefault(f.metrics[0], f)
    for m in sorted(kinds):
        print(f"[check] RANGE_OUT {m}: severity={kinds[m].severity} "
              f"ref_kind={kinds[m].evidence['ref_kind']}")
    # range：低于下限 -> warn
    if "sleep_total" not in kinds or kinds["sleep_total"].severity != "warn":
        failures.append("sleep_total 低于 range 下限应报 warn")
    elif "低于参考下限" not in kinds["sleep_total"].title:
        failures.append(f"range 类措辞不对：{kinds['sleep_total'].title}")
    # screen：超出阈值 -> warn，且必须说明是筛查不是诊断
    if "resting_hr" not in kinds or kinds["resting_hr"].severity != "warn":
        failures.append("resting_hr 超出 screen 阈值应报 warn")
    elif "超出筛查参考范围" not in kinds["resting_hr"].title:
        failures.append(f"screen 类措辞不对：{kinds['resting_hr'].title}")
    elif "筛查" not in kinds["resting_hr"].detail:
        failures.append("screen 类的 detail 必须说明这是筛查参考")
    # goal：未达目标 -> info，措辞不能出现「异常」
    if "steps" not in kinds:
        failures.append("steps 未达目标应报 RANGE_OUT（goal 分支）")
    else:
        f = kinds["steps"]
        if f.severity != "info":
            failures.append(f"goal 类 severity 应为 info：{f.severity}")
        if "未达目标" not in f.title:
            failures.append(f"goal 类措辞应含「未达目标」：{f.title}")
        if "异常" in f.title:
            failures.append(f"goal 类的标题不得出现「异常」：{f.title}")
        # detail 允许用「与身体指标异常是两件事」这种否定式说明（这正是要的语气），
        # 但必须把这件事定性为「目标」而不是「异常读数」。
        if "目标" not in f.detail:
            failures.append(f"goal 类的 detail 必须说明这是目标：{f.title}")
        print(f"[check] steps goal 分支: severity={f.severity} title={f.title}")
    for m in ("sleep_total", "resting_hr", "steps"):
        if m in kind_an.trends and kind_an.trends[m].verdict not in ("low", "high"):
            failures.append(f"{m} 应产生 low/high 判定：{kind_an.trends[m].verdict}")

    # ── 场景 4：脏输入不能崩 ─────────────────────────────
    print("=" * 78)
    print("场景 4：脏输入（坏日期 / 只有 workouts / 未知指标 / 坏标签）")
    print("=" * 78)
    dirty = Dataset(
        observations=[
            Observation(metric="body_mass", date="", value=70.0, unit="kg"),
            Observation(metric="body_mass", date="不是日期", value=69.0, unit="kg"),
            Observation(metric="不存在的指标", date="2024-01-01", value=1.0, unit="x"),
            Observation(metric="body_mass", date="2024-02-01", value=68.0, unit="kg"),
        ],
        labels={"奇怪标签": "超重", "bmi_judgement": "正常", "bmi_judgement@坏日期": "偏瘦"},
    )
    dirty_an = analyze(dirty)
    dirty_findings = check(dirty, dirty_an)
    print(f"[analysis] span={dirty_an.span} span_days={dirty_an.span_days}")
    for f in dirty_findings:
        print(f"  {f.code:22s} {f.severity:5s} {f.title}")
    print("[check] 脏输入未抛异常")

    only_workouts = Dataset(workouts=[Workout(date="2024-01-01", activity="游泳",
                                              duration_min=None, avg_hr=None)])
    ow_an = analyze(only_workouts)
    print(f"[analysis] 只有训练无观测: span={ow_an.span} trends={ow_an.trends} "
          f"workouts={ow_an.workout_summary}")
    print(f"[pitfalls] {check(only_workouts, ow_an)}")
    if ow_an.workout_summary["by_activity"]["游泳"]["minutes"] is not None:
        failures.append("训练缺值应保持 None")

    # ── 排序稳定性 ───────────────────────────────────────
    order = [(f.severity, f.code) for f in findings]
    ranks = {"alert": 0, "warn": 1, "info": 2}
    if order != sorted(order, key=lambda x: (ranks[x[0]], x[1])):
        failures.append(f"排序不稳定：{order}")
    print("=" * 78)
    if failures:
        print("FAILED:")
        for x in failures:
            print("  -", x)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
