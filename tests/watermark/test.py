"""Wasserzeichen (KI-VO Art. 50 Abs. 2) – Erkennung, Fehlalarme, Opt-out, greedy.

Ablauf
  1. Je Textlänge `samples` Texte mit temperature > 0 erzeugen (Mandant A) und an den Detektor des
     Betreibers schicken (POST /detect {"text"} -> p_value, is_watermarked).
  2. Gegenprobe: von Menschen geschriebene Texte (unten) – dürfen nie als markiert gelten.
  3. Opt-out: dieselbe Anfrage mit watermarking=false. Bei enforce=true bleibt das Wasserzeichen.
  4. greedy (temperature 0): vLLM kann hier kein Gumbel-Wasserzeichen setzen (dokumentierte Grenze).

Gegen eine Konfiguration OHNE Wasserzeichen (K0) gemessen liefert der Test die Fehlalarmrate auf
Modelltexten; gegen K6/K4 die Erkennungsrate. Die Leistungskosten zeigt der Konfigurationsvergleich.
"""
from __future__ import annotations

import random
import statistics

from app import stats

TOPICS = [
    "die Geschichte der Stadt Offenburg", "wie ein Wärmepumpen-Heizsystem funktioniert",
    "Vor- und Nachteile von Homeoffice für kleine Unternehmen", "den Wasserkreislauf der Erde",
    "die Planung einer Fahrradtour durch den Schwarzwald", "Datenschutz in einer Arztpraxis",
    "die Entwicklung der Eisenbahn in Deutschland", "gesunde Ernährung im Berufsalltag",
    "how solar panels convert light into electricity", "the role of bees in agriculture",
    "Grundlagen der doppelten Buchführung", "die Organisation eines Vereinsfestes",
]

# Von Menschen geschriebene Kontrolltexte (Gegenprobe gegen Fehlalarme)
HUMAN = [
    "Am Montag beginnt die Sanierung der Brücke über die Kinzig. Für die Dauer der Arbeiten wird der Verkehr "
    "über die Umleitung im Norden geführt. Radfahrer können die Baustelle auf einem gesicherten Streifen passieren. "
    "Die Stadt rechnet mit einer Bauzeit von etwa vier Monaten und bittet Anwohner um Verständnis für den Lärm in "
    "den frühen Morgenstunden. Fragen beantwortet das Bürgerbüro im Rathaus zu den üblichen Öffnungszeiten.",
    "Für den Hefeteig zuerst die Milch leicht erwärmen und die Hefe darin auflösen. Mehl, Zucker und eine Prise "
    "Salz in einer Schüssel mischen, die Hefemilch dazugeben und alles zu einem glatten Teig kneten. Den Teig "
    "abgedeckt an einem warmen Ort gehen lassen, bis er sich sichtbar vergrößert hat. Danach noch einmal kurz "
    "durchkneten, zu Zöpfen formen und bei mittlerer Hitze goldbraun backen.",
    "Die Mitgliederversammlung hat den Vorstand einstimmig entlastet. Der Kassenwart berichtete von einem leichten "
    "Überschuss, der vor allem auf die gut besuchte Sommerveranstaltung zurückgeht. Für das kommende Jahr sind "
    "zwei Ausflüge und ein Kurs für Erste Hilfe geplant. Neue Mitglieder sind jederzeit willkommen; der "
    "Jahresbeitrag bleibt unverändert. Die nächste Sitzung findet im Frühjahr im Gemeindesaal statt.",
    "The library will extend its opening hours during the exam period. From the first of the month, the reading "
    "rooms stay open until midnight on weekdays, and group study rooms can be booked online up to a week in "
    "advance. Please remember that food is not allowed near the shelves and that phones should be kept on silent. "
    "Lost items are collected at the front desk and kept for two weeks.",
    "Bei der Inspektion wurden die Bremsbeläge vorne erneuert und die Flüssigkeiten geprüft. Der Reifendruck war "
    "auf der rechten Seite etwas zu niedrig und wurde korrigiert. Am Unterboden zeigte sich leichter Rost, der "
    "beobachtet werden sollte, aktuell aber keine Reparatur erfordert. Die nächste Hauptuntersuchung ist im "
    "kommenden Herbst fällig; wir empfehlen, den Termin rechtzeitig zu vereinbaren.",
    "Unser Team hat die Umstellung auf das neue Ticketsystem am Wochenende abgeschlossen. Alte Anfragen wurden "
    "übernommen und behalten ihre Nummern. Wer ab heute ein Problem meldet, erhält automatisch eine Bestätigung "
    "per E-Mail. In den ersten Tagen kann es zu längeren Antwortzeiten kommen, weil sich alle an die neuen Abläufe "
    "gewöhnen müssen. Rückmeldungen zur Bedienung nehmen wir gern entgegen.",
]


