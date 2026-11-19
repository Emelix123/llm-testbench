# WSL-Einrichtung für vLLM, LiteLLM und die Testplattform

Stand: geprüft auf Windows 11 Home, WSL 2.4.13, Kernel 5.15.167.4, Docker Desktop (Engine 29.9.3),
NVIDIA RTX 3050 Laptop (4 GB), Treiber 621.24 / CUDA 13.5, 16 GB RAM, 8 logische Kerne.

## 1. Was bereits passt

| Punkt | Zustand | Prüfen mit |
|---|---|---|
| GPU in WSL | ✔ `nvidia-smi` zeigt die RTX 3050 | `nvidia-smi` |
| NVIDIA-Runtime in Docker | ✔ `nvidia` ist registriert | `docker info \| grep -i runtimes` |
| systemd in WSL | ✔ `/etc/wsl.conf` → `[boot] systemd=true` | `cat /etc/wsl.conf` |
| Modell im Linux-Dateisystem | ✔ `~/.cache/huggingface` (ext4) | `ls ~/.cache/huggingface/hub` |
| Node.js (Frontend bauen) | ✔ v22 über nvm | `node --version` |

Wichtig: **Keinen** Linux-NVIDIA-Treiber in WSL installieren. Der Windows-Treiber stellt die GPU über
`/usr/lib/wsl/lib` bereit; ein Linux-Treiber in der Distribution überschreibt das und bricht CUDA.

## 2. Was eingestellt werden muss

### 2.1 Mehr Arbeitsspeicher für WSL (`.wslconfig`) – nötig

WSL bekommt standardmäßig die Hälfte des RAM (hier ~7,7 GB). vLLM meldet beim Start nur noch **3,4 GB
verfügbaren RAM**; mit LiteLLM, Postgres, Detektor und Testplattform wird es eng (Abstürze durch OOM, Swap,
verrauschte Zeitmessungen).

Datei `%UserProfile%\.wslconfig` anlegen (also `C:\Users\<Windows-Name>\.wslconfig`):

```ini
[wsl2]
memory=12GB          # 16 GB Host: 4 GB für Windows lassen
processors=8
swap=8GB
# localhostForwarding ist Standard; Ports aus Docker sind unter 127.0.0.1 erreichbar
[experimental]
autoMemoryReclaim=gradual   # gibt ungenutzten Speicher an Windows zurück
```

Danach in PowerShell: `wsl --shutdown`, Docker Desktop neu starten. Kontrolle in WSL: `free -g` → ~11 GB.

### 2.2 Docker Desktop

- *Settings → Resources → WSL integration*: für die Distribution aktiviert (ist es).
- *Settings → Resources*: Speicher richtet sich nach `.wslconfig`.
- Docker Desktop veröffentlicht Ports auf **allen** Windows-Schnittstellen, wenn man sie nicht bindet. Alle
  Compose-Dateien binden deshalb an `127.0.0.1` (sonst wäre vLLM im WLAN erreichbar).
- `--network host` meint unter Docker Desktop die Docker-VM, nicht die WSL-Distribution. Die Testplattform
  daher direkt in WSL starten (siehe 2.3), nicht im Container.

### 2.3 Python ≥ 3.11 für die Testplattform – nötig

