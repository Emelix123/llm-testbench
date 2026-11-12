"""Tool Calling nach BFCL-Kategorien mit AST-Prüfung der Argumente."""
from __future__ import annotations

import asyncio
import json

from app import stats
from app.client import message_of
from app.quality import reference_compare


def fn(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": req}}}


WEATHER = fn("get_weather", "Aktuelles Wetter einer Stadt", {"city": {"type": "string"},
             "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}}, ["city"])
CONVERT = fn("convert_currency", "Rechnet einen Betrag in eine andere Währung um",
             {"amount": {"type": "number"}, "from_currency": {"type": "string", "description": "ISO-Code"},
              "to_currency": {"type": "string", "description": "ISO-Code"}}, ["amount", "from_currency", "to_currency"])
BOOK = fn("book_meeting_room", "Bucht einen Besprechungsraum",
          {"room": {"type": "string"}, "date": {"type": "string", "description": "YYYY-MM-DD"},
           "hours": {"type": "integer"}}, ["room", "date", "hours"])
STOCK = fn("get_stock_price", "Aktienkurs abrufen", {"ticker": {"type": "string"}}, ["ticker"])

# erwartet: Liste von (name, {arg: erlaubte Werte}) – Reihenfolge egal; [] = kein Aufruf erwartet
CASES = [
    ("simple", "Wie ist das Wetter in Köln?", [WEATHER], [("get_weather", {"city": ["Köln", "Koeln", "Cologne"]})]),
    ("simple", "Rechne 250 Euro in US-Dollar um.", [CONVERT],
     [("convert_currency", {"amount": [250], "from_currency": ["EUR"], "to_currency": ["USD"]})]),
    ("multiple", "Buche Raum Alster am 2026-12-03 für 2 Stunden.", [WEATHER, BOOK, STOCK],
     [("book_meeting_room", {"room": ["Alster"], "date": ["2026-12-03"], "hours": [2]})]),
    ("multiple", "Wie steht die Aktie mit dem Kürzel SAP?", [WEATHER, CONVERT, STOCK],
     [("get_stock_price", {"ticker": ["SAP"]})]),
    ("parallel", "Wie ist das Wetter in Berlin und in München?", [WEATHER],
     [("get_weather", {"city": ["Berlin"]}), ("get_weather", {"city": ["München", "Muenchen", "Munich"]})]),
    ("parallel_multiple", "Wie ist das Wetter in Hamburg, und was kostet die Aktie AAPL?", [WEATHER, STOCK],
     [("get_weather", {"city": ["Hamburg"]}), ("get_stock_price", {"ticker": ["AAPL"]})]),
    ("irrelevance", "Erzähl mir einen kurzen Witz über Katzen.", [STOCK, CONVERT], []),
    ("irrelevance", "Was ist die Hauptstadt von Frankreich?", [BOOK], []),
]


def estimate(params):
    return 20 + len(CASES) * params["repeats"] * 3


def _match(calls, expected):
    if not expected:
        return not calls, "kein Aufruf erwartet" + (f", aber {len(calls)} erfolgt" if calls else "")
    if len(calls) != len(expected):
        return False, f"{len(calls)} statt {len(expected)} Aufrufe"
    rest = list(expected)
    for c in calls:
        f = c.get("function") or {}
        try:
            args = json.loads(f.get("arguments") or "{}")
        except json.JSONDecodeError:
            return False, "Argumente kein JSON"
        hit = None
        for i, (name, want) in enumerate(rest):
            if f.get("name") != name:
                continue
            if all(_eq(args.get(k), vals) for k, vals in want.items()):
                hit = i
                break
        if hit is None:
            return False, f"unerwartet: {f.get('name')}({f.get('arguments')})"
        rest.pop(hit)
    return True, "korrekt"


def _eq(v, allowed):
    for a in allowed:
        if isinstance(a, (int, float)) and isinstance(v, (int, float)) and abs(v - a) < 1e-6:
            return True
        if isinstance(a, str) and isinstance(v, str) and v.strip().lower() == a.lower():
            return True
    return False


async def run(ctx):
    n = len(CASES) * ctx.params["repeats"]
    done = {"n": 0}
    sem = asyncio.Semaphore(4)

    async def one(i, cat, q, tools, exp):
        async with sem:
            r = await ctx.once({"messages": [{"role": "user", "content": q}], "tools": tools, "tool_choice": "auto",
                                "max_tokens": 300, "temperature": 0}, {"stage": "tools", "case": i}, record=True)
            m = message_of(r)
            ok, why = _match(m.get("tool_calls") or [], exp) if r["status"] == 200 else (False, f"HTTP {r['status']}")
            ctx.stage({"case": {"i": i, "cat": cat, "frage": q, "ok": ok, "why": why,
                                "calls": json.dumps(m.get("tool_calls") or [], ensure_ascii=False)[:300]}})
            done["n"] += 1
            ctx.progress(done["n"] / n, f"{done['n']} von {n}")
    await asyncio.gather(*(one(i, *c) for _ in range(ctx.params["repeats"]) for i, c in enumerate(CASES)))


def summarize(recs, stages, cfg, settings):
    cs = [s["case"] for s in stages if "case" in s]
    if not cs:
        return {"verdict": "rot", "headline": "Keine Messwerte.", "kpis": {}, "charts": [], "tables": []}
    cats = {}
    for c in cs:
        cats.setdefault(c["cat"], []).append(c["ok"])
    scores = {f"Tool Calling: {k}": 100 * sum(v) / len(v) for k, v in cats.items()}
    total = 100 * sum(c["ok"] for c in cs) / len(cs)
    scores = {"Tool Calling gesamt": total, **scores}
    cmp = reference_compare("quality_tools", cfg.get("run_id"), scores, cfg["params"]["max_drop_pts"])
    head = f"{stats.fmt_num(total, 0)} % der Tool-Aufrufe korrekt. {cmp['text']}"
    verdict = cmp["verdict"] if cmp["reference_run"] and cmp["reference_run"] != cfg.get("run_id") else (
        "gruen" if total >= 90 else ("gelb" if total >= 70 else "rot"))
    return {"verdict": verdict, "headline": head, "scores": scores, "kpis": {}, "charts": [],
            "tables": [{"title": "Kategorien und Referenz", "columns": ["Testset", "Punktzahl", "Referenz", "Abweichung"],
                        "rows": cmp["rows"]},
                       {"title": "Fehlgeschlagene Fälle", "columns": ["Kategorie", "Frage", "Grund", "Aufrufe"],
                        "rows": [[c["cat"], c["frage"], c["why"], c["calls"]] for c in cs if not c["ok"]]}]}
