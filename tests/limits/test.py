"""Grenzwerte: Limits greifen sauber, Server bleibt erreichbar."""
from __future__ import annotations

import json

from app import media
from app.client import message_of, simple_get
from app.loadprofiles import max_context


def estimate(params):
    return 120


def _deep_schema(depth=6):
    s = {"type": "object", "properties": {"wert": {"type": "string", "pattern": "^[A-Z]{3}$"},
                                          "stufe": {"type": "integer", "enum": [1, 2, 3]},
                                          "liste": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 4}},
         "required": ["wert", "stufe", "liste"], "additionalProperties": False}
    for i in range(depth):
        s = {"type": "object", "properties": {f"ebene{i}": s, "name": {"type": "string"},
                                              "art": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
             "required": [f"ebene{i}", "name", "art"], "additionalProperties": False}
    return s


async def run(ctx):
    mc = max_context(ctx)
    cases = []

    def msgs(t="Sag Hallo."):
        return [{"role": "user", "content": t}]
    cases.append(("max_tokens weit über Kontext", {"messages": msgs(), "max_tokens": mc * 10}, "4xx"))
    cases.append(("n = 2", {"messages": msgs(), "max_tokens": 20, "n": 2, "temperature": 0.8}, "ok_or_4xx"))
    cases.append(("n = 128", {"messages": msgs(), "max_tokens": 20, "n": 128}, "ok_or_4xx"))
    cases.append(("komplexes JSON-Schema (7 Ebenen, Muster, Enum)",
                  {"messages": msgs("Erzeuge ein Beispielobjekt."), "max_tokens": 600,
                   "response_format": {"type": "json_schema", "json_schema": {"name": "tief", "schema": _deep_schema()}}},
                  "ok_or_4xx"))
    cases.append(("leere Nachrichtenliste", {"messages": [], "max_tokens": 10}, "4xx"))
    cases.append(("Input über dem Kontext", {"messages": msgs(ctx.factory.make(int(mc * 1.2), ctx.rng)), "max_tokens": 10}, "4xx"))
    cases.append(("negatives max_tokens", {"messages": msgs(), "max_tokens": -5}, "4xx"))
    n_img = ctx.params["many_images"]
    imgs = [{"type": "image_url", "image_url": {"url": media.data_url(256, 256, "rot")}} for _ in range(n_img)]
    cases.append((f"{n_img} Bilder in einer Anfrage",
                  {"messages": [{"role": "user", "content": [{"type": "text", "text": "Wie viele Bilder sind das?"}, *imgs]}],
                   "max_tokens": 30}, "ok_or_4xx"))
    for i, (name, body, expect) in enumerate(cases):
        if ctx.cancelled:
            return
        ctx.progress(i / len(cases), name)
        r = await ctx.once(body, {"stage": "limits", "case": name, "expected_error": True}, record=True)
        st = r["status"]
        clean = st is not None and (st == 200 if expect == "ok" else
                                    (400 <= st < 500) if expect == "4xx" else (st == 200 or 400 <= st < 500))
        if st == 200 and name.startswith("komplexes"):
            try:
                json.loads(message_of(r).get("content") or "")
            except json.JSONDecodeError:
                clean = False
        # Server danach erreichbar?
        hs, _, _, _, err = await simple_get(ctx.client, ctx.ep, "/v1/models")
        chk = await ctx.send(ctx.chat_body("Sag Hallo.", 5, force=False), {"stage": "limits_health"})
        alive = hs == 200 and chk.ok
        ctx.stage({"check": {"key": f"limit{i}", "label": name, "ok": bool(clean and alive),
                             "detail": f"HTTP {st if st is not None else r['error']} in {r['ms']:.0f} ms"
                                       f" – Server danach {'erreichbar' if alive else 'NICHT erreichbar'}",
                             "example": (r["text"] or "")[:300]}})
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    checks = [s["check"] for s in stages if "check" in s]
    failed = [c for c in checks if not c["ok"]]
    dead = [c for c in checks if "NICHT erreichbar" in c["detail"]]
    verdict = "rot" if dead else ("gelb" if failed else "gruen")
    head = f"{len(checks) - len(failed)} von {len(checks)} Grenzfällen sauber behandelt."
    if failed:
        head += " Auffällig: " + ", ".join(c["label"] for c in failed) + "."
    if dead:
        head += " Server war danach nicht erreichbar!"
    return {"verdict": verdict, "headline": head, "checks": checks, "kpis": {}, "charts": [],
            "tables": [{"title": "Grenzfälle", "columns": ["", "Fall", "Ergebnis", "Antwort"],
                        "rows": [["✔" if c["ok"] else "✘", c["label"], c["detail"], c["example"]] for c in checks]}]}
