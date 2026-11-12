"""Rate-Test: offene Last mit stufenweise steigender Anfragerate."""
from __future__ import annotations

from app import stats
from app.loadprofiles import PROFILES, request_factory


def estimate(params):
    return 60 + min(len(params["rates"]), 6) * (params["stage_duration_s"] + 30)


async def run(ctx):
    p = ctx.params
    await ctx.warmup(n=p["warmup"])
    make = request_factory(ctx, p["load_profile"])
    rates = sorted(float(x) for x in p["rates"])
    unstable = 0
    for i, rate in enumerate(rates):
        if ctx.cancelled:
            return
        ctx.progress(i / len(rates), f"{rate:g} Anfragen/s")
        stage = f"rate:{rate:g}"
        ctx.stage({"stage": stage, "rate": rate, "event": "start"})

        def mk(n, rng, stage=stage, rate=rate):
            b, m = make(n % 100000, rng)
            return b, {**m, "stage": stage, "rate": rate}
        info = await ctx.open_load(rate, p["stage_duration_s"], mk, burstiness=float(p["burstiness"]))
        recs = [r for r in _stage_recs(ctx, stage)]
        q = stats.queue_growth(recs)
        ok = is_stable(recs, ctx.settings["slo"])
        ctx.stage({"stage": stage, "rate": rate, "event": "end", "sent": info["sent"], "stable": ok, **q})
        unstable = 0 if ok else unstable + 1
        if unstable >= p["stop_after_unstable"]:
            ctx.note(f"Ab {rate:g} Anfragen/s wächst die Warteschlange. Höhere Raten ausgelassen.")
            break
    ctx.progress(1.0, "fertig")


def is_stable(recs, slo):
    """Stabil: Warteschlange wächst nicht, keine Timeouts, Fehlerquote im Ziel."""
    agg = stats.aggregate(recs)
    q = stats.queue_growth(recs)
    return (q["growing"] is False and agg["errors"].get("timeout", 0) == 0
            and (agg["error_rate_pct"] or 0) <= slo["error_rate_pct"])


def _stage_recs(ctx, stage):
    from app import storage
    return [r for r in storage.iter_requests(ctx.dir) if r.get("stage") == stage]


def summarize(recs, stages, cfg, settings):
    slo = settings["slo"]
    by = {}
    for r in recs:
        if str(r.get("stage", "")).startswith("rate:"):
            by.setdefault(float(r["rate"]), []).append(r)
    rows, best_stable, best_slo = [], None, None
    p50, p95, done = [], [], []
    dur = cfg["params"]["stage_duration_s"]
    for rate in sorted(by):
        rs = by[rate]
        agg = stats.aggregate(rs, slo)
        q = stats.queue_growth(rs)
        stable = is_stable(rs, slo)
        if stable:
            best_stable = rate
            if agg["meets_slo"]:
                best_slo = rate
        p50.append([rate, agg["ttft_ms"]["p50"]])
        p95.append([rate, agg["ttft_ms"]["p95"]])
        done.append([rate, round(agg["ok"] / dur, 3)])
        lag = stats.pct([r.get("send_lag_ms") for r in rs if r.get("send_lag_ms") is not None], 95)
        rows.append([f"{rate:g}", agg["requests"], f"{agg['ok'] / dur:.2f}".replace(".", ","),
                     stats.fmt_ms(q["first_p50"]), stats.fmt_ms(q["last_p50"]),
                     "wächst" if q["growing"] else ("stabil" if q["growing"] is False else "–"),
                     stats.fmt_ms(agg["ttft_ms"]["p95"]), stats.fmt_ms(agg["itl_ms"]["p95"]),
                     stats.fmt_num(agg["output_tps"]), stats.fmt_pct(agg["error_rate_pct"]),
                     "✔" if agg["meets_slo"] else "✘", stats.fmt_ms(lag)])
    prof = PROFILES.get(cfg["params"]["load_profile"], {}).get("name")
    if best_slo is not None:
        head = f"{prof}: bis {stats.fmt_num(best_slo, 1)} Anfragen/s innerhalb der Zielwerte"
        if best_stable and best_stable > best_slo:
            head += f", stabile Warteschlange bis {stats.fmt_num(best_stable, 1)} Anfragen/s"
        head += "."
        verdict = "gruen"
    elif best_stable is not None:
        head = f"{prof}: Warteschlange stabil bis {stats.fmt_num(best_stable, 1)} Anfragen/s, die Zielwerte hält aber keine Stufe."
        verdict = "gelb"
    else:
        head = f"{prof}: Schon die niedrigste Rate lässt die Warteschlange wachsen."
        verdict = "rot"
    return {
        "verdict": verdict, "headline": head,
        "kpis": {}, "raw": {"best_stable_rps": best_stable, "best_slo_rps": best_slo},
        "charts": [
            {"id": "ttft", "title": "TTFT je Anfragerate", "type": "line", "x": {"name": "Anfragen/s", "log": True},
             "y": {"name": "TTFT (ms)", "log": True},
             "series": [{"name": "p50", "data": p50}, {"name": "p95", "data": p95}],
             "markLines": [{"y": slo["ttft_p95_ms"], "name": "Ziel"}]},
            {"id": "done", "title": "Erledigte gegen angebotene Rate", "type": "line",
             "x": {"name": "angeboten (Anfragen/s)", "log": True}, "y": {"name": "erledigt (Anfragen/s)", "log": True},
             "series": [{"name": "erledigt", "data": done},
                        {"name": "Ideal", "dashed": True, "data": [[r, r] for r in sorted(by)]}]},
        ],
        "tables": [{"title": "Stufen", "columns": ["Rate", "Anfragen", "erledigt/s", "TTFT p50 Anfang",
                                                   "TTFT p50 Ende", "Warteschlange", "TTFT p95", "ITL p95",
                                                   "Output Tok/s", "Fehler", "Ziel", "Sende-Verzug p95"],
                    "rows": rows}],
    }
