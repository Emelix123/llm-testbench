"""Dateiablage unter data/: Profile, Secrets, Einstellungen, Läufe.

Keine Datenbank. Alles ist JSON/JSONL, damit ein Lauf als Ordner exportiert,
verglichen und jederzeit neu ausgewertet werden kann.
"""
from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("LLMTB_DATA", ROOT / "data"))
PROFILES = DATA / "profiles"
SUITES = DATA / "suites"
RUNS = DATA / "runs"
SECRETS = DATA / "secrets.env"
SETTINGS = DATA / "settings.json"
TOKENIZER_CACHE = DATA / "tokenizers"

TOOL_VERSION = "1.0.0"

for d in (DATA, PROFILES, SUITES, RUNS, TOKENIZER_CACHE):
    d.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

DEFAULT_SETTINGS: dict[str, Any] = {
    "traffic_light": True,
    "slo": {
        "ttft_p95_ms": 2000,      # Wartezeit bis erstes Token, p95
        "itl_p95_ms": 100,        # Abstand zwischen Tokens, p95 (= 10 Tokens/s pro Nutzer)
        "error_rate_pct": 1.0,    # Fehlerquote
    },
    "cost": {
        "machine_eur_per_h": 2.50,
        "power_kw": 1.0,
        "power_eur_per_kwh": 0.30,
        "utilization_pct": 50,
        "read_time_s": 50,
    },
    "client": {
        "cpu_warn_pct": 80,
        "request_timeout_s": 600,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return deepcopy(default)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- Einstellungen

def get_settings() -> dict:
    return _deep_merge(DEFAULT_SETTINGS, read_json(SETTINGS, {}))


def save_settings(s: dict) -> dict:
    merged = _deep_merge(DEFAULT_SETTINGS, s)
    write_json(SETTINGS, merged)
    return merged


# ---------------------------------------------------------------- Secrets

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def read_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    if not SECRETS.exists():
        return out
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def set_secret(name: str, value: str) -> None:
    if not _KEY_RE.match(name):
        raise ValueError("Name des Keys: nur A-Z, 0-9, _ und mit Buchstaben beginnend")
    with _lock:
        s = read_secrets()
        s[name] = value
        SECRETS.write_text(
            "# API-Keys. Diese Datei landet nie in runs/ und nie im Export.\n"
            + "".join(f"{k}={v}\n" for k, v in sorted(s.items())),
            encoding="utf-8",
        )
        try:
            os.chmod(SECRETS, 0o600)
        except OSError:
            pass


def get_secret(name: str | None) -> str | None:
    if not name:
        return None
    return read_secrets().get(name) or os.environ.get(name)


# ---------------------------------------------------------------- Profile

_ID_RE = re.compile(r"[^a-z0-9_-]+")


def slugify(name: str) -> str:
    s = _ID_RE.sub("-", name.lower()).strip("-")
    return s or "profil"


def list_profiles() -> list[dict]:
    out = []
    for p in sorted(PROFILES.glob("*.json")):
        prof = read_json(p, {})
        prof["id"] = p.stem
        prof["has_key"] = bool(get_secret(prof.get("api_key_name")))
        out.append(prof)
    return out


def get_profile(pid: str) -> dict | None:
    p = PROFILES / f"{slugify(pid)}.json"
    if not p.exists():
        return None
    prof = read_json(p, {})
    prof["id"] = p.stem
    return prof


PROFILE_FIELDS = ("name", "base_url", "model", "tokenizer", "api_key_name",
                  "max_context", "capabilities", "notes")


def save_profile(data: dict) -> dict:
    pid = slugify(data.get("id") or data.get("name") or "profil")
    prof = {k: data.get(k) for k in PROFILE_FIELDS if data.get(k) is not None}
    prof["base_url"] = (prof.get("base_url") or "").rstrip("/")
    if not prof.get("api_key_name"):
        prof["api_key_name"] = (pid.upper().replace("-", "_") + "_API_KEY")
    existing = get_profile(pid) or {}
    if "capabilities" not in prof and existing.get("capabilities"):
        prof["capabilities"] = existing["capabilities"]
    write_json(PROFILES / f"{pid}.json", prof)
    prof["id"] = pid
    return prof


def update_profile_capabilities(pid: str, caps: dict) -> None:
    prof = get_profile(pid)
    if not prof:
        return
    prof.setdefault("capabilities", {}).update(caps)
    prof.pop("id", None)
    write_json(PROFILES / f"{slugify(pid)}.json", prof)


def delete_profile(pid: str) -> None:
    (PROFILES / f"{slugify(pid)}.json").unlink(missing_ok=True)


# ---------------------------------------------------------------- Läufe

def list_runs() -> list[dict]:
    out = []
    for d in sorted(RUNS.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        cfg = read_json(d / "config.json", {})
        summ = read_json(d / "summary.json", {})
        out.append({
            "id": d.name,
            "test": cfg.get("test"),
            "test_name": cfg.get("test_name"),
            "batch_id": cfg.get("batch_id"),
            "batch_index": cfg.get("batch_index"),
            "profile_id": (cfg.get("profile") or {}).get("id"),
            "profile_name": (cfg.get("profile") or {}).get("name"),
            "started_at": cfg.get("started_at"),
            "finished_at": cfg.get("finished_at"),
            "status": cfg.get("status"),
            "verdict": summ.get("verdict"),
            "headline": summ.get("headline"),
        })
    return out


def run_dir(run_id: str) -> Path:
    d = (RUNS / run_id).resolve()
    if RUNS.resolve() not in d.parents or not d.is_dir():
        raise FileNotFoundError(run_id)
    return d


def iter_requests(d: Path):
    p = d / "requests.jsonl"
    if not p.exists():
        return
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # halbe Zeile nach Absturz


def iter_jsonl(p: Path):
    if not p.exists():
        return
    with p.open(encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


class JsonlWriter:
    """Hängt jede Zeile sofort an und flusht, damit ein Absturz nichts kostet."""

    def __init__(self, path: Path):
        self.path = path
        self._f = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()

    def close(self) -> None:
        try:
            self._f.close()
        except Exception:
            pass


# ---------------------------------------------------------------- Referenzläufe (Qualität)

REFERENCES = DATA / "references.json"


def get_references() -> dict:
    return read_json(REFERENCES, {})


def set_reference(test: str, run_id: str | None) -> dict:
    refs = get_references()
    if run_id:
        refs[test] = run_id
    else:
        refs.pop(test, None)
    write_json(REFERENCES, refs)
    return refs
