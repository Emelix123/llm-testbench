"""Messumgebung festhalten: Hardware, Treiber, Software, Images und Code-Stand.

Zwei Stufen:

* ``run_snapshot()`` – kompakt, für jede ``config.json`` eines Laufs (GPU mit Zustand beim Start,
  Treiber/CUDA, Prozesse auf der GPU, Betriebssystem, CPU, Image-Digests, Git-Commit, Code-Hash).
* ``full_snapshot()`` – ausführlich, einmal je Gesamtlauf vor dem ersten Szenario; landet im Export
  als ``umgebung.json``, ``umgebung.md`` und ``nvidia-smi.txt`` (``nvidia-smi -q``).

Fehlt ein Werkzeug (kein nvidia-smi, kein Docker, kein Git, Windows), bleibt das Feld leer –
die Testplattform läuft weiter. Aus ``deploy/.env`` werden nur nicht geheime Werte gelesen.

Außerdem: Auswertung der Container-Logs (vLLM-Startkommando, KV-Cache-Größe, Blockgröße, Version)
für ``szenario-konfiguration.md`` im Export.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import storage

ROOT = storage.ROOT
DEPLOY = ROOT / "deploy"

# Nur diese Werte aus deploy/.env werden übernommen – alles andere kann geheim sein.
DEPLOY_ENV_PUBLIC = ("VLLM_IMAGE", "LITELLM_IMAGE", "POSTGRES_IMAGE", "MODEL", "SERVED_MODEL_NAME",
                     "MAX_MODEL_LEN", "KV_CACHE_MEMORY", "GPU_MEMORY_UTILIZATION", "KV_CACHE_MEMORY_DEDICATED",
                     "GPU_MEMORY_UTILIZATION_DEDICATED", "TENANT_POOL_SIZE", "LLMTB_GPU_VRAM_MB")

# Dateien, deren Inhalt das Messverfahren bestimmt (Code-Hash). data/ und Geheimnisse nie.
CODE_GLOBS = ("backend/app/*.py", "backend/requirements.txt", "tests/*/*.py", "tests/*/*.json", "tests/*/*.jsonl",
              "deploy/scenario.sh", "deploy/ops_check.py", "deploy/redact_logs.py", "deploy/.env.example",
              "deploy/compose/*.yaml", "deploy/litellm/*", "deploy/vllm/*", "deploy/watermark/*", "start.sh")

GPU_FIELDS_FULL = ("index", "name", "uuid", "pci.bus_id", "compute_cap", "memory.total", "memory.used",
                   "utilization.gpu", "temperature.gpu", "power.draw", "power.limit", "power.default_limit",
                   "power.max_limit", "clocks.sm", "clocks.max.sm", "clocks.mem", "clocks.max.mem",
                   "driver_version", "vbios_version", "persistence_mode", "mig.mode.current")
GPU_FIELDS_MIN = ("index", "name", "memory.total", "memory.used", "utilization.gpu", "temperature.gpu",
                  "power.draw", "power.limit", "driver_version")


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    if not shutil.which(cmd[0]):
        return 127, ""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"})
        # nur hinten kürzen: führende Leerzeichen tragen z. B. bei `git status --porcelain` Bedeutung
        return p.returncode, (p.stdout or "").rstrip()
    except (OSError, subprocess.TimeoutExpired):
        return 124, ""


# Versionsartige Felder bleiben Text („12.0“ ist keine Zahl 12)
_TEXT_FIELDS = {"name", "uuid", "pci.bus_id", "compute_cap", "driver_version", "vbios_version",
                "persistence_mode", "mig.mode.current"}


def _num(v: str, field: str = "") -> Any:
    v = v.strip()
    if not v or v.startswith("[") or v in ("N/A", "Not Supported"):
        return None
    if field in _TEXT_FIELDS:
        return v
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except ValueError:
        return v


# ======================================================================= GPU

def gpus() -> list[dict]:
    """Eine Zeile je GPU. Ältere Treiber kennen nicht alle Felder – dann die Minimalliste."""
    for fields in (GPU_FIELDS_FULL, GPU_FIELDS_MIN):
        code, out = _run(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"])
        if code == 0 and out:
            rows = []
            for line in out.splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) == len(fields):
                    rows.append({f: _num(v, f) for f, v in zip(fields, parts)})
            if rows:
                return rows
    return []


def cuda_driver_version() -> str | None:
    """Höchste CUDA-Version, die der Treiber unterstützt (Kopfzeile von nvidia-smi)."""
    _, out = _run(["nvidia-smi"])
    m = re.search(r"CUDA Version:\s*([\d.]+)", out)
    return m.group(1) if m else None


def gpu_processes() -> list[dict]:
    """Prozesse, die gerade GPU-Speicher belegen (fremde Last verfälscht Zeitmessungen)."""
    code, out = _run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                      "--format=csv,noheader,nounits"])
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 3:
            rows.append({"pid": _num(parts[0]), "name": parts[1], "used_mb": _num(parts[2])})
    return rows


# ======================================================================= System

def os_info() -> dict:
    pretty = None
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    release = platform.release()
    return {"system": platform.system(), "distribution": pretty, "kernel": release,
            "wsl": "microsoft" in release.lower(), "machine": platform.machine()}


def cpu_info() -> dict:
    model = None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        model = platform.processor() or None
    ram = None
    try:
        import psutil
        ram = round(psutil.virtual_memory().total / 2**30, 1)
    except Exception:  # noqa: BLE001
        pass
    return {"model": model, "logical_cpus": os.cpu_count(), "ram_gib": ram}


def deploy_env() -> dict:
    """Nicht geheime Werte aus deploy/.env (sonst .env.example)."""
    vals: dict[str, str] = {}
    for name in (".env.example", ".env"):
        p = DEPLOY / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in DEPLOY_ENV_PUBLIC:
                    vals[k.strip()] = v.strip()
    vals["_source"] = "deploy/.env" if (DEPLOY / ".env").exists() else "deploy/.env.example"
    return vals


def images(env: dict | None = None) -> list[dict]:
    """Image-Digests: Ein Tag kann neu belegt werden, der Digest nicht."""
    env = env or deploy_env()
    names = [env[k] for k in ("VLLM_IMAGE", "LITELLM_IMAGE", "POSTGRES_IMAGE") if env.get(k)]
    if not names:
        return []
    _, out = _run(["docker", "image", "inspect", *names,
                   "--format", "{{json .RepoTags}}|{{json .RepoDigests}}|{{.Id}}|{{.Created}}"], timeout=20)
    found: dict[str, dict] = {}
    for line in out.splitlines():
        try:
            tags, digests, iid, created = line.split("|", 3)
            tags, digests = json.loads(tags) or [], json.loads(digests) or []
        except ValueError:
            continue
        for n in names:
            if n in tags:
                found[n] = {"image": n, "digest": digests[0] if digests else None, "id": iid, "created": created}
    return [found.get(n) or {"image": n, "digest": None, "id": None, "missing": True} for n in names]


def docker_info() -> dict:
    _, server = _run(["docker", "version", "--format", "{{.Server.Version}}"])
    _, compose = _run(["docker", "compose", "version", "--short"])
    _, ctk = _run(["nvidia-ctk", "--version"])
    return {"engine": server or None, "compose": compose or None,
            "nvidia_container_toolkit": (ctk.splitlines()[0] if ctk else None)}


# ======================================================================= Code-Stand

def _strip_credentials(url: str) -> str:
    try:
        u = urlsplit(url)
        if u.username or u.password:
            host = u.hostname or ""
            if u.port:
                host += f":{u.port}"
            return urlunsplit((u.scheme, host, u.path, u.query, u.fragment))
    except ValueError:
        pass
    return url


def git_info() -> dict:
    if not (ROOT / ".git").exists():
        return {"available": False}
    g = ["git", "-C", str(ROOT)]
    code, commit = _run(g + ["rev-parse", "HEAD"])
    if code != 0:
        return {"available": False}
    _, branch = _run(g + ["rev-parse", "--abbrev-ref", "HEAD"])
    _, date = _run(g + ["log", "-1", "--format=%cI"])
    _, remote = _run(g + ["remote", "get-url", "origin"])
    _, status = _run(g + ["status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude)data"])
    changed = [line[3:] for line in status.splitlines() if len(line) > 3]   # „XY datei“
    commit = commit.strip()
    return {"available": True, "commit": commit, "short": commit[:10], "branch": branch or None,
            "commit_date": date or None, "remote": _strip_credentials(remote) if remote else None,
            "dirty": bool(changed), "changed_files": changed[:30], "changed_count": len(changed)}


def code_sha256() -> str:
    """Hash über alle Dateien, die das Messverfahren bestimmen – auch ohne Git aussagekräftig."""
    h = hashlib.sha256()
    files: set[Path] = set()
    for pattern in CODE_GLOBS:
        files.update(p for p in ROOT.glob(pattern) if p.is_file() and "__pycache__" not in p.parts)
    for p in sorted(files):
        h.update(p.relative_to(ROOT).as_posix().encode() + b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


# ======================================================================= Snapshots

def run_snapshot(local_endpoint: bool | None = None) -> dict:
    """Kompakt für config.json jedes Laufs."""
    env = deploy_env()
    snap = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": ("Rechner, auf dem die Testplattform läuft. Bei lokalem Szenario (K0–K6) ist das auch der "
                 "Inferenz-Rechner; bei entfernten Endpunkten nur der Lastgenerator."),
        "local_endpoint": local_endpoint,
        "gpus": gpus(),
        "cuda_driver": cuda_driver_version(),
        "gpu_processes": gpu_processes(),
        "os": os_info(),
        "cpu": cpu_info(),
        "python": sys.version.split()[0],
        "images": images(env),
        "git": git_info(),
        "code_sha256": code_sha256(),
        "tool_version": storage.TOOL_VERSION,
    }
    return snap


def full_snapshot() -> tuple[dict, str]:
    """Ausführlich für den Export eines Gesamtlaufs. Gibt (Daten, Text von nvidia-smi -q) zurück."""
    snap = run_snapshot(local_endpoint=True)
    snap["docker"] = docker_info()
    snap["deploy_env"] = deploy_env()
    snap["settings"] = storage.get_settings()
    _, q = _run(["nvidia-smi", "-q"], timeout=30)
    return snap, q


# ======================================================================= Darstellung

def _fmt(v: Any, unit: str = "") -> str:
    if v is None or v == "":
        return "–"
    if isinstance(v, float):
        v = f"{v:.1f}".replace(".", ",")
    return f"{v}{(' ' + unit) if unit else ''}"


def gpu_line(snap: dict) -> str | None:
    """Ein Satz für Notizen/Zusammenfassung."""
    g = (snap.get("gpus") or [None])[0]
    if not g:
        return None
    mem = g.get("memory.total")
    parts = [f"GPU {g.get('name')}"]
    if mem:
        parts.append(f"{mem / 1024:.1f} GB".replace(".", ","))
    if g.get("driver_version"):
        parts.append(f"Treiber {g['driver_version']}")
    if snap.get("cuda_driver"):
        parts.append(f"CUDA {snap['cuda_driver']}")
    if g.get("power.limit"):
        parts.append(f"Leistungsgrenze {_fmt(g['power.limit'], 'W')}")
    return ", ".join(parts)


def code_line(snap: dict) -> str:
    gi = snap.get("git") or {}
    if gi.get("available"):
        s = f"Commit {gi.get('short')} ({gi.get('branch') or '?'})"
        s += f", {gi['changed_count']} lokale Änderung(en)" if gi.get("dirty") else ", ohne lokale Änderungen"
    else:
        s = "kein Git-Repository"
    return s + f", Code-Hash {snap.get('code_sha256')}"


def render_md(snap: dict, title: str) -> str:
    out = [f"# {title}", "",
           f"Aufgenommen {snap.get('captured_at')} vor dem ersten Szenario. Rohdaten: `umgebung.json`, "
           "vollständige GPU-Angaben: `nvidia-smi.txt`.", ""]
    out += ["## Hardware", "", "| | |", "|---|---|"]
    gl = snap.get("gpus") or []
    if not gl:
        out.append("| GPU | keine NVIDIA-GPU gefunden (nvidia-smi) |")
    for g in gl:
        pre = f"GPU {g.get('index')}" if len(gl) > 1 else "GPU"
        out += [f"| {pre} | {_fmt(g.get('name'))} |",
                f"| {pre}: Speicher | {_fmt(g.get('memory.total'), 'MiB')} |",
                f"| {pre}: Compute Capability | {_fmt(g.get('compute_cap'))} |",
                f"| {pre}: Treiber / CUDA (Treiber) | {_fmt(g.get('driver_version'))} / {_fmt(snap.get('cuda_driver'))} |",
                f"| {pre}: Leistungsgrenze (Standard / max.) | {_fmt(g.get('power.limit'), 'W')} "
                f"({_fmt(g.get('power.default_limit'), 'W')} / {_fmt(g.get('power.max_limit'), 'W')}) |",
                f"| {pre}: max. Takt SM / Speicher | {_fmt(g.get('clocks.max.sm'), 'MHz')} / "
                f"{_fmt(g.get('clocks.max.mem'), 'MHz')} |",
                f"| {pre}: Persistence Mode / MIG | {_fmt(g.get('persistence_mode'))} / {_fmt(g.get('mig.mode.current'))} |",
                f"| {pre}: beim Start belegt / Auslastung / Temperatur | {_fmt(g.get('memory.used'), 'MiB')} / "
                f"{_fmt(g.get('utilization.gpu'), '%')} / {_fmt(g.get('temperature.gpu'), '°C')} |"]
    procs = snap.get("gpu_processes") or []
    out.append("| Prozesse auf der GPU beim Start | " + (", ".join(
        f"{p.get('name')} (PID {p.get('pid')}, {_fmt(p.get('used_mb'), 'MiB')})" for p in procs) or "keine") + " |")
    c = snap.get("cpu") or {}
    out += [f"| CPU | {_fmt(c.get('model'))} ({_fmt(c.get('logical_cpus'))} Threads) |",
            f"| Arbeitsspeicher | {_fmt(c.get('ram_gib'), 'GiB')} |", ""]
    o, d, e = snap.get("os") or {}, snap.get("docker") or {}, snap.get("deploy_env") or {}
    out += ["## Software", "", "| | |", "|---|---|",
            f"| Betriebssystem | {_fmt(o.get('distribution') or o.get('system'))}, Kernel {_fmt(o.get('kernel'))}"
            f"{' (WSL)' if o.get('wsl') else ''} |",
            f"| Docker Engine / Compose | {_fmt(d.get('engine'))} / {_fmt(d.get('compose'))} |",
            f"| NVIDIA Container Toolkit | {_fmt(d.get('nvidia_container_toolkit'))} |"]
    for im in snap.get("images") or []:
        dig = im.get("digest") or ("nicht geladen" if im.get("missing") else "–")
        out.append(f"| Image {im.get('image')} | `{dig}` |")
    out += [f"| Python (Testplattform) | {_fmt(snap.get('python'))} |", ""]
    out += ["## Modell und Engine (deploy/.env)", "", "| | |", "|---|---|"]
    for k in DEPLOY_ENV_PUBLIC:
        if e.get(k) not in (None, ""):
            out.append(f"| `{k}` | {e[k]} |")
    out += [f"| Quelle | {e.get('_source', '–')} |", ""]
    gi = snap.get("git") or {}
    out += ["## Code-Stand", "", "| | |", "|---|---|"]
    if gi.get("available"):
        out += [f"| Commit | `{gi.get('commit')}` ({gi.get('commit_date') or '–'}) |",
                f"| Branch / Remote | {gi.get('branch') or '–'} / {gi.get('remote') or '–'} |",
                f"| Lokale Änderungen | " + ("keine" if not gi.get("dirty") else
                                           f"{gi['changed_count']}: " + ", ".join(f"`{x}`" for x in gi['changed_files'])) + " |"]
    else:
        out.append("| Commit | kein Git-Repository |")
    out += [f"| Code-Hash (Tests, Backend, Deploy) | `{snap.get('code_sha256')}` |",
            f"| Testplattform | {snap.get('tool_version')} |", ""]
    cost = (snap.get("settings") or {}).get("cost") or {}
    if cost:
        out += ["## Kostenannahmen (Einstellungen → Kosten, Stand beim Start)", "", "| | |", "|---|---|",
                f"| Maschine | {_fmt(cost.get('machine_eur_per_h'), '€/h')} |",
                f"| Leistungsaufnahme | {_fmt(cost.get('power_kw'), 'kW')} |",
                f"| Strompreis | {_fmt(cost.get('power_eur_per_kwh'), '€/kWh')} |",
                f"| Auslastung | {_fmt(cost.get('utilization_pct'), '%')} |", ""]
    return "\n".join(out) + "\n"


# ======================================================================= Container-Logs auswerten

_LINE = re.compile(r"^(?P<svc>[A-Za-z0-9_.-]+?)(?:-\d+)?\s+\|\s?(?P<msg>.*)$")
_INTERESTING = re.compile(
    r"\[llmtb\]|API server version|vLLM version|Initializing a V\d|non-default args|KV cache|"
    r"Maximum concurrency|block[_ ]size|prefix.?caching|extensible|watermark|max_num_seqs|"
    r"attention backend|Using .*backend|CUDA graph|Model loading took|Loading weights took", re.I)
_KV_TOKENS = re.compile(r"KV cache size:?\s*([\d][\d,.\s]*?)\s*tokens", re.I)
_KV_MEM = re.compile(r"(?:Available )?KV cache memory:?\s*([\d.,]+\s*[KMGT]?i?B)", re.I)
_CONC = re.compile(r"Maximum concurrency for\s*([\d,.]+)\s*tokens per request:\s*([\d,.]+)x", re.I)
_BLOCK = re.compile(r"block[_ ]size['\"]?\s*[=:]\s*(\d+)", re.I)
_VERSION = re.compile(r"(?:API server version|vLLM version|engine \(v)\s*v?(\d+\.\d+\.\d+[\w.+-]*)", re.I)
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+\s+")


def parse_container_log(text: str) -> dict[str, dict]:
    """Je Dienst: Startkommando, Version, KV-Cache, Blockgröße, relevante Zeilen, Warnungen/Fehler."""
    svcs: dict[str, dict] = {}
    for raw in text.splitlines():
        if raw.startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        svc, msg = m.group("svc"), _TS.sub("", m.group("msg"))
        s = svcs.setdefault(svc, {"llmtb": [], "lines": [], "warnings": 0, "errors": 0, "error_lines": [],
                                  "version": None, "kv_tokens": None, "kv_memory": None, "concurrency": None,
                                  "block_size": None, "line_count": 0})
        s["line_count"] += 1
        if "[llmtb]" in msg:
            s["llmtb"].append(msg[msg.index("[llmtb]"):][:2000])
        if re.search(r"\bWARN(ING)?\b", msg):
            s["warnings"] += 1
        if re.search(r"\b(ERROR|CRITICAL|Traceback)\b", msg):
            s["errors"] += 1
            if len(s["error_lines"]) < 8:
                s["error_lines"].append(msg[:500])
        if _INTERESTING.search(msg) and len(s["lines"]) < 40:
            s["lines"].append(msg[:800])
        if (v := _VERSION.search(msg)) and not s["version"]:
            s["version"] = v.group(1)
        if v := _KV_TOKENS.search(msg):
            try:
                s["kv_tokens"] = int(re.sub(r"[,.\s]", "", v.group(1)))
            except ValueError:
                pass
        if v := _KV_MEM.search(msg):
            s["kv_memory"] = v.group(1)
        if v := _CONC.search(msg):
            s["concurrency"] = f"{v.group(2)}× bei {v.group(1)} Tokens je Anfrage"
        if (v := _BLOCK.search(msg)) and not s["block_size"]:
            s["block_size"] = int(v.group(1))
    return svcs


def kv_consistency(parsed: dict[str, dict[str, dict]]) -> str | None:
    """Alle Szenarien außer K5 sollen denselben KV-Cache haben (fest über KV_CACHE_MEMORY)."""
    vals = {}
    for sc, svcs in parsed.items():
        if sc.upper().startswith("K5"):
            continue
        for name, s in svcs.items():
            if name.startswith("vllm") and s.get("kv_tokens"):
                vals[f"{sc}/{name}"] = s["kv_tokens"]
    if len(vals) < 2:
        return None
    lo, hi = min(vals.values()), max(vals.values())
    if hi > lo * 1.05:
        return ("KV-Cache unterschiedlich groß zwischen den Szenarien (" +
                ", ".join(f"{k}: {v}" for k, v in sorted(vals.items())) +
                " Tokens) – die Szenarien sind so nicht direkt vergleichbar.")
    return None


def render_scenario_md(parsed: dict[str, dict[str, dict]], title: str, missing: list[str]) -> str:
    out = [f"# {title}", "",
           "Aus den Container-Logs jedes Szenarios, gesichert direkt nach der Messung und vor dem Stoppen. "
           "Vollständige Logs: `container-logs.zip` (Geheimnisse geschwärzt).", "",
           "## Überblick", "",
           "| Szenario | Dienst | vLLM-Version | KV-Cache (Tokens) | KV-Speicher | Max. Parallelität | Blockgröße | Warnungen / Fehler |",
           "|---|---|---|---:|---|---|---:|---|"]
    for sc in sorted(parsed):
        for name, s in sorted(parsed[sc].items()):
            if not name.startswith("vllm"):
                continue
            out.append(f"| {sc} | {name} | {_fmt(s['version'])} | {_fmt(s['kv_tokens'])} | {_fmt(s['kv_memory'])} | "
                       f"{_fmt(s['concurrency'])} | {_fmt(s['block_size'])} | {s['warnings']} / {s['errors']} |")
    if missing:
        out += ["", "Keine Logs für: " + ", ".join(missing) + "."]
    warn = kv_consistency(parsed)
    if warn:
        out += ["", f"**Achtung:** {warn}"]
    out += ["", "Fehlt ein Wert (–), stand er so nicht im Log; dann im vollständigen Log nachsehen. "
            "Erwartung: K0–K4 und K6 haben denselben KV-Cache (`KV_CACHE_MEMORY`), K5 je Instanz "
            "`KV_CACHE_MEMORY_DEDICATED`."]
    for sc in sorted(parsed):
        out += ["", f"## {sc}"]
        for name, s in sorted(parsed[sc].items()):
            if not (s["llmtb"] or s["lines"] or s["error_lines"]):
                continue
            out += ["", f"### {name} ({s['line_count']} Zeilen, {s['warnings']} Warnungen, {s['errors']} Fehler)"]
            if s["llmtb"]:
                out += ["", "Von der Testplattform protokolliert (Startkommando, Grenzen):", "", "```"]
                out += s["llmtb"][:40]
                out += ["```"]
            if s["lines"]:
                out += ["", "Relevante Zeilen:", "", "```"]
                out += s["lines"]
                out += ["```"]
            if s["error_lines"]:
                out += ["", "Fehlerzeilen (erste 8):", "", "```"]
                out += s["error_lines"]
                out += ["```"]
    return "\n".join(out) + "\n"
