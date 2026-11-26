"""FastAPI-Backend: erzeugt Last, misst, speichert. Der Browser zeigt nur an."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import zipfile
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from .client import Endpoint, make_client, measure_network_latency, simple_get, stream_chat, now_ns
from .ops import gl, ops
from .runner import batch_report, get_summary, manager, modules, summarize_run
from .sysmon import cpu

app = FastAPI(title="LLM-Inferenz-Testplattform", version=storage.TOOL_VERSION)


@app.on_event("startup")
async def _startup() -> None:
    cpu.start()
    # Läufe, die beim letzten Beenden noch liefen (Absturz, Laptop aus): als unterbrochen markieren
    # und aus den bereits gespeicherten Anfragen auswerten.
    for d in storage.RUNS.iterdir():
        cfg = storage.read_json(d / "config.json", None) if d.is_dir() else None
        if cfg and cfg.get("status") == "laeuft":
            cfg["status"] = "unterbrochen"
            cfg.setdefault("notes", []).append("Lauf wurde unterbrochen (Backend beendet). "
                                               "Ausgewertet sind alle bis dahin gespeicherten Anfragen.")
            storage.write_json(d / "config.json", cfg)
            try:
                summarize_run(d)
            except Exception:  # noqa: BLE001
                pass


def _err(status: int, msg: str):
    raise HTTPException(status_code=status, detail=msg)


# ---------------------------------------------------------------- Profile

@app.get("/api/profiles")
def get_profiles():
    return storage.list_profiles()


@app.post("/api/profiles")
async def post_profile(req: Request):
    data = await req.json()
    if not data.get("name") or not data.get("base_url") or not data.get("model"):
        _err(400, "Name, URL und Modell sind Pflicht.")
    prof = storage.save_profile(data)
    if data.get("api_key"):
        storage.set_secret(prof["api_key_name"], data["api_key"])
    for role, value in (data.get("tenant_key_values") or {}).items():
        name = (prof.get("tenant_keys") or {}).get(role)
        if value and name:
            storage.set_secret(name, value)
    prof["has_key"] = bool(storage.get_secret(prof["api_key_name"]))
    prof["tenant_has_key"] = {r: bool(storage.get_secret(n)) for r, n in (prof.get("tenant_keys") or {}).items()}
    return prof


@app.delete("/api/profiles/{pid}")
def del_profile(pid: str):
    storage.delete_profile(pid)
    return {"ok": True}


@app.post("/api/profiles/{pid}/check")
async def check_profile(pid: str):
    """Knopf „Verbindung prüfen“: Erreichbarkeit, Modell, Streaming, usage, Header."""
    prof = storage.get_profile(pid)
    if not prof:
        _err(404, "Profil nicht gefunden")
    ep = Endpoint(prof["base_url"], prof["model"], storage.get_secret(prof.get("api_key_name")))
    items = []
    async with make_client(max_connections=4, timeout_s=60) as c:
        net = await measure_network_latency(c, ep, n=3)
        st, data, dur, _, err = await simple_get(c, ep, "/v1/models")
        models = [m.get("id") for m in data.get("data", [])] if isinstance(data, dict) else []
        hint = ""
        if st == 404:
            hint = (f" – unter {ep.url('/v1/models')} gibt es nichts. Ist die URL die von LiteLLM "
                    "(z. B. https://llm.example.de, ohne /v1/chat/completions)?")
        items.append({"key": "reachable", "label": "Endpunkt erreichbar",
                      "ok": st is not None and st < 500 and st != 404,
                      "detail": (err or f"HTTP {st} in {dur:.0f} ms") + hint})
        items.append({"key": "auth", "label": "API-Key akzeptiert", "ok": st == 200,
                      "detail": {401: "HTTP 401 – Key ungültig", 403: "HTTP 403 – Key ohne Berechtigung",
                                 404: "nicht prüfbar (URL falsch, siehe oben)"}.get(st, f"HTTP {st}" if st else "–")})
        items.append({"key": "model", "label": f"Modell „{prof['model']}“ in /v1/models",
                      "ok": prof["model"] in models,
                      "detail": ", ".join(models[:8]) + (" …" if len(models) > 8 else "") if models else "keine Liste"})
        t0 = now_ns()
        r = await stream_chat(c, ep, {"messages": [{"role": "user", "content": "Sag Hallo."}],
                                      "max_tokens": 8}, t0, timeout_s=60, keep_text=True)
        items.append({"key": "stream", "label": "Streaming-Antwort", "ok": r.ok and r.t_first_token is not None,
                      "detail": (f"TTFT {r.t_first_token - r.t_send:.0f} ms, Text: {r.text[:60]!r}"
                                 if r.t_first_token is not None else (r.error_msg or r.error_type or "keine Tokens"))})
        items.append({"key": "usage", "label": "usage im Stream", "ok": r.usage_in_stream,
                      "detail": f"prompt={r.prompt_tokens}, completion={r.completion_tokens}" if r.usage_in_stream else "fehlt"})
        items.append({"key": "litellm", "label": "LiteLLM-Header vorhanden",
                      "ok": r.headers.get("litellm_overhead_ms") is not None,
                      "detail": ", ".join(sorted(r.raw_headers)) or "keine x-litellm-Header"})
    items.append({"key": "net", "label": "Netzlatenz", "ok": net["ok"],
                  "detail": f"p50 {net['p50_ms']} ms über {net['path']}" if net["ok"] else "nicht messbar"})
    return {"ok": all(i["ok"] for i in items if i["key"] in ("reachable", "auth", "stream")), "items": items}


# ---------------------------------------------------------------- Tests & Suiten

BUILTIN_SUITES = [
    {"id": "schnellcheck", "name": "Schnellcheck", "duration": "ca. 2–5 min",
     "items": [{"test": "quickcheck"}], "builtin": True},
    {"id": "parallelitaet-kapazitaet", "name": "Parallelität & Kapazität", "duration": "ca. 1 h",
     "items": [{"test": "concurrency"}, {"test": "capacity_sweep"}, {"test": "context_matrix"}], "builtin": True},
    {"id": "konfiguration", "name": "Konfiguration", "duration": "ca. 10 min",
     "items": [{"test": "config"}], "builtin": True},
    {"id": "dauerlast", "name": "Dauerlast", "duration": "1–4 h",
     "items": [{"test": "endurance"}, {"test": "spike"}, {"test": "overload"}], "builtin": True,
     "note": "Nutzt die Kapazität aus dem letzten Kapazitäts-Sweep dieses Endpunkts."},
    {"id": "konfigurationsvergleich", "name": "Konfigurationsvergleich (je Szenario)", "duration": "ca. 20–28 min",
     # Seitenkanal zusätzlich unter Last (Cohens d unter Last, Vergleich mit der Literatur): 4 parallele Anfragen
     # eines dritten Mandanten mit je 128 Output-Tokens – ~150 Anfragen/min, bleibt unter den Grenzen von K3/K4.
     "items": [{"test": "hardening_check"},
               {"test": "prefix_cache_leak", "params": {"background_load": 4, "background_output_tokens": 128}},
               {"test": "isolation_overhead"}, {"test": "salt_cost"}, {"test": "watermark"},
               {"test": "resource_contention"}], "builtin": True,
     "note": "Einmal je Szenario K0–K6 – am einfachsten als Gesamtlauf auf der Seite „Steuerung“. Ergebnis: Seite "
             "„Konfigurationen“. Wasserzeichen braucht den Detektor (K4/K6 oder Detektor auf der Seite „Steuerung“)."},
    {"id": "abnahme", "name": "Abnahme", "duration": "mehrere Stunden",
     "items": [{"test": "quickcheck"}, {"test": "config"}, {"test": "concurrency"}, {"test": "capacity_sweep"},
               {"test": "rate_test"}, {"test": "context_matrix"}, {"test": "cache_benefit"}, {"test": "cost_split"},
               {"test": "limits"}, {"test": "cold_start"}, {"test": "endurance"}, {"test": "spike"}, {"test": "overload"},
               {"test": "quality_custom"}, {"test": "quality_consistency"}, {"test": "quality_tools"}],
     "builtin": True, "report": True,
     "note": "Erzeugt am Ende einen Prüfbericht als HTML und PDF (data/reports/). lm-eval und LongBench v2 bei Bedarf dazunehmen."},
]


@app.get("/api/tests")
def get_tests():
    return sorted((m.public() for m in modules().values()), key=lambda m: (m.get("order", 999), m["id"]))


@app.get("/api/suites")
def get_suites():
    custom = []
    for p in sorted(storage.SUITES.glob("*.json")):
        s = storage.read_json(p, {})
        s["id"] = p.stem
        custom.append(s)
    avail = set(modules())
    out = []
    for s in BUILTIN_SUITES + custom:
        s = dict(s)
        s["items"] = [i for i in s["items"] if i["test"] in avail]
        out.append(s)
    return out


@app.post("/api/suites")
async def post_suite(req: Request):
    data = await req.json()
    if not data.get("name") or not data.get("items"):
        _err(400, "Name und Tests sind Pflicht.")
    sid = storage.slugify(data["name"])
    storage.write_json(storage.SUITES / f"{sid}.json", {"name": data["name"], "items": data["items"]})
    return {"id": sid}


@app.delete("/api/suites/{sid}")
def del_suite(sid: str):
    (storage.SUITES / f"{storage.slugify(sid)}.json").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/estimate")
async def estimate(req: Request):
    data = await req.json()
    mods = modules()
    per = []
    for it in data.get("items", []):
        m = mods.get(it["test"])
        if m:
            per.append({"test": m.id, "seconds": m.estimate_s(it.get("params") or {})})
    return {"total_s": sum(p["seconds"] for p in per), "items": per}


# ---------------------------------------------------------------- Batch / Live

@app.post("/api/batch")
async def start_batch(req: Request):
    data = await req.json()
    prof = storage.get_profile(data.get("profile_id") or "")
    if not prof:
        _err(400, "Bitte ein Endpunkt-Profil wählen.")
    items = data.get("items") or []
    if not items:
        _err(400, "Bitte mindestens einen Test wählen.")
    if gl.running():
        _err(409, "Ein Gesamtlauf läuft – Tests startet er selbst (Seite „Steuerung“).")
    if ops.busy():
        _err(409, f"Erst abwarten: {ops.job.label} läuft noch (Seite „Steuerung“).")
    try:
        b = manager.start(prof, items, data.get("label") or "", bool(data.get("make_report")))
    except (RuntimeError, ValueError) as e:
        _err(409, str(e))
    return b.snapshot()


@app.get("/api/batch/current")
def current_batch():
    return manager.current.snapshot() if manager.current else None


@app.post("/api/batch/cancel")
def cancel_batch():
    return {"cancelled": manager.cancel()}


@app.get("/api/events")
async def events(request: Request):
    async def gen():
        b = manager.current
        if b is None:
            yield "event: idle\ndata: {}\n\n"
            return
        q = b.subscribe()
        try:
            yield f"event: batch\ndata: {json.dumps(b.snapshot())}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: {msg['type']}\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg["type"] == "batch" and msg.get("state") != "laeuft":
                    break
        finally:
            b.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- Ergebnisse

@app.get("/api/runs")
def get_runs():
    return storage.list_runs()


@app.get("/api/batches")
def get_batches():
    """Läufe gruppiert nach Batch, neueste zuerst, mit Gesamtampel."""
    groups: "OrderedDict[str, dict]" = OrderedDict()
    order = {"rot": 3, "gelb": 2, "gruen": 1, None: 0}
    for r in storage.list_runs():
        key = r.get("batch_id") or r["id"]
        g = groups.setdefault(key, {"batch_id": key, "runs": [], "profile_name": r.get("profile_name"),
                                    "profile_id": r.get("profile_id"), "started_at": r.get("started_at"),
                                    "verdict": None})
        g["runs"].append(r)
        if r.get("started_at") and (not g["started_at"] or r["started_at"] < g["started_at"]):
            g["started_at"] = r["started_at"]
        if order.get(r.get("verdict"), 0) > order.get(g["verdict"], 0):
            g["verdict"] = r["verdict"]
    out = list(groups.values())
    for g in out:
        g["runs"].sort(key=lambda r: (r.get("batch_index") is None, r.get("batch_index") or 0, r["id"]))
    out.sort(key=lambda g: g.get("started_at") or "", reverse=True)
    return out


@app.get("/api/matrix")
def matrix(since: str | None = None, all: bool = False):
    """Konfigurationsvergleich K0–K6: neuester Lauf je Szenario und Test, Kennzahlen nebeneinander."""
    from .matrix import build
    return build(since=since, include_unfinished=all)


@app.get("/api/matrix/metrics")
def matrix_metrics():
    from .matrix import METRICS
    return [{"key": k, "label": lab, "unit": u, "better": b, "area": a, "digits": d, "test": t}
            for k, lab, u, b, a, d, t in METRICS]


@app.get("/api/scenario")
def scenario():
    """Lokal laufendes Szenario laut deploy/.active-scenario.json (oder null)."""
    return storage.active_scenario()


# ---------------------------------------------------------------- Steuerung (deploy/scenario.sh)
# Diese Endpunkte starten Docker-Container. Nur von diesem Rechner (oder per SSH-Tunnel) erlaubt,
# nur mit JSON-Content-Type (Formulare fremder Webseiten scheitern) und mit lokalem Host-Header
# (gegen DNS-Rebinding). LLMTB_OPS_REMOTE=1 hebt die Adressprüfung auf – nur hinter eigener Absicherung.

_LOCAL = {"127.0.0.1", "localhost", "::1"}


def _ops_guard(req: Request, write: bool = False) -> None:
    if os.environ.get("LLMTB_OPS_REMOTE") != "1":
        if (req.client.host if req.client else "") not in _LOCAL:
            _err(403, "Steuerung nur lokal oder per SSH-Tunnel (ssh -L 8080:localhost:8080 server).")
        host = urlparse("//" + (req.headers.get("host") or "")).hostname or ""
        if host not in _LOCAL:
            _err(403, f"Unerwarteter Host „{host}“ – bitte über http://localhost:<port> öffnen.")
    origin = req.headers.get("origin")
    if origin and (urlparse(origin).hostname or "") not in _LOCAL | {urlparse("//" + (req.headers.get("host") or "")).hostname}:
        _err(403, "Fremde Herkunft (Origin) – abgelehnt.")
    if write and "application/json" not in (req.headers.get("content-type") or ""):
        _err(415, "Nur JSON.")


def _not_while_measuring() -> None:
    if gl.running():
        _err(409, "Ein Gesamtlauf läuft – erst abbrechen oder abwarten.")
    if manager.current and manager.current.state == "laeuft":
        _err(409, "Ein Test läuft – Szenario jetzt zu ändern würde die Messung verfälschen. Erst abbrechen oder abwarten.")


@app.get("/api/ops/check")
async def ops_check(req: Request, gpu: bool = False):
    _ops_guard(req)
    try:
        return await ops.system_check(gpu_container=gpu)
    except RuntimeError as e:
        _err(500, str(e))


@app.get("/api/ops/scenarios")
def ops_scenarios(req: Request):
    _ops_guard(req)
    from .ops import scenarios
    return scenarios()


@app.get("/api/ops/status")
async def ops_status(req: Request):
    _ops_guard(req)
    from .ops import status as st
    return await st()


@app.get("/api/ops/job")
def ops_job(req: Request, tail: int = 400):
    _ops_guard(req)
    return ops.job.snapshot(tail=max(0, min(tail, 3000))) if ops.job else None


@app.post("/api/ops/run")
async def ops_run(req: Request):
    _ops_guard(req, write=True)
    from .ops import ACTIONS, scenarios
    data = await req.json()
    action = str(data.get("action") or "")
    if action == "cancel":
        return {"cancelled": ops.cancel()}
    _not_while_measuring()
    if action == "up":
        sid = str(data.get("scenario") or "").upper()
        sc = next((x for x in scenarios() if x["id"] == sid), None)
        if not re.fullmatch(r"K[0-9]", sid) or not sc:
            _err(400, "Unbekanntes Szenario.")
        if not sc["available"]:
            _err(409, f"{sid} {sc['reason']}.")
        args, label = ["up", sid], f"{sid} starten"
    elif action in ACTIONS:
        if action == "reset" and not data.get("confirm"):
            _err(400, "Zurücksetzen bitte bestätigen.")
        args, label = ACTIONS[action]
    else:
        _err(400, "Unbekannte Aktion.")
    try:
        return ops.start(action, list(args), label).snapshot()
    except RuntimeError as e:
        _err(409, str(e))


@app.get("/api/ops/logs")
async def ops_logs(req: Request, service: str = "vllm", tail: int = 200):
    _ops_guard(req)
    from .ops import logs
    try:
        return PlainTextResponse(await logs(service, max(10, min(tail, 2000))))
    except ValueError as e:
        _err(400, str(e))


@app.post("/api/gesamtlauf")
async def gesamtlauf_start(req: Request):
    _ops_guard(req, write=True)
    from .ops import Gesamtlauf, scenarios
    data = await req.json()
    if gl.running():
        _err(409, "Es läuft bereits ein Gesamtlauf.")
    if ops.busy():
        _err(409, f"Erst abwarten: {ops.job.label} läuft noch.")
    if manager.current and manager.current.state == "laeuft":
        _err(409, "Es läuft gerade ein Test – erst abwarten oder abbrechen.")
    avail = {x["id"]: x for x in scenarios()}
    wanted = [str(x).upper() for x in (data.get("scenarios") or [k for k, v in avail.items() if v["available"]])]
    bad = [x for x in wanted if x not in avail]
    if bad or not wanted:
        _err(400, "Unbekannte Szenarien: " + ", ".join(bad) if bad else "Bitte mindestens ein Szenario wählen.")
    skipped = [x for x in wanted if not avail[x]["available"]]
    if skipped:
        _err(409, "Nicht möglich auf dieser GPU: " + ", ".join(f"{x} ({avail[x]['reason']})" for x in skipped))
    reps = int(data.get("repetitions") or 1)
    if not 1 <= reps <= 10:
        _err(400, "Wiederholungen: 1 bis 10.")
    sid = data.get("suite") or "konfigurationsvergleich"
    suite = next((s for s in get_suites() if s["id"] == sid), None)
    if not suite or not suite["items"]:
        _err(400, "Unbekannte oder leere Suite.")
    profile_id = data.get("profile_id") or "lokal-litellm"
    if not storage.get_profile(profile_id):
        _err(409, f"Profil „{profile_id}“ fehlt – zuerst „Einrichten“.")
    mods = modules()
    per_rep = sum(mods[i["test"]].estimate_s({**mods[i["test"]].defaults(), **(i.get("params") or {})})
                  for i in suite["items"] if i["test"] in mods)
    g = Gesamtlauf(sorted(dict.fromkeys(wanted)), reps, suite, profile_id, per_rep, bool(data.get("keep_running")))
    gl.current = g
    g.task = asyncio.create_task(g.execute(manager))
    return g.snapshot()


@app.get("/api/gesamtlauf")
def gesamtlauf_get(req: Request):
    _ops_guard(req)
    return gl.current.snapshot() if gl.current else None


@app.post("/api/gesamtlauf/cancel")
async def gesamtlauf_cancel(req: Request):
    _ops_guard(req, write=True)
    return {"cancelled": bool(gl.current and gl.current.cancel(manager))}


@app.get("/api/exports")
def exports_list(req: Request):
    _ops_guard(req)
    from .ops import list_exports
    return list_exports()


@app.get("/api/exports/{eid}/{name}")
def exports_file(req: Request, eid: str, name: str):
    _ops_guard(req)
    from .ops import EXPORTS
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", eid) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        _err(400, "Ungültiger Name.")
    f = EXPORTS / eid / name
    if not f.is_file():
        _err(404, "Datei nicht gefunden.")
    return FileResponse(f, filename=f"{eid}-{name}")


@app.get("/api/matrix/export")
def matrix_export(format: str = "csv", since: str | None = None, base: str | None = None):
    """CSV/LaTeX der Seite „Konfigurationen“ (eine Implementierung für Oberfläche und Gesamtlauf)."""
    from . import matrix as mx
    data = mx.build(since=since)
    if format == "latex":
        return PlainTextResponse(mx.to_latex(data, base))
    if format == "csv":
        return PlainTextResponse(mx.to_csv(data))
    if format == "laeufe":
        return PlainTextResponse(mx.runs_csv(mx.runs_long(since=since)))
    if format == "md":
        rows = mx.runs_long(since=since)
        return PlainTextResponse(mx.summary_md(rows, "Konfigurationsvergleich", [f"Läufe seit {since}" if since else "alle Läufe"]))
    _err(400, "format: csv, latex, laeufe oder md")


@app.get("/api/report")
def report(runs: str):
    ids = [r for r in runs.split(",") if r]
    try:
        return batch_report(ids)
    except FileNotFoundError as e:
        _err(404, f"Lauf nicht gefunden: {e}")


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    try:
        rd = storage.run_dir(run_id)
    except FileNotFoundError:
        _err(404, "Lauf nicht gefunden")
    return {"config": storage.read_json(rd / "config.json", {}), "summary": get_summary(rd)}


@app.post("/api/runs/{run_id}/resummarize")
def resummarize(run_id: str):
    try:
        rd = storage.run_dir(run_id)
    except FileNotFoundError:
        _err(404, "Lauf nicht gefunden")
    return summarize_run(rd)


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str):
    try:
        rd = storage.run_dir(run_id)
    except FileNotFoundError:
        _err(404, "Lauf nicht gefunden")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(rd.rglob("*")):
            if f.is_file() and f.name != "secrets.env":
                z.write(f, f"{rd.name}/{f.relative_to(rd)}")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{rd.name}.zip"'})


@app.get("/api/references")
def get_refs():
    return storage.get_references()


@app.put("/api/references/{test}")
async def put_ref(test: str, req: Request):
    data = await req.json()
    return storage.set_reference(test, data.get("run_id"))


def _report_ids(runs: str) -> list[str]:
    ids = [r for r in runs.split(",") if r]
    for r in ids:
        try:
            storage.run_dir(r)
        except FileNotFoundError:
            _err(404, f"Lauf nicht gefunden: {r}")
    return ids


@app.get("/api/report/html")
def report_html(runs: str):
    from fastapi.responses import HTMLResponse
    from .report import render_html
    rep = batch_report(_report_ids(runs))
    name = ((rep["cards"][0]["config"].get("profile") or {}).get("name") if rep["cards"] else "") or ""
    return HTMLResponse(render_html(rep, f"Prüfbericht – {name}"),
                        headers={"Content-Disposition": 'inline; filename="pruefbericht.html"'})


@app.get("/api/report/pdf")
async def report_pdf(runs: str):
    import hashlib
    from .report import write_report
    ids = _report_ids(runs)
    rep = batch_report(ids)
    name = ((rep["cards"][0]["config"].get("profile") or {}).get("name") if rep["cards"] else "") or ""
    out = storage.DATA / "reports" / ("manuell-" + hashlib.sha1(runs.encode()).hexdigest()[:10])
    res = await write_report(rep, out, f"Prüfbericht – {name}")
    if not res["pdf"]:
        _err(501, res["pdf_error"] or "PDF nicht erzeugbar")
    return FileResponse(res["pdf"], media_type="application/pdf", filename="pruefbericht.pdf")


# ---------------------------------------------------------------- Einstellungen

@app.get("/api/settings")
def get_settings():
    return storage.get_settings()


@app.put("/api/settings")
async def put_settings(req: Request):
    return storage.save_settings(await req.json())


@app.get("/api/code-stand")
def code_stand():
    """Läuft die Testplattform mit dem Code, der auf der Platte liegt? (run-all startet sie sonst neu.)"""
    from . import environment
    return {**environment.code_state(),
            "busy": bool((manager.current and manager.current.state == "laeuft")
                         or (gl.current and gl.current.state == "laeuft") or ops.busy())}


@app.get("/api/status")
def status():
    return {"version": storage.TOOL_VERSION, "client_cpu_pct": cpu.value,
            "running": bool(manager.current and manager.current.state == "laeuft"),
            "ops_busy": ops.busy(), "ops_label": ops.job.label if ops.busy() else None,
            "gesamtlauf": gl.current.state if gl.current else None}


# ---------------------------------------------------------------- Frontend

DIST = storage.ROOT / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = (DIST / path).resolve()
        if path and DIST.resolve() in f.parents and f.is_file():
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
else:
    @app.get("/", include_in_schema=False)
    def no_frontend():
        return JSONResponse({"hinweis": "Frontend nicht gebaut: cd frontend && npm install && npm run build"})

