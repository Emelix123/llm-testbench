"""Härtung & Angriffsfläche – die Punkte der vLLM-Security-Doku (v0.34) von außen geprüft.

Perspektive: Angreifertyp A1 der Arbeit – ein regulärer Mandant mit gültigem API-Key. Alle Proben
sind harmlos: Betriebsendpunkte (/pause, /collective_rpc, /sleep …) werden ausschließlich mit GET
angefragt (405 = Route vorhanden, ohne sie auszulösen), Funktionsproben verwenden max_tokens=1 und
ungültige Mini-Eingaben. Kein Exploit, keine Last.

Jeder Prüfpunkt: ok | warn | fail | info | skip. Ampel: rot bei mindestens einem fail.
"""
from __future__ import annotations

import json
import random
import re

from app import stats

ENGINE_OPS = {  # Pfad -> Klasse (laut vLLM-Security-Doku nicht öffentlich)
    "/metrics": "info", "/version": "info", "/tokenize": "info", "/detokenize": "info", "/load": "info",
    "/tokenizer_info": "info", "/kv_event_sources": "info",
    "/pause": "betrieb", "/resume": "betrieb", "/is_paused": "betrieb", "/abort_requests": "betrieb",
    "/collective_rpc": "betrieb", "/server_info": "betrieb", "/reset_prefix_cache": "betrieb",
    "/sleep": "betrieb", "/wake_up": "betrieb", "/start_profile": "betrieb", "/update_weights": "betrieb",
    "/v1/load_lora_adapter": "betrieb", "/scale_elastic_ep": "betrieb",
    "/invocations": "inferenz", "/pooling": "inferenz", "/classify": "inferenz", "/score": "inferenz",
}
FIXED = {  # Engine-Version -> behobene CVEs (aus der Arbeit)
    (0, 9, 0): "CVE-2025-46570 (cache_salt) / CVE-2025-46722",
    (0, 14, 1): "CVE-2026-22778 (RCE über Video-URL)",
    (0, 24, 0): "CVE-2026-55514 / CVE-2026-54234 (Absturz der Engine)",
}


def estimate(params):
    return 60


class Checks:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, group, cid, label, status, detail="", ref=""):
        self.rows.append({"group": group, "id": cid, "label": label, "status": status,
                          "detail": str(detail)[:400], "ref": ref})


async def _http(ctx, method, url, *, key=None, body=None, timeout=20, auth=True, limit=4000):
    headers = {"Content-Type": "application/json"}
    if auth and key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        r = await ctx.client.request(method, url, headers=headers,
                                     content=json.dumps(body) if body is not None else None, timeout=timeout)
        return r.status_code, r.text[:limit], {k.lower(): v for k, v in r.headers.items()}, None
    except Exception as e:  # noqa: BLE001
        return None, "", {}, f"{type(e).__name__}: {e}"[:200]


