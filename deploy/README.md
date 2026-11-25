# Test-Szenarien K0–K6: vLLM + LiteLLM für die Mandantentrennung

Sieben Docker-Compose-Konfigurationen derselben Plattform (vLLM als Inferenz-Engine, LiteLLM als
Gateway, Postgres für die Virtual Keys), die sich jeweils in **genau den Maßnahmen** unterscheiden,
die die Bachelorarbeit untersucht. Die Testplattform misst je Szenario Sicherheit **und** Leistung;
die Seite **„Konfigurationen“** stellt sie nebeneinander (Δ % gegenüber K0, Export als LaTeX/CSV).

Grundlage: [vLLM Security Docs (v0.34)](https://docs.vllm.ai/en/stable/usage/security/) und Kapitel 1.3
der Arbeit. Nur auf eigenen Systemen betreiben.

## Schnellstart

Am einfachsten über die Weboberfläche: `../start.sh`, dann Seite **Steuerung** (Prüfen → Einrichten →
Images laden → Gesamtlauf). Server ohne Bildschirm: [`../docs/SERVER.md`](../docs/SERVER.md).
Dasselbe im Terminal:

```bash
cd deploy
./scenario.sh check       # Systemprüfung (Docker, GPU, Images, Modell, Einrichtung)
./scenario.sh init        # .env, Geheimnisse (secrets/), GPU-Werte, Endpunkt-Profile – einmalig
./scenario.sh pull        # Images (~40 GB) + Modell – einmalig
./scenario.sh run-all -n 3   # Gesamtlauf: alle passenden Szenarien, je 3 Messungen, Export nach data/exports/
```

Einzeln: `./scenario.sh up K0` startet ein Szenario (stoppt ein laufendes vorher); dann in der
Testplattform Profil **„Lokal (LiteLLM, Mandant A)“** wählen und die Suite **„Konfigurationsvergleich
(je Szenario)“** starten. Das Szenario wird je Lauf automatisch aus `deploy/.active-scenario.json`
übernommen (oben in der Oberfläche sichtbar).

```bash
./scenario.sh list | status | logs [dienst] | save-logs [datei] | down | reset | gpu
./scenario.sh detector up   # Wasserzeichen-Detektor allein (z. B. für K0 als Gegenprobe)
./scenario.sh run-all --status | --abbrechen
```

`LLMTB_PROJECT=name` trennt die Docker-Volumes mehrerer Repo-Kopien (Standard `llmtb`).

**Logs bleiben erhalten:** `down` und `reset` (auch das `down` am Anfang jedes `up`) sichern vorher die Logs
aller Container nach `data/container-logs/<Zeit>_<Szenario>.log` – mit dem `vllm serve`-Kommando aus
`start-vllm.sh` und der tatsächlichen KV-Cache-Größe. Keys, Passwörter und der Wasserzeichen-Schlüssel werden
dabei geschwärzt (`redact_logs.py`). Der Gesamtlauf legt die Logs zusätzlich in seinen Export.

| Dienst | Adresse | Wer darf |
|---|---|---|
| LiteLLM (Gateway) | `http://127.0.0.1:4000`, Modell `llm` | Mandanten (Virtual Keys A/B/C) |
| vLLM direkt | `http://127.0.0.1:8000` – **nur K0–K3, K6** (absichtlich, um die Umgehung zu zeigen) | niemand |
| Wasserzeichen-Detektor | `http://127.0.0.1:8090` – K4, K6 oder `detector up` | nur Betreiber |

Mandanten: **A = Opfer** (Haupt-Key des Profils), **B = Angreifer**, **C = Last** – drei LiteLLM-Teams mit je
einem Virtual Key. Bei jedem `up` werden die Keys mit den Grenzen des Szenarios neu angelegt.

## Die Szenarien

Jedes Szenario = K0 + genau die genannte(n) Maßnahme(n) (Ablationsstudie). K4 kombiniert alles.

| ID | Datei | Maßnahme | Bedroht (Arbeit, Tab. 1.1) | Erwartung |
|---|---|---|---|---|
| **K0** | `k0-standard.yaml` | keine: vLLM-Standard, geteilter Prefix-Cache, Engine direkt erreichbar, LiteLLM ohne Mandanten-Weitergabe | – | Seitenkanal, Noisy Neighbor, Härtung **rot** |
| **K1** | `k1-cache-salt.yaml` | `cache_salt` je Mandant im Gateway-Hook (HMAC-SHA256 aus Team, 256 Bit) | Timing-Seitenkanal Prefix Caching, gemeinsame Cache-Identität im Gateway | Seitenkanal **grün**, Leistung ≈ K0 |
| **K2** | `k2-ohne-prefix-cache.yaml` | `--disable-prefix-caching` | wie K1 + Cache-Kollisionen | Seitenkanal **grün**, TTFT deutlich schlechter |
| **K3** | `k3-ratenlimit.yaml` | je Mandant max. 8 parallel / 600 pro min, n ≤ 16 im Gateway, `VLLM_MAX_N_SEQUENCES=64` | Ressourcenverdrängung (CVE-2026-34756) | S_N kleiner, Jain-Index höher, 429 für den Störnachbarn |
| **K4** | `k4-gehaertet.yaml` | alles: K1 + K3 + Netztrennung, Engine-API-Key, Routen-Allowlist, kein `cached_tokens`, Feature-Flags aus, Wasserzeichen, Container-Härtung | alle Engine-Angriffe (Fokus „ja“) | alle Sicherheitstests **grün** |
| **K5** | `k5-dediziert.yaml` | dedizierte vLLM-Instanz je Mandant auf derselben GPU | Seitenkanal, Absturz trifft nur eigene Instanz | Seitenkanal grün, S_N > 1 nur über GPU-Rechenzeit. **Braucht ≥ 8 GB VRAM** |
| **K6** | `k6-wasserzeichen.yaml` | Gumbel-Wasserzeichen (`enforce=true`) + Detektor | KI-VO Art. 50 Abs. 2 | Erkennung ab 200 Tokens, Leistungskosten K6 vs. K0 |

### K4 im Detail (Best Practices aus der vLLM-Doku)

| Doku-Empfehlung | Umsetzung |
|---|---|
| Engine nicht öffentlich, nur über authentifizierendes Gateway | vLLM nur im Netz `backend` (`internal: true`), kein Port; LiteLLM einziger Zugang |
| `--api-key` schützt nur `/v1`, `/v2`, `/inference`, `/cohere` | `VLLM_API_KEY` gesetzt **und** Netztrennung (Betriebsendpunkte bleiben sonst offen) |
| Betriebs-/Info-Endpunkte (`/metrics`, `/pause`, `/tokenize` …) nicht öffentlich | Routen-Allowlist je Mandanten-Key (`allowed_routes`): nur Chat, Completions, Models, `/key/info` – sperrt auch LiteLLMs `/vllm/*`-Durchreichung |
| `cache_salt` je Mandant, zufälliges Geheimnis ~256 Bit | Hook `litellm/tenant_isolation.py`, Geheimnis `secrets/cache_salt_secret`; Client-Salt wird verworfen |
| Hash `sha256`, kein `xxhash` | `--prefix-caching-hash-algo sha256` |
| `VLLM_MAX_N_SEQUENCES` 64–128 | `64` in der Engine, zusätzlich `n ≤ 16` je Mandant im Gateway |
| Medien-Allowlist, keine Redirects | `--language-model-only` (keine Medien-Eingaben), `VLLM_MEDIA_URL_ALLOW_REDIRECTS=0` |
| `prompt_embeds` nur für vertrauenswürdige Nutzer | `--disable-prompt-embeds` + Hook entfernt das Feld |
| `mm_processor_kwargs`, eigenes Chat-Template nicht vertrauen | `--no-trust-request-mm-kwargs`, `--no-trust-request-chat-template` |
| `VLLM_SERVER_DEV_MODE` nie, Profiler/Tokenizer-Info nur bei Bedarf | `VLLM_SERVER_DEV_MODE=0`, `--disable-tokenizer-info-endpoint`, kein `--profiler-config` |
| LoRA zur Laufzeit / Plugins nicht | `VLLM_ALLOW_RUNTIME_LORA_UPDATING=False`, `VLLM_PLUGINS=""` |
| Cache-Verzeichnisse privat | eigenes Volume `vllm-cache`, HF-Cache nur lesend, `HF_HUB_OFFLINE=1` |
| Begrenzte Warteschlange | `--max-num-seqs 32`, `--max-num-queued-reqs 64` (503 statt unbegrenzter Latenz) |
| – (Arbeit: Datenminimierung) | `--disable-log-requests`, LiteLLM `store_prompts_in_spend_logs: false` |
| – (Arbeit: KI-VO Art. 50) | Wasserzeichen `enforce=true`, Schlüssel als Docker-Secret |
| – (Container) | `cap_drop: ALL`, `no-new-privileges`, `pids_limit`, `shm_size` statt `ipc: host` |

## Feste Rahmenbedingungen (Vergleichbarkeit)

- Images gepinnt: `vllm/vllm-openai:v0.34.0`, `ghcr.io/berriai/litellm-database:v1.106.7`.
- **Feste KV-Cache-Größe** (`KV_CACHE_MEMORY=750000000` ≈ 6 500 Tokens bei Qwen3-0.6B) in allen Szenarien –
  sonst hinge die Kapazität davon ab, was Windows gerade an GPU-Speicher belegt.
- vLLM 0.34 nutzt standardmäßig **64 Tokens je KV-Block** (`CacheConfig.DEFAULT_BLOCK_SIZE`), nicht 16 wie im
  vLLM-Paper 2023. Treffer im Prefix-Cache entstehen erst ab einem vollen Block.
- `--enable-prompt-tokens-details` ist in K0–K3/K5/K6 als **Messhilfe** an (liefert `cached_tokens`), in K4 aus.
- LiteLLM ohne Wiederholungen, ohne Antwort-Cache.

## Befunde beim Aufsetzen (für die Arbeit relevant)

1. **KeyPooling bestätigt:** In K0 erhält Mandant B mit seinem **eigenen** Virtual Key `cached_tokens` aus dem
   Präfix von Mandant A. LiteLLM gibt die Mandanten-Identität nicht an vLLM weiter (Sun u. a. 2026).
2. **Client-Salt wird durchgereicht:** LiteLLM reicht unbekannte Felder (`cache_salt`, `prompt_embeds`) unverändert an
   vLLM. Ein vom Client gewählter, vorhersagbarer Salt teilt den Cache mandantenübergreifend – der Hook muss ihn
   überschreiben, nicht nur ergänzen.
3. **`/vllm/*`-Durchreichung:** LiteLLM v1.106.7 öffnet `/vllm/{pfad}` für **jeden** gültigen Key. Ohne
   `VLLM_API_BASE` endet sie mit HTTP 500; mit gesetztem `VLLM_API_BASE` (so beschreibt es die LiteLLM-Doku) liest ein
   Mandant z. B. `/vllm/metrics` – inklusive `prefix_cache_hits_total` und KV-Cache-Auslastung (Cache-Orakel ohne
   Zeitmessung). Abhilfe: `allowed_routes` je Key (K4).
4. **Interne Adresse im Header:** LiteLLM liefert `x-litellm-model-api-base: http://vllm:8000/v1` an Mandanten aus,
   `/model/info` zeigt sie ebenfalls.
5. Die Testplattform misst: K0 Prefix-Seitenkanal AUC 1,00 (TTFT 44 ms Treffer / 85 ms Fehlschlag bei 512 Tokens).

## Dateien

```
deploy/
  scenario.sh              Steuerung (check, init, pull, run-all, up, down, profile, detector …)
  ops_check.py             Systemprüfung (auch für die Seite „Steuerung“), nur Standardbibliothek
  redact_logs.py           schwärzt Geheimnisse in gesicherten Container-Logs (save-logs, vor jedem down)
  .env.example             Vorlage; .env und secrets/ entstehen mit init (nie einchecken)
  compose/common.yaml      Bausteine (vllm, litellm, postgres, tenant-setup, wm-detector)
  compose/k0 … k6-*.yaml   die Szenarien (je Datei Kommentar mit Zweck und Erwartung)
  compose/wm-detector.yaml Detektor allein
  vllm/start-vllm.sh       baut das vllm-serve-Kommando, protokolliert es (ohne Schlüssel)
  litellm/config*.yaml     Gateway-Konfiguration (geteilt / dediziert)
  litellm/tenant_isolation.py  Mandanten-Hook: cache_salt, Feld-Filter, n-Grenze, Routing
  litellm/bootstrap_tenants.py legt Teams + Keys mit den Grenzen des Szenarios an (A, B, C und
                           pool-01 … pool-30 für den Test „Kosten der Cache-Trennung“)
  watermark/detector_server.py Detektor-Dienst (vllm.v1.watermarking, CPU)
```

## Grenzen auf dem Laptop (RTX 3050, 4 GB, WSL)

- **K5** passt nicht in 4 GB (zwei Modellkopien + Aktivierungen). Für die Zielhardware gedacht.
- **MIG, MPS-Isolation, ECC** gibt es auf GeForce-GPUs und unter WSL nicht – GPU-Ebene (Angreifertyp A2) ist hier
  nicht messbar, nur die Engine- und Gateway-Ebene.
- Zeitmessungen unter WSL haben mehr Rauschen als auf Bare Metal (siehe `../docs/WSL-SETUP.md`).
