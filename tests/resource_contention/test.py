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
    return 40 + params["baseline_s"] + params.get("attacker_alone_s", 20) + params["attack_s"]


def _roles(ctx) -> tuple[str, str]:
    if ctx.params.get("tenant_mode", "keys") == "keys" and ctx.has_tenant("angreifer"):
        return "opfer", "angreifer"
    return "opfer", "opfer"


def _now_ms(ctx) -> float:
    return round((time.perf_counter_ns() - ctx.t0_ns) / 1e6, 1)


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
                           {"stage": "victim", "phase": phase}, tenant=_roles(ctx)[0])
            frac = min(1.0, (time.monotonic() - t0) / duration)
            ctx.progress(prog_a + (prog_b - prog_a) * frac, f"Opfer misst ({phase})")
    await asyncio.gather(*(worker(i) for i in range(max(1, p["victim_concurrency"]))))


async def _noisy(ctx, stop: asyncio.Event, phase: str = "attack"):
    """Störnachbar: belegt Ressourcen bis stop gesetzt ist."""
    p = ctx.params
    mode = p["attack_mode"]
    ta = _roles(ctx)[1]

    async def backoff(rejected: bool):
        # Abgewiesene Anfragen (429 vom Gateway) nicht in einer Endlosschleife wiederholen
        if rejected:
            await asyncio.sleep(0.2)

    async def flood(i):
        wr = random.Random(ctx.rng.getrandbits(64))
        while not stop.is_set() and not ctx.cancelled:
            r = await ctx.send(ctx.chat_body(ctx.factory.make(1000, wr, unique=True), 300),
                               {"stage": "attacker", "mode": "flood", "phase": phase}, tenant=ta)
            await backoff(r.status in (429, 503))

    async def long(i):
        wr = random.Random(ctx.rng.getrandbits(64))
        while not stop.is_set() and not ctx.cancelled:
            r = await ctx.send(ctx.chat_body(ctx.factory.make(1000, wr, unique=True), p["attack_output_tokens"]),
                               {"stage": "attacker", "mode": "long", "phase": phase}, tenant=ta)
            await backoff(r.status in (429, 503))

    async def big_n(i):
        wr = random.Random(ctx.rng.getrandbits(64))
        while not stop.is_set() and not ctx.cancelled:
            body = ctx.chat_body(ctx.factory.make(1000, wr, unique=True), 200, force=False)
            body["n"] = p["attack_n"]
            body["temperature"] = 0.9
            r = await ctx.once(body, {"stage": "attacker", "mode": "big_n", "phase": phase}, record=True, tenant=ta)
            await backoff(r["status"] != 200)

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
    # Bewusst keine 16384/100000 mehr: Ein ungebremstes riesiges n kann die Engine (genau die CVE)
    # zum Absturz bringen – die Leiter zeigt die Grenze auch so (über n_ladder anpassbar).
    ladder = sorted({int(x) for x in (ctx.params.get("n_ladder") or [2, 17, 65, 129, 1024]) if int(x) > 0})
    largest_ok, reject_at, reject_msg = None, None, None
    for n in ladder:
        body = ctx.chat_body("Sag kurz Hallo.", 4, force=False)
        body["n"] = n
        body["temperature"] = 0.9
        r = await ctx.once(body, {"stage": "ncap", "n": n}, record=True, tenant=_roles(ctx)[1])
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
    r = await ctx.send(ctx.chat_body(text, 1, force=False), {"stage": "evict", "when": when},
                       tenant=_roles(ctx)[0])
    return r