def _chat(model, content, **extra):
    return {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": content}], **extra}


def _cached(text: str):
    try:
        u = json.loads(text).get("usage") or {}
        return (u.get("prompt_tokens_details") or {}).get("cached_tokens")
    except (ValueError, AttributeError):
        return None


def _version_tuple(v: str):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


async def run(ctx):
    p = ctx.params
    C = Checks()
    base = ctx.ep.base_url
    model = ctx.ep.model
    key_a = ctx.tenant_ep("opfer").api_key
    key_b = ctx.tenant_ep("angreifer").api_key
    separate = ctx.has_tenant("angreifer")
    engine_url = (p.get("engine_url") or ctx.profile.get("engine_url") or "").strip().rstrip("/")
    engine_version = None
    ctx.progress(0.05, "Authentifizierung")

    # ------------------------------------------------------------ 1 Authentifizierung
    G = "Authentifizierung"
    st, txt, _, err = await _http(ctx, "POST", base + "/v1/chat/completions", body=_chat(model, "hi"), auth=False)
    C.add(G, "auth_none", "Anfrage ohne API-Key wird abgelehnt",
          "ok" if st in (401, 403) else ("fail" if st == 200 else "warn"),
          err or f"HTTP {st}", "vLLM-Doku: --api-key / Gateway-Authentifizierung")
    st, txt, _, err = await _http(ctx, "POST", base + "/v1/chat/completions", key="sk-falsch-" + "x" * 20,
                                  body=_chat(model, "hi"))
    C.add(G, "auth_wrong", "Anfrage mit falschem API-Key wird abgelehnt",
          "ok" if st in (401, 403) else ("fail" if st == 200 else "warn"), err or f"HTTP {st}")

    # ------------------------------------------------------------ 2 /vllm/*-Durchreichung im Gateway
    ctx.progress(0.15, "Durchreichung /vllm/*")
    G = "Engine-Endpunkte über das Gateway"
    for path, label in (("/vllm/metrics", "Engine-Metriken über /vllm/metrics"),
                        ("/vllm/version", "Engine-Version über /vllm/version")):
        st, txt, _, err = await _http(ctx, "GET", base + path, key=key_b, body={"model": model})
        if st == 200:
            leak = "prefix_cache_hits" in txt or "kv_cache_usage" in txt
            C.add(G, "pt_" + path, label, "fail",
                  "HTTP 200 – Mandant liest Engine-Interna" + (" inkl. prefix_cache_hits/kv_cache_usage "
                  "(Cache-Orakel und Last anderer Mandanten ohne Zeitmessung)" if leak else f": {txt[:80]}"),
                  "LiteLLM /vllm/{endpoint}; vLLM-Doku: /metrics nicht öffentlich")
            if path.endswith("version"):
                try:
                    engine_version = json.loads(txt).get("version")
                except ValueError:
                    pass
        elif st in (401, 403):
            C.add(G, "pt_" + path, label, "ok", f"HTTP {st} – Route für Mandanten gesperrt")
        elif st == 404:
            C.add(G, "pt_" + path, label, "ok", "HTTP 404 – Route nicht vorhanden")
        elif st is not None and (st >= 500 or "VLLM_API_BASE" in txt):
            C.add(G, "pt_" + path, label, "warn",
                  f"HTTP {st} statt 401/403 – die Route ist für Mandanten-Keys freigegeben und reicht derzeit nur "
                  "mangels Ziel (VLLM_API_BASE nicht gesetzt) nicht durch. Sobald VLLM_API_BASE gesetzt wird "
                  "(LiteLLM-Doku zur vLLM-Durchreichung), liest jeder Mandant /metrics, /version … der Engine. "
                  "Abhilfe: allowed_routes je Key.", "LiteLLM pass_through/vllm")
        else:
            C.add(G, "pt_" + path, label, "warn", err or f"HTTP {st}: {txt[:120]}")
    st, txt, _, err = await _http(ctx, "POST", base + "/vllm/tokenize", key=key_b, body={"model": model, "prompt": "x"})
    C.add(G, "pt_tokenize", "Engine-/tokenize über /vllm/tokenize",
          "fail" if st == 200 else ("ok" if st in (401, 403, 404) else "warn"),
          err or f"HTTP {st}" + (" – Engine antwortet durch das Gateway" if st == 200 else
                                 " – Route offen, ohne Ziel (siehe oben)" if st and st >= 500 else ""))

    # Betriebsendpunkte am Gateway selbst (nur GET)
    reach = {"betrieb": [], "info": [], "inferenz": []}
    for path, cls in ENGINE_OPS.items():
        st, txt, _, err = await _http(ctx, "GET", base + path, key=key_b, auth=cls != "inferenz", timeout=10)
        if st == 200 and path == "/metrics" and "vllm:" not in txt:
            continue  # Metriken des Gateways selbst, nicht der Engine
        if st in (200, 405, 422):
            reach[cls].append(f"{path} ({st})")
    C.add(G, "gw_ops", "Betriebsendpunkte der Engine (/pause, /collective_rpc, /sleep …) am Gateway",
          "fail" if reach["betrieb"] else "ok", ", ".join(reach["betrieb"]) or "keiner erreichbar",
          "vLLM-Doku: Operational controls")
    C.add(G, "gw_info", "Info-Endpunkte der Engine (/metrics, /version, /tokenize …) am Gateway",
          "fail" if any(x.startswith("/metrics") for x in reach["info"]) else ("warn" if reach["info"] else "ok"),
          ", ".join(reach["info"]) or "keiner erreichbar")
    C.add(G, "gw_inf", "Inferenz ohne Authentifizierung (/invocations, /pooling, /classify, /score)",
          "fail" if reach["inferenz"] else "ok", ", ".join(reach["inferenz"]) or "keiner erreichbar")

    # ------------------------------------------------------------ 3 Engine direkt
    ctx.progress(0.35, "Engine direkt")
    G = "Engine direkt erreichbar"
    if not engine_url:
        C.add(G, "engine_none", "Direkte Engine-URL", "skip", "keine angegeben (Parameter engine_url / Profil)")
    elif engine_url.rstrip("/") == base.rstrip("/"):
        C.add(G, "engine_same", "Profil zeigt direkt auf die Engine", "warn",
              "Mandanten sprechen ohne Gateway mit vLLM – keine Mandantentrennung, keine Grenzen je Mandant.")
    else:
        st, txt, _, err = await _http(ctx, "GET", engine_url + "/health", auth=False, timeout=5)
        if st is None:
            C.add(G, "engine_reach", f"{engine_url} vom Client aus", "ok", "nicht erreichbar – Engine nur im internen Netz")
        else:
            C.add(G, "engine_reach", f"{engine_url} vom Client aus", "fail",
                  f"HTTP {st} auf /health – Mandanten können das Gateway umgehen (keine Trennung, keine Grenzen)",
                  "Arbeit, Bedrohungsmodell: interne Schnittstellen nicht erreichbar")
            st, txt, _, _ = await _http(ctx, "GET", engine_url + "/v1/models", auth=False, timeout=5)
            C.add(G, "engine_auth", "Engine verlangt API-Key (/v1/models ohne Key)",
                  "ok" if st in (401, 403) else "fail", f"HTTP {st}")
            st, txt, _, _ = await _http(ctx, "GET", engine_url + "/metrics", auth=False, timeout=5)
            C.add(G, "engine_metrics", "Engine-/metrics ohne Authentifizierung",
                  "fail" if st == 200 else "ok",
                  f"HTTP {st}" + (" – u. a. prefix_cache_hits_total, kv_cache_usage_perc" if st == 200 else ""))
            st, txt, _, _ = await _http(ctx, "GET", engine_url + "/version", auth=False, timeout=5)
            if st == 200:
                try:
                    engine_version = json.loads(txt).get("version") or engine_version
                except ValueError:
                    pass
            ops = []
            for path, cls in ENGINE_OPS.items():
                st, _, _, _ = await _http(ctx, "GET", engine_url + path, auth=False, timeout=5)
                if st in (200, 405, 422):
                    ops.append(path)
            C.add(G, "engine_ops", "Erreichbare Betriebs-/Info-Endpunkte der Engine", "fail" if ops else "ok",
                  ", ".join(ops) or "keiner")

    # ------------------------------------------------------------ 4 Gefährliche Funktionen
    ctx.progress(0.5, "Funktionen")
    G = "Gefährliche Funktionen"
    n = int(p.get("n_probe") or 129)
    st, txt, _, err = await _http(ctx, "POST", base + "/v1/chat/completions", key=key_b,
                                  body=_chat(model, "hi", n=n, temperature=0.9), timeout=120, limit=2_000_000)
    if st == 200:
        got = len(re.findall(r'"finish_reason"', txt)) or "?"
        C.add(G, "n_cap", f"n={n} wird abgelehnt (Doku: 64–128)", "warn",
              f"angenommen ({got} Antworten). VLLM_MAX_N_SEQUENCES (Standard 16384) bzw. Grenze im Gateway setzen.",
              "CVE-2026-34756")
    else:
        C.add(G, "n_cap", f"n={n} wird abgelehnt (Doku: 64–128)", "ok", err or f"HTTP {st}: {txt[:140]}")

    st, txt, _, err = await _http(ctx, "POST", base + "/v1/completions", key=key_b,
                                  body={"model": model, "prompt": "x", "prompt_embeds": "AAAA", "max_tokens": 1})
    low = txt.lower()
    if st == 200:
        # Ein ungültiges prompt_embeds ("AAAA") hätte die Engine mit einem Dekodierfehler abgelehnt.
        # HTTP 200 heißt also: Das Feld kam nie bei der Engine an (vom Gateway entfernt) oder wird ignoriert.
        stt, det = "ok", "Feld vom Gateway entfernt bzw. ignoriert (ungültige Embeddings wurden nicht verarbeitet)"
    elif "enable-prompt-embeds" in low:
        stt, det = "ok", "Engine lehnt ab: --enable-prompt-embeds nicht gesetzt (Feld erreicht die Engine aber ungefiltert)"
    elif any(w in low for w in ("base64", "decode", "shape", "tensor", "torch", "embeds")):
        stt, det = "fail", f"prompt_embeds werden verarbeitet: {txt[:160]}"
    else:
        stt, det = "ok", f"abgelehnt/gefiltert: HTTP {st} {txt[:120]}"
    C.add(G, "prompt_embeds", "prompt_embeds nicht nutzbar", stt, err or det, "CVE-2026-55514")

    for cid, label, part, ref in (
        ("video", "Video-URL wird abgelehnt", {"type": "video_url", "video_url": {"url": "http://127.0.0.1:9/x.mp4"}},
         "CVE-2026-22778"),
        ("ssrf", "Bild-URL auf interne Adresse (169.254.169.254) wird nicht geladen",
         {"type": "image_url", "image_url": {"url": "http://169.254.169.254/latest/meta-data/"}}, "SSRF, --allowed-media-domains"),
    ):
        st, txt, _, err = await _http(ctx, "POST", base + "/v1/chat/completions", key=key_b,
                                      body=_chat(model, [part, {"type": "text", "text": "hi"}]), timeout=60)
        low = txt.lower()
        if st == 200:
            stt, det = "fail", "angenommen"
        elif any(w in low for w in ("not a multimodal", "not support", "at most 0", "limit", "modality", "language-model-only", "not allowed", "allowed domains")):
            stt, det = "ok", "abgelehnt: " + re.sub(r"\s+", " ", txt)[:140]
        elif any(w in low for w in ("connect", "timed out", "timeout", "refused", "fetch", "unreachable", "download")):
            stt, det = "warn", f"Server versucht die URL zu laden (SSRF-Fläche): {txt[:140]}"
        else:
            stt, det = "warn", err or f"HTTP {st}: {txt[:140]}"
        C.add(G, cid, label, stt, det, ref)

    # ------------------------------------------------------------ 5 Cache-Trennung
    ctx.progress(0.65, "Cache-Trennung")
    G = "Mandantentrennung im Cache"
    rng = random.Random(ctx.seed)
    words = "alpha beta gamma delta omega sigma kappa lambda theta zeta".split()

    def prefix():
        return f"[{rng.getrandbits(64):x}] " + " ".join(rng.choice(words) for _ in range(400))

    if not p.get("check_cache_isolation", True):
        C.add(G, "cache", "Cache-Trennung", "skip", "abgeschaltet")
    elif not separate:
        C.add(G, "cache", "Cache-Trennung zwischen Mandanten-Keys", "skip",
              "Profil hat keinen Angreifer-Key – nicht prüfbar")
    else:
        P1 = prefix()
        st1, t1, _, _ = await _http(ctx, "POST", base + "/v1/chat/completions", key=key_a, body=_chat(model, P1 + " a"))
        st2, t2, _, _ = await _http(ctx, "POST", base + "/v1/chat/completions", key=key_a, body=_chat(model, P1 + " b"))
        st3, t3, _, _ = await _http(ctx, "POST", base + "/v1/chat/completions", key=key_b, body=_chat(model, P1 + " c"))
        own, cross = _cached(t2), _cached(t3)
        if cross is None:
            C.add(G, "cache_cross", "Mandant B trifft keinen Cache-Block von Mandant A", "skip",
                  "cached_tokens nicht geliefert – den Prefix-Cache-Test (Zeit-Seitenkanal) verwenden")
        else:
            C.add(G, "cache_cross", "Mandant B trifft keinen Cache-Block von Mandant A",
                  "fail" if cross > 0 else "ok",
                  f"cached_tokens für B: {cross}" + (" – Gateway gibt die Mandanten-Identität nicht an die Engine "
                                                     "weiter (KeyPooling); Seitenkanal offen" if cross > 0 else ""),
                  "CVE-2025-46570, Sun u. a. (KeyPooling)")
            C.add(G, "cache_own", "Cache wirkt innerhalb eines Mandanten (Leistung)", "info",
                  f"cached_tokens bei Wiederholung durch A: {own}" + (" – Prefix-Caching aus oder je Anfrage gesalzen" if not own else ""))
            P2 = prefix()
            salt = "gemeinsamer-salt-123"
            await _http(ctx, "POST", base + "/v1/chat/completions", key=key_a, body=_chat(model, P2 + " a", cache_salt=salt))
            st4, t4, _, _ = await _http(ctx, "POST", base + "/v1/chat/completions", key=key_b,
                                        body=_chat(model, P2 + " b", cache_salt=salt))
            cs = _cached(t4)
            if st4 != 200:
                C.add(G, "client_salt", "Client kann cache_salt nicht selbst wählen", "ok", f"Feld abgelehnt (HTTP {st4})")
            else:
                C.add(G, "client_salt", "Client kann cache_salt nicht selbst wählen",
                      "warn" if cs else "ok",
                      f"gleicher Client-Salt → cached_tokens für B: {cs}" +
                      (" – das Gateway übernimmt den Salt des Clients; ist er vorhersagbar, kann ein Mandant "
                       "den Cache eines anderen gezielt treffen. Gateway soll ihn überschreiben." if cs else ""))

    # ------------------------------------------------------------ 6 Informationsabfluss
    ctx.progress(0.8, "Informationsabfluss")
    G = "Informationsabfluss"
    st, txt, hdr, err = await _http(ctx, "POST", base + "/v1/chat/completions", key=key_b, body=_chat(model, "Hallo"))
    ct = _cached(txt)
    C.add(G, "oracle", "kein cached_tokens-Orakel im usage", "warn" if ct is not None else "ok",
          f"prompt_tokens_details.cached_tokens = {ct} wird ausgeliefert (Treffer direkt ablesbar)" if ct is not None
          else "nicht ausgeliefert", "InputSnatch; --enable-prompt-tokens-details aus")
    api_base = hdr.get("x-litellm-model-api-base")
    leaky = bool(api_base) and "://" in api_base
    C.add(G, "hdr_api_base", "keine interne Engine-Adresse in Antwort-Headern", "warn" if leaky else "ok",
          f"x-litellm-model-api-base: {api_base}" if leaky else
          (f"maskiert ({api_base})" if api_base else "nicht vorhanden"))
    infos = [f"{k}: {hdr[k]}" for k in ("x-litellm-version", "server") if hdr.get(k)]
    if infos:
        C.add(G, "hdr_info", "Versions-/Server-Angaben in Headern", "info", ", ".join(infos))
    st, txt, _, _ = await _http(ctx, "GET", base + "/v1/models", key=key_b)
    try:
        models = [m.get("id") for m in json.loads(txt).get("data", [])]
    except (ValueError, AttributeError):
        models = []
    foreign = [m for m in models if re.search(r"mandant|tenant|kunde", str(m), re.I)]
    C.add(G, "models", "Modellliste verrät keine anderen Mandanten", "warn" if foreign else "ok",
          ", ".join(models[:8]) or f"HTTP {st}")

    # ------------------------------------------------------------ 7 Verwaltung des Gateways
    G = "Verwaltung des Gateways"
    if p.get("check_admin_routes", True):
        for path, label in (("/key/list", "Liste aller Keys"), ("/team/list", "Liste aller Teams"),
                            ("/user/list", "Liste aller Nutzer"), ("/spend/logs", "Abrechnungs-Logs"),
                            ("/global/spend/logs", "globale Abrechnungs-Logs"), ("/model/info", "Modell-/Deployment-Details"),
                            ("/routes", "Routenliste"), ("/openapi.json", "OpenAPI-Beschreibung")):
            st, txt, _, err = await _http(ctx, "GET", base + path, key=key_b, timeout=10)
            others = sorted(set(re.findall(r"mandant-[a-z0-9]+", txt)) - {"mandant-b"})
            if st in (401, 403, 404) or st is None:
                C.add(G, "adm" + path, f"{label} ({path}) für Mandanten gesperrt", "ok", err or f"HTTP {st}")
            elif others:
                C.add(G, "adm" + path, f"{label} ({path}) für Mandanten gesperrt", "fail",
                      f"HTTP {st} – enthält Daten anderer Mandanten: {', '.join(others)}")
            elif path == "/model/info" and "api_base" in txt:
                C.add(G, "adm" + path, f"{label} ({path})", "warn", f"HTTP {st} – zeigt interne api_base der Engine")
            else:
                C.add(G, "adm" + path, f"{label} ({path})", "info", f"HTTP {st} – erreichbar, keine fremden Mandanten erkannt")

    # ------------------------------------------------------------ 8 Version
    G = "Version"
    vt = _version_tuple(engine_version or "")
    if vt:
        missing = [cve for fix, cve in FIXED.items() if vt < fix]
        C.add(G, "engine_version", f"vLLM {engine_version} enthält die Fixes der Arbeit",
              "fail" if missing else "ok", ("fehlt: " + "; ".join(missing)) if missing else "≥ 0.24.0")
    else:
        C.add(G, "engine_version", "Engine-Version", "info",
              "von außen nicht ermittelbar (gut). Version im Betrieb dokumentieren und ≥ 0.24.0 halten.")
    ctx.extra_config["checks"] = C.rows
    ctx.extra_config["engine_version"] = engine_version
    ctx.progress(1.0, "fertig")


# ---------------------------------------------------------------------- Auswertung

ICON = {"ok": "✔", "warn": "⚠", "fail": "✘", "info": "ℹ", "skip": "–"}


def summarize(recs, stages, cfg, settings):
    rows = cfg.get("checks") or []
    if not rows:
        return {"verdict": "rot", "headline": "Keine Prüfpunkte – lief der Test?", "kpis": {}, "charts": [], "tables": []}
    cnt = {k: sum(1 for r in rows if r["status"] == k) for k in ICON}
    scored = cnt["ok"] + cnt["warn"] + cnt["fail"]
    verdict = "rot" if cnt["fail"] else ("gelb" if cnt["warn"] else "gruen")
    fails = [r["label"] for r in rows if r["status"] == "fail"]
    warns = [r["label"] for r in rows if r["status"] == "warn"]
    sc = cfg.get("deploy_scenario") or {}
    head = (f"{'Szenario ' + sc['id'] + ': ' if sc.get('id') else ''}{cnt['ok']} von {scored} Prüfpunkten bestanden, "
            f"{cnt['fail']} kritisch, {cnt['warn']} Hinweise.")
    if fails:
        head += " Kritisch: " + "; ".join(fails[:4]) + ("…" if len(fails) > 4 else "") + "."
    elif warns:
        head += " Hinweise: " + "; ".join(warns[:3]) + ("…" if len(warns) > 3 else "") + "."
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    tables = [{"title": g, "columns": ["", "Prüfpunkt", "Ergebnis", "Bezug"],
               "rows": [[ICON[r["status"]], r["label"], r["detail"], r.get("ref", "")] for r in rs]}
              for g, rs in groups.items()]
    return {
        "verdict": verdict, "headline": head, "security": True,
        "kpis": {"hardening": {"value": f"{cnt['ok']}/{scored}", "unit": "Prüfpunkte bestanden",
                               "text": f"{cnt['fail']} kritisch, {cnt['warn']} Hinweise"}},
        "matrix": {"hardening_ok": cnt["ok"], "hardening_total": scored, "hardening_fail": cnt["fail"],
                   "hardening_warn": cnt["warn"], "hardening_score": f"{cnt['ok']}/{scored}"},
        "raw": {"counts": cnt, "engine_version": cfg.get("engine_version")},
        "charts": [], "tables": tables,
        "method_note": ("Alle Proben mit dem Key eines regulären Mandanten (Angreifertyp A1). Betriebsendpunkte nur per GET "
                        "(405 = Route vorhanden, nicht ausgelöst); Funktionsproben mit max_tokens=1 und ungültigen "
                        "Mini-Eingaben. Grundlage: vLLM Security Docs v0.34 und Kapitel 1.3 der Arbeit."),
    }
