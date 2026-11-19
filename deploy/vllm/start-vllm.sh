#!/usr/bin/env bash
# Startet vLLM mit den gemeinsamen Grundparametern plus den szenariospezifischen Flags.
#
#   VLLM_SCENARIO_ARGS   Flags des Szenarios (Leerzeichen-getrennt), z. B. "--disable-prefix-caching"
#   WATERMARK_KEY_FILE   Pfad zum geheimen Wasserzeichen-Schlüssel (Docker-Secret). Gesetzt =>
#                        --watermark-config wird erzeugt. Der Schlüssel steht so weder in der
#                        Compose-Datei noch im Git, sondern nur in deploy/secrets/ (chmod 600).
#   WATERMARK_ENFORCE    true/false – Opt-out per Request (watermarking=false) übersteuern
#
# Das tatsächliche Kommando wird (ohne Schlüssel) ins Log geschrieben, damit jede Messung
# eindeutig einer Konfiguration zugeordnet werden kann.
set -euo pipefail

args=(
  "${MODEL:?MODEL fehlt}"
  --host 0.0.0.0 --port 8000
  --served-model-name "${SERVED_MODEL_NAME:-llm}"
  --max-model-len "${MAX_MODEL_LEN:-2048}"
)
# Obergrenze des GPU-Speichers (vLLM prüft sie beim Start gegen den freien Speicher; unter WSL
# belegt Windows einen Teil der GPU, daher < 0,8 auf einer 4-GB-Karte).
args+=(--gpu-memory-utilization "${GPU_MEMORY_UTILIZATION:-0.78}")
# Feste KV-Cache-Größe (Bytes): gleiche Cache-Kapazität in allen Szenarien, unabhängig davon,
# was Windows/WSL gerade an GPU-Speicher belegt.
if [[ -n "${KV_CACHE_MEMORY:-}" ]]; then
  args+=(--kv-cache-memory "${KV_CACHE_MEMORY}")
fi

if [[ -n "${VLLM_SCENARIO_ARGS:-}" ]]; then
  read -r -a extra <<< "${VLLM_SCENARIO_ARGS}"
  args+=("${extra[@]}")
fi

shown=("${args[@]}")
if [[ -n "${WATERMARK_KEY_FILE:-}" ]]; then
  key="$(tr -d '[:space:]' < "${WATERMARK_KEY_FILE}")"
  [[ "$key" =~ ^[0-9]+$ ]] || { echo "Wasserzeichen-Schlüssel muss eine Ganzzahl sein" >&2; exit 1; }
  wm_tail="\"context_width\":${WATERMARK_CONTEXT_WIDTH:-4},\"enforce\":${WATERMARK_ENFORCE:-true}}"
  args+=(--watermark-config "{\"algorithm\":\"gumbel\",\"key\":${key},${wm_tail}")
  shown+=(--watermark-config "{\"algorithm\":\"gumbel\",\"key\":<geheim>,${wm_tail}")
fi

echo "[llmtb] Szenario ${LLMTB_SCENARIO:-?}: vllm serve ${shown[*]}"
echo "[llmtb] VLLM_MAX_N_SEQUENCES=${VLLM_MAX_N_SEQUENCES:-<Standard 16384>} VLLM_API_KEY=$([[ -n "${VLLM_API_KEY:-}" ]] && echo gesetzt || echo -)"
exec vllm serve "${args[@]}"
