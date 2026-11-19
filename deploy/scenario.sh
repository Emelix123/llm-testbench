#!/usr/bin/env bash
# Steuert die Test-Szenarien K0–K6 (vLLM + LiteLLM) für die Testplattform.
#
# Alles hier geht auch über die Weboberfläche (./start.sh → Seite „Steuerung“).
#
#   ./scenario.sh check           Systemprüfung: Docker, GPU, Images, Modell, Einrichtung (--gpu-container: GPU-Test)
#   ./scenario.sh init            .env, Geheimnisse (deploy/secrets/), GPU-Werte und Endpunkt-Profile – einmalig
#   ./scenario.sh pull            Docker-Images (~40 GB) und Modell vorab laden
#   ./scenario.sh run-all [-s K0,K1,…] [-n 3] [--suite ID]
#                                 Gesamtlauf: jedes Szenario starten, Suite messen, nächstes; am Ende Export
#                                 nach data/exports/ (startet die Testplattform bei Bedarf im Hintergrund)
#   ./scenario.sh run-all --status | --abbrechen
#   ./scenario.sh gpu             GPU-Werte in deploy/.env neu aus dem VRAM berechnen
#   ./scenario.sh list            Szenarien anzeigen
#   ./scenario.sh up K1           Szenario starten (stoppt ein laufendes vorher), Mandanten anlegen
#   ./scenario.sh down            alles stoppen (Daten bleiben)
#   ./scenario.sh status          laufendes Szenario + Container
#   ./scenario.sh logs [dienst]   Logs (Standard: vllm)
#   ./scenario.sh profile         Endpunkt-Profile + Mandanten-Keys in der Testplattform anlegen
#   ./scenario.sh pull-model      nur das Modell in den Hugging-Face-Cache laden
#   ./scenario.sh detector up|down  Wasserzeichen-Detektor allein (für Messungen in Szenarien ohne ihn)
#   ./scenario.sh reset           alles stoppen UND Volumes (Postgres, Compile-Cache) löschen
#
# Es läuft immer genau ein Szenario (Compose-Projekt "llmtb", änderbar mit LLMTB_PROJECT), alle auf
# denselben Ports: LiteLLM 127.0.0.1:4000 · vLLM direkt 127.0.0.1:8000 (nur K0–K3, K6) · Detektor 127.0.0.1:8090
set -euo pipefail
cd "$(dirname "$0")"
DEPLOY="$PWD"
ROOT="$(cd .. && pwd)"
ACTIVE="$DEPLOY/.active-scenario.json"
PROJECT="${LLMTB_PROJECT:-llmtb}"
BENCH="http://127.0.0.1:${PORT:-8080}"

die() { echo "Fehler: $*" >&2; exit 1; }
rand_b64() { python3 -c "import secrets,sys; print(secrets.token_urlsafe(int(sys.argv[1])))" "$1"; }

