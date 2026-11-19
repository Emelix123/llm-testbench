"""Leistungskosten der Isolation – einheitliche Mehrmandanten-Last für den Konfigurationsvergleich.

Jede Schutzmaßnahme der Arbeit (cache_salt je Mandant, Prefix-Caching aus, Grenzen je Mandant,
Wasserzeichen, dedizierte Instanzen) kostet Leistung. Damit sich die Konfigurationen K0–K6 fair
vergleichen lassen, läuft hier immer dieselbe Last:

  System-Prompt = [gemeinsame Firmenvorlage (für ALLE Mandanten gleich)] + [Prompt des Mandanten]
  Nutzerfrage   = eindeutig je Anfrage

Mehrere Mandanten (eigene API-Keys: Haupt-, Angreifer- und Last-Key des Profils) senden parallel,
stufenweise mehr Anfragen. Gemessen je Laststufe: TTFT, TPOT, Ende-zu-Ende, Durchsatz, Cache-
Trefferquote (cached_tokens / prompt_tokens) und Ablehnungen (429/503).

Erwartung:
  K0  geteilter Cache   → gemeinsamer Präfix mandantenübergreifend im Cache (schnellste TTFT)
  K1  cache_salt        → Präfix nur noch je Mandant im Cache (je Mandant einmal Prefill, etwas mehr KV)
  K2  ohne Caching      → jeder Prefill komplett (TTFT deutlich höher)
  K3  Grenzen           → über max_parallel_requests 429 statt Warteschlange
  K6  Wasserzeichen     → Sampling-Aufwand in TPOT/Durchsatz
Die Ampel bewertet nur Fehler und Zielwerte; das Sicherheitsurteil liefern die anderen Tests.
"""
from __future__ import annotations

import random

from app import stats

ROLES = ("opfer", "angreifer", "last")


def estimate(params):
    return 30 + len(params["levels"]) * (params["duration_s"] + 5)


def _tenants(ctx) -> list[str]:
    want = max(1, min(3, int(ctx.params.get("tenants") or 3)))
    have = [r for r in ROLES if ctx.has_tenant(r)]
    return have[:want]


def _body(ctx, system: str, user: str) -> dict:
    p = ctx.params
    body = ctx.chat_body(user, p["output_tokens"], temperature=float(p["temperature"]))
    body["messages"] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return body


async def run(ctx):
    p = ctx.params
    roles = _tenants(ctx)
    if len(roles) < int(p.get("tenants") or 3):
        ctx.note(f"Nur {len(roles)} Mandanten-Key(s) im Profil – gemessen wird mit {len(roles)} Mandant(en). "
                 "Für den Konfigurationsvergleich Angreifer- und Last-Key im Profil hinterlegen "
                 "(deploy/scenario.sh profile).")
    fixed = random.Random(4242)   # gleiche Vorlage in jedem Lauf und jeder Konfiguration
    shared = "Firmenvorlage: " + ctx.factory._filler(int(p["shared_prefix_tokens"]), fixed)
    tenant_sys = {r: f"\n\nRegeln für {r}: " + ctx.factory._filler(int(p["tenant_prefix_tokens"]), fixed)
                  for r in roles}
    ctx.extra_config["workload"] = {"tenants": roles, "shared_prefix_tokens": p["shared_prefix_tokens"],
                                    "tenant_prefix_tokens": p["tenant_prefix_tokens"],
                                    "user_tokens": p["user_tokens"], "output_tokens": p["output_tokens"]}

    await ctx.warmup(n=6, output_tokens=32, concurrency=3)

    def make(role: str, rng: random.Random) -> dict:
        user = ctx.factory.unique_prefix(rng) + ctx.factory._filler(int(p["user_tokens"]), rng)
        return _body(ctx, shared + tenant_sys[role], user)

    # Je Mandant einmal vorwärmen (nicht gewertet) – gemessen wird der eingeschwungene Zustand
    for r in roles:
        await ctx.send(make(r, ctx.rng), {"stage": "prime"}, record=False, tenant=r)

    levels = [int(x) for x in p["levels"] if int(x) > 0]
    for li, c in enumerate(levels):
        if ctx.cancelled:
            return
        ctx.stage({"level": c, "event": "start"})
        ctx.progress(li / len(levels), f"Laststufe {c} parallel ({len(roles)} Mandanten)")

        def make_request(i, wrng, c=c):
            role = roles[i % len(roles)]
            return make(role, wrng), {"stage": "main", "level": c, "tenant": role}

        await ctx.closed_load(c, make_request, duration_s=float(p["duration_s"]))
        ctx.stage({"level": c, "event": "end"})
    ctx.progress(1.0, "fertig")


