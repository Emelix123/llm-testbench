"""Überlast: 200 % der Kapazität. Kontrollierte Ablehnung oder alle langsam?"""
from __future__ import annotations

from app import stats
from app.client import simple_get
from app.loadprofiles import request_factory


def estimate(params):
    return 60 + params["duration_s"] + 20


async def run(ctx):
    p = ctx.params
    ctx.compact_itl = True
    cap = ctx.capacity_or_param(p["load_profile"], p["capacity"])
    c = max(2, round(cap * p["load_pct"] / 100))
    ctx.extra_config.update(capacity=cap, concurrency=c)
    await ctx.warmup(n=10)
    make = request_factory(ctx, p["load_profile"])

    def mk(w, rng):
        b, m = make(w, rng)
        return b, {**m, "stage": "ueberlast"}
    await ctx.variable_load([{"duration_s": p["duration_s"], "concurrency": c, "phase": "ueberlast"}], mk)
    st, _, dur, _, err = await simple_get(ctx.client, ctx.ep, "/v1/models")
    r = await ctx.send(ctx.chat_body("Sag Hallo.", 5, force=False), {"stage": "danach"})
    ctx.extra_config["after"] = {"models_status": st, "chat_ok": r.ok, "ttft_ms": (r.t_first_token - r.t_send) if r.t_first_token else None}
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    slo = settings["slo"]
    rs = [r for r in recs if r.get("stage") == "ueberlast"]
    if not rs:
        return {"verdict": "rot", "headline": "Keine Messwerte.", "kpis": {}, "charts": [], "tables": []}
    a = stats.aggregate(rs, slo)
    rej = [r for r in rs if r.get("error_type") in ("429",) or r.get("status") == 503]
    rej_ms = stats.pct([r["t_end"] - r["t_send"] for r in rej], 95)
    share = 100 * len(rej) / len(rs)
    after = cfg.get("after") or {}
    alive = after.get("chat_ok") and after.get("models_status") == 200
    e = a["errors"]
    if rej and (rej_ms or 0) < 2000:
        mode = "kontrolliert abgelehnt"
    elif (a["ttft_ms"]["p95"] or 0) > slo["ttft_p95_ms"]:
        mode = "alle Anfragen werden langsam (keine Ablehnung)"
    else:
        mode = "noch innerhalb der Zielwerte"
    head = (f"Bei {cfg.get('concurrency')} parallel ({cfg['params']['load_pct']} %): {mode}. "
            f"Abgelehnt {stats.fmt_pct(share)} (p95 nach {stats.fmt_ms(rej_ms)}), angenommene TTFT p95 "
            f"{stats.fmt_ms(a['ttft_ms']['p95'])}, Timeouts {e.get('timeout', 0)}, 5xx {e.get('5xx', 0)}. "
            f"Server danach {'erreichbar' if alive else 'NICHT erreichbar'}.")
    if not alive or e.get("5xx", 0) or e.get("timeout", 0):
        verdict = "rot"
    elif mode.startswith("alle"):
        verdict = "gelb"
    else:
        verdict = "gruen"
    wins = stats.windows(rs, 15)
    s_t = [[t, stats.aggregate(w)["ttft_ms"]["p95"]] for t, w in wins]
    s_r = [[t, round(100 * sum(1 for r in w if r.get("error_type") == "429" or r.get("status") == 503) / len(w), 1)] for t, w in wins]
    return {
        "verdict": verdict, "headline": head, "kpis": {}, "raw": {"mode": mode, "rejected_pct": share, "alive": alive},
        "charts": [{"id": "ttft", "title": "TTFT p95 (angenommene Anfragen)", "type": "line", "x": {"name": "Sekunde"},
                    "y": {"name": "ms"}, "series": [{"name": "TTFT p95", "data": s_t}],
                    "markLines": [{"y": slo["ttft_p95_ms"], "name": "Ziel"}]},
                   {"id": "rej", "title": "Anteil abgelehnter Anfragen", "type": "line", "x": {"name": "Sekunde"},
                    "y": {"name": "%"}, "series": [{"name": "abgelehnt", "data": s_r}]}],
        "tables": [{"title": "Überlast", "columns": ["Anfragen", "angenommen", "abgelehnt (429/503)", "Timeout", "5xx",
                                                     "TTFT p95", "E2E p95", "Server danach"],
                    "rows": [[a["requests"], a["ok"], len(rej), e.get("timeout", 0), e.get("5xx", 0),
                              stats.fmt_ms(a["ttft_ms"]["p95"]), stats.fmt_ms(a["e2e_ms"]["p95"]),
                              "erreichbar" if alive else "nicht erreichbar"]]}],
    }