def estimate(params):
    n = len(params["lengths"]) * params["samples"]
    n += params["samples"] * (int(params["check_opt_out"]) + int(params["check_greedy"]))
    return 20 + n * 2.5


def _detector(ctx) -> str:
    return ((ctx.params.get("detector_url") or ctx.profile.get("watermark_detector_url")
             or "http://127.0.0.1:8090").strip().rstrip("/"))


async def _detect(ctx, url: str, text: str) -> dict | None:
    try:
        r = await ctx.client.post(url + "/detect", json={"text": text}, timeout=60)
        return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:160]}


def _write(ctx, row: dict) -> None:
    ctx._req_seq += 1
    ctx.writer.write({"seq": ctx._req_seq, **row})


async def run(ctx):
    p = ctx.params
    url = _detector(ctx)
    ctx.extra_config["detector_url"] = url
    try:
        h = await ctx.client.get(url + "/health", timeout=10)
        ctx.extra_config["detector"] = h.json() if h.status_code == 200 else {"error": f"HTTP {h.status_code}"}
    except Exception as e:  # noqa: BLE001
        ctx.extra_config["detector"] = {"error": f"{type(e).__name__}: {e}"[:200]}
        ctx.note(f"Detektor {url} nicht erreichbar. Starten: deploy/scenario.sh up K6 (oder K4) bzw. "
                 "./scenario.sh detector up.")
        return
    rng = random.Random(ctx.seed)
    temp = float(p["temperature"])
    jobs = [("standard", L, temp, None) for L in p["lengths"] for _ in range(p["samples"])]
    ref_len = 200 if 200 in p["lengths"] else max(p["lengths"])
    if p["check_opt_out"]:
        jobs += [("opt_out", ref_len, temp, False) for _ in range(p["samples"])]
    if p["check_greedy"]:
        jobs += [("greedy", ref_len, 0.0, None) for _ in range(max(2, p["samples"] // 2))]
    total = len(jobs) + len(HUMAN)
    for i, (cond, L, t, wm) in enumerate(jobs):
        if ctx.cancelled:
            return
        ctx.progress(i / total, f"{cond}: {L} Tokens ({i + 1}/{len(jobs)})")
        topic = rng.choice(TOPICS)
        body = ctx.chat_body(f"Schreibe einen ausführlichen, gut lesbaren Text über {topic}.", int(L),
                             force=False, temperature=t, min_tokens=int(L))
        if wm is not None:
            body["watermarking"] = wm
        r = await ctx.send(body, {"stage": "gen", "cond": cond, "length": L}, keep_text=True)
        if not r.ok or not r.text:
            continue
        d = await _detect(ctx, url, r.text)
        _write(ctx, {"stage": "detect", "cond": cond, "length": L, "tokens": r.completion_tokens, **(d or {})})
    for j, text in enumerate(HUMAN):
        ctx.progress((len(jobs) + j) / total, "Gegenprobe mit Menschentexten")
        d = await _detect(ctx, url, text)
        _write(ctx, {"stage": "detect", "cond": "human", "length": None, **(d or {})})
    ctx.progress(1.0, "fertig")


# ---------------------------------------------------------------------- Auswertung

def _rate(rows):
    v = [r for r in rows if "is_watermarked" in r]
    return (sum(1 for r in v if r["is_watermarked"]) / len(v)) if v else None, len(v)


def _ci_txt(rate, n) -> str:
    """95%-KI nach Wilson als Text, z. B. „57–93 %“."""
    if rate is None or not n:
        return "–"
    ci = stats.wilson_ci(round(rate * n), n)
    return f"{stats.fmt_num(100 * ci[0], 0)}–{stats.fmt_num(100 * ci[1], 0)} %" if ci else "–"


def optout_blocked(opt, std_ref, fpr) -> bool | None:
    """Wirkt watermarking=false? Vergleich mit den beiden Bezugsgrößen statt mit einer festen Schwelle.

    Greift enforce, sind die Opt-out-Texte genauso markiert wie normale Texte GLEICHER Länge (Erkennung
    ≈ std_ref). Wirkt das Opt-out, sind sie unmarkiert (Erkennung ≈ Fehlalarmrate fpr). Entschieden wird
    nach der näheren Bezugsgröße (Mittelpunktregel). Früher: Vergleich mit min_rate (90 %) – das hängt an
    der Empfindlichkeit des Detektors und meldete „Opt-out möglich“, obwohl enforce griff.
    """
    if opt is None or std_ref is None:
        return None
    return opt >= (std_ref + (fpr or 0.0)) / 2


def summarize(recs, stages, cfg, settings):
    p = cfg["params"]
    det = cfg.get("detector") or {}
    D = [r for r in recs if r.get("stage") == "detect"]
    if det.get("error") or not D:
        return {"verdict": None, "headline": f"Nicht geprüft: Detektor nicht erreichbar ({det.get('error') or 'keine Messungen'}). "
                                            "Starten mit deploy/scenario.sh detector up (läuft in K4/K6 automatisch).",
                "kpis": {}, "charts": [], "tables": [], "matrix": {"wm_detect_rate": None}}
    std = [r for r in D if r["cond"] == "standard"]
    lengths = sorted({r["length"] for r in std})
    rows, pts = [], []
    for L in lengths:
        rs = [r for r in std if r["length"] == L]
        rate, n = _rate(rs)
        pv = [r["p_value"] for r in rs if r.get("p_value") is not None]
        rows.append([L, n, (stats.fmt_pct(100 * rate, 0) + f" ({_ci_txt(rate, n)})") if rate is not None else "–",
                     f"{statistics.median(pv):.2g}" if pv else "–",
                     stats.fmt_num(statistics.median([r.get("num_scored_tokens") or 0 for r in rs]), 0) if rs else "–"])
        if rate is not None:
            pts.append([L, round(100 * rate, 1)])
    long_rows = [r for r in std if (r["length"] or 0) >= 200] or std
    rate_long, n_long = _rate(long_rows)
    fpr, n_h = _rate([r for r in D if r["cond"] == "human"])
    opt_rows = [r for r in D if r["cond"] == "opt_out"]
    opt, n_o = _rate(opt_rows)
    ref_len = opt_rows[0]["length"] if opt_rows else None
    std_ref, n_ref = _rate([r for r in std if r["length"] == ref_len]) if ref_len else (None, 0)
    blocked = optout_blocked(opt, std_ref, fpr)
    gre, n_g = _rate([r for r in D if r["cond"] == "greedy"])

    ok_rate = rate_long is not None and rate_long >= float(p["min_rate"])
    if rate_long is None or rate_long < 0.5:
        verdict = "rot"
    elif ok_rate and not fpr and blocked is not False:
        verdict = "gruen"
    else:
        verdict = "gelb"
    sc = cfg.get("deploy_scenario") or {}
    head = (f"{'Szenario ' + sc['id'] + ': ' if sc.get('id') else ''}Wasserzeichen ab 200 Tokens in "
            f"{stats.fmt_pct(100 * (rate_long or 0), 0)} der Texte nachgewiesen (n={n_long}, 95%-KI "
            f"{_ci_txt(rate_long, n_long)}), Fehlalarme bei "
            f"Menschentexten {stats.fmt_pct(100 * (fpr or 0), 0)} (n={n_h}).")
    if opt is not None:
        ref_txt = (f"normale Texte gleicher Länge ({ref_len} Tokens): {stats.fmt_pct(100 * std_ref, 0)}"
                   if std_ref is not None else "keine Vergleichstexte gleicher Länge")
        head += (f" Opt-out per watermarking=false: {stats.fmt_pct(100 * opt, 0)} markiert ({ref_txt}) – "
                 + ("wirkungslos, enforce greift." if blocked else
                    "möglich, die Texte sind unmarkiert." if blocked is False else "nicht entscheidbar."))
    if gre is not None:
        head += f" greedy (temperature 0): {stats.fmt_pct(100 * gre, 0)} markiert (erwartet: 0 – dokumentierte Grenze)."
    if rate_long is not None and rate_long < 0.5:
        head = (f"{'Szenario ' + sc['id'] + ': ' if sc.get('id') else ''}KEIN Wasserzeichen nachweisbar "
                f"({stats.fmt_pct(100 * rate_long, 0)} ab 200 Tokens) – Kennzeichnungspflicht nach KI-VO Art. 50 "
                "nicht erfüllt (oder anderer Schlüssel im Detektor).")
    gen = [r for r in recs if r.get("stage") == "gen" and r.get("cond") == "standard"]
    agg = stats.aggregate(gen, settings["slo"]) if gen else None
    tables = [
        {"title": "Erkennung je Textlänge", "columns": ["Output-Tokens", "Texte", "erkannt (95%-KI)", "p-Wert (Median)",
                                                         "bewertete Tokens (Median)"], "rows": rows},
        {"title": "Gegen- und Sonderfälle", "columns": ["Fall", "Texte", "als markiert erkannt", "Erwartung"],
         "rows": [["Menschentexte (Fehlalarme)", n_h, stats.fmt_pct(100 * (fpr or 0), 0), "0 %"],
                  ["Opt-out watermarking=false", n_o, stats.fmt_pct(100 * opt, 0) if opt is not None else "–",
                   f"wie normale Texte gleicher Länge ({stats.fmt_pct(100 * std_ref, 0)}, n={n_ref}) bei enforce; "
                   "≈ Fehlalarmrate, wenn Opt-out wirkt" if std_ref is not None else "wie Standard (enforce)"],
                  ["greedy, temperature 0", n_g, stats.fmt_pct(100 * gre, 0) if gre is not None else "–",
                   "0 % (kein Gumbel-Sampling)"]]},
    ]
    if agg:
        tables.append({"title": "Latenz der markierten Anfragen (Kosten → Konfigurationsvergleich K6 vs. K0)",
                       "columns": ["TTFT p50", "TPOT p50", "TPOT p95"],
                       "rows": [[stats.fmt_ms(agg["ttft_ms"]["p50"]), stats.fmt_ms(agg["tpot_ms"]["p50"]),
                                 stats.fmt_ms(agg["tpot_ms"]["p95"])]]})
    charts = [{"id": "wm_rate", "title": "Erkennungsrate über die Textlänge", "type": "line",
               "x": {"name": "Output-Tokens"}, "y": {"name": "% erkannt"},
               "series": [{"name": "erkannt", "data": pts}], "markLines": [{"y": 100 * float(p["min_rate"]), "name": "Ziel"}]}]
    return {
        "verdict": verdict, "headline": head, "security": True,
        "kpis": {"wm_rate": {"value": round(100 * (rate_long or 0), 1), "unit": "% erkannt (≥ 200 Tokens)",
                             "text": f"Fehlalarme {stats.fmt_pct(100 * (fpr or 0), 0)}"}},
        "matrix": {"wm_detect_rate": round(100 * rate_long, 1) if rate_long is not None else None,
                   "wm_fpr": round(100 * fpr, 1) if fpr is not None else None,
                   "wm_detect_ci": _ci_txt(rate_long, n_long) if rate_long is not None else None,
                   "wm_optout_blocked": blocked,
                   "wm_optout_rate": round(100 * opt, 1) if opt is not None else None,
                   "wm_greedy_rate": round(100 * gre, 1) if gre is not None else None},
        "charts": charts, "tables": tables,
        "method_note": ("Detektor: vllm.v1.watermarking.GumbelWatermarkDetector mit demselben Schlüssel und context_width "
                        "wie die Engine; Text wird mit dem Modell-Tokenizer ohne Sondertokens neu tokenisiert. "
                        "Nachweis bei p < 0,01. Erzwungene Länge über min_tokens."),
    }
