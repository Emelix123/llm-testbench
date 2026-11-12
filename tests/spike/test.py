"""Lastspitze: 0 → 150 % für 2 min → 50 %, Erholungszeit."""
from __future__ import annotations

from app import stats
from app.loadprofiles import request_factory


def estimate(params):
    return 60 + params["spike_s"] + params["after_s"]


async def run(ctx):
    p = ctx.params
    ctx.compact_itl = True
    cap = ctx.capacity_or_param(p["load_profile"], p["capacity"])
    hi, lo = max(1, round(cap * p["spike_pct"] / 100)), max(1, round(cap * p["after_pct"] / 100))
    ctx.extra_config.update(capacity=cap, spike_concurrency=hi, after_concurrency=lo)
    await ctx.warmup(n=10)
    make = request_factory(ctx, p["load_profile"])

    def mk(w, rng):
        b, m = make(w, rng)
        return b, {**m, "stage": "spike"}
    await ctx.variable_load([{"duration_s": p["spike_s"], "concurrency": hi, "phase": "spitze"},
                             {"duration_s": p["after_s"], "concurrency": lo, "phase": "danach"}], mk)
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    slo = settings["slo"]
    rs = [r for r in recs if r.get("stage") == "spike"]
    sp = [r for r in rs if r.get("phase") == "spitze"]
    af = [r for r in rs if r.get("phase") == "danach"]
    if not sp:
        return {"verdict": "rot", "headline": "Keine Messwerte.", "kpis": {}, "charts": [], "tables": []}
    a_sp, a_af = stats.aggregate(sp), stats.aggregate(af) if af else None
    # Referenz: letzte 60 s der Phase "danach"
    t_drop = min((r["t_send"] for r in af), default=None)
    t_end = max((r["t_send"] for r in af), default=None)
    ref = [r for r in af if t_end is not None and r["t_send"] >= t_end - 60_000]
    ref_p95 = stats.pct([stats.ttft(r) for r in ref if stats.is_ok(r)], 95)
    recovery_s = None
    series = []
    if t_drop is not None:
        wins = stats.windows(rs, 15)
        t0 = min(r["t_send"] for r in rs)
        for t, wr in wins:
            p95 = stats.pct([stats.ttft(r) for r in wr if stats.is_ok(r)], 95)
            series.append([round(t, 0), None if p95 is None else round(p95, 1)])
        thr = max((ref_p95 or 0) * 1.2, (ref_p95 or 0) + 200)
        after_wins = [(t0 + t * 1000, v) for t, v in [(x, y) for x, y in series] if t0 + t * 1000 >= t_drop]
        for i, (ts, v) in enumerate(after_wins):
            if all(vv is not None and vv <= thr for _, vv in after_wins[i:]):
                recovery_s = (ts - t_drop) / 1000
                break
    e = a_sp["errors"]
    n429, nto = e.get("429", 0), e.get("timeout", 0)
    head = (f"Spitze {cfg.get('spike_concurrency')} parallel: TTFT p95 {stats.fmt_ms(a_sp['ttft_ms']['p95'])}, "
            f"{n429}× 429, {nto}× Timeout, {e.get('5xx', 0)}× 5xx. ")
    head += (f"Erholung auf Normalniveau nach ≈ {stats.fmt_num(recovery_s)} s." if recovery_s is not None
             else "Keine Erholung im Beobachtungszeitraum.")
    if recovery_s is None or e.get("5xx", 0) or nto:
        verdict = "rot" if (nto or e.get("5xx", 0)) else "gelb"
    else:
        verdict = "gruen" if recovery_s <= 60 else "gelb"
    rows = [["Spitze", a_sp["requests"], stats.fmt_ms(a_sp["ttft_ms"]["p50"]), stats.fmt_ms(a_sp["ttft_ms"]["p95"]),
             stats.fmt_num(a_sp["output_tps"]), n429, nto, e.get("5xx", 0)]]
    if a_af:
        rows.append(["danach", a_af["requests"], stats.fmt_ms(a_af["ttft_ms"]["p50"]), stats.fmt_ms(a_af["ttft_ms"]["p95"]),
                     stats.fmt_num(a_af["output_tps"]), a_af["errors"].get("429", 0), a_af["errors"].get("timeout", 0),
                     a_af["errors"].get("5xx", 0)])
    return {
        "verdict": verdict, "headline": head, "kpis": {},
        "raw": {"recovery_s": recovery_s, "n429": n429, "timeouts": nto},
        "charts": [{"id": "ttft", "title": "TTFT p95 in 15-s-Fenstern (Spitze, dann 50 %)", "type": "line",
                    "x": {"name": "Sekunde"}, "y": {"name": "ms", "log": True}, "series": [{"name": "TTFT p95", "data": series}],
                    "markLines": [{"y": slo["ttft_p95_ms"], "name": "Ziel"}]}],
        "tables": [{"title": "Phasen", "columns": ["Phase", "Anfragen", "TTFT p50", "TTFT p95", "Output Tok/s", "429", "Timeout", "5xx"],
                    "rows": rows}],
    }
