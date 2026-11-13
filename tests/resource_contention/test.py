"""Ressourcenverdrängung / Noisy Neighbor (CVE-2026-34756).

Bei einer geteilten Inferenz-Engine konkurrieren alle Anfragen um denselben Speicher
für den KV-Cache. Der Scheduler ist fair je Anfrage, nicht je Mandant. Ein Mandant mit
vielen, sehr langen oder n-starken Anfragen belegt entsprechend viel Speicher und
verzögert oder unterbricht die Anfragen anderer.

Was der Test macht
------------------
Ein **Opfer** (gut benehmender Mandant) stellt die ganze Zeit gleichmäßig normale
Anfragen. Gemessen wird dessen Latenz in zwei Phasen:
  1. ohne Angriff  (Grundmessung),
  2. mit einem **Störnachbarn**, der den geteilten KV-Cache belegt – per Flut, sehr
     langen Anfragen oder großem n (dem Hebel aus CVE-2026-34756).
Der Vergleich zeigt, wie stark ein einzelner Mandant die Verfügbarkeit für andere senkt:
TTFT, TPOT (= Zeit je Output-Token), Goodput, Fehler und Abbrüche des Opfers.

Zusätzlich:
  - **n-Grenze:** Nimmt der Server sehr großes n an? vLLM begrenzt es nur global
    (VLLM_MAX_N_SEQUENCES, Standard 16384; Empfehlung 64–128), nicht je Mandant.
  - **Präfix-Verdrängung:** Ein gecachter Opfer-Präfix wird vor und während des Angriffs
    gemessen. Steigt seine TTFT bzw. fallen die cached_tokens, hat der Störnachbar die
    zwischengespeicherten Blöcke des Opfers verdrängt.

Ampel aus Verteidigersicht: rot = Opfer stark beeinträchtigt / verwundbar.
Nur gegen eigene Systeme mit ausdrücklicher Erlaubnis.
"""
from __future__ import annotations

import asyncio
import random
import time

from app import stats


def estimate(params):
    return 30 + params["baseline_s"] + params["attack_s"]


# ------------------------------------------------------------------ Bausteine

async def _victim_phase(ctx, phase: str, duration: float, prog_a: float, prog_b: float):
    p = ctx.params
    end = time.monotonic() + duration
    t0 = time.monotonic()

    async def worker(i):
        wr = random.Random(ctx.rng.getrandbits(64))
        while time.monotonic() < end and not ctx.cancelled:
            text = ctx.factory.make(p["victim_input"], wr, unique=True)  # frisch -> misst Verdrängung, nicht Cache
            await ctx.send(ctx.chat_body(text, p["victim_output"]),
                           {"stage": "victim", "phase": phase})
            frac = min(1.0, (time.monotonic() - t0) / duration)
            ctx.progress(prog_a + (prog_b - prog_a) * frac, f"Opfer misst ({phase})")
    await asyncio.gather(*(worker(i) for i in range(max(1, p["victim_concurrency"]))))


