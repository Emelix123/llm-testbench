"""Kosten je Token-Art: Prefill- und Decode-Durchsatz getrennt messen."""
from __future__ import annotations

from app import stats, storage
from app.loadprofiles import PROFILES


def estimate(params):
    return 60 + 2 * (params["stage_duration_s"] + 20)


async def run(ctx):
    p = ctx.params
    await ctx.warmup(n=10)
    ctx.progress(0.05, "Prefill (max_tokens = 1)")
    L = p["prefill_input_tokens"]
    await ctx.closed_load(p["prefill_concurrency"],
                          lambda w, rng: (ctx.chat_body(ctx.factory.make(L, rng), 1, force=False),
                                          {"stage": "prefill", "context_len": L}),
                          duration_s=p["stage_duration_s"])
    if ctx.cancelled:
        return
    c = p["decode_concurrency"] or max(8, ctx.capacity_or_param("generierung", None))
    ctx.progress(0.5, f"Decode ({c} parallel)")
    O = p["decode_output_tokens"]
    await ctx.closed_load(c, lambda w, rng: (ctx.chat_body(ctx.factory.make(100, rng), O),
                                             {"stage": "decode", "context_len": 100}),
                          duration_s=p["stage_duration_s"])
    # Cache-Faktor aus dem letzten Cache-Nutzen-Lauf dieses Endpunkts
    for r in storage.list_runs():
        if r.get("test") == "cache_benefit" and r.get("profile_id") == ctx.profile.get("id"):
            f = (storage.read_json(storage.RUNS / r["id"] / "summary.json", {}).get("raw") or {}).get("cache_cost_factor")
            if f:
                ctx.extra_config["cache_cost_factor"] = {"value": f, "run_id": r["id"]}
                break
    ctx.progress(1.0, "fertig")


def prices(input_tps, output_tps, cache_factor, cost):
    eur_s = stats.eur_per_hour(cost) / 3600 / (cost["utilization_pct"] / 100)
    p_in = eur_s / input_tps * 1e6 if input_tps else None
    p_out = eur_s / output_tps * 1e6 if output_tps else None
    p_cache = p_in * cache_factor if p_in is not None and cache_factor else None
    mix = {}
    for k, v in PROFILES.items():
        lo, hi = v["input"]
        n_in = (lo + (hi or lo * 2)) / 2
        n_out = v["output"]
        if p_in is not None and p_out is not None:
            # Maschinenzeit je Anfrage = Prefill-Zeit + Decode-Zeit (je bei Volllast)
            mix[k] = (n_in * p_in + n_out * p_out) / (n_in + n_out)
    return p_in, p_out, p_cache, mix


def summarize(recs, stages, cfg, settings):
    cost = settings["cost"]
    pre = [r for r in recs if r.get("stage") == "prefill"]
    dec = [r for r in recs if r.get("stage") == "decode"]
    a_pre = stats.aggregate(pre) if pre else None
    a_dec = stats.aggregate(dec) if dec else None
    in_tps = a_pre["input_tps"] if a_pre else None
    out_tps = a_dec["output_tps"] if a_dec else None
    cf = (cfg.get("cache_cost_factor") or {}).get("value")
    cf_note = "aus dem Test Cache-Nutzen" if cf else "Annahme 10 % (Test Cache-Nutzen noch nicht gelaufen)"
    cf = cf or 0.10
    p_in, p_out, p_cache, mix = prices(in_tps, out_tps, cf, cost)
    if p_in is None or p_out is None:
        return {"verdict": "rot", "headline": "Durchsatz nicht messbar.", "kpis": {}, "charts": [], "tables": []}
    head = (f"€ pro 1 Mio. Tokens: Input {stats.fmt_num(p_in, 3)} €, Output {stats.fmt_num(p_out, 2)} €, "
            f"Cache {stats.fmt_num(p_cache, 3)} € – bei {stats.fmt_num(cost['utilization_pct'])} % Auslastung.")
    rows = [[PROFILES[k]["name"], f"{stats.fmt_num(v, 3)} €"] for k, v in mix.items()]
    return {
        "verdict": "gruen", "headline": head,
        "kpis": {"eur_split": {"input": round(p_in, 4), "output": round(p_out, 4), "cache": round(p_cache, 4)}},
        "raw": {"input_tps": in_tps, "output_tps": out_tps, "cache_cost_factor": cf},
        "charts": [],
        "tables": [
            {"title": "Gemessen", "columns": ["Messung", "Durchsatz", "Anfragen", "Fehler"],
             "rows": [["Prefill (Input-Tokens/s)", stats.fmt_num(in_tps), a_pre["requests"], stats.fmt_pct(a_pre["error_rate_pct"])],
                      ["Decode (Output-Tokens/s)", stats.fmt_num(out_tps), a_dec["requests"], stats.fmt_pct(a_dec["error_rate_pct"])],
                      ["Cache-Faktor", f"{stats.fmt_num(cf * 100, 0)} % der Input-Kosten ({cf_note})", "", ""]]},
            {"title": "€ pro 1 Mio. Tokens", "columns": ["Token-Art", "Preis"],
             "rows": [["Input", f"{stats.fmt_num(p_in, 3)} €"], ["Output", f"{stats.fmt_num(p_out, 2)} €"],
                      ["Cache-Treffer", f"{stats.fmt_num(p_cache, 3)} €"]]},
            {"title": "Mischpreis je Lastprofil (€ / 1 Mio. Tokens)", "columns": ["Lastprofil", "Mischpreis"], "rows": rows},
        ],
        "cost_note": (f"Maschine {stats.fmt_num(cost['machine_eur_per_h'], 2)} €/h + {stats.fmt_num(cost['power_kw'], 2)} kW × "
                      f"{stats.fmt_num(cost['power_eur_per_kwh'], 2)} €/kWh, Auslastung {stats.fmt_num(cost['utilization_pct'])} %. "
                      "Mischpreis: Maschinenzeit für Prefill und Decode je Anfrage, jeweils bei Volllast gemessen."),
    }
