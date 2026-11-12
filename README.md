# LLM-Inferenz-Testplattform

Lokales Test-Tool: zeigt per Knopfdruck, wie viele Anfragen eine vLLM-Maschine hinter LiteLLM
wirklich gleichzeitig rechnet, wie viele Nutzer sie trägt, was 1 Mio. Tokens kostet und ob die
Konfiguration stimmt. Läuft komplett auf dem Laptop – kein Server, keine Datenbank.

## Start

```bash
./start.sh          # Windows: start.bat
```

Dann <http://localhost:8080> öffnen. Voraussetzung: Python ≥ 3.11 (empfohlen 3.12).
Das gebaute Frontend liegt in `frontend/dist`; npm braucht man nur, um es neu zu bauen.

### Ohne GPU ausprobieren

```bash
./mock/start_mock.sh --slots 24 --speed 4
```

Startet einen Mock, der vLLM + LiteLLM nachbildet (feste Slots, KV-Cache, Prefill/Decode-Zeiten,
Prefix-Cache, LiteLLM-Header). Endpunkt-Profil: URL `http://127.0.0.1:4000`, Modell `mock-llm`,
beliebiger Key. Der Parallelitäts-Test sollte dann 24 messen.

## Aufbau

```
backend/app/        FastAPI: Lastgenerator, Messung, Ablage, API, SSE
  client.py         Streaming-Client, Zeitstempel pro Chunk, LiteLLM-Header
  runner.py         Modulsystem, RunContext (Aufwärmen, geschlossene Last), Batches, Live-Events
  prompts.py        synthetische Prompts mit exakter Token-Länge (HF tokenizers)
  stats.py          p50/p95/p99, Goodput, Hänger, Nutzer-Umrechnung, Kosten
  loadprofiles.py   die sechs Lastprofile
tests/<name>/       ein Ordner pro Test: manifest.json + test.py (run, summarize, estimate)
frontend/           Svelte + Vite + ECharts
mock/               Mock-Endpunkt zum Ausprobieren
data/               alles, was das Tool speichert (siehe unten)
```

### Einen Test hinzufügen

Neuer Ordner `tests/<name>/` mit

- `manifest.json`: `id`, `name`, `category`, `description`, `estimated_duration_s`, `params`
  (Typen: `int`, `float`, `int_list`, `multi`, `bool`, `string`). Daraus baut die Oberfläche das Formular.
- `test.py`:
  - `async def run(ctx)` – erzeugt Anfragen über `ctx.send()` / `ctx.closed_load()`, meldet
    `ctx.progress()` und `ctx.stage()`. Jede Anfrage landet sofort in `requests.jsonl`.
  - `def summarize(recs, stages, cfg, settings) -> dict` – Kennzahlen, `verdict` (`gruen`/`gelb`/`rot`),
    `headline` (ein Satz Klartext), `charts`, `tables`, optional `kpis`. Jederzeit neu ausführbar.
  - optional `def estimate(params) -> Sekunden`.

An der Oberfläche ändert sich nichts.

## Datenablage

```
data/secrets.env           API-Keys (nie in runs/, nie im Export; HF_TOKEN für gesperrte Tokenizer)
data/settings.json         Zielwerte, Kostenannahmen, Ampel an/aus
data/profiles/             Endpunkt-Profile
data/suites/               eigene Testzusammenstellungen
data/runs/<datum>_<uhrzeit>_<test>/
    config.json            Parameter, Seed, Tool-Version, Steckbrief, Netzlatenz, Hinweise
    requests.jsonl         eine Zeile pro Anfrage, sofort geschrieben
    progress.jsonl         Zwischenstände pro Laststufe
    summary.json           Kennzahlen und Ampel (wird bei geänderten Einstellungen neu berechnet)
```

Bricht das Backend ab (Absturz, Laptop aus), wird der Lauf beim nächsten Start als „unterbrochen“
markiert und aus den bis dahin gespeicherten Anfragen ausgewertet.

## Stand: Phase 1

| Punkt | Stand |
|---|---|
| Projektgerüst, start.sh / start.bat | ✔ |
| Endpunkt-Profile, secrets.env, „Verbindung prüfen“ | ✔ |
| Modulsystem (manifest.json, run, summarize, Formulare aus Manifest) | ✔ |
| Lastgenerator geschlossene Last, Zeitmessung pro Chunk, sofortiges Schreiben | ✔ |
| Synthetische Prompts mit Tokenizer auf exakte Länge (Fallback: kalibrierte Schätzung) | ✔ |
| LiteLLM-Header (Overhead, Retries, Fallbacks, Rate-Limits) | ✔ |
| Schnellcheck | ✔ |
| Parallelitäts-Test | ✔ |
| Kapazitäts-Sweep mit sechs Lastprofilen | ✔ |
| Ergebnisseite: vier Kennzahlen, Karten pro Test, Ampel abschaltbar, Details auf Klick | ✔ |

Vorgezogen: Export als ZIP, Ruhezustand verhindern, Client-CPU je Anfrage, Netzlatenz vor jedem Lauf,
Steckbrief in jedem Lauf, Mischpreis € / 1 Mio. Tokens, Umrechnung in aktive Nutzer.

Offen ab Phase 2: Rate-Test (offene Last), Kontextlängen-Matrix, Cache-Nutzen, Kosten je Token-Art,
Vergleich zweier Läufe; danach Konfiguration, Stabilität, Qualität und Prüfbericht.

## Messregeln (umgesetzt)

- Zeiten als p50/p95/p99, nie als Durchschnitt; monotone Uhr (`perf_counter_ns`).
- Token-Zahlen nur aus `usage`, nie aus Chunks geschätzt.
- 10 Aufwärm-Anfragen vor jeder Messung, nicht gewertet; dabei Kalibrierung des Chat-Template-Overheads.
- Eindeutiger Zufallsanfang je Anfrage, damit der Prefix-Cache nicht mitmisst.
- `ignore_eos` + `min_tokens`, falls LiteLLM sie durchreicht; sonst Hinweis und tatsächliche Längen.
- Keine automatischen Wiederholungen; Timeout je Anfrage; Fehler werden gezählt, nie Abbruchgrund.
- Seed je Lauf gespeichert.
- Echte Parallelität = höchste Zahl gleichzeitig strömender Antworten (≥ 1 s gehalten) bzw. Sprung in der TTFT.
- Aktive Nutzer ≈ Parallelität × (Antwortzeit + Lesezeit) / Antwortzeit – Annahmen stehen immer daneben.
