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
Prefix-Cache, LiteLLM-Header, Tool Calling, JSON-Schema, Stop, Seed, Logprobs; mit `--vision` Bilder,
mit `--reasoning` Denk-Teil, mit `--response-cache` ein fälschlich aktiver Antwort-Cache).
Der Mock ist bewusst einfach: Qualitätswerte gegen ihn sagen nichts aus. Endpunkt-Profil: URL `http://127.0.0.1:4000`, Modell `mock-llm`,
beliebiger Key. Der Parallelitäts-Test sollte dann 24 messen.

## Aufbau

```
backend/app/        FastAPI: Lastgenerator, Messung, Ablage, API, SSE
  client.py         Streaming-Client, Zeitstempel pro Chunk, LiteLLM-Header
  runner.py         Modulsystem, RunContext (Aufwärmen, geschlossene Last), Batches, Live-Events
  prompts.py        synthetische Prompts mit exakter Token-Länge (HF tokenizers)
  stats.py          p50/p95/p99, Goodput, Hänger, Drift, Nutzer-Umrechnung, Kosten
  loadprofiles.py   die sechs Lastprofile
  quality.py        Referenzvergleich, LLM als Prüfer
  report.py         Prüfbericht HTML (SVG-Diagramme) und PDF
  media.py          Testbilder
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
  - `async def run(ctx)` – erzeugt Anfragen über `ctx.send()`, `ctx.once()`, `ctx.closed_load()`,
    `ctx.open_load()` oder `ctx.variable_load()`, meldet
    `ctx.progress()` und `ctx.stage()`. Jede Anfrage landet sofort in `requests.jsonl`.
  - `def summarize(recs, stages, cfg, settings) -> dict` – Kennzahlen, `verdict` (`gruen`/`gelb`/`rot`),
    `headline` (ein Satz Klartext), `charts`, `tables`, optional `kpis`. Jederzeit neu ausführbar.
  - optional `def estimate(params) -> Sekunden`.

An der Oberfläche ändert sich nichts; das Backend lädt neue oder geänderte Testordner ohne Neustart.

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
data/references.json       Referenzläufe der Qualitätstests
data/reports/              Prüfberichte (HTML, PDF)
data/quality/cases.jsonl   eigene Kundenaufgaben (optional)
```

Bricht das Backend ab (Absturz, Laptop aus), wird der Lauf beim nächsten Start als „unterbrochen“
markiert und aus den bis dahin gespeicherten Anfragen ausgewertet.

## Tests

