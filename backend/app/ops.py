"""Steuerung aus der Weboberfläche: Systemprüfung, Einrichten, Szenarien starten/stoppen, Gesamtlauf.

Alles läuft über deploy/scenario.sh – dieselben Befehle wie im Terminal; die Ausgabe erscheint
live auf der Seite „Steuerung“. Es läuft immer höchstens ein Befehl gleichzeitig.

Gesamtlauf: für jedes gewählte Szenario `scenario.sh up Kx` → Suite N-mal messen → nächstes
Szenario; am Ende `down` und Export nach data/exports/<id>/ (CSV, LaTeX, Zusammenfassung mit
Mittelwert ± Standardabweichung über die Wiederholungen).

Sicherheit: Diese Funktionen starten Docker-Container. Die Testplattform lauscht deshalb nur auf
127.0.0.1 (Zugriff vom Server aus per SSH-Tunnel); main.py lehnt Steuerbefehle von anderen
Adressen, fremden Host-Headern und ohne JSON-Content-Type ab (Schutz vor CSRF/DNS-Rebinding).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import traceback
import uuid
from datetime import datetime

from . import matrix as mx
from . import storage

DEPLOY = storage.ROOT / "deploy"
SCRIPT = DEPLOY / "scenario.sh"
CHECK = DEPLOY / "ops_check.py"
EXPORTS = storage.DATA / "exports"
PROJECT = os.environ.get("LLMTB_PROJECT", "llmtb")
START_S = 180          # Schätzung je Szenario: vLLM laden, Gateway, Mandanten
K5_MIN_MB = 8000
SERVICES = ("vllm", "vllm-a", "vllm-b", "litellm", "postgres", "wm-detector")

# Aktion → (Argumente für scenario.sh, Beschriftung)
ACTIONS = {
    "init": (["init"], "Einrichten"),
    "pull": (["pull"], "Images & Modell laden"),
    "down": (["down"], "Szenario stoppen"),
    "reset": (["reset"], "Zurücksetzen (Gateway-DB, Compile-Cache)"),
    "detector-up": (["detector", "up"], "Wasserzeichen-Detektor starten"),
    "detector-down": (["detector", "down"], "Wasserzeichen-Detektor stoppen"),
    "gpu": (["gpu"], "GPU-Werte neu berechnen"),
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _env() -> dict:
    return {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "LLMTB_PROJECT": PROJECT, "PYTHONUNBUFFERED": "1"}


# ======================================================================= Befehle (scenario.sh)

class Job:
    MAX_LINES = 3000

    def __init__(self, action: str, args: list[str], label: str):
        self.id = uuid.uuid4().hex[:8]
        self.action, self.args, self.label = action, args, label
        self.lines: list[str] = []
        self.state = "laeuft"
        self.code: int | None = None
        self.started_at = now_iso()
        self.finished_at: str | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None

    def add(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > self.MAX_LINES:
            del self.lines[: len(self.lines) - self.MAX_LINES]

    def snapshot(self, tail: int = 400) -> dict:
        return {"id": self.id, "action": self.action, "label": self.label, "args": self.args, "state": self.state,
                "code": self.code, "started_at": self.started_at, "finished_at": self.finished_at,
                "lines": self.lines[-tail:], "total_lines": len(self.lines)}


class Ops:
    def __init__(self) -> None:
        self.job: Job | None = None
        self.last_check: dict | None = None

    def busy(self) -> bool:
        return bool(self.job and self.job.state == "laeuft")

    def start(self, action: str, args: list[str], label: str) -> Job:
        if self.busy():
            raise RuntimeError(f"Es läuft bereits: {self.job.label}")
        job = Job(action, args, label)
        self.job = job
        job.task = asyncio.create_task(self._exec(job))
        return job

    async def run(self, action: str, args: list[str], label: str) -> Job:
        job = self.start(action, args, label)
        await job.task
        return job

    def cancel(self) -> bool:
        j = self.job
        if not (j and j.state == "laeuft" and j.proc and j.proc.returncode is None):
            return False
        try:
            os.killpg(j.proc.pid, signal.SIGTERM)   # ganze Prozessgruppe (bash + docker)
        except ProcessLookupError:
            return False
        j.add("→ Abbruch angefordert")
        return True

    async def _exec(self, job: Job) -> None:
        job.add(f"$ ./deploy/scenario.sh {' '.join(job.args)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(SCRIPT), *job.args, cwd=str(DEPLOY), env=_env(), start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            job.proc = proc
            buf = b""
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                *parts, buf = re.split(rb"\r\n|\n|\r", buf + chunk)
                for p in parts:
                    line = p.decode(errors="replace").rstrip()
                    if line:
                        job.add(line)
            if buf.strip():
                job.add(buf.decode(errors="replace").rstrip())
            job.code = await proc.wait()
            job.state = "fertig" if job.code == 0 else "fehler"
        except Exception as e:  # noqa: BLE001
            job.add(f"Interner Fehler: {e}")
            job.code, job.state = -1, "fehler"
        finally:
            job.finished_at = now_iso()
            job.add(f"→ beendet ({'ok' if job.code == 0 else f'Code {job.code}'})")

    async def system_check(self, gpu_container: bool = False) -> dict:
        args = [sys.executable, str(CHECK), "--json"] + (["--gpu-container"] if gpu_container else [])
        proc = await asyncio.create_subprocess_exec(*args, env=_env(), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=240)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Systemprüfung hat zu lange gedauert")
        try:
            res = json.loads(out.decode())
        except ValueError:
            raise RuntimeError("Systemprüfung fehlgeschlagen: " + err.decode(errors="replace")[-400:])
        self.last_check = res
        return res


ops = Ops()


# ======================================================================= Szenarien & Status

def vram_mb() -> int | None:
    if ops.last_check and ops.last_check.get("vram_mb"):
        return int(ops.last_check["vram_mb"])
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        vals = [int(float(x)) for x in out.split() if x.strip()]
        return max(vals) if vals else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def scenarios() -> list[dict]:
    """K0–K6 aus deploy/compose/k*.yaml (Kopfkommentar + x-scenario), mit Verfügbarkeit je VRAM."""
    vram = vram_mb()
    out = []
    for f in sorted((DEPLOY / "compose").glob("k*.yaml")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"^x-scenario:\n((?:[ \t]+.*\n)+)", text, re.M)
        block = m.group(1) if m else ""

        def field(k: str) -> str:
            mm = re.search(rf"^\s+{k}:\s*(.+)$", block, re.M)
            return mm.group(1).strip() if mm else ""

        lines = text.splitlines()
        head = lines[0].lstrip("# ").strip() if lines else ""
        titel = head.split("–", 1)[1].strip() if "–" in head else head
        para: list[str] = []
        for line in lines[2:]:          # erster Absatz des Kopfkommentars
            t = line[1:].strip() if line.startswith("#") else ""
            if not t:
                break
            para.append(t)
        desc = " ".join(para)
        if len(desc) > 420:
            desc = desc[:417].rsplit(" ", 1)[0] + " …"
        exp = re.search(r"^# (?:Erwartung[^:]*|Zweck):\s*(.+(?:\n#[ \t]+\S.*)*)", text, re.M)
        dedicated = "vllm-b:" in text
        need = K5_MIN_MB if dedicated else 3800
        ok = vram is None or vram >= need
        out.append({
            "id": field("id"), "name": field("name"), "file": f"compose/{f.name}",
            "massnahmen": [x.strip() for x in field("massnahmen").strip("[]").split(",") if x.strip()],
            "titel": titel, "beschreibung": desc,
            "erwartung": re.sub(r"\n#\s+", " ", exp.group(1)).strip() if exp else "",
            "needs_vram_mb": need, "available": ok,
            "reason": "" if ok else f"braucht ≥ {need // 1000} GB VRAM (hier {vram / 1024:.1f} GB)",
        })
    return out


async def _run_capture(*args: str, timeout: int = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, env=_env(), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "Zeitüberschreitung"
    return proc.returncode or 0, out.decode(errors="replace")


def _parse_ps(raw: str) -> list[dict]:
    raw = raw.strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
        items = items if isinstance(items, list) else [items]
    except ValueError:
        items = []
        for line in raw.splitlines():
            try:
                items.append(json.loads(line))
            except ValueError:
                pass
    return [{"service": i.get("Service"), "name": i.get("Name"), "state": i.get("State"),
             "health": i.get("Health") or "", "status": i.get("Status"), "ports": i.get("Publishers") and ", ".join(
                 f"{p.get('URL')}:{p.get('PublishedPort')}" for p in i["Publishers"] if p.get("PublishedPort")) or ""}
            for i in items]


async def status() -> dict:
    code, raw = await _run_capture("docker", "compose", "-p", PROJECT, "ps", "-a", "--format", "json")
    c2, raw2 = await _run_capture("docker", "compose", "-p", f"{PROJECT}-wm", "ps", "-a", "--format", "json")
    return {"active": storage.active_scenario(), "project": PROJECT,
            "containers": _parse_ps(raw) if code == 0 else [], "docker_error": "" if code == 0 else raw.strip()[:300],
            "detector": _parse_ps(raw2) if c2 == 0 else [],
            "job": ops.job.snapshot(tail=0) if ops.job else None}


async def logs(service: str, tail: int = 200) -> str:
    if service not in SERVICES:
        raise ValueError("unbekannter Dienst")
    proj = f"{PROJECT}-wm" if service == "wm-detector" and not storage.active_scenario() else PROJECT
    _, out = await _run_capture("docker", "compose", "-p", proj, "logs", "--no-color", "--tail", str(int(tail)), service,
                                timeout=30)
    return out[-200_000:]


# ======================================================================= Gesamtlauf

class Gesamtlauf:
    def __init__(self, scenarios_: list[str], repetitions: int, suite: dict, profile_id: str, per_rep_s: float,
                 keep_running: bool = False):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.suite_id, self.suite_name, self.items = suite["id"], suite["name"], suite["items"]
        self.repetitions, self.profile_id, self.keep_running = repetitions, profile_id, keep_running
        self.state = "laeuft"
        self.phase = ""
        self.started_at = now_iso()
        self.finished_at: str | None = None
        self.estimate_s = len(scenarios_) * (START_S + repetitions * per_rep_s)
        self.steps = [{"scenario": s, "status": "wartet", "text": "", "batches": [], "runs": [],
                       "started_at": None, "finished_at": None} for s in scenarios_]
        self.cancel_event = asyncio.Event()
        self.batch = None
        self.export: dict | None = None
        self.error = ""
        self.task: asyncio.Task | None = None

    def snapshot(self) -> dict:
        b = self.batch
        return {"id": self.id, "state": self.state, "phase": self.phase, "suite": self.suite_id,
                "suite_name": self.suite_name, "repetitions": self.repetitions, "started_at": self.started_at,
                "finished_at": self.finished_at, "estimate_s": self.estimate_s, "steps": self.steps,
                "export": self.export, "error": self.error,
                "batch": {"runs": [{k: r.get(k) for k in ("test", "name", "status", "progress", "text")} for r in b.runs]}
                if b else None}

    async def execute(self, manager) -> None:
        final = "fertig"
        try:
            for st in self.steps:
                sc = st["scenario"]
                if self.cancel_event.is_set():
                    st["status"], st["text"] = "abgebrochen", "nicht gestartet"
                    continue
                st["status"], st["started_at"] = "startet", now_iso()
                st["text"] = "wird gestartet (vLLM laden, Gateway, Mandanten) …"
                self.phase = f"{sc} starten"
                job = await ops.run("up", ["up", sc], f"Gesamtlauf: {sc} starten")
                st["job_id"] = job.id
                if job.code != 0:
                    if self.cancel_event.is_set():
                        st["status"], st["text"] = "abgebrochen", "Start abgebrochen"
                    else:
                        st["status"], st["text"] = "fehler", "Start fehlgeschlagen – siehe Ausgabe"
                        st["error_tail"] = job.lines[-15:]
                    st["finished_at"] = now_iso()
                    continue
                prof = storage.get_profile(self.profile_id)
                if not prof:
                    st["status"], st["text"] = "fehler", f"Profil „{self.profile_id}“ fehlt – „Einrichten“ ausführen"
                    st["finished_at"] = now_iso()
                    continue
                ok = True
                for i in range(self.repetitions):
                    if self.cancel_event.is_set():
                        break
                    st["status"], st["text"] = "misst", f"Messung {i + 1} von {self.repetitions}"
                    self.phase = f"{sc} messen ({i + 1}/{self.repetitions})"
                    try:
                        b = manager.start(prof, self.items, label=f"Gesamtlauf {self.id} · {sc} · {i + 1}/{self.repetitions}")
                    except (RuntimeError, ValueError) as e:
                        st["status"], st["text"], ok = "fehler", str(e), False
                        break
                    self.batch = b
                    st["batches"].append(b.id)
                    await b.task
                    self.batch = None
                    st["runs"] += [{"run_id": r.get("run_id"), "test": r.get("test"), "name": r.get("name"),
                                    "status": r.get("status"), "verdict": _verdict(r.get("run_id"))} for r in b.runs]
                    if b.state != "fertig":
                        ok = False
                done = sum(1 for r in st["runs"] if r.get("status") == "fertig")
                if self.cancel_event.is_set():
                    st["status"] = "abgebrochen"
                elif st["status"] != "fehler":
                    st["status"] = "fertig" if ok else "teilweise"
                st["text"] = f"{done} von {len(st['runs'])} Läufen fertig" if st["runs"] else st["text"]
                st["finished_at"] = now_iso()
            final = "abgebrochen" if self.cancel_event.is_set() else (
                "fertig" if all(s["status"] == "fertig" for s in self.steps) else "teilweise")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.error, final = str(e), "fehler"
        finally:
            self.batch = None
            if not self.keep_running:
                self.phase = "Szenario stoppen"
                try:
                    await ops.run("down", ["down"], "Gesamtlauf: Szenario stoppen")
                except RuntimeError:
                    pass
            self.phase = "Export schreiben"
            self.finished_at = now_iso()
            try:
                self.export = write_export(self)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.error = (self.error + " · " if self.error else "") + f"Export fehlgeschlagen: {e}"
            self.phase, self.state = "", final
            if self.export:
                storage.write_json(EXPORTS / self.id / "gesamtlauf.json", self.snapshot())

    def cancel(self, manager) -> bool:
        if self.state != "laeuft":
            return False
        self.cancel_event.set()
        manager.cancel()      # laufenden Test beenden
        if ops.job and ops.job.action == "up":
            ops.cancel()      # Szenario-Start nicht erst abwarten
        return True


def _verdict(run_id: str | None) -> str | None:
    if not run_id:
        return None
    from .runner import get_summary
    try:
        return get_summary(storage.RUNS / run_id).get("verdict")
    except Exception:  # noqa: BLE001
        return None


class GLManager:
    def __init__(self) -> None:
        self.current: Gesamtlauf | None = None

    def running(self) -> bool:
        return bool(self.current and self.current.state == "laeuft")


gl = GLManager()


def write_export(g: Gesamtlauf) -> dict:
    d = EXPORTS / g.id
    d.mkdir(parents=True, exist_ok=True)
    scen = [s["scenario"] for s in g.steps]
    data = mx.build(since=g.started_at)
    data["groups"] = [x for x in data["groups"] if x.get("scenario") in scen and x.get("profile_id") == g.profile_id]
    rows = [r for r in mx.runs_long(since=g.started_at, scenarios=scen)]
    active = storage.read_json(DEPLOY / ".active-scenario.json", {}) or {}
    gpu = (ops.last_check or {}).get("gpus") or []
    notes = [f"Gesamtlauf {g.id}: {g.started_at} bis {g.finished_at or now_iso()}",
             f"Suite „{g.suite_name}“, {g.repetitions} Wiederholung(en) je Szenario, Profil {g.profile_id}",
             "Szenarien: " + ", ".join(f"{s['scenario']} ({s['status']})" for s in g.steps),
             f"Testplattform {storage.TOOL_VERSION}"
             + (f", GPU {gpu[0]['name']} ({gpu[0]['total_mb'] / 1024:.1f} GB)" if gpu else "")
             + (f", Modell {active.get('model')}" if active.get("model") else ""),
             "konfigurationsvergleich.csv/.tex: neuester Lauf je Szenario und Test; diese Zusammenfassung: alle Wiederholungen."]
    files = {
        "zusammenfassung.md": mx.summary_md(rows, f"Konfigurationsvergleich – Gesamtlauf {g.id}", notes),
        "konfigurationsvergleich.tex": mx.to_latex(data),
        "konfigurationsvergleich.csv": "﻿" + mx.to_csv(data),
        "laeufe.csv": "﻿" + mx.runs_csv(rows),
    }
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return {"id": g.id, "dir": str(d.relative_to(storage.ROOT)) if storage.ROOT in d.parents else str(d),
            "files": [*files, "gesamtlauf.json"], "runs": len(rows)}


def list_exports() -> list[dict]:
    if not EXPORTS.exists():
        return []
    out = []
    for d in sorted(EXPORTS.iterdir(), reverse=True):
        if d.is_dir():
            snap = storage.read_json(d / "gesamtlauf.json", {}) or {}
            out.append({"id": d.name, "files": sorted(p.name for p in d.iterdir() if p.is_file()),
                        "state": snap.get("state"), "started_at": snap.get("started_at"),
                        "scenarios": [s.get("scenario") for s in snap.get("steps", [])],
                        "repetitions": snap.get("repetitions"), "suite_name": snap.get("suite_name")})
    return out