scenario_file() {
  local id="${1,,}"
  [[ "$id" =~ ^k[0-9]$ ]] || die "Szenario wie K0 … K6 angeben"
  local f; f=$(ls compose/"$id"-*.yaml 2>/dev/null | head -1)
  [[ -n "$f" ]] || die "kein Szenario $1 in deploy/compose/"
  echo "$f"
}
xfield() { awk -v k="$2" '/^x-scenario:/{s=1;next} s&&/^[^ ]/{s=0} s&&$1==k":"{sub(/^[^:]*: */,"");print;exit}' "$1"; }
compose() { docker compose -p "$PROJECT" --env-file .env -f "$@"; }
need_env() { [[ -f .env && -f secrets/cache_salt_secret && -f secrets/watermark_key ]] || die "zuerst ./scenario.sh init"; }
envval() { local f=.env; [[ -f $f ]] || f=.env.example; grep -E "^$1=" "$f" | head -1 | cut -d= -f2-; }
setenv() { if grep -qE "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
gpu_vram_mb() { nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | tr -d ' ' | sort -n | tail -1; }

cmd_init() {
  mkdir -p secrets && chmod 700 secrets
  if [[ ! -f .env ]]; then
    cp .env.example .env
    local k v
    for k in LITELLM_MASTER_KEY LITELLM_SALT_KEY POSTGRES_PASSWORD VLLM_API_KEY TENANT_A_KEY TENANT_B_KEY TENANT_C_KEY; do
      case $k in
        LITELLM_MASTER_KEY) v="sk-admin-$(rand_b64 24)";;
        TENANT_A_KEY) v="sk-mandant-a-$(rand_b64 24)";;
        TENANT_B_KEY) v="sk-mandant-b-$(rand_b64 24)";;
        TENANT_C_KEY) v="sk-mandant-c-$(rand_b64 24)";;
        *) v="$(rand_b64 32)";;
      esac
      sed -i "s|^$k=.*|$k=$v|" .env
    done
    sed -i "s|^LLMTB_UID=.*|LLMTB_UID=$(id -u)|; s|^LLMTB_GID=.*|LLMTB_GID=$(id -g)|" .env
    chmod 600 .env
    echo "deploy/.env angelegt (chmod 600)."
    cmd_gpu
    if docker volume inspect "${PROJECT}_pgdata" >/dev/null 2>&1; then
      echo "Achtung: Die Gateway-Datenbank (Volume ${PROJECT}_pgdata) stammt von einer früheren Einrichtung und"
      echo "         kennt das neue Postgres-Passwort nicht. Einmal ./scenario.sh reset ausführen (löscht nur"
      echo "         Gateway-DB und Compile-Cache; Messergebnisse in data/ bleiben)."
    fi
  else
    echo "deploy/.env existiert – unverändert."
  fi
  # Geheimnis für die Pool-Mandanten (Test „Kosten der Cache-Trennung“); ältere .env bekommen es nachgetragen
  if ! grep -qE '^TENANT_POOL_SECRET=.+' .env; then
    grep -qE '^TENANT_POOL_SIZE=' .env || setenv TENANT_POOL_SIZE 30
    setenv TENANT_POOL_SECRET "$(rand_b64 32)"
    echo "TENANT_POOL_SECRET in deploy/.env ergänzt ($(envval TENANT_POOL_SIZE) Pool-Mandanten, ab dem nächsten Szenario-Start)."
  fi
  # 256 Bit HMAC-Geheimnis für cache_salt (Doku: zufällig, ~256 Bit, keine vorhersagbare ID)
  [[ -f secrets/cache_salt_secret ]] || { rand_b64 32 > secrets/cache_salt_secret; echo "secrets/cache_salt_secret erzeugt."; }
  # 63-Bit-Ganzzahl als Wasserzeichen-Schlüssel (Philox: <= 64 Bit)
  [[ -f secrets/watermark_key ]] || { python3 -c "import secrets; print(secrets.randbits(63))" > secrets/watermark_key; echo "secrets/watermark_key erzeugt."; }
  chmod 600 secrets/*
  # Container lesen die Secrets als root – die Dateien bleiben trotzdem nur für dich lesbar.
  cmd_profile
  echo
  echo "Einrichtung fertig. Weiter: ./scenario.sh check, dann ./scenario.sh pull (Images ~40 GB + Modell)."
}

# GPU-Werte aus dem VRAM: Der KV-Cache bleibt fest (KV_CACHE_MEMORY) – so sind Messungen auf Laptop
# und Server vergleichbar. Angepasst wird nur die Obergrenze, die vLLM beim Start gegen den freien
# Speicher prüft: ~5 GB je Instanz genügen für Qwen3-0.6B; auf kleinen GPUs höchstens 0,78 (WSL).
cmd_gpu() {
  [[ -f .env ]] || die "zuerst ./scenario.sh init"
  local mb; mb=$(gpu_vram_mb || true)
  if [[ -z "$mb" ]]; then
    echo "Keine NVIDIA-GPU gefunden (nvidia-smi) – GPU-Werte in deploy/.env bleiben unverändert."; return 0
  fi
  local u ud
  u=$(awk -v m="$mb" 'BEGIN{v=5000/m; if(v>0.78)v=0.78; if(v<0.2)v=0.2; printf "%.2f", v}')
  ud=$(awk -v m="$mb" 'BEGIN{v=3800/m; if(v>0.45)v=0.45; if(v<0.15)v=0.15; printf "%.2f", v}')
  setenv GPU_MEMORY_UTILIZATION "$u"
  setenv GPU_MEMORY_UTILIZATION_DEDICATED "$ud"
  setenv LLMTB_GPU_VRAM_MB "$mb"
  echo "GPU: $((mb / 1024)) GB VRAM → GPU_MEMORY_UTILIZATION=$u, je K5-Instanz $ud (KV-Cache fest: $(envval KV_CACHE_MEMORY) Bytes)."
  (( mb >= 8000 )) || echo "Hinweis: K5 (zwei Instanzen) braucht >= 8 GB VRAM und wird im Gesamtlauf übersprungen."
}

cmd_check() { python3 "$DEPLOY/ops_check.py" "$@"; }

cmd_pull() {
  local img
  for img in "$(envval VLLM_IMAGE)" "$(envval LITELLM_IMAGE)" "$(envval POSTGRES_IMAGE)"; do
    echo "== docker pull $img"
    docker pull "$img"
  done
  cmd_pull_model
  echo "== Images und Modell sind da."
}

cmd_list() {
  printf "%-4s %-32s %s\n" ID NAME DATEI
  for f in compose/k*.yaml; do printf "%-4s %-32s %s\n" "$(xfield "$f" id)" "$(xfield "$f" name)" "$f"; done
}

cmd_pull_model() {
  local model img cache
  model=$(envval MODEL); img=$(envval VLLM_IMAGE); cache=$(envval HF_CACHE); cache="${cache/#\~/$HOME}"
  mkdir -p "$cache"
  echo "Lade $model nach $cache …"
  # als eigener Nutzer, damit die Dateien im HF-Cache nicht root gehören
  docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e HF_HOME=/hf -v "$cache:/hf" -e HF_TOKEN="$(envval HF_TOKEN)" \
    --entrypoint python3 "$img" \
    -c "from huggingface_hub import snapshot_download as d; print(d('$model'))"
}

cmd_down() { docker compose -p "$PROJECT" down --remove-orphans 2>/dev/null || true; rm -f "$ACTIVE"; }

cmd_up() {
  need_env
  local id="${1:-}"; [[ -n "$id" ]] || die "Szenario angeben, z. B. ./scenario.sh up K1"
  local f; f=$(scenario_file "$id")
  local sid sname; sid=$(xfield "$f" id); sname=$(xfield "$f" name)
  echo "== Starte $sid – $sname ($f)"
  cmd_down
  if grep -q 'HF_HUB_OFFLINE: "1"' "$f" && grep -q 'internal: true' "$f"; then
    local cache; cache=$(envval HF_CACHE); cache="${cache/#\~/$HOME}"
    local mdir="$cache/hub/models--$(envval MODEL | sed 's|/|--|g')"
    [[ -d "$mdir/snapshots" ]] || cmd_pull_model
  fi
  local svcs; svcs=$(compose "$f" config --services | grep -v '^tenant-setup$' | tr '\n' ' ')
  # shellcheck disable=SC2086
  if ! compose "$f" up -d --wait --wait-timeout 1500 $svcs; then
    echo
    echo "== Start von $sid fehlgeschlagen. Letzte Meldungen:"
    compose "$f" logs --tail 25 2>/dev/null | grep -iE 'error|fehler|failed|password|memory|cuda' | tail -15 || true
    if compose "$f" logs litellm 2>/dev/null | grep -q 'password authentication failed'; then
      echo "→ Gateway-DB passt nicht zum Passwort in deploy/.env: ./scenario.sh reset, dann erneut starten."
    fi
    exit 1
  fi
  compose "$f" run --rm tenant-setup
  python3 - "$ACTIVE" "$sid" "$sname" "$f" <<PY
import hashlib, json, os, sys, datetime, pathlib
out, sid, name, f = sys.argv[1:5]
env = dict(l.split("=", 1) for l in pathlib.Path(".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
h = hashlib.sha256()
for p in [f, "compose/common.yaml", "litellm/config.yaml", "litellm/config.dedicated.yaml", "litellm/tenant_isolation.py", "vllm/start-vllm.sh"]:
    h.update(pathlib.Path(p).read_bytes())
massn = []
for line in pathlib.Path(f).read_text().splitlines():
    if line.strip().startswith("massnahmen:"):
        raw = line.split(":", 1)[1].strip().strip("[]")
        massn = [x.strip() for x in raw.split(",") if x.strip()]
json.dump({"id": sid, "name": name, "file": f, "massnahmen": massn,
           "started_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
           "vllm_image": env.get("VLLM_IMAGE"), "litellm_image": env.get("LITELLM_IMAGE"),
           "model": env.get("MODEL"), "max_model_len": env.get("MAX_MODEL_LEN"),
           "config_sha256": h.hexdigest()[:16]}, open(out, "w"), indent=2, ensure_ascii=False)
PY
  echo
  echo "== $sid läuft."
  echo "   LiteLLM:   http://127.0.0.1:4000   (Modell: $(envval SERVED_MODEL_NAME))"
  grep -q '127.0.0.1:8000:8000' "$f" && echo "   vLLM direkt: http://127.0.0.1:8000 (absichtlich erreichbar – Härtungstest meldet das)"
  grep -q 'wm-detector:' "$f" && echo "   Wasserzeichen-Detektor: http://127.0.0.1:8090 (nur Betreiber)"
  local code; code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $(envval TENANT_A_KEY)" http://127.0.0.1:4000/v1/models || true)
  echo "   Probe /v1/models mit Mandant A: HTTP $code"
  [[ -f "$ROOT/data/profiles/lokal-litellm.json" ]] || echo "   Tipp: ./scenario.sh profile legt die Endpunkt-Profile in der Testplattform an."
}

cmd_status() {
  if [[ -f "$ACTIVE" ]]; then cat "$ACTIVE"; else echo "kein Szenario aktiv (laut $ACTIVE)"; fi
  docker compose -p "$PROJECT" ps 2>/dev/null || true
}

cmd_logs() { docker compose -p "$PROJECT" logs --tail 200 -f "${1:-vllm}"; }

cmd_detector() {
  need_env
  case "${1:-up}" in
    up) docker compose -p "${PROJECT}-wm" --env-file .env -f compose/wm-detector.yaml up -d --wait --wait-timeout 300
        echo "Detektor: http://127.0.0.1:8090 (nur Betreiber)";;
    down) docker compose -p "${PROJECT}-wm" down;;
    *) die "detector up|down";;
  esac
}

cmd_reset() { docker compose -p "$PROJECT" down --remove-orphans -v 2>/dev/null || true; rm -f "$ACTIVE"; }

cmd_profile() {
  need_env
  python3 - "$ROOT/data" <<PY
import base64, hashlib, hmac, json, os, pathlib, sys
data = pathlib.Path(sys.argv[1]); (data / "profiles").mkdir(parents=True, exist_ok=True)
env = dict(l.split("=", 1) for l in pathlib.Path(".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
sec_p = data / "secrets.env"
sec = {}
if sec_p.exists():
    for l in sec_p.read_text().splitlines():
        if "=" in l and not l.startswith("#"):
            k, v = l.split("=", 1); sec[k.strip()] = v.strip()
sec.update({"LOKAL_LITELLM_API_KEY": env["TENANT_A_KEY"], "LOKAL_LITELLM_ANGREIFER_KEY": env["TENANT_B_KEY"],
            "LOKAL_LITELLM_LAST_KEY": env["TENANT_C_KEY"]})
# Pool-Mandanten pool-01 … pool-NN: gleiche Ableitung wie deploy/litellm/bootstrap_tenants.py
for k in [k for k in sec if k.startswith("LOKAL_LITELLM_POOL_")]:
    del sec[k]
pool = []
pool_secret = env.get("TENANT_POOL_SECRET", "").strip()
for i in range(1, int(env.get("TENANT_POOL_SIZE", "0") or 0) + 1 if pool_secret else 1):
    mac = hmac.new(pool_secret.encode(), f"llmtb-pool-key\x00{i}".encode(), hashlib.sha256).digest()
    name = f"LOKAL_LITELLM_POOL_{i:02d}"
    sec[name] = f"sk-pool-{i:02d}-" + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()[:32]
    pool.append(name)
sec_p.write_text("# API-Keys. Diese Datei landet nie in runs/ und nie im Export.\n" + "".join(f"{k}={v}\n" for k, v in sorted(sec.items())))
os.chmod(sec_p, 0o600)
common = {"model": env.get("SERVED_MODEL_NAME", "llm"), "tokenizer": env.get("MODEL"),
          "max_context": int(env.get("MAX_MODEL_LEN", "2048")),
          "engine_url": "http://127.0.0.1:8000", "watermark_detector_url": "http://127.0.0.1:8090"}
profiles = {
  "lokal-litellm": {"name": "Lokal (LiteLLM, Mandant A)", "base_url": "http://127.0.0.1:4000",
                    "api_key_name": "LOKAL_LITELLM_API_KEY",
                    "tenant_keys": {"angreifer": "LOKAL_LITELLM_ANGREIFER_KEY", "last": "LOKAL_LITELLM_LAST_KEY"},
                    "tenant_pool": pool,
                    "notes": "Angelegt von deploy/scenario.sh. Szenario (K0–K6) wird je Lauf aus deploy/.active-scenario.json übernommen."},
  "lokal-vllm-direkt": {"name": "Lokal (vLLM direkt, ohne Gateway)", "base_url": "http://127.0.0.1:8000",
                        "api_key_name": "LOKAL_VLLM_DIREKT_API_KEY",
                        "notes": "Nur K0–K3/K6: misst den Overhead des Gateways. Ohne Mandanten-Trennung."},
}
for pid, p in profiles.items():
    path = data / "profiles" / f"{pid}.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    merged = {**old, **common, **p}
    if pid == "lokal-vllm-direkt":
        merged.pop("tenant_keys", None)
        merged.pop("tenant_pool", None)
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    print("Profil", path)
print("Keys in", sec_p, f"(3 Mandanten + {len(pool)} Pool-Mandanten)")
PY
}

# ------------------------------------------------------------------ Gesamtlauf (über die Testplattform)
ensure_bench() {
  curl -sf "$BENCH/api/status" >/dev/null 2>&1 && return 0
  echo "Testplattform läuft nicht – starte sie im Hintergrund (Log: data/testbench.log) …"
  "$ROOT/start.sh" --hintergrund
  local i
  for i in $(seq 1 90); do curl -sf "$BENCH/api/status" >/dev/null 2>&1 && return 0; sleep 2; done
  die "Testplattform startet nicht – siehe data/testbench.log"
}

cmd_run_all() {
  local scen="" reps=1 suite="konfigurationsvergleich" mode="start"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -s|--szenarien) scen="${2:-}"; shift 2;;
      -n|--wiederholungen) reps="${2:-1}"; shift 2;;
      --suite) suite="${2:-}"; shift 2;;
      --status) mode="status"; shift;;
      --abbrechen) mode="cancel"; shift;;
      *) die "unbekannte Option $1 (erlaubt: -s K0,K1 · -n 3 · --suite ID · --status · --abbrechen)";;
    esac
  done
  ensure_bench
  python3 - "$BENCH" "$mode" "$scen" "$reps" "$suite" <<'PY'
import json, sys, time, urllib.request, urllib.error
base, mode, scen, reps, suite = sys.argv[1:6]
def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {"detail": str(e)}
if mode == "cancel":
    code, r = call("POST", "/api/gesamtlauf/cancel", {})
    print("Abbruch angefordert." if r.get("cancelled") else "Es läuft kein Gesamtlauf."); sys.exit(0)
if mode == "start":
    body = {"scenarios": [x.strip().upper() for x in scen.split(",") if x.strip()] or None,
            "repetitions": int(reps), "suite": suite}
    code, r = call("POST", "/api/gesamtlauf", body)
    if code >= 400:
        print("Fehler:", r.get("detail")); sys.exit(1)
    print(f"Gesamtlauf {r['id']} gestartet: {', '.join(s['scenario'] for s in r['steps'])} · {r['repetitions']}× "
          f"„{r['suite_name']}“ · geschätzt {round(r['estimate_s'] / 60)} min")
    print("Fortschritt auch in der Weboberfläche (Seite „Steuerung“). Strg+C beendet nur diese Anzeige.\n")
last = {}
try:
    while True:
        code, g = call("GET", "/api/gesamtlauf")
        if not g:
            print("Kein Gesamtlauf vorhanden."); break
        for st in g["steps"]:
            key = (st["status"], st.get("text"))
            if last.get(st["scenario"]) != key:
                last[st["scenario"]] = key
                print(time.strftime("%H:%M"), f"{st['scenario']:<3} {st['status']:<11} {st.get('text') or ''}", flush=True)
        if g["state"] != "laeuft":
            print(f"\nGesamtlauf {g['state']}.")
            if g.get("export"):
                print("Ergebnisse:", g["export"]["dir"])
                for f in g["export"]["files"]: print("  ", f)
            break
        time.sleep(10)
except KeyboardInterrupt:
    print("\nAnzeige beendet – der Gesamtlauf läuft weiter. Stand: ./scenario.sh run-all --status")
PY
}

case "${1:-}" in
  init) cmd_init;;
  check) shift; cmd_check "$@";;
  pull) cmd_pull;;
  gpu) cmd_gpu;;
  run-all) shift; cmd_run_all "$@";;
  list) cmd_list;;
  up) shift; cmd_up "${1:-}";;
  down) cmd_down;;
  status) cmd_status;;
  logs) shift; cmd_logs "${1:-}";;
  profile) cmd_profile;;
  pull-model) cmd_pull_model;;
  reset) cmd_reset;;
  detector) shift; cmd_detector "${1:-up}";;
  *) sed -n '2,25p' "$0"; exit 1;;
esac