# ---------------------------------------------------------------------- Auswertung

def _level_metrics(rows: list[dict], slo: dict) -> dict:
    agg = stats.aggregate(rows, slo)
    ok = [r for r in rows if stats.is_ok(r)]
    prompt = sum(r.get("prompt_tokens") or 0 for r in ok)
    cached = [r.get("cached_tokens") for r in ok if r.get("cached_tokens") is not None]
    rejected = sum(1 for r in rows if r.get("status") in (429, 503))
    agg["cache_hit_pct"] = round(100 * sum(cached) / prompt, 1) if cached and prompt else None
    agg["rejected_pct"] = round(100 * rejected / len(rows), 1) if rows else None
    agg["other_error_pct"] = round(100 * (len(rows) - len(ok) - rejected) / len(rows), 1) if rows else None
    return agg


def summarize(recs, stages, cfg, settings):
    p = cfg["params"]
    slo = settings["slo"]
    main = [r for r in recs if r.get("stage") == "main"]
    if not main:
        return {"verdict": "rot", "headline": "Keine Messungen – lief der Endpunkt?", "kpis": {}, "charts": [], "tables": []}
    levels = sorted({r["level"] for r in main})
    per = {c: _level_metrics([r for r in main if r["level"] == c], slo) for c in levels}
    ref = int(p.get("reference_level") or 0)
    if ref not in per:
        ref = min(levels, key=lambda c: abs(c - ref)) if ref else levels[len(levels) // 2]
    R = per[ref]
    tenants = (cfg.get("workload") or {}).get("tenants") or sorted({r.get("tenant") for r in main if r.get("tenant")})
    sc = cfg.get("deploy_scenario") or {}

    # Mandanten bei der Bezugsstufe
    trows = []
    tps = []
    for t in tenants:
        tr = [r for r in main if r["level"] == ref and r.get("tenant") == t]
        if not tr:
            continue
        a = _level_metrics(tr, slo)
        tps.append(a.get("output_tps") or 0)
        trows.append([t, a["requests"], stats.fmt_ms(a["ttft_ms"]["p50"]), stats.fmt_ms(a["tpot_ms"]["p50"]),
                      stats.fmt_num(a.get("output_tps"), 1), stats.fmt_pct(a.get("cache_hit_pct"), 1),
                      stats.fmt_pct(a.get("rejected_pct"), 1)])
    jain_t = stats.jain_index(tps) if len(tps) > 1 else None

    peak = max((m.get("output_tps") or 0) for m in per.values())
    matrix = {
        "ttft_p50_ms": R["ttft_ms"]["p50"], "ttft_p95_ms": R["ttft_ms"]["p95"],
        "tpot_p50_ms": R["tpot_ms"]["p50"], "tpot_p95_ms": R["tpot_ms"]["p95"],
        "e2e_p95_ms": R["e2e_ms"]["p95"], "output_tps": R.get("output_tps"), "rps": R.get("rps"),
        "peak_output_tps": peak, "cache_hit_pct": R.get("cache_hit_pct"),
        "error_rate_pct": R.get("error_rate_pct"), "rejected_pct": R.get("rejected_pct"),
        "reference_level": ref,
    }

    other_err = R.get("other_error_pct") or 0
    if other_err > 5:
        verdict = "rot"
    elif (R.get("rejected_pct") or 0) > 0 or not R.get("meets_slo"):
        verdict = "gelb"
    else:
        verdict = "gruen"
    head = (f"{'Szenario ' + sc['id'] + ': ' if sc.get('id') else ''}Bei {ref} parallelen Anfragen "
            f"({len(tenants)} Mandanten) TTFT p50 {stats.fmt_ms(R['ttft_ms']['p50'])}, TPOT p50 "
            f"{stats.fmt_ms(R['tpot_ms']['p50'])}, {stats.fmt_num(R.get('output_tps'), 0)} Output-Tokens/s, "
            f"Cache-Treffer {stats.fmt_pct(R.get('cache_hit_pct'), 0) if R.get('cache_hit_pct') is not None else 'nicht geliefert'}.")
    if R.get("rejected_pct"):
        head += f" {stats.fmt_pct(R['rejected_pct'], 1)} abgewiesen (429/503 – Grenze je Mandant)."
    if other_err:
        head += f" {stats.fmt_pct(other_err, 1)} sonstige Fehler."
    head += " Den Vergleich mit den anderen Konfigurationen zeigt die Seite „Konfigurationen“."

    def lrow(c, m):
        return [c, m["requests"], stats.fmt_ms(m["ttft_ms"]["p50"]), stats.fmt_ms(m["ttft_ms"]["p95"]),
                stats.fmt_ms(m["tpot_ms"]["p50"]), stats.fmt_ms(m["tpot_ms"]["p95"]), stats.fmt_ms(m["e2e_ms"]["p95"]),
                stats.fmt_num(m.get("output_tps"), 1), stats.fmt_pct(m.get("cache_hit_pct"), 1),
                stats.fmt_pct(m.get("rejected_pct"), 1), stats.fmt_pct(m.get("other_error_pct"), 1)]

    tables = [
        {"title": "Je Laststufe", "columns": ["parallel", "Anfragen", "TTFT p50", "TTFT p95", "TPOT p50", "TPOT p95",
                                               "E2E p95", "Output-Tokens/s", "Cache-Treffer", "abgewiesen", "Fehler"],
         "rows": [lrow(c, per[c]) for c in levels]},
        {"title": f"Je Mandant bei {ref} parallel" + (f" (Jain-Index Durchsatz {stats.fmt_num(jain_t, 3)})" if jain_t else ""),
         "columns": ["Mandant", "Anfragen", "TTFT p50", "TPOT p50", "Output-Tokens/s", "Cache-Treffer", "abgewiesen"],
         "rows": trows},
        {"title": "Last", "columns": ["Parameter", "Wert"],
         "rows": [["Gemeinsamer System-Prompt", f"{p['shared_prefix_tokens']} Tokens (alle Mandanten gleich)"],
                  ["System-Prompt je Mandant", f"{p['tenant_prefix_tokens']} Tokens"],
                  ["Nutzerfrage", f"{p['user_tokens']} Tokens, eindeutig"],
                  ["Output", f"{p['output_tokens']} Tokens, temperature {p['temperature']}"],
                  ["Mandanten", ", ".join(tenants)],
                  ["Szenario", f"{sc.get('id')} – {sc.get('name')}" if sc.get("id") else "– (nicht lokal erkannt)"]]},
    ]
    ser = lambda key, sub: [[c, per[c][key][sub]] for c in levels if per[c][key][sub] is not None]  # noqa: E731
    charts = [
        {"id": "ttft_levels", "title": "TTFT über die Last", "type": "line", "x": {"name": "parallele Anfragen"},
         "y": {"name": "ms"}, "series": [{"name": "TTFT p50", "data": ser("ttft_ms", "p50")},
                                         {"name": "TTFT p95", "data": ser("ttft_ms", "p95")}],
         "markLines": [{"y": slo["ttft_p95_ms"], "name": "Ziel p95"}]},
        {"id": "tpot_levels", "title": "TPOT über die Last", "type": "line", "x": {"name": "parallele Anfragen"},
         "y": {"name": "ms je Token"}, "series": [{"name": "TPOT p50", "data": ser("tpot_ms", "p50")},
                                                  {"name": "TPOT p95", "data": ser("tpot_ms", "p95")}]},
        {"id": "tps_levels", "title": "Durchsatz über die Last", "type": "line", "x": {"name": "parallele Anfragen"},
         "y": {"name": "Output-Tokens/s"},
         "series": [{"name": "Output-Tokens/s", "data": [[c, per[c].get("output_tps")] for c in levels]}]},
    ]
    if any(per[c].get("cache_hit_pct") is not None for c in levels):
        charts.append({"id": "cache_levels", "title": "Cache-Trefferquote über die Last", "type": "line",
                       "x": {"name": "parallele Anfragen"}, "y": {"name": "% der Prompt-Tokens"},
                       "series": [{"name": "cached_tokens / prompt_tokens",
                                   "data": [[c, per[c].get("cache_hit_pct")] for c in levels
                                            if per[c].get("cache_hit_pct") is not None]}]})
    return {
        "verdict": verdict, "headline": head,
        "kpis": {"output_tps": {"value": R.get("output_tps"), "unit": f"Output-Tokens/s bei {ref} parallel",
                                "text": f"TTFT p50 {stats.fmt_ms(R['ttft_ms']['p50'])}, TPOT p50 {stats.fmt_ms(R['tpot_ms']['p50'])}"}},
        "matrix": matrix, "perf": matrix,
        "charts": charts, "tables": tables,
        "method_note": ("Geschlossene Last je Stufe; Arbeiter i gehört zu Mandant i mod M und sendet mit dessen API-Key. "
                        "Der gemeinsame System-Prompt ist in jedem Lauf und jeder Konfiguration identisch (fester Seed), "
                        "die Nutzerfrage eindeutig. Je Mandant eine ungewertete Anfrage vorweg; gemessen wird der "
                        "eingeschwungene Zustand. Zeiten als Perzentile, Tokens aus usage."),
    }
