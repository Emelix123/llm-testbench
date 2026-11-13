"""Prefix-Cache-Seitenkanal (CVE-2025-46570) als nachstellbarer Test.

Was der Test macht, Schritt für Schritt
---------------------------------------
Prefix Caching teilt Cache-Blöcke allein anhand von Tokens und Präfix, nicht anhand
des Mandanten. In einem geteilten Cache hängt die Prefill-Zeit einer Anfrage damit
davon ab, ob ein *anderer* Mandant denselben Präfix schon gesendet hat.

Je Messung:
  1. Opfer (ein Mandant) sendet einen zufälligen Präfix P  ->  seine Blöcke landen im Cache.
  2. Angreifer (anderer Mandant) sendet
       - Treffer-Fall:   P + eigener Zusatz   -> gemeinsame Blöcke sind ein Cache-Treffer.
       - Fehlschlag-Fall: frischer Präfix P'  -> nie gesehen, Cache-Fehlschlag.
     und misst jeweils die Zeit bis zum ersten Token (TTFT) sowie das Feld
     prompt_tokens_details.cached_tokens.
  P ist je Messung frisch zufällig, damit nichts aus früheren Messungen nachwirkt.

Zwei Signale:
  - Zeit (TTFT): Treffer sind schneller. Maß der Unterscheidbarkeit ist die AUC
    (0,5 = Raten, 1,0 = perfekt) und Cohens d (Effektstärke).
  - cached_tokens: Verrät der Endpunkt die Zahl zwischengespeicherter Blöcke, kann der
    Angreifer Treffer direkt ablesen, ganz ohne Zeitmessung (InputSnatch-Prinzip).

Einstellungen, die über Erfolg entscheiden (per Parameter):
  - cache_salt: keiner (angreifbar) / pro Mandant (Opfer und Angreifer verschieden ->
    kein geteilter Block) / pro Anfrage (eindeutig).
  - Prefix-Caching an/aus (am Server).
  - Blockgröße: der Granularitäts-Sweep zeigt, dass Treffer nur an Blockgrenzen
    entstehen; bei 16 Tokens je Block muss ein Angreifer je einen ganzen Block raten.
  - Last anderer Mandanten: senkt die Effektstärke (Cohens d), wie in der Literatur.

Nur gegen eigene Systeme mit ausdrücklicher Erlaubnis einsetzen.
"""
from __future__ import annotations

import asyncio
import random

from app import stats
from app.prompts import _WORDS


# --------------------------------------------------------------- Prompt-Bausteine

def _words(rng: random.Random, n: int) -> list[str]:
    return [rng.choice(_WORDS) for _ in range(n)]


