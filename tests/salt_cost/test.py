"""Kosten der Cache-Trennung (Salting) – was cache_salt je Mandant an Zeit, Durchsatz und Geld kostet.

cache_salt je Mandant (K1, K4) schließt den Prefix-Cache-Seitenkanal (CVE-2025-46570): Gleiche
Präfixe verschiedener Mandanten landen in verschiedenen Cache-Blöcken. Der Preis ist, dass Mandanten
gemeinsame Vorlagen (Firmenrichtlinie, Tool-Beschreibungen, RAG-Kontext) nicht mehr aus dem Cache
der anderen nutzen können. „Leistungskosten der Isolation“ misst den eingeschwungenen Zustand mit
drei Mandanten – dort ist der Unterschied klein, weil jeder Mandant seine Kopie schnell im Cache hat.
Die Kosten zeigen sich an zwei anderen Stellen:

1. Kaltstart. Mandant A berechnet eine neue Vorlage. Danach sendet ein ANDERER Mandant dieselbe
   Vorlage mit eigener Frage („neu“), und gleich noch einmal („warm“). Ohne Salt ist schon „neu“ ein
   Cache-Treffer, mit Salt erst „warm“. Aufschlag je neuem Mandanten = TTFT(neu) − TTFT(warm).
   Sequenziell ohne Last, mehrere Vorlagenlängen, je Wiederholung eine neue Vorlage.

2. Viele Mandanten. N Mandanten (1 … 30; Haupt-, Angreifer-, Last-Key und die Pool-Mandanten aus
   ./scenario.sh init) senden abwechselnd Chats mit derselben Vorlage, immer gleich viele parallel.
   Mit Salt liegt die Vorlage N-mal im KV-Cache. Der ist in allen Szenarien gleich groß
   (KV_CACHE_MEMORY ≈ 6 500 Tokens: 965 MB bei Qwen3-8B, 750 MB bei Qwen3-0.6B – siehe Modellprofile in
   deploy/scenario.sh). Solange N × Vorlage hineinpasst,
   kostet der Salt kaum etwas; darüber verdrängen sich die Kopien, Treffer fallen, TTFT steigt und
   der Durchsatz sinkt. Ohne Salt (K0) bleibt alles wie bei einem Mandanten.

Die Seite „Konfigurationen“ rechnet aus dem Durchsatz die Kosten je 1 Mio. Output-Tokens
(Annahmen unter Einstellungen → Kosten) und zeigt Δ % zu K0.
"""
from __future__ import annotations

import itertools
import random

from app import stats

MAIN_ROLES = ("opfer", "angreifer", "last")
COLD_OUTPUT = 16
PHASES = ("erster", "neu", "warm")


def estimate(params):
    # gemessen auf RTX 3050 Ti: ~0,25 s je Kaltstart-Anfrage, je Mandantenzahl Dauer + ~1 s
    cold = len(params.get("cold_template_tokens") or []) * int(params.get("cold_reps") or 0) * 3 * 0.25
    counts = [int(n) for n in params.get("tenant_counts") or [] if int(n) > 0]
    scale = sum(int(params["duration_s"]) + 1 + 0.05 * n for n in counts)
    return 15 + cold + scale


def _body(ctx, system: str, user: str, max_tokens: int, force: bool = True) -> dict:
    body = ctx.chat_body(user, max_tokens, force=force, temperature=0.7)
    body["messages"] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return body


def _max_template(ctx, wanted: int, user_tokens: int, output_tokens: int) -> int:
    limit = int(ctx.profile.get("max_context") or 0)
    if not limit:
        return wanted
    return max(16, min(wanted, limit - user_tokens - output_tokens - 96))