| Kategorie | Test | Ergebnis |
|---|---|---|
| Leistung & Kapazität | Schnellcheck | Haken je Punkt (Erreichbarkeit, Modell, Streaming, usage, cached_tokens, LiteLLM-Header, Key-Limits, Netzlatenz, ignore_eos), Referenz mit 1 Nutzer |
| | Parallelitäts-Test | echte Parallelität je Kontextlänge (gleichzeitig strömende Antworten, TTFT-Sprung), 429 = LiteLLM-Limit |
| | Kapazitäts-Sweep | Durchsatz gegen Latenz je Lastprofil, höchste Parallelität im Ziel, Nutzer, € / 1 Mio. Tokens |
| | Rate-Test (offene Last) | höchste Anfragerate ohne wachsende Warteschlange (Poisson oder stoßweise) |
| | Kontextlängen-Matrix | TTFT, Durchsatz und bediente Anfragen je Input-Länge, Prefill-Geschwindigkeit |
| | Cache-Nutzen | TTFT-Ersparnis bei gemeinsamem System-Prompt und wachsendem Verlauf, Trefferquote |
| | Kosten je Token-Art | € / 1 Mio. Input-, Output- und Cache-Tokens, Mischpreis je Lastprofil |
| Konfiguration & Funktionen | Konfiguration & Funktionen | 13 Prüfpunkte: Tool Calling, Structured Output, Reasoning, Chat-Template, Kontextgrenze, Nadel im Heuhaufen, Multimodal, Parameter, Streaming-Format, Abbruch, LiteLLM-Durchreichung, LiteLLM-Antwort-Cache, Steckbrief |
| Stabilität & Betrieb | Dauerlast | 1–4 h bei 75 % der Kapazität, Drift von TTFT und Tokens/s pro Stunde |
| | Lastspitze | 150 % für 2 min, dann 50 %: Erholungszeit, 429 gegenüber Timeouts |
| | Überlast | 200 % für 5 min: kontrollierte Ablehnung oder alle langsam, Server danach erreichbar |
| | Grenzwerte | riesiges max_tokens, n > 1, tiefe JSON-Schemas, viele Bilder, leere Anfrage … |
| | Kaltstart | erste Anfrage nach Pause gegen warme Anfragen |
| Qualität | eigene Kundenaufgaben | LLM als Prüfer (Note 1–5) gegen Musterantworten; eigene Fälle in `data/quality/cases.jsonl` |
| | Konsistenz unter Last | temperature 0 einzeln gegen 64 parallel |
| | Tool Calling (BFCL-Stil) | simple, multiple, parallel, parallel-multiple, irrelevance mit AST-Prüfung |
| | lm-evaluation-harness | z. B. GSM8K, IFEval über local-chat-completions (`pip install "lm-eval[api]"`) |
| | LongBench v2 | Langkontext-Multiple-Choice (lädt den Datensatz einmalig von Hugging Face) |

Qualitätstests vergleichen immer mit einem **Referenzlauf**: auf der Ergebnisseite „Als Referenz markieren“.
Für die volle BFCL das offizielle Paket separat gegen den Endpunkt laufen lassen; der eingebaute Test
deckt die Kategorien mit einer kleinen Auswahl ab.

Stabilitätstests nehmen die Kapazität aus dem letzten Kapazitäts-Sweep desselben Endpunkts, wenn
sie nicht als Parameter gesetzt ist. Sie speichern ITL pro Anfrage zusammengefasst (n, p50, p95, max),
damit ein 4-h-Lauf nicht mehrere GB Rohdaten erzeugt.

## Suiten

| Suite | Enthält | Dauer |
|---|---|---|
| Schnellcheck | Schnellcheck | ca. 2–5 min |
| Parallelität & Kapazität | Parallelitäts-Test, Kapazitäts-Sweep, Kontextlängen-Matrix | ca. 1 h |
| Konfiguration | alle Prüfpunkte | ca. 10 min |
| Dauerlast | Dauerlast, Lastspitze, Überlast | 1–4 h |
| Abnahme | alles zusammen, plus Prüfbericht als HTML und PDF in `data/reports/` | mehrere Stunden |

Den Prüfbericht gibt es für jeden Lauf auch über die Knöpfe auf der Ergebnisseite. Das PDF erzeugt
ein installierter Chrome, Edge oder Chromium im Headless-Modus (Pfad notfalls über `LLMTB_CHROME`);
ohne Browser bleibt der HTML-Bericht, der sich drucken lässt.

## Ergebnisse, Vergleich, Kosten

- Ergebnisseite: vier Kennzahlen, Karte je Test mit Ampel und Klartext, Details auf Klick, ZIP-Export.
- Kostenrechner mit Reglern für Auslastung, Lesezeit und Maschinenkosten.
- Vergleich: zwei Läufe desselben Tests nebeneinander, Diagramme überlagert, Differenzen.
- Absichtlich provozierte Ablehnungen (Grenzwerte, Kontextgrenze, Abbruch-Test) zählen nicht zur Fehlerquote.

## Endpunkt-Profil

URL von LiteLLM, Modell, Tokenizer (Hugging-Face-Name oder Pfad zu `tokenizer.json`), max. Kontext,
API-Key und die Häkchen „Vision-Modell“ und „Reasoning-Modell“ (steuern Multimodal- und Reasoning-Prüfung).

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