def _blocktext(ctx, rng: random.Random, n_tokens: int, lead: list[str] | None = None) -> tuple[str, list[str]]:
    """Text aus ganzen Wörtern mit ungefähr n_tokens Tokens.

    lead erzwingt den Anfang (für geteilte Präfixe). Gibt (Text, Wortliste) zurück,
    damit sich der Anfang exakt wiederverwenden lässt.
    """
    words = list(lead or [])
    # grob mit Wörtern auffüllen, dann an der Token-Zahl feinjustieren
    while ctx.factory.count(" ".join(words)) < n_tokens:
        words += _words(rng, max(1, n_tokens // 2))
    # auf Zielgröße kürzen
    lo, hi = len(lead or []), len(words)
    while lo < hi:
        mid = (lo + hi) // 2
        if ctx.factory.count(" ".join(words[:mid])) >= n_tokens:
            hi = mid
        else:
            lo = mid + 1
    words = words[:max(len(lead or []), lo)]
    return " ".join(words), words


def _salts(ctx, trial: int):
    mode = ctx.params["salt_mode"]
    if mode == "none":
        return None, None
    if mode == "per_tenant":
        return "mandant-opfer", "mandant-angreifer"
    # per_request: jede Anfrage ein eindeutiger Salt
    return f"opfer-{trial}-{ctx.rng.getrandbits(32):x}", f"ang-{trial}-{ctx.rng.getrandbits(32):x}"


def _body(ctx, text: str, salt: str | None):
    body = ctx.chat_body(text, ctx.params["probe_tokens"], force=False)
    field = (ctx.params.get("salt_field") or "").strip()
    if salt is not None and field:
        body[field] = salt
    return body


def estimate(params):
    main = params["trials"] * 3 * (1 + (1 if params["background_load"] else 0))
    sweep = len(params["sweep_tokens"]) * params["sweep_trials"] * 2 if params["granularity_sweep"] else 0
    return 40 + (main + sweep) * 0.25


# ------------------------------------------------------------------------- Ablauf

async def _measure_pair(ctx, trial: int, phase: str):
    """Eine Treffer- und eine Fehlschlag-Messung mit frischem Präfix."""
    vs, as_ = _salts(ctx, trial)
    rng = random.Random(ctx.rng.getrandbits(64))
    P_text, P_words = _blocktext(ctx, rng, ctx.params["prefix_tokens"])
    sfx = " " + " ".join(_words(rng, 6)) + f" {rng.getrandbits(32):x}"
    # frischer Präfix fürs Fehlschlag-Szenario
    M_text, _ = _blocktext(ctx, rng, ctx.params["prefix_tokens"])
    msfx = " " + " ".join(_words(rng, 6)) + f" {rng.getrandbits(32):x}"

    async def hit():
        await ctx.send(_body(ctx, P_text, vs), {"stage": "victim", "phase": phase, "trial": trial})
        return await ctx.send(_body(ctx, P_text + sfx, as_),
                              {"stage": "main", "phase": phase, "cond": "hit", "trial": trial})

    async def miss():
        return await ctx.send(_body(ctx, M_text + msfx, as_),
                              {"stage": "main", "phase": phase, "cond": "miss", "trial": trial})

    # Reihenfolge abwechseln, um langsame Drift auszugleichen
    if trial % 2 == 0:
        await hit(); await miss()
    else:
        await miss(); await hit()


async def _background(ctx, concurrency: int, stop: asyncio.Event):
    """Last eines dritten Mandanten mit eindeutigen Prompts (stört, misst aber nicht)."""
    async def worker(i):
        wr = random.Random(ctx.rng.getrandbits(64))
        while not stop.is_set() and not ctx.cancelled:
            text, _ = _blocktext(ctx, wr, max(256, ctx.params["prefix_tokens"]))
            bsalt = None if ctx.params["salt_mode"] == "none" else f"last-{i}"
            await ctx.send(_body(ctx, text + f" {wr.getrandbits(48):x}", bsalt),
                           {"stage": "bg"})
    await asyncio.gather(*(worker(i) for i in range(concurrency)))


async def _sweep(ctx):
    """Granularitäts-Sweep: Angreifer teilt die ersten m Tokens mit dem Opfer."""
    levels = sorted({max(0, int(x)) for x in ctx.params["sweep_tokens"]} | {0})
    n = len(levels) * ctx.params["sweep_trials"]
    done = 0
    for m in levels:
        for _ in range(ctx.params["sweep_trials"]):
            if ctx.cancelled:
                return
            vs, as_ = _salts(ctx, done)
            rng = random.Random(ctx.rng.getrandbits(64))
            P_text, P_words = _blocktext(ctx, rng, ctx.params["prefix_tokens"])
            # Opfer legt den vollen Präfix in den Cache
            await ctx.send(_body(ctx, P_text, vs), {"stage": "victim_sweep", "match": m})
            # Angreifer übernimmt die ersten m Tokens von P (wortweise genähert) + frischer Rest
            shared = []
            for w in P_words:
                if ctx.factory.count(" ".join(shared + [w])) > m:
                    break
                shared.append(w)
            probe_text, _ = _blocktext(ctx, rng, ctx.params["prefix_tokens"], lead=shared)
            probe_text += f" {rng.getrandbits(32):x}"
            await ctx.send(_body(ctx, probe_text, as_),
                           {"stage": "sweep", "match": m, "match_tokens": ctx.factory.count(" ".join(shared))})
            done += 1
            ctx.progress(0.5 + 0.5 * done / n, f"Sweep: {m} Tokens Übereinstimmung ({done}/{n})")


async def run(ctx):
    p = ctx.params
    ctx.extra_config["attack"] = {
        "salt_mode": p["salt_mode"], "salt_field": p.get("salt_field"),
        "prefix_tokens": p["prefix_tokens"], "background_load": p["background_load"],
    }
    ctx.note("Prefix-Cache-Seitenkanal (CVE-2025-46570). Nur gegen eigene Systeme mit Erlaubnis. "
             "Die Ampel bewertet aus Verteidigersicht: rot = Angriff erfolgreich / verwundbar.")
    # Aufwärmen (nicht gewertet), zugleich Kalibrierung des Template-Overheads
    sample, _ = _blocktext(ctx, ctx.rng, 400)
    r = await ctx.send(_body(ctx, sample, None), record=False)
    if r.ok and r.prompt_tokens:
        ctx.factory.calibrate(sample, r.prompt_tokens)
    if r.cached_tokens is None:
        ctx.note("Der Endpunkt liefert kein prompt_tokens_details.cached_tokens. Der direkte "
                 "cached_tokens-Oracle entfällt; gemessen wird nur der Zeit-Seitenkanal.")

    # Hauptmessung, ruhig
    for i in range(p["trials"]):
        if ctx.cancelled:
            return
        ctx.progress(0.4 * (i + 1) / p["trials"] * (0.5 if p["background_load"] else 1),
                     f"Messung ruhig: {i + 1}/{p['trials']}")
        await _measure_pair(ctx, i, "quiet")

    # Optional: dieselbe Messung unter Last anderer Mandanten
    if p["background_load"]:
        stop = asyncio.Event()
        bg = asyncio.create_task(_background(ctx, p["background_load"], stop))
        try:
            for i in range(p["trials"]):
                if ctx.cancelled:
                    break
                ctx.progress(0.2 + 0.2 * (i + 1) / p["trials"], f"Messung unter Last: {i + 1}/{p['trials']}")
                await _measure_pair(ctx, i, "load")
        finally:
            stop.set()
            await bg

    if p["granularity_sweep"] and not ctx.cancelled:
        await _sweep(ctx)
    ctx.progress(1.0, "fertig")


# ---------------------------------------------------------------------- Auswertung

def _ttft(r):
    return stats.ttft(r)


def _phase_metrics(recs, phase):
    hit = [r for r in recs if r.get("stage") == "main" and r.get("phase") == phase and r.get("cond") == "hit" and stats.is_ok(r)]
    miss = [r for r in recs if r.get("stage") == "main" and r.get("phase") == phase and r.get("cond") == "miss" and stats.is_ok(r)]
    th = [t for t in (_ttft(r) for r in hit) if t is not None]
    tm = [t for t in (_ttft(r) for r in miss) if t is not None]
    out = {"n_hit": len(th), "n_miss": len(tm)}
    if not th or not tm:
        return out
    # Treffer sind schneller -> Score = -TTFT, positive Klasse = Treffer
    out["auc"] = stats.auc([-t for t in th], [-t for t in tm])
    out["d"] = abs(stats.cohens_d(tm, th) or 0)
    bt = stats.best_threshold_accuracy([-t for t in th], [-t for t in tm])
    out["accuracy"] = bt[1] if bt else None
    out["ttft_hit_p50"] = stats.pct(th, 50)
    out["ttft_miss_p50"] = stats.pct(tm, 50)
    out["saving_pct"] = None if not out["ttft_miss_p50"] else 100 * (1 - out["ttft_hit_p50"] / out["ttft_miss_p50"])
    # cached_tokens-Oracle
    ch = [r.get("cached_tokens") for r in hit if r.get("cached_tokens") is not None]
    cm = [r.get("cached_tokens") for r in miss if r.get("cached_tokens") is not None]
    if ch and cm:
        out["cached_available"] = True
        out["cached_hit_share"] = 100 * sum(1 for x in ch if x > 0) / len(ch)
        out["cached_miss_share"] = 100 * sum(1 for x in cm if x > 0) / len(cm)
        out["cached_auc"] = stats.auc(ch, cm)
        out["cached_hit_p50"] = stats.pct(ch, 50)
    else:
        out["cached_available"] = False
    out["_th"], out["_tm"] = th, tm
    return out


def summarize(recs, stages, cfg, settings):
    p = cfg["params"]
    quiet = _phase_metrics(recs, "quiet")
    load = _phase_metrics(recs, "load") if p["background_load"] else None

    if "auc" not in quiet:
        return {"verdict": "rot", "headline": "Keine auswertbaren Messungen – lief der Endpunkt?",
                "kpis": {}, "charts": [], "tables": []}

    auc = quiet["auc"]
    leak = quiet.get("cached_available") and (
        (quiet.get("cached_auc") or 0) >= 0.9 or
        (quiet.get("cached_hit_share", 0) - quiet.get("cached_miss_share", 0)) >= 50)

    # Verdict aus Verteidigersicht: rot = Angriff funktioniert
    if leak or auc >= p["auc_fail"]:
        verdict = "rot"
    elif auc >= p["auc_warn"]:
        verdict = "gelb"
    else:
        verdict = "gruen"

    salt_txt = {"none": "ohne cache_salt (geteilter Cache)",
                "per_tenant": "mit cache_salt pro Mandant",
                "per_request": "mit cache_salt pro Anfrage"}[p["salt_mode"]]
    if verdict == "rot":
        head = (f"Angriff erfolgreich {salt_txt}: Mandanten an der Antwortzeit unterscheidbar "
                f"(AUC {stats.fmt_num(auc, 2)}, Cohens d {stats.fmt_num(quiet['d'], 2)}, "
                f"Trefferquote {stats.fmt_pct(100 * (quiet.get('accuracy') or 0), 0)}).")
        if leak:
            head += (" Zusätzlich verrät cached_tokens die Treffer direkt, ohne Zeitmessung "
                     f"({stats.fmt_pct(quiet.get('cached_hit_share'), 0)} der Treffer gegen "
                     f"{stats.fmt_pct(quiet.get('cached_miss_share'), 0)} der Fehlschläge).")
    elif verdict == "gelb":
        head = (f"Schwaches Signal {salt_txt}: AUC {stats.fmt_num(auc, 2)}, Cohens d "
                f"{stats.fmt_num(quiet['d'], 2)}. Mit mehr Messungen oder zu ruhigen Zeiten evtl. nutzbar.")
    else:
        head = (f"Kein nutzbares Signal {salt_txt}: AUC {stats.fmt_num(auc, 2)} (0,5 = Raten). "
                "Treffer und Fehlschläge sind an der Antwortzeit nicht zu unterscheiden.")
    if load:
        head += (f" Unter Last anderer Mandanten sinkt Cohens d von {stats.fmt_num(quiet['d'], 2)} "
                 f"auf {stats.fmt_num(load.get('d'), 2)} (AUC {stats.fmt_num(load.get('auc'), 2)}).")

    # --- Methoden- und Ergebnistabelle
    def mrow(label, m):
        if not m or "auc" not in m:
            return [label, "–", "–", "–", "–", "–"]
        return [label, m["n_hit"] + m["n_miss"], stats.fmt_num(m["auc"], 3), stats.fmt_num(m["d"], 2),
                stats.fmt_pct(100 * (m.get("accuracy") or 0), 0),
                f"{stats.fmt_ms(m['ttft_hit_p50'])} / {stats.fmt_ms(m['ttft_miss_p50'])}"]
    phase_tbl = {"title": "Unterscheidbarkeit (TTFT-Seitenkanal)",
                 "columns": ["Bedingung", "Messungen", "AUC", "Cohens d", "Trefferquote", "TTFT p50 Treffer / Fehlschlag"],
                 "rows": [mrow("ruhig", quiet)] + ([mrow("unter Last", load)] if load else [])}

    einstellungen = {"title": "Geprüfte Einstellung",
                     "columns": ["Parameter", "Wert"],
                     "rows": [
                         ["cache_salt", salt_txt + (f" (Feld „{p['salt_field']}“)" if p["salt_mode"] != "none" else "")],
                         ["Opfer-Präfix", f"{stats.fmt_num(p['prefix_tokens'])} Tokens"],
                         ["Last anderer Mandanten", "keine" if not p["background_load"] else f"{p['background_load']} parallele Anfragen"],
                         ["Messungen je Bedingung", stats.fmt_num(p["trials"])],
                         ["cached_tokens geliefert", "ja" if quiet.get("cached_available") else "nein"],
                     ]}

    # --- Charts
    charts = []
    th, tm = quiet.get("_th"), quiet.get("_tm")
    if th and tm:
        lo = min(min(th), min(tm))
        hi = max(max(th), max(tm))
        charts.append({"id": "dist", "title": "Antwortzeit-Verteilung: Treffer (Opfer war da) vs. Fehlschlag",
                       "type": "line", "x": {"name": "TTFT (ms)"}, "y": {"name": "Anzahl Messungen"},
                       "series": [{"name": "Treffer (gecacht)", "data": stats.histogram(th, 22, lo, hi)},
                                  {"name": "Fehlschlag (frisch)", "data": stats.histogram(tm, 22, lo, hi)}]})

    # Sweep
    sweep_rows, sweep_tbl = [], None
    sw = [r for r in recs if r.get("stage") == "sweep" and stats.is_ok(r)]
    if sw:
        by_m = {}
        for r in sw:
            by_m.setdefault(r.get("match"), []).append(r)
        base = [t for t in (_ttft(r) for r in by_m.get(0, [])) if t is not None]
        auc_pts, cached_pts = [], []
        has_cached = any(r.get("cached_tokens") is not None for r in sw)
        for m in sorted(by_m):
            rs = by_m[m]
            tt = [t for t in (_ttft(r) for r in rs) if t is not None]
            a = stats.auc([-t for t in tt], [-t for t in base]) if base and tt and m != 0 else (0.5 if m == 0 else None)
            cached_med = stats.pct([r.get("cached_tokens") for r in rs if r.get("cached_tokens") is not None], 50)
            if a is not None:
                auc_pts.append([m, round(a, 3)])
            if cached_med is not None:
                cached_pts.append([m, cached_med])
            sweep_rows.append([stats.fmt_num(m), len(rs), stats.fmt_num(a, 3) if a is not None else "–",
                               stats.fmt_ms(stats.pct(tt, 50)),
                               stats.fmt_num(cached_med) if cached_med is not None else "–"])
        charts.append({"id": "sweep_auc", "title": "Unterscheidbarkeit über die Präfix-Übereinstimmung (Block-Granularität)",
                       "type": "line", "x": {"name": "übereinstimmende Tokens"}, "y": {"name": "AUC"},
                       "series": [{"name": "AUC", "data": auc_pts}], "markLines": [{"y": 0.5, "name": "Raten"}]})
        if has_cached:
            charts.append({"id": "sweep_cached", "title": "cached_tokens über die Präfix-Übereinstimmung (direkter Oracle)",
                           "type": "line", "x": {"name": "übereinstimmende Tokens"}, "y": {"name": "cached_tokens (p50)"},
                           "series": [{"name": "cached_tokens", "data": cached_pts}]})
        sweep_tbl = {"title": "Granularitäts-Sweep (Treffer entstehen nur an Blockgrenzen)",
                     "columns": ["übereinstimmende Tokens", "Messungen", "AUC vs. 0", "TTFT p50", "cached_tokens p50"],
                     "rows": sweep_rows}

    tables = [einstellungen, phase_tbl]
    if sweep_tbl:
        tables.append(sweep_tbl)

    return {
        "verdict": verdict, "headline": head,
        "security": True,
        "kpis": {"attack_auc": {"value": round(auc, 3), "unit": "AUC (0,5 = Raten, 1,0 = perfekt)",
                                "text": f"Cohens d {stats.fmt_num(quiet['d'], 2)}"
                                        + (", cached_tokens-Leck" if leak else "")}},
        "raw": {"auc": auc, "cohens_d": quiet["d"], "cached_token_leak": bool(leak),
                "auc_under_load": load.get("auc") if load else None,
                "d_under_load": load.get("d") if load else None},
        "charts": charts,
        "tables": tables,
        "method_note": ("Treffer-Fall: Opfer sendet Präfix P, Angreifer sendet P + Zusatz. Fehlschlag-Fall: "
                        "Angreifer sendet frischen Präfix. Je Messung ist P neu zufällig. Gemessen wird die "
                        "Zeit bis zum ersten Token und cached_tokens. AUC/Cohens d bewerten, wie gut sich beide "
                        "Fälle trennen lassen – also wie zuverlässig ein Angreifer erkennt, dass ein anderer "
                        "Mandant denselben Präfix gesendet hat."),
    }
