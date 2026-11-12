"""Lastprofile aus dem Plan: Input-/Output-Längen und typische Nutzung."""
from __future__ import annotations

import random

PROFILES = {
    "chat_kurz":    {"name": "Chat kurz",        "input": (400, 600),     "output": 300,  "typ": "einfache Fragen"},
    "chat_verlauf": {"name": "Chat mit Verlauf", "input": (2000, 4000),   "output": 400,  "typ": "Assistent"},
    "rag":          {"name": "RAG",              "input": (8000, 16000),  "output": 500,  "typ": "Dokumentensuche"},
    "langkontext":  {"name": "Langkontext",      "input": (32000, None),  "output": 500,  "typ": "Verträge, Code"},
    "generierung":  {"name": "Generierung",      "input": (900, 1100),    "output": 2000, "typ": "Texte schreiben"},
    "agent":        {"name": "Agent",            "input": (10000, 100000), "output": 300, "typ": "Tool-Agenten",
                     "growing": True, "turns": 10},
}


def max_context(ctx) -> int:
    mc = (ctx.profile or {}).get("max_context")
    try:
        return int(mc) if mc else 32768
    except (TypeError, ValueError):
        return 32768


def request_factory(ctx, profile_key: str):
    """Liefert make_request(worker, rng) -> (body, meta) für closed_load."""
    p = PROFILES[profile_key]
    out = p["output"]
    limit = max_context(ctx) - out - 64
    lo, hi = p["input"]
    hi = limit if hi is None else min(hi, limit)
    lo = min(lo, hi)
    if lo < p["input"][0]:
        ctx.note(f"Lastprofil {p['name']}: Input auf {hi} Tokens begrenzt (max. Kontext {max_context(ctx)}). "
                 "Max. Kontext im Endpunkt-Profil eintragen, falls größer.")

    if not p.get("growing"):
        def make(worker: int, rng: random.Random):
            n_in = rng.randint(lo, hi)
            text = ctx.factory.make(n_in, rng, unique=True)
            return ctx.chat_body(text, out), {"profile": profile_key, "context_len": n_in}
        return make

    # Agent: jede Sitzung hat einen eigenen Anfang (eindeutig) und wächst Zug um Zug.
    turns = p.get("turns", 10)
    step = max(1, (hi - lo) // max(1, turns - 1))
    sessions: dict[int, dict] = {}

    def make_agent(worker: int, rng: random.Random):
        s = sessions.get(worker)
        if s is None or s["tokens"] + step > hi:
            s = {"text": ctx.factory.make(lo, rng, unique=True), "tokens": lo, "turn": 0}
            sessions[worker] = s
        else:
            s["text"] = s["text"] + "\n" + ctx.factory.make(step, rng, unique=False)
            s["tokens"] += step
            s["turn"] += 1
        return (ctx.chat_body(s["text"], out),
                {"profile": profile_key, "context_len": s["tokens"], "turn": s["turn"]})
    return make_agent