In der Distribution ist Python **3.10** installiert; die Testplattform braucht ≥ 3.11 (`asyncio.timeout`).
`./start.sh` erledigt das selbst: Fehlt ein passendes Python, legt es die `.venv` mit
[uv](https://docs.astral.sh/uv/) und Python 3.12 an (ohne root, System-Python bleibt unverändert) und
fragt vorher, ob es uv installieren darf. Von Hand ginge es so:

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
```

```bash
cd ~/work/vLLM/llm-testbench && ~/.local/bin/uv venv --python 3.12 .venv && ./start.sh
```

Alternative ohne uv: `sudo add-apt-repository ppa:deadsnakes/ppa` und
`sudo apt install python3.12 python3.12-venv`, dann `PYTHON=python3.12 ./start.sh`.

### 2.4 GPU-Speicher: feste KV-Cache-Größe

Unter WSL belegt Windows einen Teil der GPU (hier ~0,8 GB). vLLM 0.34 prüft beim Start, ob
`--gpu-memory-utilization` × 4 GB frei ist, und bricht sonst ab. Deshalb in `deploy/.env`:

```ini
GPU_MEMORY_UTILIZATION=0.78     # < freier Anteil (3,17 von 4 GB)
KV_CACHE_MEMORY=750000000       # feste KV-Cache-Größe in allen Szenarien (~6 500 Tokens)
```

Vor Messungen GPU-hungrige Windows-Programme schließen (Browser mit Hardwarebeschleunigung, Spiele,
Videokonferenz). `nvidia-smi` sollte vor dem Start < 400 MiB belegt zeigen.

### 2.5 Für saubere Zeitmessungen (Seitenkanal-Tests)

Der Prefix-Cache-Test misst Unterschiede von wenigen Millisekunden. Auf dem Laptop:

- Netzteil anschließen, Windows-Energiemodus **„Beste Leistung“** (Einstellungen → System → Strom).
- NVIDIA-Systemsteuerung → 3D-Einstellungen → Energieverwaltungsmodus **„Maximale Leistung bevorzugen“**.
- Keine anderen Lasten (Windows-Update, Indexer, Virenscan) während der Messung.
- Messungen mit und ohne Last anderer Mandanten durchführen (Parameter `background_load`) – die Arbeit fordert
  das (Abschn. 1.3.4, Abu Bakar 2026).
- Hinweis in vLLM-Logs `Using 'pin_memory=False' as WSL is detected` ist erwartet: WSL unterstützt kein
  gepinntes Host-Memory; das kostet etwas Durchsatz, betrifft aber alle Szenarien gleich.

### 2.6 Optional: Docker Engine direkt in WSL statt Docker Desktop

Für Bare-Metal-nahe Messungen kann man Docker Desktop durch die Docker Engine in der Distribution ersetzen
(`docker-ce` + `nvidia-container-toolkit`, danach `sudo nvidia-ctk runtime configure --runtime=docker`).
Nicht nötig für die Tests; Docker Desktop funktioniert mit der vorhandenen `nvidia`-Runtime.

## 3. Was unter WSL/GeForce grundsätzlich nicht geht

| Funktion | Warum relevant (Arbeit) | Unter WSL / RTX 3050 |
|---|---|---|
| MIG (Multi-Instance GPU) | Hardware-Trennung von Mandanten | nur Rechenzentrums-GPUs (A100/H100 …), nicht unter WSL |
| MPS mit Mandanten-Isolation | geteilte GPU über Prozesse | unter WSL nicht unterstützt |
| ECC | Schutz gegen Rowhammer (GPUHammer) | GeForce ohne ECC |
| IOMMU-Einstellungen, GPU-Treiber-Härtung | Annahmen des Bedrohungsmodells | Sache des Windows-Hosts |
| Confidential Computing | Ausblick der Arbeit | nicht verfügbar |

Die GPU-/Host-Ebene (Angreifertyp A2) lässt sich auf dem Laptop daher nicht messen – das deckt sich mit dem
Bedrohungsmodell der Arbeit (Fokus A1). Für die Zielhardware von Leitwerk gelten die Szenarien unverändert;
dort zusätzlich K5 (≥ 8 GB VRAM) und ggf. MIG.

## 4. Kurzablauf

```bash
cd ~/work/vLLM/llm-testbench/deploy && ./scenario.sh init && ./scenario.sh profile
```

```bash
./scenario.sh up K0
```

```bash
cd .. && ./start.sh
```

Im Browser `http://localhost:8080` → Tests → Suite „Konfigurationsvergleich (je Szenario)“ mit Profil
„Lokal (LiteLLM, Mandant A)“. Danach `./scenario.sh up K1` usw.; Ergebnis unter **Konfigurationen**.
