#!/usr/bin/env python3
"""Systemprüfung für die Szenarien K0–K6: Ist dieser Rechner bereit?

Nur Standardbibliothek (Python >= 3.8), damit sie auch vor der Einrichtung läuft.
Genutzt von `./scenario.sh check` (Text) und der Weboberfläche, Seite „Steuerung“ (--json).

  python3 deploy/ops_check.py [--json] [--gpu-container]

--gpu-container startet zusätzlich einen kurzen Container mit GPU (nvidia-smi im vLLM-Image),
sofern das Image schon da ist. Das ist der einzige sichere Nachweis, dass Docker die GPU sieht.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEPLOY = Path(__file__).resolve().parent
ROOT = DEPLOY.parent
PROJECT = os.environ.get("LLMTB_PROJECT", "llmtb")
K5_MIN_MB = 8000          # zwei vLLM-Instanzen auf einer GPU
MIN_VRAM_MB = 3800        # Qwen3-0.6B + 0,7 GiB KV-Cache
IMAGES_GB = 40            # vLLM ~36 GB + LiteLLM ~1,7 GB + Postgres


def power_state() -> Optional[dict]:
    """Netzteil oder Akku? Liest /sys/class/power_supply (Linux, auch WSL). None = kein Akku (Server, Desktop)."""
    base = "/sys/class/power_supply"
    try:
        names = os.listdir(base)
    except OSError:
        return None

    def rd(name: str, f: str) -> Optional[str]:
        try:
            with open(os.path.join(base, name, f)) as fh:
                return fh.read().strip()
        except OSError:
            return None

    mains_online, bats = None, []
    for n in names:
        t = rd(n, "type")
        if t == "Mains":
            mains_online = bool(mains_online) or rd(n, "online") == "1"
        elif t == "Battery":
            bats.append((rd(n, "status"), rd(n, "capacity")))
    if not bats:
        return None
    on_battery = any(st == "Discharging" for st, _ in bats) or mains_online is False
    cap = next((int(c) for _, c in bats if (c or "").isdigit()), None)
    return {"on_battery": on_battery, "capacity": cap}


def run(cmd: list[str], timeout: int = 25) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "nicht gefunden"
    except subprocess.TimeoutExpired:
        return 124, "", "Zeitüberschreitung"


def read_env() -> dict[str, str]:
    vals: dict[str, str] = {}
    for name in (".env.example", ".env"):        # .env überschreibt die Vorlage
        p = DEPLOY / name
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
    return vals


def gpu_info() -> list[dict]:
    code, out, _ = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
                        "--format=csv,noheader,nounits"])
    gpus = []
    if code == 0:
        for line in out.splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) >= 4:
                try:
                    gpus.append({"name": parts[0], "total_mb": int(float(parts[1])),
                                 "used_mb": int(float(parts[2])), "driver": parts[3]})
                except ValueError:
                    pass
    return gpus


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.lstrip("v").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        if not num:
            break
        out.append(int(num))
    return tuple(out)


def check(gpu_container: bool = False) -> dict:
    env = read_env()
    items: list[dict] = []

    def add(key, label, status, detail="", fix=""):
        items.append({"key": key, "label": label, "status": status, "detail": detail, "fix": fix})

    # ------------------------------------------------------------------ Docker
    code, ver, err = run(["docker", "version", "--format", "{{.Server.Version}}"])
    docker_ok = code == 0 and bool(ver)
    desktop = False
    info: dict = {}
    if docker_ok:
        c2, raw, _ = run(["docker", "info", "--format", "{{json .}}"])
        if c2 == 0:
            try:
                info = json.loads(raw)
            except ValueError:
                info = {}
        desktop = "Docker Desktop" in str(info.get("OperatingSystem", ""))
        add("docker", "Docker", "ok", f"Engine {ver}" + (" (Docker Desktop)" if desktop else ""))
    elif code == 127:
        add("docker", "Docker", "fail", "docker ist nicht installiert",
            "Docker Engine installieren (Linux-Server) bzw. Docker Desktop mit WSL-Integration (Windows).")
    else:
        add("docker", "Docker", "fail", (err or "Docker-Dienst nicht erreichbar")[:200],
            "Docker starten; auf Linux den eigenen Nutzer in die Gruppe 'docker' aufnehmen (danach neu anmelden).")

    c3, cver, _ = run(["docker", "compose", "version", "--short"])
    if c3 == 0 and cver:
        ok = version_tuple(cver) >= (2, 20)
        add("compose", "Docker Compose v2", "ok" if ok else "warn", cver,
            "" if ok else "Compose >= 2.20 empfohlen (extends, --wait-timeout).")
    else:
        add("compose", "Docker Compose v2", "fail", "docker compose fehlt", "Paket docker-compose-plugin installieren.")

    # ------------------------------------------------------------------ GPU
    gpus = gpu_info()
    vram = max((g["total_mb"] for g in gpus), default=0)
    if gpus:
        g = max(gpus, key=lambda x: x["total_mb"])
        st = "ok" if vram >= MIN_VRAM_MB else "fail"
        add("gpu", "NVIDIA-GPU", st,
            f"{g['name']}, {g['total_mb'] / 1024:.1f} GB VRAM ({g['used_mb'] / 1024:.1f} GB belegt), Treiber {g['driver']}"
            + (f" – {len(gpus)} GPUs, genutzt wird GPU 0" if len(gpus) > 1 else ""),
            "" if st == "ok" else "Mindestens 4 GB VRAM nötig.")
        if vram >= K5_MIN_MB:
            add("k5", "Szenario K5 (zwei Instanzen)", "ok", "genug VRAM für zwei vLLM-Instanzen")
        else:
            add("k5", "Szenario K5 (zwei Instanzen)", "warn", f"braucht >= 8 GB VRAM, hier {vram / 1024:.1f} GB",
                "K5 wird im Gesamtlauf übersprungen; in der Arbeit als „nicht gemessen“ kennzeichnen.")
    else:
        add("gpu", "NVIDIA-GPU", "fail", "nvidia-smi nicht gefunden oder keine GPU",
            "NVIDIA-Treiber installieren (unter WSL: Windows-Treiber, nicht im Linux installieren).")

    vllm_img = env.get("VLLM_IMAGE", "vllm/vllm-openai:v0.34.0")
    images = [vllm_img, env.get("LITELLM_IMAGE", ""), env.get("POSTGRES_IMAGE", "postgres:16-alpine")]
    present = {}
    if docker_ok:
        for img in [i for i in images if i]:
            present[img] = run(["docker", "image", "inspect", "-f", "{{.Id}}", img])[0] == 0

    if docker_ok:
        runtimes = json.dumps(info.get("Runtimes") or {})
        cdi = any(Path(p).exists() for p in ("/etc/cdi/nvidia.yaml", "/var/run/cdi/nvidia.yaml"))
        if gpu_container and present.get(vllm_img):
            c4, out4, err4 = run(["docker", "run", "--rm", "--gpus", "all", "--entrypoint", "nvidia-smi", vllm_img, "-L"],
                                 timeout=90)
            if c4 == 0:
                add("gpu_docker", "GPU im Container", "ok", out4.splitlines()[0][:120] if out4 else "sichtbar")
            else:
                add("gpu_docker", "GPU im Container", "fail", (err4 or out4)[:200],
                    "NVIDIA Container Toolkit installieren und Docker neu starten (nvidia-ctk runtime configure).")
        elif "nvidia" in runtimes or cdi or desktop:
            add("gpu_docker", "GPU im Container", "ok" if not gpu_container else "info",
                ("Docker Desktop reicht die GPU durch" if desktop else "NVIDIA Container Toolkit erkannt")
                + ("" if present.get(vllm_img) else " – echter Test nach dem Laden der Images möglich"))
        else:
            add("gpu_docker", "GPU im Container", "warn", "NVIDIA Container Toolkit nicht erkannt",
                "nvidia-container-toolkit installieren, dann: sudo nvidia-ctk runtime configure --runtime=docker "
                "&& sudo systemctl restart docker")

    # ------------------------------------------------------------------ Images, Platz, Modell
    if docker_ok:
        missing = [i for i, ok in present.items() if not ok]
        if missing:
            add("images", "Docker-Images", "warn", "fehlen: " + ", ".join(missing),
                f"„Images & Modell laden“ (./scenario.sh pull) – rund {IMAGES_GB} GB Download.")
        else:
            add("images", "Docker-Images", "ok", "alle vorhanden (" + ", ".join(present) + ")")
        need_gb = IMAGES_GB + 10 if missing else 5
        root_dir = info.get("DockerRootDir") or ""
        target = Path(root_dir) if root_dir and Path(root_dir).exists() else None
        if target is not None:
            try:
                free_gb = shutil.disk_usage(target).free / 1e9
                add("disk", "Speicherplatz (Docker)", "ok" if free_gb >= need_gb else "fail",
                    f"{free_gb:.0f} GB frei unter {target}",
                    "" if free_gb >= need_gb else f"Mindestens {need_gb} GB frei machen.")
            except OSError:
                pass
        else:
            add("disk", "Speicherplatz (Docker)", "info",
                ("liegt in der virtuellen Platte von Docker Desktop – unter Windows prüfen" if desktop
                 else "nicht prüfbar") + (f"; nötig: ~{need_gb} GB" if missing else ""))

    cache = os.path.expanduser(env.get("HF_CACHE", "~/.cache/huggingface"))
    model = env.get("MODEL", "Qwen/Qwen3-0.6B")
    mdir = Path(cache) / "hub" / ("models--" + model.replace("/", "--")) / "snapshots"
    model_ok = mdir.is_dir() and any(mdir.iterdir())
    if model_ok:
        add("model", "Modell im Cache", "ok", f"{model} in {cache}")
    else:
        online = True
        try:
            socket.create_connection(("huggingface.co", 443), timeout=3).close()
        except OSError:
            online = False
        add("model", "Modell im Cache", "warn", f"{model} fehlt in {cache}"
            + ("" if online else " – huggingface.co nicht erreichbar"),
            "„Images & Modell laden“ (./scenario.sh pull)" if online
            else "Ohne Internet: HF-Cache von einem anderen Rechner kopieren (siehe docs/SERVER.md).")

    # ------------------------------------------------------------------ Einrichtung
    env_file = DEPLOY / ".env"
    secrets_ok = (DEPLOY / "secrets" / "cache_salt_secret").exists() and (DEPLOY / "secrets" / "watermark_key").exists()
    env = read_env() if env_file.exists() else {}
    init_ok = env_file.exists() and bool(env.get("LITELLM_MASTER_KEY")) and secrets_ok
    pool_ok = bool(env.get("TENANT_POOL_SECRET"))
    if init_ok and not pool_ok:
        add("init", "Einrichtung (Schlüssel)", "warn", "es fehlen die Pool-Mandanten (neu seit dem Test „Kosten der Cache-Trennung“)",
            "„Einrichten“ erneut ausführen (./scenario.sh init) – bestehende Schlüssel bleiben, danach Szenario neu starten.")
    else:
        add("init", "Einrichtung (Schlüssel)", "ok" if init_ok else "fail",
            "deploy/.env und deploy/secrets/ vorhanden" if init_ok else "noch nicht eingerichtet",
            "" if init_ok else "„Einrichten“ (./scenario.sh init)")
    prof_p = ROOT / "data" / "profiles" / "lokal-litellm.json"
    prof_ok = prof_p.exists()
    try:
        n_pool = len(json.loads(prof_p.read_text()).get("tenant_pool") or []) if prof_ok else 0
    except (OSError, ValueError):
        n_pool = 0
    add("profile", "Endpunkt-Profile", "ok" if prof_ok else "warn",
        (f"lokal-litellm (3 + {n_pool} Mandanten), lokal-vllm-direkt" if prof_ok else "fehlen"),
        "" if prof_ok else "„Einrichten“ legt sie an.")

    # ------------------------------------------------------------------ Stromversorgung (Laptop)
    pw = power_state()
    if pw is not None:
        if pw["on_battery"]:
            add("power", "Stromversorgung", "warn", f"läuft auf Akku ({pw['capacity']} %) – CPU und GPU takten herunter",
                "Netzteil anschließen, sonst sind die Messwerte langsamer und nicht vergleichbar.")
        else:
            add("power", "Stromversorgung", "ok", "am Netzteil")

    # ------------------------------------------------------------------ Ports
    if docker_ok:
        running = run(["docker", "compose", "-p", PROJECT, "ps", "-q"])[1]
        busy = [p for p in (4000, 8000, 8090) if port_open(p)]
        if busy and not running:
            add("ports", "Ports 4000/8000/8090", "warn", "belegt von einem anderen Programm: " + ", ".join(map(str, busy)),
                "Programm beenden (oder anderes Compose-Projekt: docker ps).")
        else:
            add("ports", "Ports 4000/8000/8090", "ok", "vom laufenden Szenario belegt" if running else "frei")

    add("python", "Python (Testplattform)", "info", sys.version.split()[0] + f" ({sys.executable})")

    vram_txt = f"{vram / 1024:.1f} GB" if vram else "–"
    fails = [i for i in items if i["status"] == "fail"]
    return {
        "ok": not fails, "items": items, "gpus": gpus, "vram_mb": vram, "k5_possible": vram >= K5_MIN_MB,
        "images_missing": [i for i, ok in present.items() if not ok], "model_cached": model_ok,
        "init_done": init_ok, "profiles_done": prof_ok, "docker_desktop": desktop, "project": PROJECT,
        "summary": ("bereit" if not fails else f"{len(fails)} Problem(e)") + f" · VRAM {vram_txt}",
        "checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def main() -> int:
    res = check(gpu_container="--gpu-container" in sys.argv)
    if "--json" in sys.argv:
        print(json.dumps(res, ensure_ascii=False))
        return 0
    sym = {"ok": "✔", "warn": "!", "fail": "✘", "info": "·"}
    for it in res["items"]:
        print(f" {sym[it['status']]} {it['label']:<30} {it['detail']}")
        if it["fix"] and it["status"] in ("warn", "fail"):
            print(f"   → {it['fix']}")
    print()
    print("Ergebnis:", res["summary"])
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
