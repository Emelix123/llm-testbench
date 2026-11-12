"""Dauerlast: Stunden bei 75 % der Kapazität, Drift je Stunde."""
from __future__ import annotations

from app import stats
from app.loadprofiles import request_factory


def estimate(params):
    return 60 + params["duration_h"] * 3600


async def run(ctx):
    p = ctx.params
    ctx.compact_itl = True
    cap = ctx.capacity_or_param(p["load_profile"], p["capacity"])
    c = max(1, round(cap * p["load_pct"] / 100))
    ctx.extra_config["concurrency"] = c
    await ctx.warmup(n=10)
    make = request_factory(ctx, p["load_profile"])
    dur = p["duration_h"] * 3600

    def mk(w, rng):
        b, m = make(w, rng)
        return b, {**m, "stage": "dauer", "concurrency": c}
    await ctx.variable_load([{"duration_s": dur, "concurrency": c, "phase": "dauer"}], mk)
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    slo = settings["slo"]
    rs = [r for r in recs if r.get("stage") == "dauer"]
    if not rs:
        return {"verdict": "rot", "headline": "Keine Messwerte.", "kpis": {}, "charts": [], "tables": []}
    w = cfg["params"]["window_min"] * 60
    wins = stats.windows(rs, w)
    rows, s_ttft, s_tps, s_err, pt_ttft, pt_tps = [], [], [], [], [], []
    for t, wr in wins:
        a = stats.aggregate(wr)
        x = round(t / 60, 1)
        s_ttft.append([x, a["ttft_ms"]["p95"]])
        s_tps.append([x, a["output_tps"]])
        s_err.append([x, a["error_rate_pct"]])
        pt_ttft.append((t, a["ttft_ms"]["p95"]))
        pt_tps.append((t, a["output_tps"]))
        rows.append([stats.fmt_num(x, 1), a["requests"], stats.fmt_ms(a["ttft_ms"]["p50"]), stats.fmt_ms(a["ttft_ms"]["p95"]),
                     stats.fmt_ms(a["itl_ms"]["p95"]), stats.fmt_num(a["output_tps"]), stats.fmt_pct(a["error_rate_pct"]),
                     a["stalled_requests"]])
    # Letztes Fenster ist meist unvollständig: für die Drift weglassen
    if len(pt_tps) > 3:
        pt_tps, pt_ttft = pt_tps[:-1], pt_ttft[:-1]
    base_tps = stats.pct([y for _, y in pt_tps], 50) or 1
    base_ttft = stats.pct([y for _, y in pt_ttft], 50) or 1
    tot = stats.aggregate(rs, slo)
    hours = (max(r["t_send"] for r in rs) - min(r["t_send"] for r in rs)) / 3.6e6
    if hours >= 0.25:
        d_tps = stats.slope_per_hour(pt_tps)
        d_ttft = stats.slope_per_hour(pt_ttft)
    else:
        d_tps = d_ttft = None  # zu kurz, um eine Drift pro Stunde seriös anzugeben
    def sg(x):
        return "–" if x is None else ("+" if x >= 0 else "−") + stats.fmt_num(abs(x), 1)
    d_tps_pct = None if d_tps is None else 100 * d_tps / base_tps
    d_ttft_pct = None if d_ttft is None else 100 * d_ttft / base_ttft
    span = f"{stats.fmt_num(hours, 1)} h" if hours >= 1 else f"{stats.fmt_num(hours * 60, 0)} min"
    if hours >= 0.25:
        head = (f"Über {span} bei {cfg.get('concurrency')} parallel: Tokens/s {sg(d_tps_pct)} %/h, "
                f"TTFT p95 {sg(d_ttft_pct)} %/h, Fehler {stats.fmt_pct(tot['error_rate_pct'], 2)}.")
    else:
        head = (f"Über {span} bei {cfg.get('concurrency')} parallel: Fehler {stats.fmt_pct(tot['error_rate_pct'], 2)}, "
                f"Output {stats.fmt_num(tot['output_tps'])} Tokens/s. Für eine Drift pro Stunde ist der Lauf zu kurz (mind. 15 min).")
    bad_err = (tot["error_rate_pct"] or 0) > slo["error_rate_pct"]
    if bad_err or (d_tps_pct is not None and d_tps_pct < -10):
        verdict = "rot"
    elif (d_tps_pct is not None and d_tps_pct < -5) or (d_ttft_pct is not None and d_ttft_pct > 20):
        verdict = "gelb"
        head += " Sinkende Tokens/s können auf Hitze oder Drosselung hindeuten."
    else:
        verdict = "gruen"
    return {
        "verdict": verdict, "headline": head, "kpis": {},
        "raw": {"drift_tps_pct_per_h": d_tps_pct, "drift_ttft_pct_per_h": d_ttft_pct, "error_rate_pct": tot["error_rate_pct"]},
        "charts": [
            {"id": "tps", "title": "Output-Tokens/s über die Zeit", "type": "line", "x": {"name": "Minute"},
             "y": {"name": "Tokens/s"}, "series": [{"name": "Tokens/s", "data": s_tps}]},
            {"id": "ttft", "title": "TTFT p95 über die Zeit", "type": "line", "x": {"name": "Minute"},
             "y": {"name": "ms"}, "series": [{"name": "TTFT p95", "data": s_ttft}],
             "markLines": [{"y": slo["ttft_p95_ms"], "name": "Ziel"}]},
            {"id": "err", "title": "Fehlerquote über die Zeit", "type": "line", "x": {"name": "Minute"},
             "y": {"name": "%"}, "series": [{"name": "Fehler", "data": s_err}]},
        ],
        "tables": [{"title": f"Fenster à {cfg['params']['window_min']:g} min",
                    "columns": ["ab Minute", "Anfragen", "TTFT p50", "TTFT p95", "ITL p95", "Output Tok/s", "Fehler", "Hänger"],
                    "rows": rows}],
    }