async def _noisy(ctx, stop: asyncio.Event):
    """Störnachbar: belegt Ressourcen bis stop gesetzt ist."""
    p = ctx.params
    mode = p["attack_mode"]

    async def flood(i):
        wr = random.Random(ctx.rng.getrandbits(64))
        while not stop.is_set() and not ctx.cancelled:
            await ctx.send(ctx.chat_body(ctx.factory.make(1000, wr, unique=True), 300),
                           {"stage": "attacker", "mode": "flood"})

    async def long(i):
        wr = random.Random(ctx.rng.getrandbits(64))
        while not stop.is_set() and not ctx.cancelled:
            await ctx.send(ctx.chat_body(ctx.factory.make(1000, wr, unique=True), p["attack_output_tokens"]),
                           {"stage": "attacker", "mode": "long"})

    async def big_n(i):
        wr = random.Random(ctx.rng.getrandbits(64))
        while not stop.is_set() and not ctx.cancelled:
            body = ctx.chat_body(ctx.factory.make(1000, wr, unique=True), 200, force=False)
            body["n"] = p["attack_n"]
            body["temperature"] = 0.9
            await ctx.once(body, {"stage": "attacker", "mode": "big_n"}, record=True)

    conc = max(1, p["attack_concurrency"])
    if mode == "flood":
        workers = [flood(i) for i in range(conc)]
    elif mode == "long":
        workers = [long(i) for i in range(conc)]
    elif mode == "big_n":
        workers = [big_n(i) for i in range(conc)]
    else:  # combined
        workers = [flood(i) for i in range(conc // 2)] + [big_n(i) for i in range(conc - conc // 2)]
    await asyncio.gather(*workers, return_exceptions=True)


async def _n_cap_probe(ctx):
    """Prüft, bis zu welchem n der Server annimmt (ohne Mandantengrenze offen)."""
    ladder = sorted({2, 64, 128, 1024, 16384, int(ctx.params["attack_n"]), 100000})
    largest_ok, reject_at, reject_msg = None, None, None
    for n in ladder:
        body = ctx.chat_body("Sag kurz Hallo.", 8, force=False)
        body["n"] = n
        body["temperature"] = 0.9
        r = await ctx.once(body, {"stage": "ncap", "n": n}, record=True)
        if r["status"] == 200:
            choices = len(((r.get("json") or {}).get("choices")) or [])
            largest_ok = {"n": n, "choices": choices}
        else:
            reject_at = n
            reject_msg = (r.get("text") or "")[:160]
            break
    ctx.extra_config["n_cap"] = {"largest_ok": largest_ok, "reject_at": reject_at, "reject_msg": reject_msg,
                                 "attack_n": int(ctx.params["attack_n"])}


async def _evict_measure(ctx, when: str, text: str):
    """Misst die TTFT des (zuvor gecachten) Opfer-Präfixes – ohne ihn neu zu primen."""
    r = await ctx.send(ctx.chat_body(text, 1, force=False), {"stage": "evict", "when": when})
    return r


async def run(ctx):
    p = ctx.params
    ctx.extra_config["attack"] = {"mode": p["attack_mode"], "attack_concurrency": p["attack_concurrency"],
                                  "attack_n": p["attack_n"], "attack_output_tokens": p["attack_output_tokens"],
                                  "victim_concurrency": p["victim_concurrency"]}
    ctx.note("Ressourcenverdrängung / Noisy Neighbor (CVE-2026-34756). Nur gegen eigene Systeme mit Erlaubnis. "
             "Die Ampel bewertet aus Verteidigersicht: rot = Opfer stark beeinträchtigt.")
    await ctx.warmup(n=10)

    # n-Grenze prüfen
    ctx.progress(0.05, "Prüfe n-Grenze")
    await _n_cap_probe(ctx)

    # Präfix-Verdrängung: Opfer-Präfix einmal cachen, ruhig messen
    evict_text = None
    if p["eviction_test"]:
        evict_text = ctx.factory.make(p["evict_prefix_tokens"], ctx.rng, unique=True)
        await ctx.send(ctx.chat_body(evict_text, 1, force=False), {"stage": "evict_prime", "when": "setup"})
        await _evict_measure(ctx, "before", evict_text)

    # Phase 1: ohne Angriff
    if ctx.cancelled:
        return
    await _victim_phase(ctx, "baseline", p["baseline_s"], 0.1, 0.4)

    # Phase 2: mit Störnachbar
    if ctx.cancelled:
        return
    stop = asyncio.Event()
    atk = asyncio.create_task(_noisy(ctx, stop))
    try:
        await asyncio.sleep(min(6, max(2, p["attack_s"] * 0.15)))  # Störnachbar füllt den Cache
        if p["eviction_test"] and not ctx.cancelled:
            await _evict_measure(ctx, "during", evict_text)
        await _victim_phase(ctx, "attack", p["attack_s"], 0.45, 0.98)
    finally:
        stop.set()
        await atk
    ctx.progress(1.0, "fertig")


# ---------------------------------------------------------------------- Auswertung

def _victim(recs, phase):
    return [r for r in recs if r.get("stage") == "victim" and r.get("phase") == phase]


def _ratio(a, b):
    if not a or not b:
        return None
    return a / b


def summarize(recs, stages, cfg, settings):
    p = cfg["params"]
    slo = settings["slo"]
    base = _victim(recs, "baseline")
    atk = _victim(recs, "attack")
    if not base or not atk:
        return {"verdict": "rot", "headline": "Keine auswertbaren Opfer-Messungen.", "kpis": {}, "charts": [], "tables": []}
    ab = stats.aggregate(base, slo)
    aa = stats.aggregate(atk, slo)

    ttft_ratio = _ratio(aa["ttft_ms"]["p95"], ab["ttft_ms"]["p95"])
    tpot_ratio = _ratio(aa["tpot_ms"]["p95"], ab["tpot_ms"]["p95"])
    gp_base = ab.get("goodput_pct")
    gp_atk = aa.get("goodput_pct")
    victim_errors = sum(1 for r in atk if not stats.is_ok(r))
    preempt = sum(1 for r in atk if r.get("error_type") in ("timeout", "abbruch", "5xx"))

    # n-Grenze
    ncap = cfg.get("n_cap") or {}
    lok = ncap.get("largest_ok") or {}
    n_uncapped = lok.get("n", 0) >= 100000 or (ncap.get("reject_at") is None)
    n_high = lok.get("n", 0) >= 1024

    # Präfix-Verdrängung
    ev = {r["when"]: r for r in recs if r.get("stage") == "evict"}
    evicted = None
    ev_before = ev.get("before")
    ev_during = ev.get("during")
    if ev_before and ev_during:
        tb = stats.ttft(ev_before)
        td = stats.ttft(ev_during)
        cb = ev_before.get("cached_tokens")
        cd = ev_during.get("cached_tokens")
        if cb is not None and cd is not None:
            verdraengt = cd < cb * 0.5            # zuverlässig: Blöcke aus dem Cache gefallen
            ev_note = ("cached_tokens unverändert – nicht verdrängt; ein TTFT-Anstieg käme hier von der "
                       "Warteschlange") if not verdraengt else "cached_tokens gefallen – Blöcke verdrängt"
        else:
            verdraengt = None                     # ohne cached_tokens nicht von Warteschlange zu trennen
            ev_note = "cached_tokens nicht geliefert – Verdrängung nicht von Warteschlange unterscheidbar"
        evicted = {"ttft_before": tb, "ttft_during": td, "cached_before": cb, "cached_during": cd,
                   "verdraengt": verdraengt, "note": ev_note}

    # Verdict (Verteidigersicht): rot = Opfer stark beeinträchtigt
    strong = ((ttft_ratio or 1) >= 3 or (gp_base and gp_atk is not None and gp_atk < gp_base * 0.5)
              or preempt > 0)
    moderate = ((ttft_ratio or 1) >= 1.5 or (tpot_ratio or 1) >= 1.5
                or (gp_base and gp_atk is not None and gp_atk < gp_base * 0.8))
    verdict = "rot" if strong else ("gelb" if moderate else "gruen")

    mode_txt = {"flood": "Flut", "long": "lange Anfragen", "big_n": f"großes n={p['attack_n']}",
                "combined": "Flut + großes n"}[p["attack_mode"]]
    head = (f"Störnachbar ({mode_txt}, {p['attack_concurrency']} parallel): TTFT p95 des Opfers "
            f"{stats.fmt_ms(ab['ttft_ms']['p95'])} → {stats.fmt_ms(aa['ttft_ms']['p95'])}"
            f"{f' (×{stats.fmt_num(ttft_ratio, 1)})' if ttft_ratio else ''}, "
            f"TPOT p95 ×{stats.fmt_num(tpot_ratio, 1) if tpot_ratio else '–'}, "
            f"Goodput {stats.fmt_pct(gp_base, 0)} → {stats.fmt_pct(gp_atk, 0)}.")
    if preempt:
        head += f" {preempt} Opfer-Anfragen unterbrochen/Timeout."
    if verdict == "gruen":
        head += " Geringe Beeinträchtigung – Begrenzung je Mandant scheint zu greifen."
    if evicted and evicted["verdraengt"] is True:
        head += " Der gecachte Präfix des Opfers wurde verdrängt (cached_tokens fallen)."
    if n_uncapped:
        head += f" ⚠ n wird nicht wirksam begrenzt (bis n={stats.fmt_num(lok.get('n'))} angenommen)."
    elif n_high:
        head += f" ⚠ n bis {stats.fmt_num(lok.get('n'))} angenommen (Empfehlung 64–128)."

    # Zeitreihe der Opfer-TTFT
    allv = sorted(base + atk, key=lambda r: r["t_send"])
    t0 = allv[0]["t_send"]
    wins = stats.windows(allv, 5)
    ser_base, ser_atk = [], []
    atk_start = min((r["t_send"] for r in atk), default=None)
    for t, w in wins:
        p95 = stats.pct([stats.ttft(r) for r in w if stats.is_ok(r)], 95)
        if p95 is None:
            continue
        pt = [round(t, 0), round(p95, 1)]
        (ser_atk if atk_start is not None and (t0 + t * 1000) >= atk_start else ser_base).append(pt)

    def crow(label, a):
        return [label, a["requests"], stats.fmt_ms(a["ttft_ms"]["p50"]), stats.fmt_ms(a["ttft_ms"]["p95"]),
                stats.fmt_ms(a["tpot_ms"]["p95"]), stats.fmt_ms(a["e2e_ms"]["p95"]),
                stats.fmt_pct(a.get("goodput_pct")), stats.fmt_pct(a["error_rate_pct"])]

    tables = [
        {"title": "Opfer-Latenz: ohne vs. mit Störnachbar",
         "columns": ["Phase", "Anfragen", "TTFT p50", "TTFT p95", "TPOT p95", "E2E p95", "Goodput", "Fehler"],
         "rows": [crow("ohne Angriff", ab), crow("unter Angriff", aa)]},
        {"title": "Geprüfte Einstellung",
         "columns": ["Parameter", "Wert"],
         "rows": [["Angriffsart", mode_txt], ["Störnachbar parallel", stats.fmt_num(p["attack_concurrency"])],
                  ["Opfer parallel", stats.fmt_num(p["victim_concurrency"])],
                  ["n-Grenze des Servers", (f"bis n={stats.fmt_num(lok.get('n'))} angenommen"
                                            + (f", abgelehnt ab n={stats.fmt_num(ncap.get('reject_at'))}" if ncap.get("reject_at") else " (keine erkannt)"))]]},
    ]
    if evicted:
        tables.append({"title": "Präfix-Verdrängung (gecachter Opfer-Präfix)",
                       "columns": ["Zeitpunkt", "TTFT", "cached_tokens"],
                       "rows": [["vor Angriff", stats.fmt_ms(evicted["ttft_before"]),
                                 evicted["cached_before"] if evicted["cached_before"] is not None else "–"],
                                ["während Angriff", stats.fmt_ms(evicted["ttft_during"]),
                                 evicted["cached_during"] if evicted["cached_during"] is not None else "–"],
                                ["Bewertung", evicted["note"], ""]]})

    charts = [{"id": "ttft_time", "title": "Opfer-TTFT p95 über die Zeit (ohne → mit Störnachbar)",
               "type": "line", "x": {"name": "Sekunde"}, "y": {"name": "TTFT p95 (ms)"},
               "series": [{"name": "ohne Angriff", "data": ser_base}, {"name": "unter Angriff", "data": ser_atk}],
               "markLines": [{"y": slo["ttft_p95_ms"], "name": "Ziel"}]}]

    return {
        "verdict": verdict, "headline": head, "security": True,
        "kpis": {"victim_ttft_factor": {"value": round(ttft_ratio, 2) if ttft_ratio else None,
                                        "unit": "× TTFT p95 des Opfers",
                                        "text": f"Goodput {stats.fmt_pct(gp_base, 0)} → {stats.fmt_pct(gp_atk, 0)}"}},
        "raw": {"ttft_p95_ratio": ttft_ratio, "tpot_p95_ratio": tpot_ratio,
                "goodput_base": gp_base, "goodput_attack": gp_atk, "victim_preempted": preempt,
                "n_uncapped": n_uncapped, "largest_n_accepted": lok.get("n"),
                "prefix_evicted": bool(evicted and evicted["verdraengt"])},
        "charts": charts, "tables": tables,
        "method_note": ("Das Opfer stellt in beiden Phasen dieselben, gleichmäßigen Anfragen (frische Prompts). "
                        "Der Störnachbar läuft nur in Phase 2 und belegt den geteilten KV-Cache. Der Unterschied "
                        "in TTFT, TPOT und Goodput ist die Beeinträchtigung, die ein Mandant dem anderen zufügt. "
                        "Da vLLM je Anfrage fair plant (nicht je Mandant), erhält der Mandant mit mehr Anfragen "
                        "mehr Ressourcen (Noisy Neighbor)."),
    }
