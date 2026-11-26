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
#   ./scenario.sh gpu [index]     GPU-Werte neu berechnen; mit Index/UUID: GPU wechseln, auf der vLLM rechnet
#                                 (bei mehreren GPUs wählt init die mit dem meisten Speicher, ohne Monitor)
#   ./scenario.sh modell [name]   Modellprofil anzeigen oder wechseln: standard (Qwen3-8B, ab ~44 GB VRAM,
#                                 wählt init automatisch) oder klein (Qwen3-0.6B, Laptop)
#   ./scenario.sh list            Szenarien anzeigen
#   ./scenario.sh up K1           Szenario starten (stoppt ein laufendes vorher), Mandanten anlegen
#   ./scenario.sh down            alles stoppen (Daten bleiben)
#   ./scenario.sh status          laufendes Szenario + Container
#   ./scenario.sh logs [dienst]   Logs (Standard: vllm)
#   ./scenario.sh save-logs [datei]  Logs aller Dienste sichern, Geheimnisse geschwärzt (Standard:
#                                 data/container-logs/<Zeit>_<Szenario>.log). Passiert auch vor jedem down.
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
# ------------------------------------------------------------------ GPU-Auswahl
# vLLM rechnet auf genau einer GPU: LLMTB_GPU in deploy/.env (Index aus nvidia-smi oder UUID).
selected_gpu() { local g; g=$(envval LLMTB_GPU 2>/dev/null || true); echo "${g:-0}"; }
gpu_vram_mb() { nvidia-smi -i "$(selected_gpu)" --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | tr -d ' ' | head -1; }
# eine Zeile je GPU: Index, UUID, Name, Speicher (MB), Monitor angeschlossen (Enabled/Disabled)
gpu_list() {
  nvidia-smi --query-gpu=index,uuid,name,memory.total,display_active --format=csv,noheader,nounits 2>/dev/null \
    || nvidia-smi --query-gpu=index,uuid,name,memory.total --format=csv,noheader,nounits 2>/dev/null
}

