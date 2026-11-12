"""Kapazitäts-Sweep (geschlossene Last) je Lastprofil."""
from __future__ import annotations

import time

from app import stats
from app.loadprofiles import PROFILES, request_factory

_TYPICAL_STAGES = 8  # Annahme für die Dauer-Schätzung


def estimate(params):
    n = min(len(params["levels"]), _TYPICAL_STAGES)
    return 60 + len(params["load_profiles"]) * (n * (params["stage_duration_s"] + 20) + 30)


async def run(ctx):
    p = ctx.params
    profs = [x for x in p["load_profiles"] if x in PROFILES]
    levels = sorted(int(x) for x in p["levels"])
    await ctx.warmup(n=p["warmup"])
    n_total = len(profs) * len(levels)
    done = 0
    for prof in profs:
        make = request_factory(ctx, prof)
        best, flat = 0.0, 0
        for lvl in levels:
            if ctx.cancelled:
                return
            ctx.progress(done / max(1, n_total), f"{PROFILES[prof]['name']}: Parallelität {lvl}")
            stage = f"{prof}:{lvl}"
            ctx.stage({"stage": stage, "profile": prof, "concurrency": lvl, "event": "start"})
            tok = {"out": 0}

            def on_done(r, tok=tok):
                tok["out"] += r.completion_tokens or 0

            def make_staged(w, rng, stage=stage, lvl=lvl):
                body, meta = make(w, rng)
                return body, {**meta, "stage": stage, "concurrency": lvl}

            t0 = time.monotonic()
            await ctx.closed_load(lvl, make_staged, duration_s=p["stage_duration_s"], on_done=on_done)
            dt = max(1e-3, time.monotonic() - t0)
            tps = tok["out"] / dt
            ctx.stage({"stage": stage, "profile": prof, "concurrency": lvl, "event": "end",
                       "output_tps": round(tps, 1)})
            done += 1
            if tps < best * (1 + p["saturation_gain_pct"] / 100):
                flat += 1
            else:
                flat = 0
            best = max(best, tps)
            if flat >= p["flat_stages"] and lvl > 1:
                ctx.note(f"{PROFILES[prof]['name']}: Durchsatz steigt ab Parallelität {lvl} nicht mehr "
                         f"(< {p['saturation_gain_pct']} %). Weitere Stufen ausgelassen.")
                done += len([x for x in levels if x > lvl])
                break
    ctx.progress(1.0, "fertig")


def _stage_rows(recs):
    by = {}
    for r in recs:
        st = r.get("stage")
        if st and ":" in str(st):
            by.setdefault(st, []).append(r)
    return by


