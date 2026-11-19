"""Konfigurationsvergleich: Sicherheit und Leistung je Szenario (K0–K6) nebeneinander.

Je Gruppe (Szenario aus deploy/.active-scenario.json + Endpunkt-Profil) wird der neueste fertige
Lauf jedes beteiligten Tests genommen. Die Tests liefern ihre Kennzahlen im Feld `matrix` der
Auswertung; hier stehen nur Beschriftung, Einheit und Richtung („besser = kleiner/größer“), damit
die Oberfläche Differenzen zur Referenz (meist K0) einfärben und als CSV/LaTeX ausgeben kann.
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import datetime
from decimal import Decimal

from . import stats, storage

MATRIX_TESTS = ("isolation_overhead", "salt_cost", "prefix_cache_leak", "resource_contention", "hardening_check",
                "watermark")

# key, Beschriftung, Einheit, besser ("lower"/"higher"/None), Bereich, Nachkommastellen, Test
METRICS = [
    ("ttft_p50_ms", "TTFT p50", "ms", "lower", "leistung", 0, "isolation_overhead"),
    ("ttft_p95_ms", "TTFT p95", "ms", "lower", "leistung", 0, "isolation_overhead"),
    ("tpot_p50_ms", "TPOT p50", "ms", "lower", "leistung", 1, "isolation_overhead"),
    ("tpot_p95_ms", "TPOT p95", "ms", "lower", "leistung", 1, "isolation_overhead"),
    ("e2e_p95_ms", "E2E p95", "ms", "lower", "leistung", 0, "isolation_overhead"),
    ("output_tps", "Durchsatz", "Tok/s", "higher", "leistung", 1, "isolation_overhead"),
    ("peak_output_tps", "Spitzendurchsatz", "Tok/s", "higher", "leistung", 1, "isolation_overhead"),
    ("cache_hit_pct", "Cache-Treffer", "%", "higher", "leistung", 1, "isolation_overhead"),
    ("rejected_pct", "abgewiesen", "%", "lower", "leistung", 1, "isolation_overhead"),
    ("error_rate_pct", "Fehler", "%", "lower", "leistung", 1, "isolation_overhead"),
    # abgeleitet: Kostenannahme (Einstellungen → Kosten) / Spitzendurchsatz
    ("cost_per_mtok_eur", "Kosten je 1 Mio. Output-Tokens", "€", "lower", "leistung", 3, "isolation_overhead"),

    ("salt_new_tenant_ttft_ms", "TTFT neuer Mandant", "ms", "lower", "kosten", 0, "salt_cost"),
    ("salt_new_tenant_extra_ms", "Aufschlag neuer Mandant", "ms", "lower", "kosten", 0, "salt_cost"),
    ("salt_new_tenant_hit_pct", "Vorlage aus fremdem Cache", "%", None, "kosten", 0, "salt_cost"),
    ("salt_cold_template_tokens", "Vorlage Kaltstart", "Tok", None, "kosten", 0, "salt_cost"),
    ("salt_tenants_max", "Mandanten (max.)", "", None, "kosten", 0, "salt_cost"),
    ("salt_hit_pct_nmax", "Cache-Treffer bei max. Mandanten", "%", "higher", "kosten", 1, "salt_cost"),
    ("salt_ttft_p50_nmax_ms", "TTFT p50 bei max. Mandanten", "ms", "lower", "kosten", 0, "salt_cost"),
    ("salt_tps_nmax", "Durchsatz bei max. Mandanten", "Tok/s", "higher", "kosten", 1, "salt_cost"),
    ("salt_tps_change_pct", "Durchsatz max. vs. 1 Mandant", "%", "higher", "kosten", 1, "salt_cost"),
    ("salt_cost_nmax_eur", "Kosten je 1 Mio. Output-Tokens bei max. Mandanten", "€", "lower", "kosten", 3, "salt_cost"),
    ("salt_tenants_ok", "Mandanten ohne Cache-Einbruch", "", "higher", "kosten", 0, "salt_cost"),

    ("prefix_auc", "AUC Seitenkanal", "", None, "vertraulichkeit", 3, "prefix_cache_leak"),
    ("prefix_auc_dev", "Unterscheidbarkeit max(AUC, 1−AUC)", "", "lower", "vertraulichkeit", 3, "prefix_cache_leak"),
    ("prefix_auc_ci", "95%-KI AUC", "", None, "vertraulichkeit", None, "prefix_cache_leak"),
    ("prefix_d", "Cohens d ruhig", "", "lower", "vertraulichkeit", 2, "prefix_cache_leak"),
    ("prefix_d_load", "Cohens d unter Last", "", "lower", "vertraulichkeit", 2, "prefix_cache_leak"),
    ("prefix_cached_leak", "cached_tokens-Orakel", "", "lower", "vertraulichkeit", None, "prefix_cache_leak"),

    ("slowdown_sn", "Slowdown S_N", "×", "lower", "verfuegbarkeit", 2, "resource_contention"),
    ("victim_ttft_ratio", "TTFT-Faktor Opfer", "×", "lower", "verfuegbarkeit", 2, "resource_contention"),
    ("jain_index", "Jain-Index", "", "higher", "verfuegbarkeit", 3, "resource_contention"),
    ("victim_goodput_attack_pct", "Goodput Opfer unter Angriff", "%", "higher", "verfuegbarkeit", 1, "resource_contention"),
    ("attacker_rejected_pct", "Störnachbar abgewiesen", "%", None, "verfuegbarkeit", 1, "resource_contention"),
    ("n_largest_accepted", "größtes angenommenes n", "", "lower", "verfuegbarkeit", 0, "resource_contention"),

    ("hardening_score", "Härtung bestanden", "", None, "haertung", None, "hardening_check"),
    ("hardening_fail", "Härtung kritisch", "", "lower", "haertung", 0, "hardening_check"),
    ("hardening_warn", "Härtung Hinweise", "", "lower", "haertung", 0, "hardening_check"),

    ("wm_detect_rate", "Wasserzeichen erkannt ≥200 Tok", "%", "higher", "kennzeichnung", 1, "watermark"),
    ("wm_fpr", "Fehlalarme", "%", "lower", "kennzeichnung", 1, "watermark"),
    ("wm_optout_blocked", "Opt-out verhindert", "", "higher", "kennzeichnung", None, "watermark"),
]
# Kennzahlen, bei denen Δ % zur Referenz nichts aussagt (Differenzen, Anteile nahe 0, Mengenangaben)
NO_DELTA = {"salt_new_tenant_extra_ms", "salt_new_tenant_hit_pct", "salt_cold_template_tokens", "salt_tenants_max",
            "salt_tps_change_pct", "salt_tenants_ok"}
AREAS = {"leistung": "Leistung (Mehrmandanten-Last)",
         "kosten": "Kosten der Cache-Trennung (neuer Mandant, viele Mandanten)",
         "vertraulichkeit": "Vertraulichkeit (Prefix-Cache-Seitenkanal)",
         "verfuegbarkeit": "Verfügbarkeit (Noisy Neighbor)", "haertung": "Härtung & Angriffsfläche",
         "kennzeichnung": "Kennzeichnung (KI-VO Art. 50)"}


def _group(cfg: dict) -> tuple[str, dict]:
    sc = cfg.get("deploy_scenario") or {}
    prof = cfg.get("profile") or {}
    pname = prof.get("name") or prof.get("id") or "?"
    if sc.get("id"):
        return f"{sc['id']}|{prof.get('id')}", {"scenario": sc.get("id"), "scenario_name": sc.get("name"),
                                                 "massnahmen": sc.get("massnahmen"), "config_sha256": sc.get("config_sha256"),
                                                 "profile_id": prof.get("id"), "profile_name": pname}
    return f"-|{prof.get('id')}", {"scenario": None, "scenario_name": None, "profile_id": prof.get("id"),
                                    "profile_name": pname}


def cost_settings() -> dict:
    return (storage.get_settings() or {}).get("cost") or {}


def derive(values: dict, cost: dict) -> dict:
    """Kosten je 1 Mio. Output-Tokens aus dem Durchsatz – mit den aktuellen Annahmen (Einstellungen → Kosten)."""
    for src, dst in (("peak_output_tps", "cost_per_mtok_eur"), ("salt_tps_nmax", "salt_cost_nmax_eur")):
        v = values.get(src)
        if _isnum(v) and v > 0 and cost:
            e = stats.eur_per_million(v, cost)
            if e is not None:
                values[dst] = round(e, 4)
    return values


def cost_note(cost: dict | None = None) -> str:
    c = cost if cost is not None else cost_settings()
    if not c:
        return ""
    return (f"Kosten je 1 Mio. Output-Tokens = ({num_de(c.get('machine_eur_per_h', 0), 2)} €/h Maschine + "
            f"{num_de(c.get('power_kw', 0), 2)} kW × {num_de(c.get('power_eur_per_kwh', 0), 2)} €/kWh) / "
            f"(Durchsatz × 3 600 s × {c.get('utilization_pct')} % Auslastung); Annahmen unter Einstellungen → Kosten")


def build(since: str | None = None, include_unfinished: bool = False) -> dict:
    from .runner import get_summary
    groups: dict[str, dict] = {}
    for r in storage.list_runs():   # neueste zuerst
        if r.get("test") not in MATRIX_TESTS:
            continue
        if not include_unfinished and r.get("status") != "fertig":
            continue
        if since and (r.get("started_at") or "") < since:
            continue
        rd = storage.RUNS / r["id"]
        cfg = storage.read_json(rd / "config.json", {})
        key, info = _group(cfg)
        g = groups.setdefault(key, {"key": key, **info, "runs": {}, "values": {}})
        if r["test"] in g["runs"]:
            continue  # es zählt der neueste Lauf je Test
        summ = get_summary(rd)
        g["runs"][r["test"]] = {"id": r["id"], "started_at": r.get("started_at"), "verdict": summ.get("verdict"),
                                "headline": summ.get("headline"), "params": cfg.get("params")}
        for k, v in (summ.get("matrix") or {}).items():
            g["values"].setdefault(k, v)
    out = sorted(groups.values(), key=lambda g: (g["scenario"] is None, g["scenario"] or "", g["profile_name"] or ""))
    cost = cost_settings()
    for g in out:
        g["label"] = (f"{g['scenario']} – {g['scenario_name']}" if g["scenario"] else g["profile_name"])
        derive(g["values"], cost)
    return {
        "groups": out,
        "cost_note": cost_note(cost),
        "metrics": [{"key": k, "label": lab, "unit": u, "better": b, "area": a, "digits": d, "test": t,
                     "delta": k not in NO_DELTA} for k, lab, u, b, a, d, t in METRICS],
        "areas": AREAS,
        "tests": list(MATRIX_TESTS),
    }


# ======================================================================= Export
# Gleiche Formatierung wie die Seite „Konfigurationen“ (deutsches Zahlenformat, LaTeX mit booktabs).
# Die Oberfläche holt CSV/LaTeX von hier (/api/matrix/export) – es gibt nur EINE Implementierung.


PERF_KEYS = ("output_tps", "ttft_p50_ms", "tpot_p50_ms", "cost_per_mtok_eur")
COST_DELTA_KEYS = ("salt_new_tenant_ttft_ms", "salt_ttft_p50_nmax_ms", "salt_tps_nmax", "salt_cost_nmax_eur")
SUMMARY_SECTIONS = [
    ("Sicherheit", ("prefix_auc", "prefix_auc_dev", "prefix_d", "slowdown_sn", "jain_index", "hardening_score",
                    "wm_detect_rate", "wm_fpr")),
    ("Leistung (3 Mandanten, eingeschwungen)", ("output_tps", "ttft_p50_ms", "tpot_p50_ms", "cache_hit_pct",
                                                "cost_per_mtok_eur")),
    ("Kosten der Cache-Trennung", ("salt_new_tenant_ttft_ms", "salt_new_tenant_extra_ms", "salt_new_tenant_hit_pct",
                                   "salt_tenants_max", "salt_hit_pct_nmax", "salt_ttft_p50_nmax_ms", "salt_tps_nmax",
                                   "salt_tps_change_pct", "salt_cost_nmax_eur", "salt_tenants_ok")),
]
SUMMARY_KEYS = tuple(k for _, ks in SUMMARY_SECTIONS for k in ks)


def _isnum(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def num_de(v: float, d: int) -> str:
    """1234.5 → '1.234,5' (wie Intl.NumberFormat('de-DE') mit fester Nachkommazahl)."""
    s = f"{v:,.{d}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _js_str(v) -> str:
    """Zahl wie JavaScripts String(v): 3.0 → '3', 0.25 → '0.25'."""
    if isinstance(v, int):
        return str(v)
    if float(v).is_integer():
        return str(int(v))
    s = repr(float(v))
    if "e" in s and abs(v) >= 1e-6:
        s = format(Decimal(s), "f")
    return s


def fmt(m: dict | None, v) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "ja" if v else "nein"
    if not _isnum(v):
        return str(v)
    d = m["digits"] if m and m.get("digits") is not None else 2
    unit = (m or {}).get("unit") or ""
    return num_de(v, d) + (("×" if unit == "×" else " " + unit) if unit else "")


def delta(m: dict | None, v, b):
    """Differenz zur Referenz in % und ob besser/schlechter (± 2 % gilt als gleich)."""
    if not _isnum(v) or not _isnum(b) or b == 0 or not m:
        return None
    pct = (v - b) / abs(b) * 100
    cls = "same"
    if m.get("better") and abs(pct) > 2:
        cls = "good" if ((pct < 0) if m["better"] == "lower" else (pct > 0)) else "bad"
    return pct, cls


def dtxt(d) -> str:
    return ("+" if d[0] > 0 else "") + num_de(d[0], 1) + " %" if d else ""


def base_group(data: dict, base_key: str | None = None) -> dict | None:
    groups = data.get("groups") or []
    return (next((g for g in groups if g["key"] == base_key), None)
            or next((g for g in groups if g.get("scenario") == "K0"), None)
            or (groups[0] if groups else None))


def _short(g: dict) -> str:
    return g.get("scenario") or g.get("profile_name") or "?"


def _csv_cell(v) -> str:
    if v is None:
        s = ""
    elif isinstance(v, bool):
        s = "ja" if v else "nein"
    elif _isnum(v):
        s = _js_str(v).replace(".", ",")
    else:
        s = str(v)
    return '"' + s.replace('"', '""') + '"' if re.search(r'[;"\n]', s) else s


def to_csv(data: dict) -> str:
    groups = data.get("groups") or []
    ms = [m for m in data["metrics"] if any(g["values"].get(m["key"]) is not None for g in groups)]
    head = ["Szenario", "Konfiguration", *[m["label"] + (f" [{m['unit']}]" if m["unit"] else "") for m in ms]]
    rows = [[g.get("scenario") or "", g.get("scenario_name") or g.get("profile_name"),
             *[g["values"].get(m["key"]) for m in ms]] for g in groups]
    return "\n".join(";".join(_csv_cell(x) for x in r) for r in [head, *rows])


def _tex(s) -> str:
    s = re.sub(r"([%&_#$])", r"\\\1", str(s))
    s = s.replace("×", "$\\times$").replace("≥", "$\\geq$").replace("–", "--").replace("€", "\\texteuro{}")
    return re.sub(r"^-(?=\d)", "$-$", s)    # echtes Minuszeichen statt Bindestrich


def to_latex(data: dict, base_key: str | None = None, now: datetime | None = None) -> str:
    groups = data.get("groups") or []
    M = {m["key"]: m for m in data["metrics"]}
    B = base_group(data, base_key)

    def cell(k, g):
        v = g["values"].get(k)
        return "--" if v is None else _tex(fmt(M.get(k), v))

    def dcell(k, g):
        if not B:
            return "--"
        if g["key"] == B["key"]:
            return "Ref."
        d = delta(M.get(k), g["values"].get(k), B["values"].get(k))
        return _tex(dtxt(d)) if d else "--"

    lines = [" & ".join([_tex(_short(g)), _tex(g.get("scenario_name") or g.get("profile_name")),
                         cell("prefix_auc", g), cell("prefix_d", g), cell("slowdown_sn", g), cell("jain_index", g),
                         cell("hardening_score", g), cell("wm_detect_rate", g),
                         dcell("output_tps", g), dcell("ttft_p50_ms", g), dcell("tpot_p50_ms", g)]) + " \\\\"
             for g in groups]
    stamp = (now or datetime.now()).strftime("%d.%m.%Y, %H:%M:%S")
    ref = _tex(_short(B) if B else "Referenz")
    out = [
        "% Erzeugt von der LLM-Inferenz-Testplattform (Konfigurationsvergleich), " + stamp,
        "% benötigt \\usepackage{booktabs}. Passt so auf A4 mit 2,5 cm Rand; sonst die tabular-Umgebung in",
        "% \\resizebox{\\textwidth}{!}{...} (Paket graphicx) einschließen.",
        "\\begin{table}[htbp]", "\\centering", "\\footnotesize", "\\setlength{\\tabcolsep}{2.5pt}",
        "\\begin{tabular}{llrrrrrrrrr}", "\\toprule",
        " & & \\multicolumn{2}{c}{Seitenkanal} & \\multicolumn{2}{c}{Verfügbarkeit} & Härtung & WZ & "
        "\\multicolumn{3}{c}{$\\Delta$ zu " + ref + "} \\\\",
        "\\cmidrule(lr){3-4}\\cmidrule(lr){5-6}\\cmidrule(lr){9-11}",
        "ID & Konfiguration & AUC & $d$ & $S_N$ & $J$ & bestanden & erkannt & Durchsatz & TTFT p50 & TPOT p50 \\\\",
        "\\midrule", *lines, "\\bottomrule", "\\end{tabular}",
        "\\caption{Sicherheit und Leistungskosten der Konfigurationen (neuester Lauf je Test)}",
        "\\label{tab:konfigurationsvergleich}", "\\end{table}"]

    # Kosten der Cache-Trennung: eigene Tabelle, sobald Kosten oder der Salting-Test vorliegen
    ck = ("cost_per_mtok_eur", "salt_new_tenant_ttft_ms", "salt_tps_nmax")
    if any(g["values"].get(k) is not None for g in groups for k in ck):
        nmax = max((g["values"].get("salt_tenants_max") or 0 for g in groups), default=0)
        lc = max((g["values"].get("salt_cold_template_tokens") or 0 for g in groups), default=0)
        clines = [" & ".join([_tex(_short(g)), _tex(g.get("scenario_name") or g.get("profile_name")),
                              cell("cost_per_mtok_eur", g), dcell("cost_per_mtok_eur", g),
                              cell("salt_new_tenant_ttft_ms", g), cell("salt_new_tenant_extra_ms", g),
                              cell("salt_hit_pct_nmax", g), cell("salt_ttft_p50_nmax_ms", g),
                              dcell("salt_tps_nmax", g), cell("salt_cost_nmax_eur", g),
                              dcell("salt_cost_nmax_eur", g)]) + " \\\\" for g in groups]
        out += [
            "", "% " + (cost_note() or "Kosten je 1 Mio. Output-Tokens: Annahmen unter Einstellungen → Kosten"),
            "\\begin{table}[htbp]", "\\centering", "\\footnotesize", "\\setlength{\\tabcolsep}{2.5pt}",
            "\\begin{tabular}{llrrrrrrrrr}", "\\toprule",
            " & & \\multicolumn{2}{c}{3 Mandanten} & \\multicolumn{2}{c}{neuer Mandant"
            + (f" ({lc} Tok.)" if lc else "") + "} & \\multicolumn{5}{c}{"
            + (f"{nmax} Mandanten" if nmax else "viele Mandanten") + ", gemeinsame Vorlage} \\\\",
            "\\cmidrule(lr){3-4}\\cmidrule(lr){5-6}\\cmidrule(lr){7-11}",
            "ID & Konfiguration & \\texteuro/1 Mio. & $\\Delta$ & TTFT & Aufschlag & Treffer & TTFT p50 & Durchsatz $\\Delta$ "
            "& \\texteuro/1 Mio. & $\\Delta$ \\\\",
            "\\midrule", *clines, "\\bottomrule", "\\end{tabular}",
            "\\caption{Kosten der Cache-Trennung je Konfiguration ($\\Delta$ zu " + ref + "; \\texteuro/1 Mio. = Kosten je "
            "1 Mio. Output-Tokens)}",
            "\\label{tab:kosten-cache-trennung}", "\\end{table}"]
    return "\n".join(out)


def runs_long(since: str | None = None, scenarios: list[str] | None = None) -> list[dict]:
    """Alle fertigen Läufe der Vergleichstests (auch Wiederholungen), älteste zuerst."""
    from .runner import get_summary
    out = []
    cost = cost_settings()
    for r in storage.list_runs():
        if r.get("test") not in MATRIX_TESTS or r.get("status") != "fertig":
            continue
        if since and (r.get("started_at") or "") < since:
            continue
        rd = storage.RUNS / r["id"]
        cfg = storage.read_json(rd / "config.json", {})
        sc = cfg.get("deploy_scenario") or {}
        if scenarios and sc.get("id") not in scenarios:
            continue
        summ = get_summary(rd)
        out.append({"scenario": sc.get("id"), "scenario_name": sc.get("name"), "test": r["test"], "run_id": r["id"],
                    "started_at": r.get("started_at"), "verdict": summ.get("verdict"),
                    "batch_label": cfg.get("batch_label") or "", "values": derive(dict(summ.get("matrix") or {}), cost)})
    out.sort(key=lambda x: x["started_at"] or "")
    return out


def runs_csv(rows: list[dict]) -> str:
    keys = [m[0] for m in METRICS]
    head = ["Szenario", "Konfiguration", "Test", "Lauf", "Start", "Ergebnis",
            *[f"{lab} [{u}]" if u else lab for _, lab, u, *_ in METRICS]]
    body = [[r["scenario"] or "", r["scenario_name"] or "", r["test"], r["run_id"], r["started_at"] or "",
             r["verdict"] or "", *[r["values"].get(k) for k in keys]] for r in rows]
    return "\n".join(";".join(_csv_cell(x) for x in row) for row in [head, *body])


def aggregate(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """Je Szenario und Kennzahl: Mittelwert, Standardabweichung, n (über Wiederholungen)."""
    acc: dict[str, dict[str, list]] = {}
    for r in rows:
        if not r["scenario"]:
            continue
        for k, v in r["values"].items():
            acc.setdefault(r["scenario"], {}).setdefault(k, []).append(v)
    out: dict[str, dict[str, dict]] = {}
    for sc, kv in acc.items():
        for k, vals in kv.items():
            nums = [v for v in vals if _isnum(v)]
            if nums:
                out.setdefault(sc, {})[k] = {"mean": statistics.fmean(nums), "n": len(nums),
                                             "sd": statistics.stdev(nums) if len(nums) > 1 else None}
            else:
                seen = list(dict.fromkeys(fmt(None, v) for v in vals if v is not None))
                out.setdefault(sc, {})[k] = {"text": " / ".join(seen), "n": len(vals)}
    return out


def summary_md(rows: list[dict], title: str, notes: list[str] | None = None) -> str:
    M = {m[0]: {"key": m[0], "label": m[1], "unit": m[2], "better": m[3], "digits": m[5]} for m in METRICS}
    agg = aggregate(rows)
    scen = sorted(agg)
    names = {r["scenario"]: r["scenario_name"] for r in rows if r["scenario"]}

    def cell(sc, k):
        a = agg.get(sc, {}).get(k)
        if not a:
            return "–"
        if "text" in a:
            return a["text"]
        d = M[k]["digits"] if M[k]["digits"] is not None else 2
        s = num_de(a["mean"], d)          # Einheit steht im Spaltenkopf
        if a["sd"] is not None:
            s += " ± " + num_de(a["sd"], d)
        return s + (f" (n={a['n']})" if a["n"] > 1 else "")

    def head(k):
        return M[k]["label"] + (f" [{M[k]['unit']}]" if M[k]["unit"] else "")

    out = [f"# {title}", ""]
    out += [f"- {n}" for n in (notes or [])]
    out += ["", "## Kennzahlen je Szenario", "",
            "Mittelwert ± Standardabweichung über die Wiederholungen (n = Anzahl Läufe)."]
    for sec, ks in SUMMARY_SECTIONS:
        keys = [k for k in ks if any(k in agg.get(sc, {}) for sc in scen)]
        if not keys:
            continue
        out += ["", f"### {sec}", "", "| Szenario | " + " | ".join(head(k) for k in keys) + " |",
                "|---|" + "---:|" * len(keys)]
        for sc in scen:
            out.append(f"| {sc} {names.get(sc) or ''} | " + " | ".join(cell(sc, k) for k in keys) + " |")
    ref = "K0" if "K0" in agg else (scen[0] if scen else None)
    if ref:
        for sec, ks in ((f"Leistung und Kosten gegenüber {ref}", PERF_KEYS),
                        (f"Kosten der Cache-Trennung gegenüber {ref}", COST_DELTA_KEYS)):
            keys = [k for k in ks if any(k in agg.get(sc, {}) for sc in scen)]
            if not keys:
                continue
            out += ["", f"## {sec} (Δ % der Mittelwerte)", "",
                    "| Szenario | " + " | ".join(M[k]["label"] for k in keys) + " |", "|---|" + "---:|" * len(keys)]
            for sc in scen:
                cells = []
                for k in keys:
                    a, b = agg.get(sc, {}).get(k), agg.get(ref, {}).get(k)
                    d = delta(M[k], a and a.get("mean"), b and b.get("mean"))
                    cells.append("Referenz" if sc == ref else (dtxt(d) if d else "–"))
                out.append(f"| {sc} | " + " | ".join(cells) + " |")
    out += ["", "## Läufe", "", "| Start | Szenario | Test | Ergebnis | Lauf |", "|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['started_at']} | {r['scenario'] or '–'} | {r['test']} | {r['verdict'] or '–'} | {r['run_id']} |")
    return "\n".join(out) + "\n"
