# Testplattform auf einem GPU-Server

Ziel: Repo auf einem anderen Rechner klonen und die Messungen der Arbeit (Szenarien K0–K6) mit
möglichst wenig Handarbeit fahren. Bedient wird alles über die Weboberfläche (Seite **Steuerung**),
auf Wunsch auch komplett im Terminal.

Getestet unter WSL2 (Ubuntu 22.04, Docker Desktop, RTX 3050 Ti 4 GB). Ein Linux-Server mit Docker
Engine funktioniert genauso; die Systemprüfung zeigt vor dem großen Download, ob etwas fehlt.

## Voraussetzungen

| | Mindestens | Prüfen |
|---|---|---|
| GPU | NVIDIA; Qwen3-8B: ≥ 22 GB VRAM (K5: ≥ 44 GB), auf kleineren GPUs Qwen3-0.6B: ≥ 4 GB (K5: ≥ 8 GB) | `nvidia-smi` |
| Docker | Engine ≥ 25 mit Compose v2 ≥ 2.20, eigener Nutzer in der Gruppe `docker` | `docker compose version` |
| GPU in Containern | [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) | `./deploy/scenario.sh check --gpu-container` |
| Speicherplatz | ~50 GB für Docker (vLLM-Image allein 36 GB) + Modell im Hugging-Face-Cache (Qwen3-8B ~16 GB) | Systemprüfung |
| Internet | Docker Hub, ghcr.io, huggingface.co, PyPI (sonst: [ohne Internet](#server-ohne-internet)) | Systemprüfung |
| Werkzeuge | `git`, `curl`, `python3` (beliebige Version, für kleine Hilfsskripte) | – |

Python ≥ 3.11 für die Testplattform selbst ist **keine** Voraussetzung: Fehlt es, richtet
`./start.sh` es über [uv](https://docs.astral.sh/uv/) ein (ohne root, nur für dieses Projekt).

Toolkit nach der Installation einmal mit Docker verbinden:

```bash
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

## Ablauf mit Weboberfläche

Auf dem **Server**:

```bash
git clone https://github.com/Emelix123/llm-testbench.git
cd llm-testbench
./start.sh --hintergrund
```

Beim ersten Start fragt `start.sh`, ob es uv installieren darf, falls Python ≥ 3.11 fehlt. Danach
läuft die Testplattform im Hintergrund weiter, auch wenn die SSH-Sitzung endet
(Log: `data/testbench.log`, beenden: `./start.sh --stopp`).

Auf dem **eigenen Rechner** einen SSH-Tunnel öffnen und im Browser <http://localhost:8080> aufrufen:

```bash
ssh -L 8080:localhost:8080 nutzer@server
```

Dann in der Oberfläche (Menü → **Anleitung** erklärt jeden Schritt):

1. **Steuerung → Systemprüfung**: alles ✘ zuerst beheben (unter jedem Punkt steht, wie).
2. **Einrichten** (Schlüssel, GPU-Werte, Profile mit 3 + 30 Mandanten-Keys) und **Images & Modell laden** (~40 GB + ~16 GB Qwen3-8B).
3. Nach dem Laden **Gründlich (GPU im Container)** prüfen.
4. **Gesamtlauf**: Szenarien sind vorausgewählt (K5 nur, wenn zwei Modellkopien passen: Qwen3-8B ab 44 GB), Wiederholungen **3** für die
   Arbeit, starten. Der Browser darf zu – der Lauf geht weiter; später einfach den Tunnel neu öffnen.
5. Ergebnisse: Seite **Konfigurationen** oder Download auf der Seite Steuerung. Auf den eigenen
   Rechner holen:

```bash
scp -r nutzer@server:llm-testbench/data/exports ./llmtb-exports
```

## Ablauf nur im Terminal

Für Server ohne Browser-Zugang, am besten in `tmux` (überlebt das Trennen der SSH-Verbindung):

```bash
tmux new -s llmtb
cd llm-testbench
./deploy/scenario.sh check          # Systemprüfung
./deploy/scenario.sh init           # Schlüssel, GPU-Werte, Profile
./deploy/scenario.sh pull           # Images + Modell
./deploy/scenario.sh check --gpu-container
./deploy/scenario.sh run-all -n 3   # Gesamtlauf; startet die Testplattform bei Bedarf selbst
```

`Strg+B`, dann `D` trennt tmux; `tmux attach -t llmtb` holt es zurück. Weitere Optionen:
`run-all -s K0,K1,K4` (Auswahl), `--suite ID`, `--status` (Fortschritt), `--abbrechen`.
Die Ergebnisse landen wie oben in `data/exports/<Datum>/`.

## Was für die Dokumentation festgehalten wird

Ohne Zutun, für die Beschreibung des Versuchsaufbaus in der Arbeit:

- **Jeder Lauf** (`data/runs/…/config.json`, Feld `environment`): GPU mit Treiber, CUDA, Leistungsgrenze und
  Zustand beim Start, Prozesse auf der GPU, Betriebssystem, CPU, Image-Digests, Git-Commit (mit lokalen
  Änderungen) und ein Code-Hash über Tests, Backend und Deploy-Dateien.
- **Jeder Gesamtlauf** (`data/exports/<Datum>/`): `umgebung.md` (Tabelle zum Übernehmen), `nvidia-smi.txt`,
  `szenario-konfiguration.md` (vllm-serve-Kommando, KV-Cache, Blockgröße je Szenario) und `container-logs.zip`.
- **Jedes Stoppen eines Szenarios** sichert die Container-Logs nach `data/container-logs/` (Geheimnisse geschwärzt).

Empfehlung: vor dem Gesamtlauf alles committen (`git status` sauber) und den Code bis zum Ende nicht ändern –
die Zusammenfassung warnt sonst.

## Nach einem Update (`git pull`)

```bash
git pull
./deploy/scenario.sh run-all -n 3
```

`run-all` startet eine Testplattform, die noch mit dem alten Code läuft, selbst neu (läuft sie im Vordergrund
in einem Terminal, dort mit Strg+C beenden). Haben sich Modellprofile geändert, bricht `run-all` mit der
Liste der Abweichungen ab – dann einmal `./deploy/scenario.sh modell standard` (bei gestopptem Szenario) und
erneut starten.

## Dauer

| Schritt | Dauer |
|---|---|
| Download Images + Modell | einmalig, je nach Leitung 10–30 min |
| Szenario starten | ~3 min (beim allerersten Start etwas länger: Kernel-Kompilierung) |
| Suite „Konfigurationsvergleich“ | ~15–17 min je Szenario und Wiederholung |
| Gesamtlauf, 6 Szenarien × 1 | ~2 h |
| Gesamtlauf, 6 Szenarien × 3 | ~5 h (mit K5: 7 Szenarien, ~6 h) |

## GPU-Einstellungen

`init` liest den VRAM und setzt in `deploy/.env`:

- das **Modellprofil**: `standard` (Qwen3-8B), sobald alle Szenarien einschließlich K5 hineinpassen
  (2 × 22 GB + Puffer ≈ 44 GB, z. B. RTX PRO 6000 mit 96 GB), sonst `klein` (Qwen3-0.6B). Wechseln mit
  `./deploy/scenario.sh modell standard|klein` (bei gestopptem Szenario), danach `pull-model`.
- `KV_CACHE_MEMORY` bleibt **fest** bei ≈ 6 540 Tokens (Qwen3-8B: 965 MB, Qwen3-0.6B: 750 MB). Damit
  verhalten sich alle Szenarien gleich, und die Sicherheitsbefunde (z. B. Verdrängung bei vollem
  KV-Cache, Kosten der Cache-Trennung mit 1–30 Mandanten) bleiben untereinander vergleichbar.
- `GPU_MEMORY_UTILIZATION` ist nur die Obergrenze, die vLLM beim Start gegen den **freien** Speicher
  prüft: `MODEL_GPU_MB` je Instanz (Qwen3-8B ~24 GB → 0,25 auf 96 GB; Qwen3-0.6B ~5 GB); auf kleinen
  GPUs höchstens 0,78. Auf geteilten GPUs bleibt so Platz für andere.
- K5 (zwei Instanzen auf einer GPU) wird nur angeboten, wenn zwei Modellkopien passen.

Neu berechnen: `./deploy/scenario.sh gpu` (oder Steuerung → Wartung) – rechnet mit dem eingestellten
Modellprofil. Wer die ganze GPU für den
KV-Cache nutzen will, setzt `KV_CACHE_MEMORY=` leer und `GPU_MEMORY_UTILIZATION` z. B. auf 0.85 – die
Ergebnisse sind dann aber nicht mehr mit anderen Rechnern vergleichbar.

### Mehrere GPUs

vLLM rechnet immer auf **genau einer** GPU: `LLMTB_GPU` in `deploy/.env` (Index aus `nvidia-smi -L` oder
UUID). `init` wählt bei mehreren GPUs die mit dem meisten Speicher, bei Gleichstand die ohne Monitor, und
trägt ihre UUID ein (bleibt auch gleich, wenn sich die Reihenfolge der Karten ändert). In K5 laufen beide
Instanzen auf dieser GPU.

```bash
nvidia-smi -L                     # Index und UUID aller GPUs
./deploy/scenario.sh gpu          # Liste mit Markierung (→), Werte neu berechnen
./deploy/scenario.sh gpu 1        # vLLM auf GPU 1 (nur bei gestopptem Szenario)
```

Für saubere Zeitmessungen: Monitor an die **andere** GPU anschließen (die Systemprüfung warnt sonst) und
auf der Mess-GPU nichts anderes laufen lassen. Welche GPU genutzt wurde, steht in jeder `config.json`
(`environment.vllm_gpu`) und in `umgebung.md` des Gesamtlaufs.

## Gemeinsam genutzter Server

Messungen reagieren empfindlich auf fremde GPU-Last. Vor dem Gesamtlauf mit `nvidia-smi` prüfen, dass
die Mess-GPU (`LLMTB_GPU`) frei ist (die Systemprüfung zeigt den belegten Speicher). Ports 4000, 8000, 8090 und 8080
müssen frei sein; einen anderen Port für die Oberfläche wählt `PORT=8081 ./start.sh`. Laufen mehrere
Kopien des Repos auf einem Rechner, trennt `LLMTB_PROJECT=name` die Docker-Projekte (Volumes), die
Ports bleiben aber gleich – also nie gleichzeitig messen.

## Server ohne Internet

Auf einem Rechner **mit** Internet:

```bash
docker pull vllm/vllm-openai:v0.34.0 ghcr.io/berriai/litellm-database:v1.106.7 postgres:16-alpine
docker save vllm/vllm-openai:v0.34.0 ghcr.io/berriai/litellm-database:v1.106.7 postgres:16-alpine | gzip > llmtb-images.tar.gz
tar -czf llmtb-modell.tar.gz -C ~/.cache/huggingface hub/models--Qwen--Qwen3-8B   # bzw. Qwen3-0.6B
```

Beide Dateien auf den Server kopieren, dort:

```bash
gunzip -c llmtb-images.tar.gz | docker load
mkdir -p ~/.cache/huggingface && tar -xzf llmtb-modell.tar.gz -C ~/.cache/huggingface
```

Die Testplattform selbst braucht Python ≥ 3.11 mit den Paketen aus `backend/requirements.txt`
(PyPI-Spiegel oder vorinstalliert); `start.sh` nimmt eine vorhandene `.venv`.

## Sicherheit

Die Oberfläche kann Docker-Container starten. Sie lauscht deshalb nur auf `127.0.0.1`; der Zugriff
von außen geht über den SSH-Tunnel. Die Steuerbefehle lehnen außerdem Anfragen von anderen
Adressen, fremden Host-Namen und ohne JSON ab (Schutz gegen fremde Webseiten im eigenen Browser).
`LLMTB_HOST=0.0.0.0` öffnet die Oberfläche im Netz – nur hinter eigener Absicherung und ohne
Steuerung (die bleibt gesperrt, außer mit `LLMTB_OPS_REMOTE=1`).

## Fehlerbehebung

| Meldung | Lösung |
|---|---|
| `permission denied … /var/run/docker.sock` | `sudo usermod -aG docker $USER`, neu anmelden |
| `could not select device driver "nvidia"` / GPU im Container ✘ | NVIDIA Container Toolkit installieren, `nvidia-ctk runtime configure`, Docker neu starten |
| `Free memory on device … less than desired GPU memory utilization` | GPU teilweise belegt: andere Prozesse beenden oder `./deploy/scenario.sh gpu` |
| LiteLLM startet nicht, `password authentication failed` | `deploy/.env` wurde neu erzeugt, die Gateway-DB ist älter: `./deploy/scenario.sh reset` |
| Steuerung meldet „nur lokal oder per SSH-Tunnel“ | Oberfläche über `http://localhost:8080` (Tunnel) öffnen, nicht über die Server-IP |
| Gesamtlauf teilweise fehlgeschlagen | erneut mit den fehlenden Szenarien starten (`-s K3,K4`); je Szenario zählt der neueste Lauf |
