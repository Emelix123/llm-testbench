#!/usr/bin/env bash
# Steuert die Test-Szenarien K0–K6 (vLLM + LiteLLM) für die Testplattform.
#
#   ./scenario.sh init            .env und Geheimnisse (deploy/secrets/) erzeugen – einmalig
#   ./scenario.sh list            Szenarien anzeigen
#   ./scenario.sh up K1           Szenario starten (stoppt ein laufendes vorher), Mandanten anlegen
#   ./scenario.sh down            alles stoppen (Daten bleiben)
#   ./scenario.sh status          laufendes Szenario + Container
#   ./scenario.sh logs [dienst]   Logs (Standard: vllm)
#   ./scenario.sh profile         Endpunkt-Profile + Mandanten-Keys in der Testplattform anlegen
#   ./scenario.sh pull-model      Modell in den Hugging-Face-Cache laden (für das Offline-Szenario K4)
#   ./scenario.sh detector up|down  Wasserzeichen-Detektor allein (für Messungen in Szenarien ohne ihn)
#   ./scenario.sh reset           alles stoppen UND Volumes (Postgres, Compile-Cache) löschen
#
# Es läuft immer genau ein Szenario (Compose-Projekt "llmtb"), alle auf denselben Ports:
#   LiteLLM 127.0.0.1:4000 · vLLM direkt 127.0.0.1:8000 (nur K0–K3, K6) · Detektor 127.0.0.1:8090 (K4, K6)
set -euo pipefail
cd "$(dirname "$0")"
DEPLOY="$PWD"
ROOT="$(cd .. && pwd)"
ACTIVE="$DEPLOY/.active-scenario.json"
PROJECT=llmtb

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
envval() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }

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
  else
    echo "deploy/.env existiert – unverändert."
  fi
  # 256 Bit HMAC-Geheimnis für cache_salt (Doku: zufällig, ~256 Bit, keine vorhersagbare ID)
  [[ -f secrets/cache_salt_secret ]] || { rand_b64 32 > secrets/cache_salt_secret; echo "secrets/cache_salt_secret erzeugt."; }
  # 63-Bit-Ganzzahl als Wasserzeichen-Schlüssel (Philox: <= 64 Bit)
  [[ -f secrets/watermark_key ]] || { python3 -c "import secrets; print(secrets.randbits(63))" > secrets/watermark_key; echo "secrets/watermark_key erzeugt."; }
  chmod 600 secrets/*
  # Container lesen die Secrets als root – die Dateien bleiben trotzdem nur für dich lesbar.
}

cmd_list() {
  printf "%-4s %-32s %s\n" ID NAME DATEI
  for f in compose/k*.yaml; do printf "%-4s %-32s %s\n" "$(xfield "$f" id)" "$(xfield "$f" name)" "$f"; done
}

cmd_pull_model() {
  need_env
  local model img cache
  model=$(envval MODEL); img=$(envval VLLM_IMAGE); cache=$(envval HF_CACHE); cache="${cache/#\~/$HOME}"
  mkdir -p "$cache"
  echo "Lade $model nach $cache …"
  docker run --rm -v "$cache:/root/.cache/huggingface" -e HF_TOKEN="$(envval HF_TOKEN)" --entrypoint python3 "$img" \
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
  compose "$f" up -d --wait --wait-timeout 1500 $svcs
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
    up) docker compose -p llmtb-wm --env-file .env -f compose/wm-detector.yaml up -d --wait --wait-timeout 300
        echo "Detektor: http://127.0.0.1:8090 (nur Betreiber)";;
    down) docker compose -p llmtb-wm down;;
    *) die "detector up|down";;
  esac
}

cmd_reset() { docker compose -p "$PROJECT" down --remove-orphans -v 2>/dev/null || true; rm -f "$ACTIVE"; }

cmd_profile() {
  need_env
  python3 - "$ROOT/data" <<PY
import json, os, pathlib, sys
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
sec_p.write_text("# API-Keys. Diese Datei landet nie in runs/ und nie im Export.\n" + "".join(f"{k}={v}\n" for k, v in sorted(sec.items())))
os.chmod(sec_p, 0o600)
common = {"model": env.get("SERVED_MODEL_NAME", "llm"), "tokenizer": env.get("MODEL"),
          "max_context": int(env.get("MAX_MODEL_LEN", "2048")),
          "engine_url": "http://127.0.0.1:8000", "watermark_detector_url": "http://127.0.0.1:8090"}
profiles = {
  "lokal-litellm": {"name": "Lokal (LiteLLM, Mandant A)", "base_url": "http://127.0.0.1:4000",
                    "api_key_name": "LOKAL_LITELLM_API_KEY",
                    "tenant_keys": {"angreifer": "LOKAL_LITELLM_ANGREIFER_KEY", "last": "LOKAL_LITELLM_LAST_KEY"},
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
    path.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
    print("Profil", path)
print("Keys in", sec_p)
PY
}

case "${1:-}" in
  init) cmd_init;;
  list) cmd_list;;
  up) shift; cmd_up "${1:-}";;
  down) cmd_down;;
  status) cmd_status;;
  logs) shift; cmd_logs "${1:-}";;
  profile) cmd_profile;;
  pull-model) cmd_pull_model;;
  reset) cmd_reset;;
  detector) shift; cmd_detector "${1:-up}";;
  *) sed -n '2,17p' "$0"; exit 1;;
esac