def summarize(recs, stages, cfg, settings):
    slo, cost = settings["slo"], settings["cost"]
    by = _stage_rows(recs)
    profs = []
    for st in by:
        pk = st.split(":")[0]
        if pk not in profs:
            profs.append(pk)
    results, tables, s_tput, s_curve, s_user = {}, [], [], [], []
    for pk in profs:
        lvls = sorted(int(s.split(":")[1]) for s in by if s.split(":")[0] == pk)
        rows, best_ok, pts_tput, pts_curve, pts_user = [], None, [], [], []
        for lvl in lvls:
            agg = stats.aggregate(by[f"{pk}:{lvl}"], slo)
            agg["concurrency"] = lvl
            if agg["meets_slo"]:
                best_ok = agg
            pts_tput.append([lvl, agg["output_tps"]])
            pts_curve.append([agg["output_tps"], agg["ttft_ms"]["p95"]])
            pts_user.append([agg["output_tps"], agg["tokens_per_s_user_p50"]])
            rows.append([lvl, agg["requests"], stats.fmt_ms(agg["ttft_ms"]["p50"]), stats.fmt_ms(agg["ttft_ms"]["p95"]),
                         stats.fmt_ms(agg["itl_ms"]["p95"]), stats.fmt_ms(agg["e2e_ms"]["p50"]),
                         stats.fmt_num(agg["tokens_per_s_user_p50"], 1), stats.fmt_num(agg["output_tps"]),
                         stats.fmt_num(agg["input_tps"]), stats.fmt_pct(agg["goodput_pct"]), stats.fmt_pct(agg["error_rate_pct"]),
                         agg["stalled_requests"],
                         "✔" if agg["meets_slo"] else "✘"])
        name = PROFILES.get(pk, {}).get("name", pk)
        res = {"name": name, "levels": lvls, "best": None}
        if best_ok:
            e2e_s = (best_ok["e2e_ms"]["p50"] or 0) / 1000
            users = stats.active_users(best_ok["concurrency"], e2e_s, cost["read_time_s"])
            tps_total = (best_ok["output_tps"] or 0) + (best_ok["input_tps"] or 0)
            res["best"] = {
                "concurrency": best_ok["concurrency"], "users": round(users),
                "e2e_s": round(e2e_s, 2), "output_tps": best_ok["output_tps"], "input_tps": best_ok["input_tps"],
                "ttft_p95_ms": best_ok["ttft_ms"]["p95"], "itl_p95_ms": best_ok["itl_ms"]["p95"],
                "eur_per_m_total": _r2(stats.eur_per_million(tps_total, cost)),
                "eur_per_m_output": _r2(stats.eur_per_million(best_ok["output_tps"] or 0, cost)),
            }
        res["peak_output_tps"] = max((x[1] or 0 for x in pts_tput), default=0)
        results[pk] = res
        tables.append({"title": f"Stufen – {name}",
                       "columns": ["Parallelität", "Anfragen", "TTFT p50", "TTFT p95", "ITL p95", "E2E p50",
                                   "Tok/s je Nutzer p50", "Output Tok/s", "Input Tok/s", "Goodput", "Fehler",
                                   "Anfragen mit Hängern", "Ziel"],
                       "rows": rows})
        s_tput.append({"name": name, "data": pts_tput})
        s_curve.append({"name": name, "data": pts_curve})
        s_user.append({"name": name, "data": pts_user})

    if not results:
        return {"verdict": "rot", "headline": "Keine auswertbaren Stufen.", "kpis": {}, "charts": [], "tables": []}

    ok_res = {k: v for k, v in results.items() if v["best"]}
    parts = []
    for pk, v in results.items():
        if v["best"]:
            b = v["best"]
            parts.append(f"{v['name']}: {b['concurrency']} parallel (≈ {stats.fmt_num(b['users'])} Nutzer), "
                         f"{stats.fmt_num(b['eur_per_m_total'], 2)} € / 1 Mio. Tokens")
        else:
            parts.append(f"{v['name']}: schon bei Parallelität {v['levels'][0]} außerhalb der Zielwerte")
    head = "Innerhalb der Zielwerte – " + "; ".join(parts) + "."
    verdict = "gruen" if len(ok_res) == len(results) else ("gelb" if ok_res else "rot")
    kpis = {}
    if ok_res:
        mn = min(ok_res.items(), key=lambda kv: kv[1]["best"]["users"])
        kpis["max_users"] = {
            "value": mn[1]["best"]["users"], "unit": f"Nutzer ({mn[1]['name']})",
            "text": " · ".join(f"{v['name']}: {stats.fmt_num(v['best']['users'])}" for v in ok_res.values())
                    + f" – Lesezeit {cost['read_time_s']} s, Annahme: aktive Nutzer ≈ Parallelität × (Antwortzeit + Lesezeit) / Antwortzeit",
        }
        first = next(iter(ok_res.values()))
        kpis["eur_per_m"] = {
            "value": first["best"]["eur_per_m_total"], "unit": f"€ / 1 Mio. Tokens ({first['name']}, Mischpreis)",
            "text": " · ".join(f"{v['name']}: {stats.fmt_num(v['best']['eur_per_m_total'], 2)} € gesamt, "
                               f"{stats.fmt_num(v['best']['eur_per_m_output'], 2)} € nur Output"
                               for v in ok_res.values())
                    + f" – Auslastung {stats.fmt_num(cost['utilization_pct'])} %, {stats.fmt_num(cost['machine_eur_per_h'], 2)} €/h + "
                      f"{stats.fmt_num(cost['power_kw'], 2)} kW × {stats.fmt_num(cost['power_eur_per_kwh'], 2)} €/kWh",
        }
    slo_line = (f"Ziel: TTFT p95 ≤ {stats.fmt_ms(slo['ttft_p95_ms'])}, ITL p95 ≤ {stats.fmt_ms(slo['itl_p95_ms'])}, "
                f"Fehler ≤ {stats.fmt_pct(slo['error_rate_pct'])}")
    return {
        "verdict": verdict, "headline": head, "slo_text": slo_line, "kpis": kpis, "profiles": results,
        "users_note": "Parallele Anfragen sind nicht gleich Nutzer. Umrechnung: aktive Nutzer ≈ Parallelität × "
                      "(Antwortzeit + Lesezeit) / Antwortzeit.",
        "charts": [
            {"id": "curve", "title": "Durchsatz gegen Wartezeit (TTFT p95)", "type": "line",
             "x": {"name": "Output-Tokens/s gesamt"}, "y": {"name": "TTFT p95 (ms)", "log": True},
             "series": s_curve, "markLines": [{"y": slo["ttft_p95_ms"], "name": "Ziel"}]},
            {"id": "user", "title": "Durchsatz gegen Tokens/s je Nutzer", "type": "line",
             "x": {"name": "Output-Tokens/s gesamt"}, "y": {"name": "Tokens/s je Nutzer (p50)"},
             "series": s_user, "markLines": [{"y": round(1000 / slo["itl_p95_ms"], 1), "name": "Ziel"}]},
            {"id": "tput", "title": "Durchsatz je Parallelität", "type": "line",
             "x": {"name": "Parallelität", "log": True}, "y": {"name": "Output-Tokens/s"}, "series": s_tput},
        ],
        "tables": tables,
    }


def _r2(x):
    return None if x is None else round(x, 2)