async def run(ctx):
    p = ctx.params
    user_tokens = int(p["user_tokens"])
    main = [r for r in MAIN_ROLES if ctx.has_tenant(r)]
    roles_all = main + list(getattr(ctx, "pool_roles", []))
    ctx.extra_config["workload"] = {"tenants_available": len(roles_all), "pool": len(roles_all) - len(main),
                                    "template_tokens": p["template_tokens"], "user_tokens": user_tokens,
                                    "output_tokens": p["output_tokens"], "concurrency": p["concurrency"]}

    await ctx.warmup(n=6, output_tokens=32, concurrency=3)

    # -------------------------------------------------------------- 1) Kaltstart
    lengths = [_max_template(ctx, int(x), user_tokens, COLD_OUTPUT) for x in p["cold_template_tokens"] if int(x) > 0]
    lengths = sorted(set(lengths))
    reps = max(1, int(p["cold_reps"]))
    if len(roles_all) < 2:
        ctx.note("Für den Kaltstart braucht es zwei Mandanten mit eigenen Keys (Angreifer-Key im Profil oder "
                 "Pool-Mandanten aus ./scenario.sh init) – Teil 1 übersprungen.")
        lengths = []
    first, second = (roles_all[0], roles_all[1]) if len(roles_all) >= 2 else (None, None)
    total_cold = len(lengths) * reps
    done = 0
    for L in lengths:
        for rep in range(reps):
            if ctx.cancelled:
                return
            trng = random.Random(ctx.rng.getrandbits(64))
            # eindeutiger Anfang: Diese Vorlage hat noch niemand berechnet
            template = "Vorlage " + ctx.factory.unique_prefix(trng) + ctx.factory._filler(L, trng)
            for phase, tenant in (("erster", first), ("neu", second), ("warm", second)):
                user = ctx.factory.unique_prefix(trng) + ctx.factory._filler(user_tokens, trng)
                await ctx.send(_body(ctx, template, user, COLD_OUTPUT),
                               {"stage": "cold", "phase": phase, "template_tokens": L, "rep": rep}, tenant=tenant)
            done += 1
            ctx.progress(0.3 * done / max(1, total_cold), f"Kaltstart: Vorlage {L} Tokens ({rep + 1}/{reps})")

    # -------------------------------------------------------------- 2) Viele Mandanten
    wanted = sorted({int(n) for n in p["tenant_counts"] if int(n) > 0})
    if wanted and max(wanted) > len(roles_all):
        ctx.note(f"Nur {len(roles_all)} Mandanten-Keys vorhanden, gewünscht bis {max(wanted)}. Weitere Mandanten "
                 "legt „Einrichten“ an (./scenario.sh init, danach Szenario neu starten).")
    counts = sorted({min(n, len(roles_all)) for n in wanted if roles_all})
    L = _max_template(ctx, int(p["template_tokens"]), user_tokens, int(p["output_tokens"]))
    template = "Firmenvorlage für alle Mandanten: " + ctx.factory._filler(L, random.Random(4343))
    conc = max(1, int(p["concurrency"]))
    for ci, n in enumerate(counts):
        if ctx.cancelled:
            return
        roles = roles_all[:n]
        ctx.stage({"tenants": n, "event": "start"})
        ctx.progress(0.3 + 0.7 * ci / len(counts), f"{n} Mandant(en), {conc} parallel")
        # je Mandant einmal vorwärmen (1 Token, nicht gewertet)
        for r in roles:
            if ctx.cancelled:
                return
            user = ctx.factory.unique_prefix(ctx.rng) + ctx.factory._filler(user_tokens, ctx.rng)
            await ctx.send(_body(ctx, template, user, 1, force=False), {"stage": "prime"}, record=False, tenant=r)
        turn = itertools.count()

        def make_request(i, wrng, roles=roles, n=n):
            role = roles[next(turn) % n]   # reihum, damit alle Mandanten gleich oft dran sind
            user = ctx.factory.unique_prefix(wrng) + ctx.factory._filler(user_tokens, wrng)
            return (_body(ctx, template, user, int(p["output_tokens"])),
                    {"stage": "scale", "tenants": n, "tenant": role})

        await ctx.closed_load(conc, make_request, duration_s=float(p["duration_s"]))
        ctx.stage({"tenants": n, "event": "end"})
    ctx.extra_config["workload"]["scale_template_tokens"] = L
    ctx.progress(1.0, "fertig")


# ---------------------------------------------------------------------- Auswertung

def _hit_pct(rows: list[dict]) -> float | None:
    ok = [r for r in rows if stats.is_ok(r) and r.get("cached_tokens") is not None]
    prompt = sum(r.get("prompt_tokens") or 0 for r in ok)
    return round(100 * sum(r["cached_tokens"] for r in ok) / prompt, 1) if ok and prompt else None


def _prefill_tokens(rows: list[dict]) -> float | None:
    """Im Mittel tatsächlich berechnete Prompt-Tokens je Anfrage (prompt − cached)."""
    ok = [r for r in rows if stats.is_ok(r) and r.get("cached_tokens") is not None and r.get("prompt_tokens")]
    return round(sum(r["prompt_tokens"] - r["cached_tokens"] for r in ok) / len(ok), 0) if ok else None


