"""Konsistenz: temperature 0 einzeln gegen unter Last."""
from __future__ import annotations

import asyncio
import difflib

from app import stats
from app.quality import reference_compare

TOPICS = ["Photosynthese", "Inflation", "das Grundgesetz", "Vulkane", "Impfungen", "den Euro", "Bienen", "Ebbe und Flut",
          "das Internet", "Kernfusion", "die Hanse", "Blockchain", "den Wasserkreislauf", "Antibiotika", "die Börse",
          "Gletscher", "das Immunsystem", "Solarzellen", "die Reformation", "Wechselkurse", "Erdbeben", "Datenschutz",
          "die Mondphasen", "Kompostierung", "Elektroautos", "das Wahlrecht", "Schimmel", "Regenbögen", "Zinsen",
          "Wärmepumpen", "den Föderalismus", "Plattentektonik"]


def estimate(params):
    return 30 + params["questions"] * 3


async def run(ctx):
    p = ctx.params
    qs = [f"Erkläre {TOPICS[i % len(TOPICS)]} in drei Sätzen." + (f" (Variante {i // len(TOPICS)})" if i >= len(TOPICS) else "")
          for i in range(p["questions"])]

    def body(q):
        return {"messages": [{"role": "user", "content": q}], "max_tokens": p["max_tokens"], "temperature": 0}
    single = {}
    for i, q in enumerate(qs):
        if ctx.cancelled:
            return
        r = await ctx.send(body(q), {"stage": "einzeln", "q": i}, keep_text=True)
        single[i] = r.text
        ctx.progress(0.5 * (i + 1) / len(qs), f"einzeln {i + 1}/{len(qs)}")
    # unter Last: jede Frage einmal, aufgefüllt mit Lastanfragen bis zur Parallelität
    ctx.progress(0.55, f"unter Last ({p['load_concurrency']} parallel)")
    filler = max(0, p["load_concurrency"] - len(qs))
    tasks = [ctx.send(body(q), {"stage": "last", "q": i}, keep_text=True) for i, q in enumerate(qs)]
    tasks += [ctx.send(ctx.chat_body(ctx.factory.make(500, ctx.rng), p["max_tokens"]), {"stage": "fueller"})
              for _ in range(filler)]
    res = await asyncio.gather(*tasks)
    for i, r in enumerate(res[:len(qs)]):
        a, b = single.get(i, ""), r.text
        ctx.stage({"pair": {"q": i, "frage": qs[i], "gleich": a == b,
                            "aehnlichkeit": round(difflib.SequenceMatcher(None, a, b).ratio(), 4),
                            "einzeln": a[:300], "last": b[:300]}})
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    pairs = [s["pair"] for s in stages if "pair" in s]
    if not pairs:
        return {"verdict": "rot", "headline": "Keine Messwerte.", "kpis": {}, "charts": [], "tables": []}
    same = 100 * sum(p["gleich"] for p in pairs) / len(pairs)
    sim = 100 * sum(p["aehnlichkeit"] for p in pairs) / len(pairs)
    scores = {"identische Antworten (%)": same, "Textähnlichkeit (%)": sim}
    cmp = reference_compare("quality_consistency", cfg.get("run_id"), scores, cfg["params"]["max_drop_pts"])
    head = (f"{stats.fmt_num(same, 0)} % der Antworten bei {cfg['params']['load_concurrency']} parallel identisch mit "
            f"der Einzelanfrage, Ähnlichkeit {stats.fmt_num(sim, 1)} %. ")
    verdict = "gruen" if same >= 95 else ("gelb" if sim >= 90 else "rot")
    if cmp["reference_run"] and cmp["reference_run"] != cfg.get("run_id"):
        head += cmp["text"]
        verdict = max(verdict, cmp["verdict"], key=["gruen", "gelb", "rot"].index)
    return {"verdict": verdict, "headline": head, "scores": scores, "kpis": {}, "charts": [],
            "tables": [{"title": "Vergleich mit Referenz", "columns": ["Wert", "Punktzahl", "Referenz", "Abweichung"],
                        "rows": cmp["rows"]},
                       {"title": "Abweichende Antworten", "columns": ["Frage", "Ähnlichkeit", "einzeln", "unter Last"],
                        "rows": [[p["frage"], stats.fmt_pct(100 * p["aehnlichkeit"]), p["einzeln"], p["last"]]
                                 for p in pairs if not p["gleich"]][:30]}]}
