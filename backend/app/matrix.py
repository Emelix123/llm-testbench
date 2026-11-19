"""Konfigurationsvergleich: Sicherheit und Leistung je Szenario (K0–K6) nebeneinander.

Je Gruppe (Szenario aus deploy/.active-scenario.json + Endpunkt-Profil) wird der neueste fertige
Lauf jedes beteiligten Tests genommen. Die Tests liefern ihre Kennzahlen im Feld `matrix` der
Auswertung; hier stehen nur Beschriftung, Einheit und Richtung („besser = kleiner/größer“), damit
die Oberfläche Differenzen zur Referenz (meist K0) einfärben und als CSV/LaTeX ausgeben kann.
"""
from __future__ import annotations

from . import storage

MATRIX_TESTS = ("isolation_overhead", "prefix_cache_leak", "resource_contention", "hardening_check", "watermark")

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
AREAS = {"leistung": "Leistung (Mehrmandanten-Last)", "vertraulichkeit": "Vertraulichkeit (Prefix-Cache-Seitenkanal)",
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
    for g in out:
        g["label"] = (f"{g['scenario']} – {g['scenario_name']}" if g["scenario"] else g["profile_name"])
    return {
        "groups": out,
        "metrics": [{"key": k, "label": lab, "unit": u, "better": b, "area": a, "digits": d, "test": t}
                    for k, lab, u, b, a, d, t in METRICS],
        "areas": AREAS,
        "tests": list(MATRIX_TESTS),
    }
