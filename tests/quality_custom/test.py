"""Eigene Kundenaufgaben mit Musterantwort, Bewertung durch LLM als Prüfer."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app import stats, storage
from app.client import message_of
from app.quality import judge, judge_endpoint, reference_compare

HERE = Path(__file__).parent


def _cases_path(p):
    if p.get("cases_file"):
        return Path(p["cases_file"]).expanduser()
    own = storage.DATA / "quality" / "cases.jsonl"
    return own if own.exists() else HERE / "beispielfaelle.jsonl"


def estimate(params):
    try:
        n = sum(1 for line in _cases_path(params).read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        n = 12
    return 30 + n * 15 / max(1, params["concurrency"])


async def run(ctx):
    p = ctx.params
    path = _cases_path(p)
    cases = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    ctx.extra_config["cases_file"] = str(path)
    jep, jname = judge_endpoint(ctx, p.get("judge_profile"))
    ctx.extra_config["judge"] = jname
    sem = asyncio.Semaphore(p["concurrency"])
    done = {"n": 0}

    async def one(c):
        async with sem:
            if ctx.cancelled:
                return
            r = await ctx.once({"messages": [{"role": "user", "content": c["aufgabe"]}], "max_tokens": p["max_tokens"],
                                "temperature": 0}, {"stage": "antwort", "case": c["id"]}, record=True)
            ans = message_of(r).get("content") or ""
            note, why = await judge(ctx, jep, c["aufgabe"], c["muster"], ans)
            ctx.stage({"case": {"id": c["id"], "aufgabe": c["aufgabe"][:200], "antwort": ans[:600],
                                "note": note, "begruendung": why, "status": r["status"]}})
            done["n"] += 1
            ctx.progress(done["n"] / len(cases), f"{done['n']} von {len(cases)} Fällen")
    await asyncio.gather(*(one(c) for c in cases))


def summarize(recs, stages, cfg, settings):
    cases = [s["case"] for s in stages if "case" in s]
    notes = [c["note"] for c in cases if c["note"] is not None]
    score = 100 * (sum(notes) / len(notes) - 1) / 4 if notes else None
    scores = {"Kundenaufgaben (0–100)": score}
    cmp = reference_compare("quality_custom", cfg.get("run_id"), scores, cfg["params"]["max_drop_pts"])
    head = (f"Durchschnittsnote {stats.fmt_num(sum(notes) / len(notes), 2)} von 5 über {len(notes)} Fälle "
            f"(Prüfer: {cfg.get('judge')}). {cmp['text']}") if notes else "Keine bewerteten Fälle."
    return {"verdict": cmp["verdict"] if notes else "rot", "headline": head, "scores": scores, "kpis": {},
            "charts": [],
            "tables": [{"title": "Vergleich mit Referenz", "columns": ["Testset", "Punktzahl", "Referenz", "Abweichung"],
                        "rows": cmp["rows"]},
                       {"title": "Fälle", "columns": ["Fall", "Note", "Begründung", "Antwort"],
                        "rows": [[c["id"], c["note"] if c["note"] is not None else "–", c["begruendung"], c["antwort"][:300]]
                                 for c in sorted(cases, key=lambda c: c["id"])]}]}