async def run(ctx):
    p = ctx.params
    tv, ta = _roles(ctx)
    ctx.extra_config["attack"] = {"mode": p["attack_mode"], "attack_concurrency": p["attack_concurrency"],
                                  "attack_n": p["attack_n"], "attack_output_tokens": p["attack_output_tokens"],
                                  "victim_concurrency": p["victim_concurrency"],
                                  "tenant_mode": "keys" if ta != tv else "single"}
    phases = ctx.extra_config.setdefault("phases", {})
    if p.get("tenant_mode", "keys") == "keys" and ta == tv:
        ctx.note("Das Profil hat keinen Angreifer-Key – Opfer und Störnachbar teilen einen Key. "
                 "Grenzen je Mandant (LiteLLM) treffen dann beide; der Test misst so nur die Engine.")
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
        await ctx.send(ctx.chat_body(evict_text, 1, force=False), {"stage": "evict_prime", "when": "setup"}, tenant=tv)
        await _evict_measure(ctx, "before", evict_text)

    # Phase 1: ohne Angriff
    if ctx.cancelled:
        return
    t = _now_ms(ctx)
    await _victim_phase(ctx, "baseline", p["baseline_s"], 0.1, 0.35)
    phases["baseline"] = [t, _now_ms(ctx)]

    # Phase 2: Störnachbar allein (Bezugsgröße für seine normierte Leistung / Fairness)
    if ctx.cancelled:
        return
    if p.get("attacker_alone_s", 20) > 0:
        ctx.progress(0.37, "Störnachbar allein")
        stop = asyncio.Event()
        t = _now_ms(ctx)
        atk = asyncio.create_task(_noisy(ctx, stop, phase="alone"))
        await asyncio.sleep(p.get("attacker_alone_s", 20))
        stop.set()
        phases["alone"] = [t, _now_ms(ctx)]
        await atk
        await asyncio.sleep(2)  # laufende Anfragen ausklingen lassen

    # Phase 3: Opfer mit Störnachbar
    if ctx.cancelled:
        return
    stop = asyncio.Event()
    atk = asyncio.create_task(_noisy(ctx, stop, phase="attack"))
    try:
        await asyncio.sleep(min(6, max(2, p["attack_s"] * 0.15)))  # Störnachbar füllt den Cache
        if p["eviction_test"] and not ctx.cancelled:
            await _evict_measure(ctx, "during", evict_text)
        t = _now_ms(ctx)
        await _victim_phase(ctx, "attack", p["attack_s"], 0.45, 0.98)
        phases["attack"] = [t, _now_ms(ctx)]
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
    # Slowdown-Faktor S_N (Arbeit, Gl. 6.1): Ende-zu-Ende-Verlangsamung des Opfers durch den Störnachbarn
    slowdown = _ratio(aa["e2e_ms"]["p95"], ab["e2e_ms"]["p95"])
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

    # Fairness: normierte Leistung je Mandant (bediente gewichtete Tokens/s unter Konkurrenz ÷ allein)
    ph = cfg.get("phases") or {}

    def rate(rows, key):
        w = ph.get(key)
        if not w or w[1] <= w[0]:
            return None
        return stats.service_tokens(rows) / ((w[1] - w[0]) / 1000)

    atk_rows = [r for r in recs if r.get("stage") == "attacker"]
    a_alone = [r for r in atk_rows if r.get("phase") == "alone"]
    a_atk = [r for r in atk_rows if r.get("phase") == "attack"]
    v_base_rate, v_atk_rate = rate(base, "baseline"), rate(atk, "attack")
    a_alone_rate, a_atk_rate = rate(a_alone, "alone"), rate(a_atk, "attack")
    np_v = (v_atk_rate / v_base_rate) if v_base_rate and v_atk_rate is not None else None
    np_a = (a_atk_rate / a_alone_rate) if a_alone_rate and a_atk_rate is not None else None
    jain = stats.jain_index([np_v, np_a]) if np_v is not None and np_a is not None else None
    victim_share = (100 * v_atk_rate / (v_atk_rate + a_atk_rate)) if v_atk_rate and a_atk_rate else None
    a_rej = [r for r in a_atk if r.get("status") in (429, 503)]
    a_rej_pct = 100 * len(a_rej) / len(a_atk) if a_atk else None

    # Verdict (Verteidigersicht): rot = Opfer stark beeinträchtigt
    strong = ((ttft_ratio or 1) >= 3 or (gp_base and gp_atk is not None and gp_atk < gp_base * 0.5)
              or preempt > 0)
    moderate = ((ttft_ratio or 1) >= 1.5 or (tpot_ratio or 1) >= 1.5
                or (gp_base and gp_atk is not None and gp_atk < gp_base * 0.8))
    verdict = "rot" if strong else ("gelb" if moderate else "gruen")

    mode_txt = {"flood": "Flut", "long": "lange Anfragen", "big_n": f"großes n={p['attack_n']}",
                "combined": "Flut + großes n"}[p["attack_mode"]]
    vlabel = "VERWUNDBAR" if verdict == "rot" else ("NICHT VERWUNDBAR" if verdict == "gruen" else "Teilweise")
    head = (f"{vlabel} ({mode_txt}, {p['attack_concurrency']} parallel): Slowdown-Faktor S_N "
            f"×{stats.fmt_num(slowdown, 1) if slowdown else '–'} (Ende-zu-Ende), TTFT p95 des Opfers "
            f"{stats.fmt_ms(ab['ttft_ms']['p95'])} → {stats.fmt_ms(aa['ttft_ms']['p95'])}"
            f"{f' (×{stats.fmt_num(ttft_ratio, 1)})' if ttft_ratio else ''}, "
            f"Goodput {stats.fmt_pct(gp_base, 0)} → {stats.fmt_pct(gp_atk, 0)}.")
    if preempt:
        head += f" {preempt} Opfer-Anfragen unterbrochen/Timeout."
    if verdict == "gruen":
        head += " Geringe Verdrängung – Ratenbegrenzung je Mandant scheint zu greifen."
    if evicted and evicted["verdraengt"] is True:
        head += " Der gecachte Präfix des Opfers wurde verdrängt (cached_tokens fallen)."
    if jain is not None:
        head += (f" Fairness (Jain, normierte Leistung): J={stats.fmt_num(jain, 2)} – Opfer behält "
                 f"{stats.fmt_pct(100 * np_v, 0)}, Störnachbar {stats.fmt_pct(100 * np_a, 0)} seiner Leistung allein.")
    if a_rej_pct:
        head += f" {stats.fmt_pct(a_rej_pct, 0)} der Störnachbar-Anfragen abgewiesen (429/503 – Grenze greift)."
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
         "rows": [crow("ohne Angriff", ab), crow("unter Angriff", aa),
                  ["Slowdown-Faktor S_N", "", "", f"×{stats.fmt_num(ttft_ratio, 2)}" if ttft_ratio else "–",
                   f"×{stats.fmt_num(tpot_ratio, 2)}" if tpot_ratio else "–",
                   f"×{stats.fmt_num(slowdown, 2)}" if slowdown else "–", "", ""]]},
        {"title": "Geprüfte Einstellung",
         "columns": ["Parameter", "Wert"],
         "rows": [["Angriffsart", mode_txt], ["Störnachbar parallel", stats.fmt_num(p["attack_concurrency"])],
                  ["Opfer parallel", stats.fmt_num(p["victim_concurrency"])],
                  ["n-Grenze des Servers", (f"bis n={stats.fmt_num(lok.get('n'))} angenommen"
                                            + (f", abgelehnt ab n={stats.fmt_num(ncap.get('reject_at'))}" if ncap.get("reject_at") else " (keine erkannt)"))]]},
    ]
    tables.append({"title": "Fairness zwischen den Mandanten (normierte Leistung)",
                   "columns": ["Mandant", "Leistung allein (gew. Tokens/s)", "unter Konkurrenz", "normierte Leistung"],
                   "rows": [["Opfer", stats.fmt_num(v_base_rate, 0), stats.fmt_num(v_atk_rate, 0),
                             stats.fmt_pct(100 * np_v, 0) if np_v is not None else "–"],
                            ["Störnachbar", stats.fmt_num(a_alone_rate, 0), stats.fmt_num(a_atk_rate, 0),
                             stats.fmt_pct(100 * np_a, 0) if np_a is not None else "–"],
                            ["Jain-Index J", "", "", stats.fmt_num(jain, 3) if jain is not None else "–"],
                            ["Anteil des Opfers an der bedienten Arbeit", "", "",
                             stats.fmt_pct(victim_share, 1) if victim_share is not None else "–"],
                            ["Störnachbar abgewiesen (429/503)", "", "",
                             stats.fmt_pct(a_rej_pct, 1) if a_rej_pct is not None else "–"]]})
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
        "kpis": {"slowdown_factor": {"value": round(slowdown, 2) if slowdown else None,
                                     "unit": "Slowdown-Faktor S_N des Opfers",
                                     "text": f"TTFT ×{stats.fmt_num(ttft_ratio, 1)}, Goodput "
                                             f"{stats.fmt_pct(gp_base, 0)} → {stats.fmt_pct(gp_atk, 0)}"}},
        "matrix": {"slowdown_sn": round(slowdown, 2) if slowdown else None,
                   "victim_ttft_ratio": round(ttft_ratio, 2) if ttft_ratio else None,
                   "victim_goodput_attack_pct": gp_atk,
                   "jain_index": round(jain, 3) if jain is not None else None,
                   "victim_share_pct": round(victim_share, 1) if victim_share is not None else None,
                   "attacker_rejected_pct": round(a_rej_pct, 1) if a_rej_pct is not None else None,
                   "n_largest_accepted": lok.get("n"),
                   "noisy_vulnerable": verdict == "rot"},
        "raw": {"slowdown_factor_e2e": slowdown, "ttft_p95_ratio": ttft_ratio, "tpot_p95_ratio": tpot_ratio,
                "jain_index": jain, "np_victim": np_v, "np_attacker": np_a, "victim_share_pct": victim_share,
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
