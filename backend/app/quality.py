"""Gemeinsames für Qualitätstests: Referenzlauf-Vergleich und LLM als Prüfer.

Ziel ist keine Rangliste, sondern Verschlechterungen zu erkennen (Quantisierung,
geänderte Konfiguration). Verglichen wird immer mit einem gespeicherten Referenzlauf
desselben Tests, der in der Oberfläche als Referenz markiert wird.
"""
from __future__ import annotations

import json
import re

from . import stats, storage
from .client import Endpoint, chat_once, message_of


def reference_compare(test_id: str, run_id: str | None, scores: dict[str, float | None],
                      max_drop_pts: float) -> dict:
    """Vergleicht Punktzahlen (0–100) mit dem Referenzlauf. Gibt rows, verdict, text zurück."""
    ref_id = storage.get_references().get(test_id)
    ref_scores, ref_cfg = {}, {}
    if ref_id and ref_id != run_id:
        try:
            rd = storage.run_dir(ref_id)
            ref_scores = storage.read_json(rd / "summary.json", {}).get("scores") or {}
            ref_cfg = storage.read_json(rd / "config.json", {})
        except FileNotFoundError:
            ref_id = None
    rows, worst = [], 0.0
    for name, v in scores.items():
        r = ref_scores.get(name)
        d = None if v is None or r is None else v - r
        if d is not None:
            worst = min(worst, d)
        rows.append([name, stats.fmt_num(v, 1) if v is not None else "–",
                     stats.fmt_num(r, 1) if r is not None else "–",
                     ("+" if d >= 0 else "−") + stats.fmt_num(abs(d), 1) if d is not None else "–"])
    is_ref = ref_id is not None and ref_id == run_id
    if ref_id is None:
        verdict, text = "gelb", "Kein Referenzlauf – diesen Lauf als Referenz markieren, um Verschlechterungen zu erkennen."
    elif is_ref or ref_id == run_id:
        verdict, text = "gruen", "Dieser Lauf ist die Referenz."
    elif not ref_scores:
        verdict, text = "gelb", f"Referenzlauf {ref_id} hat keine vergleichbaren Punktzahlen."
    else:
        rp = (ref_cfg.get("profile") or {})
        verdict = "rot" if worst < -max_drop_pts else ("gelb" if worst < -max_drop_pts / 2 else "gruen")
        text = (f"Referenz: {ref_id} ({rp.get('name')}, {rp.get('model')}). Größte Abweichung "
                f"{'−' if worst < 0 else '+'}{stats.fmt_num(abs(worst), 1)} Punkte (Grenze {stats.fmt_num(max_drop_pts, 1)}).")
    return {"rows": rows, "verdict": verdict, "text": text, "reference_run": ref_id}


def judge_endpoint(ctx, profile_id: str | None) -> tuple[Endpoint, str]:
    if profile_id:
        prof = storage.get_profile(profile_id)
        if prof:
            return (Endpoint(prof["base_url"], prof["model"], storage.get_secret(prof.get("api_key_name"))),
                    f"{prof.get('name')} ({prof['model']})")
        ctx.note(f"Prüfer-Profil „{profile_id}“ nicht gefunden – der getestete Endpunkt bewertet sich selbst.")
    else:
        ctx.note("Kein eigenes Prüfer-Profil gewählt: der getestete Endpunkt bewertet sich selbst. "
                 "Für belastbare Vergleiche ein festes Prüfer-Modell wählen.")
    return ctx.ep, f"{ctx.profile.get('name')} (selbst)"


JUDGE_PROMPT = """Du bist ein strenger Prüfer. Bewerte die ANTWORT auf die AUFGABE im Vergleich zur MUSTERANTWORT.
Kriterien: inhaltliche Richtigkeit, Vollständigkeit, Befolgen der Aufgabe, Sprache Deutsch.
Bewerte mit einer Note von 1 (unbrauchbar) bis 5 (so gut wie die Musterantwort oder besser).
Gib ausschließlich JSON zurück: {{"note": <1-5>, "begruendung": "<ein Satz>"}}

AUFGABE:
{task}

MUSTERANTWORT:
{reference}

ANTWORT:
{answer}
"""


async def judge(ctx, ep: Endpoint, task: str, reference: str, answer: str) -> tuple[int | None, str]:
    body = {"model": ep.model, "messages": [{"role": "user", "content": JUDGE_PROMPT.format(
        task=task, reference=reference, answer=answer or "(leer)")}], "max_tokens": 300, "temperature": 0}
    r = await chat_once(ctx.client, ep, body, timeout_s=ctx.timeout_s)
    c = message_of(r).get("content") or ""
    try:
        m = re.search(r"\{.*\}", c, re.S)
        obj = json.loads(m.group(0)) if m else {}
        n = int(obj.get("note"))
        return max(1, min(5, n)), str(obj.get("begruendung", ""))[:200]
    except (ValueError, TypeError, json.JSONDecodeError):
        m = re.search(r"\b([1-5])\b", c)
        return (int(m.group(1)) if m else None), c[:200]