show_gpus() {
  local list sel; list=$(gpu_list || true); sel=$(selected_gpu)
  [[ -n "$list" ]] || return 0
  local n; n=$(grep -c . <<< "$list" || true)
  local idx uuid name mem disp mark
  (( n > 1 )) && echo "GPUs (→ = genutzt):"
  while IFS=',' read -r idx uuid name mem disp; do
    idx=${idx// /}; uuid=${uuid// /}; mem=${mem// /}; disp=${disp// /}; name=$(echo $name)
    mark="  "; [[ "$sel" == "$idx" || "$sel" == "$uuid" ]] && mark="→ "
    if (( n > 1 )); then
      printf "  %s%s  %-48s %3d GB%s\n" "$mark" "$idx" "$name" $(( (mem + 1023) / 1024 )) \
        "$([[ "$disp" == Enabled ]] && echo '  (Monitor angeschlossen)')"
    fi
    if [[ "$mark" == "→ " && "$disp" == Enabled ]]; then
      echo "Hinweis: An GPU $idx hängt ein Monitor. Die Bildschirmausgabe stört die Zeitmessungen – Monitor an eine"
      echo "         andere GPU anschließen oder eine GPU ohne Monitor wählen (./scenario.sh gpu <Index>)."
    fi
  done <<< "$list"
  (( n > 1 )) && echo "Ändern: ./scenario.sh gpu <Index>"
  return 0
}

# init: bei mehreren GPUs die mit dem meisten Speicher, bei Gleichstand ohne Monitor, dann kleinster Index
choose_gpu() {
  local list; list=$(gpu_list || true)
  [[ -n "$list" ]] || return 0
  local best
  best=$(awk -F',' '{gsub(/ /,"",$1); gsub(/ /,"",$2); gsub(/ /,"",$4); gsub(/ /,"",$5);
                     print $4, ($5 == "Enabled" ? 0 : 1), $1, $2}' <<< "$list" \
         | sort -k1,1nr -k2,2nr -k3,3n | head -1 | awk '{print $4}')
  [[ -n "$best" ]] && setenv LLMTB_GPU "$best"
}

# ------------------------------------------------------------------ Modellprofile
# Ein Profil legt Modell, KV-Cache und Speicherbedarf fest. Der KV-Cache steht in Bytes, ist aber so
# gewählt, dass er in TOKENS in jedem Profil gleich groß ist: ~6 540 Tokens je Instanz.
# Darauf sind die Tests ausgelegt (z. B. „Kosten der Cache-Trennung“: Vorlage 512 Tokens × 1 … 30 Mandanten).
#   Bytes je Token (bf16) = 2 (K und V) × Schichten × KV-Köpfe × Kopfdimension × 2 Byte
#   Qwen3-0.6B: 2 × 28 × 8 × 128 × 2 = 114 688 B  →  750 MB ≈ 6 540 Tokens,  500 MB ≈ 4 360 Tokens
#   Qwen3-8B:   2 × 36 × 8 × 128 × 2 = 147 456 B  →  965 MB ≈ 6 540 Tokens
# K5 (zwei Instanzen auf einer GPU):
#   standard  jede Instanz genau wie K0 (gleicher KV-Cache, CUDA-Graphen an) – K5 unterscheidet sich von
#             K0 nur durch die Trennung; so misst der Vergleich die Kosten der Trennung und nichts anderes.
#   klein     Sparbetrieb für 8–24-GB-GPUs: kleinerer KV-Cache, --enforce-eager (ohne CUDA-Graphen) und
#             --max-num-seqs 16. Die Leistungswerte von K5 sind dann NICHT mit K0 vergleichbar.
# Felder: MODEL | KV_CACHE_MEMORY | KV_CACHE_MEMORY_DEDICATED | MODEL_GPU_MB | MODEL_GPU_MIN_MB | K5_ENGINE_ARGS
#   MODEL_GPU_MB      Speicher, den eine Instanz anfordert (daraus GPU_MEMORY_UTILIZATION)
#   MODEL_GPU_MIN_MB  Mindestbedarf je Instanz (Gewichte + KV-Cache + Puffer); K5 braucht 2 × davon + 400 MB
#   K5_ENGINE_ARGS    zusätzliche vLLM-Optionen nur für die beiden K5-Instanzen
declare -A MODELL_PROFILE=(
  [standard]="Qwen/Qwen3-8B|965000000|965000000|24000|22000|"
  [klein]="Qwen/Qwen3-0.6B|750000000|500000000|5000|3800|--enforce-eager --max-num-seqs 16"
)
PROFILE_KEYS=(MODEL KV_CACHE_MEMORY KV_CACHE_MEMORY_DEDICATED MODEL_GPU_MB MODEL_GPU_MIN_MB K5_ENGINE_ARGS)
profile_field() { local f; IFS='|' read -r -a f <<< "${MODELL_PROFILE[$1]}"; echo "${f[$2]}"; }
k5_need_mb() { echo $(( 2 * $1 + 400 )); }
gb() { echo $(( ($1 + 1023) / 1024 )); }   # MB → GB, aufgerundet

apply_model_profile() {
  [[ -n "${MODELL_PROFILE[$1]:-}" ]] || die "unbekanntes Modellprofil „$1“ (verfügbar: standard, klein)"
  local model kv kvd need needmin k5args
  IFS='|' read -r model kv kvd need needmin k5args <<< "${MODELL_PROFILE[$1]}"
  setenv MODELL_PROFIL "$1"
  setenv MODEL "$model"
  setenv KV_CACHE_MEMORY "$kv"
  setenv KV_CACHE_MEMORY_DEDICATED "$kvd"
  setenv MODEL_GPU_MB "$need"
  setenv MODEL_GPU_MIN_MB "$needmin"
  setenv K5_ENGINE_ARGS "$k5args"
  echo "Modellprofil „$1“: $model – KV-Cache $kv Bytes (~6 540 Tokens); K5 je Instanz $kvd Bytes" \
       "$([[ -n "$k5args" ]] && echo "mit $k5args (Sparbetrieb)" || echo "und Engine-Einstellungen wie K0")."
}

# Weicht deploy/.env vom eingestellten Modellprofil ab (z. B. weil sich ein Profil mit einem Update geändert
# hat oder ein Wert von Hand verstellt wurde)? Gibt die Abweichungen aus und liefert dann 1.
profile_drift() {
  local prof; prof=$(envval MODELL_PROFIL || true)
  [[ -n "$prof" && -n "${MODELL_PROFILE[$prof]:-}" ]] || return 0
  local vals i cur diff=""
  IFS='|' read -r -a vals <<< "${MODELL_PROFILE[$prof]}"
  for i in "${!PROFILE_KEYS[@]}"; do
    cur=$(envval "${PROFILE_KEYS[$i]}" || true)
    [[ "$cur" == "${vals[$i]:-}" ]] || diff+="  ${PROFILE_KEYS[$i]}: deploy/.env „${cur}“ – Profil „${prof}“: „${vals[$i]:-}“"$'\n'
  done
  [[ -z "$diff" ]] && return 0
  printf "%s" "$diff"
  return 1
}

# init: Profil nach VRAM – „standard“, sobald alle Szenarien einschließlich K5 hineinpassen
choose_model_profile() {
  local mb; mb=$(gpu_vram_mb || true)
  local grenze; grenze=$(k5_need_mb "$(profile_field standard 4)")
  if [[ -z "$mb" ]]; then
    echo "Keine NVIDIA-GPU gefunden – Modellprofil „standard“ (Qwen3-8B) aus der Vorlage bleibt."
    apply_model_profile standard
  elif (( mb >= grenze )); then
    echo "GPU mit $((mb / 1024)) GB VRAM: alle Szenarien einschließlich K5 passen mit Qwen3-8B."
    apply_model_profile standard
  else
    echo "GPU mit $((mb / 1024)) GB VRAM: zu klein für Qwen3-8B in allen Szenarien (ab $(gb "$grenze") GB) – nehme Qwen3-0.6B."
    apply_model_profile klein
  fi
  echo "Wechseln: ./scenario.sh modell standard|klein"
}

cmd_modell() {
  [[ -f .env ]] || die "zuerst ./scenario.sh init"
  local name="${1:-}"
  if [[ -z "$name" ]]; then
    echo "Aktuelles Modellprofil: $(envval MODELL_PROFIL || true) – $(envval MODEL || true)"
    echo "  standard: $(profile_field standard 0)  (ab $(gb "$(k5_need_mb "$(profile_field standard 4)")") GB VRAM für alle Szenarien)"
    echo "  klein:    $(profile_field klein 0)  (Laptop, ab 4 GB VRAM)"
    echo "Wechseln: ./scenario.sh modell standard|klein"
    return 0
  fi
  [[ ! -f "$ACTIVE" ]] || die "erst das laufende Szenario stoppen (./scenario.sh down) – sonst rechnet vLLM noch mit dem alten Modell"
  apply_model_profile "$name"
  cmd_gpu
  [[ -f secrets/cache_salt_secret ]] && cmd_profile
  echo "Weiter: ./scenario.sh pull-model (lädt $(envval MODEL) in den Hugging-Face-Cache), dann Szenario starten."
}

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
    choose_gpu
    choose_model_profile
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

# GPU-Werte aus dem VRAM: Der KV-Cache bleibt fest (KV_CACHE_MEMORY aus dem Modellprofil) – so sind die
# Szenarien untereinander vergleichbar. Angepasst wird nur die Obergrenze, die vLLM beim Start gegen den
# freien Speicher prüft: MODEL_GPU_MB je Instanz (Qwen3-8B ~24 GB, Qwen3-0.6B ~5 GB); auf kleinen GPUs
# höchstens 0,78 (WSL). Ältere .env ohne Modellprofil: Werte von Qwen3-0.6B.
# Mit Argument (Index oder UUID) wird zuerst die GPU gewechselt, auf der vLLM rechnet (LLMTB_GPU).
cmd_gpu() {
  [[ -f .env ]] || die "zuerst ./scenario.sh init"
  if [[ -n "${1:-}" ]]; then
    [[ ! -f "$ACTIVE" ]] || die "erst das laufende Szenario stoppen (./scenario.sh down) – es rechnet noch auf der alten GPU"
    local uuid; uuid=$(nvidia-smi -i "$1" --query-gpu=uuid --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ' || true)
    [[ "$uuid" == GPU-* ]] || die "GPU „$1“ nicht gefunden – Index oder UUID aus nvidia-smi -L angeben"
    setenv LLMTB_GPU "$uuid"
  fi
  local mb; mb=$(gpu_vram_mb || true)
  if [[ -z "$mb" ]]; then
    if gpu_list >/dev/null 2>&1; then
      die "LLMTB_GPU=$(selected_gpu) in deploy/.env passt zu keiner GPU – ./scenario.sh gpu <Index> (Liste: nvidia-smi -L)"
    fi
    echo "Keine NVIDIA-GPU gefunden (nvidia-smi) – GPU-Werte in deploy/.env bleiben unverändert."; return 0
  fi
  show_gpus
  local need needmin u ud
  need=$(envval MODEL_GPU_MB || true); needmin=$(envval MODEL_GPU_MIN_MB || true)
  need=${need:-5000}; needmin=${needmin:-3800}
  u=$(awk -v m="$mb" -v n="$need" 'BEGIN{v=n/m; if(v>0.78)v=0.78; if(v<0.2)v=0.2; printf "%.2f", v}')
  ud=$(awk -v m="$mb" -v n="$needmin" 'BEGIN{v=n/m; if(v>0.45)v=0.45; if(v<0.15)v=0.15; printf "%.2f", v}')
  setenv GPU_MEMORY_UTILIZATION "$u"
  setenv GPU_MEMORY_UTILIZATION_DEDICATED "$ud"
  setenv LLMTB_GPU_VRAM_MB "$mb"
  local gname; gname=$(nvidia-smi -i "$(selected_gpu)" --query-gpu=index,name --format=csv,noheader 2>/dev/null | head -1 | sed 's/, / /' || true)
  echo "GPU ${gname:-?}: $((mb / 1024)) GB VRAM, Modell $(envval MODEL) → GPU_MEMORY_UTILIZATION=$u, je K5-Instanz $ud (KV-Cache fest: $(envval KV_CACHE_MEMORY) Bytes)."
  if (( mb < needmin )); then
    echo "Achtung: $(envval MODEL) braucht ~$(gb "$needmin") GB VRAM, hier $((mb / 1024)) GB – kleineres Modell: ./scenario.sh modell klein"
  fi
  local k5; k5=$(k5_need_mb "$needmin")
  (( mb >= k5 )) || echo "Hinweis: K5 (zwei Instanzen) braucht mit diesem Modell >= $(gb "$k5") GB VRAM und wird im Gesamtlauf übersprungen."
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

# Logs aller Dienste des Projekts sichern, bevor `down` die Container (und damit ihre Logs) löscht.
# Darin stehen u. a. das vllm-serve-Kommando (start-vllm.sh) und die tatsächliche KV-Cache-Größe.
# Geheimnisse (Keys, Passwörter, Wasserzeichen-Schlüssel) schwärzt redact_logs.py.
cmd_save_logs() {
  local ids; ids=$(docker compose -p "$PROJECT" ps -aq 2>/dev/null || true)
  if [[ -z "$ids" ]]; then
    [[ -n "${1:-}" ]] && echo "Keine Container im Projekt $PROJECT – keine Logs zu sichern."
    return 0
  fi
  local sid=""
  [[ -f "$ACTIVE" ]] && sid=$(grep -o '"id": *"[^"]*"' "$ACTIVE" | head -1 | cut -d'"' -f4 || true)
  sid="${sid:-ohne-szenario}"
  local out="${1:-$ROOT/data/container-logs/$(date +%Y-%m-%d_%H%M%S)_${sid}.log}"
  mkdir -p "$(dirname "$out")"
  {
    echo "# Container-Logs – Projekt $PROJECT – Szenario $sid – gesichert $(date -Iseconds)"
    [[ -f "$ACTIVE" ]] && sed 's/^/# /' "$ACTIVE"
    echo "# docker compose ps:"
    docker compose -p "$PROJECT" ps -a 2>&1 | sed 's/^/# /'
    echo "#"
    docker compose -p "$PROJECT" logs --no-color --timestamps 2>&1
  } | python3 "$DEPLOY/redact_logs.py" > "$out"
  echo "Container-Logs gesichert: ${out#"$ROOT/"} ($(wc -l < "$out") Zeilen, Geheimnisse geschwärzt)"
}

cmd_down() {
  cmd_save_logs || echo "Warnung: Container-Logs konnten nicht gesichert werden." >&2
  docker compose -p "$PROJECT" down --remove-orphans 2>/dev/null || true; rm -f "$ACTIVE"
}

cmd_up() {
  need_env
  local id="${1:-}"; [[ -n "$id" ]] || die "Szenario angeben, z. B. ./scenario.sh up K1"
  local f; f=$(scenario_file "$id")
  local sid sname; sid=$(xfield "$f" id); sname=$(xfield "$f" name)
  echo "== Starte $sid – $sname ($f)"
  local drift; drift=$(profile_drift) || {
    echo "Warnung: deploy/.env weicht vom Modellprofil ab – die Messung entspricht nicht dem dokumentierten Profil:"
    printf "%s\n" "$drift"
    echo "         Angleichen: ./scenario.sh modell $(envval MODELL_PROFIL || true)"
  }
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

cmd_reset() {
  cmd_save_logs || echo "Warnung: Container-Logs konnten nicht gesichert werden." >&2
  docker compose -p "$PROJECT" down --remove-orphans -v 2>/dev/null || true; rm -f "$ACTIVE"
}

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
start_bench() {
  echo "Starte die Testplattform im Hintergrund (Log: data/testbench.log) …"
  "$ROOT/start.sh" --hintergrund
  local i
  for i in $(seq 1 90); do curl -sf "$BENCH/api/status" >/dev/null 2>&1 && return 0; sleep 2; done
  die "Testplattform startet nicht – siehe data/testbench.log"
}

# Läuft die Testplattform noch mit älterem Code als auf der Platte (z. B. nach git pull)? Python hat die
# Module beim Start geladen – sie würde mit dem alten Stand messen, während Commit und Code-Hash im
# Export den neuen dokumentieren. Vor einem neuen Gesamtlauf darum neu starten (nur wenn sie frei ist).
restart_if_stale() {
  local cs st state
  cs=$(curl -sf "$BENCH/api/code-stand" 2>/dev/null || true)
  # Eine Testplattform, die älter ist als diese Prüfung, kennt /api/code-stand nicht und antwortet mit der
  # Weboberfläche (HTML) – das gilt ebenfalls als veraltet. Darum nie ungeprüft als JSON lesen.
  state=$(python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    print("unbekannt"); sys.exit(0)
print("veraltet" if d.get("stale") else "aktuell")' <<< "$cs" 2>/dev/null || echo unbekannt)
  [[ "$state" == aktuell ]] && return 0
  st=$(curl -sf "$BENCH/api/status" 2>/dev/null || true)
  if python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("running") or d.get("gesamtlauf") == "laeuft" or d.get("ops_busy") else 1)' <<< "$st" 2>/dev/null; then
    die "Die Testplattform läuft mit älterem Code und ist gerade beschäftigt – erst nach dem Ende neu starten (./start.sh --stopp)."
  fi
  echo "Die Testplattform läuft noch mit älterem Code als auf der Platte (z. B. nach git pull) – starte sie neu,"
  echo "damit die Messung den aktuellen Stand nutzt."
  [[ -f "$ROOT/data/testbench.pid" ]] || die "Sie läuft nicht im Hintergrund (vermutlich ./start.sh in einem Terminal) – dort mit Strg+C beenden, dann erneut starten."
  "$ROOT/start.sh" --stopp
  local i
  for i in $(seq 1 30); do curl -sf "$BENCH/api/status" >/dev/null 2>&1 || break; sleep 1; done
  start_bench
}

ensure_bench() {   # $1 = fresh: vor einem neuen Gesamtlauf auf aktuellen Code prüfen
  if curl -sf "$BENCH/api/status" >/dev/null 2>&1; then
    [[ "${1:-}" == fresh ]] && restart_if_stale
    return 0
  fi
  echo "Testplattform läuft nicht."
  start_bench
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
  if [[ "$mode" == start ]]; then
    local drift; drift=$(profile_drift) || {
      if [[ "${LLMTB_PROFIL_PRUEFUNG:-an}" != aus ]]; then
        echo "deploy/.env weicht vom Modellprofil „$(envval MODELL_PROFIL)“ ab:" >&2
        printf "%s\n" "$drift" >&2
        die "Angleichen mit ./deploy/scenario.sh modell $(envval MODELL_PROFIL) (oder bewusst so lassen: LLMTB_PROFIL_PRUEFUNG=aus voranstellen)."
      fi
      echo "Hinweis: deploy/.env weicht vom Modellprofil ab (LLMTB_PROFIL_PRUEFUNG=aus) – gewollt?"
    }
    ensure_bench fresh
  else
    ensure_bench
  fi
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
  gpu) shift; cmd_gpu "${1:-}";;
  modell|model) shift; cmd_modell "${1:-}";;
  run-all) shift; cmd_run_all "$@";;
  list) cmd_list;;
  up) shift; cmd_up "${1:-}";;
  down) cmd_down;;
  status) cmd_status;;
  logs) shift; cmd_logs "${1:-}";;
  save-logs) shift; cmd_save_logs "${1:-}";;
  profile) cmd_profile;;
  pull-model) cmd_pull_model;;
  reset) cmd_reset;;
  detector) shift; cmd_detector "${1:-up}";;
  *) awk 'NR == 1 { next } /^#/ { print; next } { exit }' "$DEPLOY/$(basename "$0")"; exit 1;;
esac