def _p50(rows: list[dict]) -> float | None:
    v = stats.pct([stats.ttft(r) for r in rows if stats.is_ok(r)], 50)
    return None if v is None else round(v, 1)


def summarize(recs, stages, cfg, settings):
    p = cfg["params"]
    slo = settings["slo"]
    cost = settings.get("cost") or {}
    sc = cfg.get("deploy_scenario") or {}
    wl = cfg.get("workload") or {}
    cold = [r for r in recs if r.get("stage") == "cold"]
    scale = [r for r in recs if r.get("stage") == "scale"]
    if not cold and not scale:
        return {"verdict": "rot", "headline": "Keine Messungen – lief der Endpunkt?", "kpis": {}, "charts": [], "tables": []}

    # ---------------------------------------------------------- Kaltstart
    lengths = sorted({r["template_tokens"] for r in cold})
    cb: dict[int, dict] = {}
    for L in lengths:
        rows = {ph: [r for r in cold if r["template_tokens"] == L and r.get("phase") == ph] for ph in PHASES}
        t = {ph: _p50(rows[ph]) for ph in PHASES}
        extra = round(t["neu"] - t["warm"], 1) if t["neu"] is not None and t["warm"] is not None else None
        cb[L] = {**t, "extra": extra, "hit_neu": _hit_pct(rows["neu"]), "hit_warm": _hit_pct(rows["warm"]),
                 "n": min(len(v) for v in rows.values())}
    Lc = max(lengths) if lengths else None
    C = cb.get(Lc) or {}

    # ---------------------------------------------------------- Viele Mandanten
    counts = sorted({r["tenants"] for r in scale})
    per: dict[int, dict] = {}
    for n in counts:
        rows = [r for r in scale if r["tenants"] == n]
        a = stats.aggregate(rows, slo)
        a["cache_hit_pct"] = _hit_pct(rows)
        a["prefill_tokens"] = _prefill_tokens(rows)
        a["rejected_pct"] = round(100 * sum(1 for r in rows if r.get("status") in (429, 503)) / len(rows), 1)
        a["eur_mtok"] = stats.eur_per_million(a.get("output_tps") or 0, cost) if cost else None
        per[n] = a
    n1, nmax = (counts[0], counts[-1]) if counts else (None, None)
    A1, AN = per.get(n1) or {}, per.get(nmax) or {}
    t1 = (A1.get("ttft_ms") or {}).get("p50")
    h1 = A1.get("cache_hit_pct")
    n_ok = None
    if h1 is not None and h1 >= 10:
        for n in counts:   # größte Mandantenzahl, bis zu der die Trefferquote mindestens 90 % von 1 Mandant erreicht
            hn = per[n].get("cache_hit_pct")
            if hn is None or hn < 0.9 * h1:
                break
            n_ok = n
    tps_change = (round(100 * (AN["output_tps"] / A1["output_tps"] - 1), 1)
                  if A1.get("output_tps") and AN.get("output_tps") is not None and nmax != n1 else None)

    matrix = {
        "salt_new_tenant_ttft_ms": C.get("neu"), "salt_new_tenant_extra_ms": C.get("extra"),
        "salt_new_tenant_hit_pct": C.get("hit_neu"), "salt_cold_template_tokens": Lc,
        "salt_tenants_max": nmax, "salt_hit_pct_nmax": AN.get("cache_hit_pct"),
        "salt_ttft_p50_nmax_ms": (AN.get("ttft_ms") or {}).get("p50"), "salt_tps_nmax": AN.get("output_tps"),
        "salt_tps_change_pct": tps_change, "salt_tenants_ok": n_ok,
        "salt_template_tokens": wl.get("scale_template_tokens") or p.get("template_tokens"),
    }

    errs = [r for r in cold + scale if not stats.is_ok(r) and r.get("status") not in (429, 503)]
    err_pct = 100 * len(errs) / max(1, len(cold) + len(scale))
    verdict = "rot" if err_pct > 5 else ("gelb" if err_pct > 0 or (AN.get("rejected_pct") or 0) > 0 else "gruen")

    head = f"Szenario {sc['id']}: " if sc.get("id") else ""
    if C:
        ex = C.get("extra")
        head += f"Ein neuer Mandant wartet bei einer Vorlage mit {Lc} Tokens {stats.fmt_ms(C.get('neu'))} auf das erste Token"
        if ex is not None and C.get("warm") and ex > max(5.0, 0.1 * C["warm"]):
            head += f", {stats.fmt_ms(ex)} mehr als bei seiner zweiten Anfrage"
        elif ex is not None:
            head += ", kaum länger als bei seiner zweiten Anfrage"
        hn = C.get("hit_neu")
        head += (f" (Vorlage zu {stats.fmt_pct(hn, 0)} aus dem Cache eines anderen Mandanten). " if hn is not None
                 else ". ")
    if counts:
        hit1, hitn = A1.get("cache_hit_pct"), AN.get("cache_hit_pct")
        head += (f"Bei {nmax} Mandanten mit gemeinsamer Vorlage: TTFT p50 {stats.fmt_ms(AN['ttft_ms']['p50'])} "
                 f"(1 Mandant: {stats.fmt_ms(t1)})")
        if hitn is not None and hit1 is not None:
            head += f", Cache-Treffer {stats.fmt_pct(hitn, 0)} (1 Mandant: {stats.fmt_pct(hit1, 0)})"
        if tps_change is not None:
            head += f", Durchsatz {'+' if tps_change >= 0 else ''}{stats.fmt_num(tps_change, 1)} %"
        head += "."
        if n_ok is not None and n_ok < nmax:
            head += f" Bis {n_ok} Mandanten bleibt die Trefferquote auf dem Niveau eines Mandanten, darüber verdrängen sich die Kopien."
    if errs:
        head += f" {len(errs)} Anfragen fehlgeschlagen."
    head += " Kosten je 1 Mio. Tokens und Δ % zu K0: Seite „Konfigurationen“."

    tables = []
    if cb:
        tables.append({
            "title": "Kaltstart: erste Anfrage eines neuen Mandanten (TTFT p50)",
            "columns": ["Vorlage (Tokens)", "Mandant A, neue Vorlage", "anderer Mandant, 1. Anfrage",
                        "derselbe, 2. Anfrage", "Aufschlag neuer Mandant", "Cache-Treffer 1. Anfrage",
                        "Cache-Treffer 2. Anfrage", "Wiederholungen"],
            "rows": [[L, stats.fmt_ms(v["erster"]), stats.fmt_ms(v["neu"]), stats.fmt_ms(v["warm"]),
                      stats.fmt_ms(v["extra"]), stats.fmt_pct(v["hit_neu"], 0) if v["hit_neu"] is not None else "–",
                      stats.fmt_pct(v["hit_warm"], 0) if v["hit_warm"] is not None else "–", v["n"]]
                     for L, v in cb.items()]})
    if per:
        Ls = matrix["salt_template_tokens"] or 0
        tables.append({
            "title": f"Viele Mandanten: gemeinsame Vorlage {Ls} Tokens, {p['concurrency']} Anfragen parallel",
            "columns": ["Mandanten", "Vorlage × Mandanten (Tokens)", "Anfragen", "Cache-Treffer",
                        "berechnete Prompt-Tokens je Anfrage", "TTFT p50", "TTFT p95", "TPOT p50", "Output-Tokens/s",
                        "€ je 1 Mio. Output-Tokens", "abgewiesen"],
            "rows": [[n, f"{n * Ls:,}".replace(",", "."), a["requests"],
                      stats.fmt_pct(a["cache_hit_pct"], 1) if a["cache_hit_pct"] is not None else "–",
                      stats.fmt_num(a["prefill_tokens"]) if a["prefill_tokens"] is not None else "–",
                      stats.fmt_ms(a["ttft_ms"]["p50"]), stats.fmt_ms(a["ttft_ms"]["p95"]), stats.fmt_ms(a["tpot_ms"]["p50"]),
                      stats.fmt_num(a.get("output_tps"), 1),
                      (stats.fmt_num(a["eur_mtok"], 2) + " €") if a.get("eur_mtok") else "–",
                      stats.fmt_pct(a["rejected_pct"], 1)] for n, a in per.items()]})
    tables.append({"title": "Last", "columns": ["Parameter", "Wert"], "rows": [
        ["Mandanten verfügbar", f"{wl.get('tenants_available', '?')} (davon {wl.get('pool', 0)} Pool-Mandanten)"],
        ["Kaltstart", f"Vorlagen {', '.join(str(L) for L in lengths) or '–'} Tokens, je {p['cold_reps']}× neu, "
                      f"Output {COLD_OUTPUT} Tokens, nacheinander ohne Last"],
        ["Viele Mandanten", f"Mandantenzahlen {', '.join(str(n) for n in counts) or '–'}, je {p['duration_s']} s, "
                            f"{p['concurrency']} parallel, reihum"],
        ["Nutzerfrage / Output", f"{p['user_tokens']} Tokens eindeutig / {p['output_tokens']} Tokens"],
        ["Kostenannahme", (f"{stats.fmt_num(stats.eur_per_hour(cost), 2)} €/h bei {cost.get('utilization_pct')} % Auslastung "
                           "(Einstellungen → Kosten)") if cost else "–"],
        ["Szenario", f"{sc.get('id')} – {sc.get('name')}" if sc.get("id") else "– (nicht lokal erkannt)"]]})

    charts = []
    if cb:
        charts.append({"id": "cold_ttft", "title": "Kaltstart: TTFT je Vorlagenlänge", "type": "line",
                       "x": {"name": "Vorlage (Tokens)"}, "y": {"name": "ms (p50)"},
                       "series": [{"name": nm, "data": [[L, v[ph]] for L, v in cb.items() if v[ph] is not None]}
                                  for ph, nm in (("erster", "Mandant A, neue Vorlage"),
                                                 ("neu", "anderer Mandant, 1. Anfrage"),
                                                 ("warm", "derselbe, 2. Anfrage"))]})
    if per:
        if any(a["cache_hit_pct"] is not None for a in per.values()):
            charts.append({"id": "scale_hit", "title": "Cache-Treffer über die Mandantenzahl", "type": "line",
                           "x": {"name": "Mandanten"}, "y": {"name": "% der Prompt-Tokens"},
                           "series": [{"name": "Cache-Treffer", "data": [[n, a["cache_hit_pct"]] for n, a in per.items()
                                                                         if a["cache_hit_pct"] is not None]}]})
        charts.append({"id": "scale_ttft", "title": "TTFT über die Mandantenzahl", "type": "line",
                       "x": {"name": "Mandanten"}, "y": {"name": "ms"},
                       "series": [{"name": "TTFT p50", "data": [[n, a["ttft_ms"]["p50"]] for n, a in per.items()]},
                                  {"name": "TTFT p95", "data": [[n, a["ttft_ms"]["p95"]] for n, a in per.items()]}]})
        charts.append({"id": "scale_tps", "title": "Durchsatz über die Mandantenzahl", "type": "line",
                       "x": {"name": "Mandanten"}, "y": {"name": "Output-Tokens/s"},
                       "series": [{"name": "Output-Tokens/s", "data": [[n, a.get("output_tps")] for n, a in per.items()]}]})

    kpis = {}
    if C:
        kpis["salt_new_tenant_extra_ms"] = {"value": C.get("extra"), "unit": "ms Aufschlag je neuem Mandanten",
                                            "text": f"Vorlage {Lc} Tokens"}
    if counts:
        kpis["salt_tps_nmax"] = {"value": AN.get("output_tps"), "unit": f"Output-Tokens/s bei {nmax} Mandanten",
                                 "text": f"1 Mandant: {stats.fmt_num(A1.get('output_tps'), 1)}"}
    return {
        "verdict": verdict, "headline": head, "kpis": kpis, "matrix": matrix,
        "charts": charts, "tables": tables,
        "method_note": ("Kaltstart: je Wiederholung eine neue Vorlage mit eindeutigem Anfang; Mandant A, dann ein anderer "
                        "Mandant zweimal, nacheinander ohne Last; TTFT als Median. Viele Mandanten: geschlossene Last mit "
                        "fester Parallelität, Anfragen reihum über die Mandanten (eigene Virtual Keys), dieselbe Vorlage "
                        "in jedem Lauf und jeder Konfiguration (fester Seed), je Mandant eine ungewertete Anfrage vorweg. "
                        "Cache-Treffer = cached_tokens / prompt_tokens (fehlt, wenn die Engine sie nicht meldet, z. B. K4). "
                        "Viele Keys kosten auch im Gateway etwas (Authentifizierung, Zählung) – das wirkt in allen Szenarien "
                        "gleich. Die Kosten des Saltings sind deshalb der Unterschied zu K0 bei gleicher Mandantenzahl "
                        "(Seite „Konfigurationen“)."),
    }
