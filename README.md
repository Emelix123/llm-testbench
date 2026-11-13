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
mit `--reasoning` Denk-Teil, mit `--response-cache` ein fälschlich aktiver Antwort-Cache; `--block-size`, `--no-prefix-caching`, `--ignore-cache-salt`, `--no-cached-tokens` für den Prefix-Cache-Seitenkanal; `--max-n` für die n-Grenze bei der Ressourcenverdrängung).
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
| Sicherheit (Mandantentrennung) | Prefix-Cache-Seitenkanal (CVE-2025-46570) | Timing- und cached_tokens-Seitenkanal; AUC/Cohens d je Einstellung (cache_salt, Caching, Blockgröße, Last) |
| | Ressourcenverdrängung / Noisy Neighbor (CVE-2026-34756) | Opfer-Latenz mit/ohne Störnachbar (Flut, lange Anfragen, großes n); Präfix-Verdrängung; n-Grenze |
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

## Sicherheitstest: Prefix-Cache-Seitenkanal (CVE-2025-46570)

Der Test `prefix_cache_leak` stellt den Timing-Seitenkanal von geteiltem Prefix-Caching nach
und misst, **ob er je nach Einstellung funktioniert**. Nur gegen eigene Systeme mit Erlaubnis.
Die Ampel bewertet aus Verteidigersicht: **rot = verwundbar**, grün = kein nutzbares Signal.

Was gemessen wird, Schritt für Schritt:

1. Ein **Opfer** (ein Mandant) sendet einen zufälligen Präfix P. Dessen Blöcke landen im Cache.
2. Der **Angreifer** (anderer Mandant) sendet im Treffer-Fall `P + eigener Zusatz`
   (gemeinsame Blöcke = Cache-Treffer) und im Fehlschlag-Fall einen frischen Präfix (Cache-Fehlschlag).
   P ist je Messung neu zufällig, damit nichts nachwirkt.
3. Gemessen werden zwei Signale: die **Antwortzeit (TTFT)** und das Feld
   `prompt_tokens_details.cached_tokens`.

Zwei Ergebnismaße:

- **AUC** – wie gut sich Treffer und Fehlschlag an der TTFT trennen lassen (0,5 = Raten,
  1,0 = perfekt unterscheidbar). Das ist das Maß aus der CVE.
- **Cohens d** – die Effektstärke. In der Literatur senkt Last anderer Mandanten sie deutlich;
  der Test misst mit `background_load > 0` zusätzlich unter Last.
- **cached_tokens-Oracle** – liefert der Endpunkt das Feld, kann der Angreifer Treffer direkt
  ablesen, ganz ohne Zeitmessung (InputSnatch-Prinzip). Das Gateway darf dieses Feld und den
  Präfix-Cache-Zählerendpunkt von vLLM nicht nach außen geben.

Variierbare Einstellungen (Parameter des Tests):

| Parameter | Wirkung |
|---|---|
| `salt_mode` = `none` | kein `cache_salt` → geteilter Cache → **angreifbar** |
| `salt_mode` = `per_tenant` | Gateway setzt festen Salt je Mandant → Opfer und Angreifer teilen keine Blöcke |
| `salt_mode` = `per_request` | eindeutiger Salt je Anfrage → schaltet Caching praktisch aus |
| `salt_field` | Feldname des Salts im Request (prüft zugleich, ob LiteLLM ihn durchreicht – vgl. CVE-2026-105752) |
| `prefix_tokens` | Länge des Opfer-Präfixes |
| `background_load` | Zahl paralleler Mitmandanten; misst zusätzlich unter Last |
| `granularity_sweep` | Sweep über die Präfix-Übereinstimmung: zeigt, dass Treffer nur an **Blockgrenzen** entstehen (bei 16 Tokens je Block muss ein Angreifer je einen ganzen Block raten) |

Der Granularitäts-Sweep reproduziert den CVE-Befund: unterhalb einer Blockgröße (z. B. 8 von 16
Tokens) bleibt die AUC bei ~0,5, ab einem vollen Block springt sie gegen 1,0; `cached_tokens`
steigt blockweise.

Mit cache_salt pro Mandant oder abgeschaltetem Prefix-Caching fällt die AUC auf ~0,5 zurück
(grün). So lässt sich belegen, ob eine Gegenmaßnahme greift – ohne und mit Last.

### Ressourcenverdrängung / Noisy Neighbor (CVE-2026-34756)

Der Test `resource_contention` misst, wie stark ein einzelner Mandant die **Verfügbarkeit**
für andere senkt. Alle Anfragen teilen sich denselben KV-Cache-Speicher, und vLLM plant fair
je Anfrage, nicht je Mandant – ein Mandant mit vielen, langen oder n-starken Anfragen erhält
also mehr Ressourcen und verzögert die anderen.

Ablauf: Ein **Opfer** (gut benehmender Mandant) stellt die ganze Zeit gleichmäßig normale
Anfragen. Gemessen wird dessen Latenz **ohne** und **mit** einem **Störnachbarn**:

- Angriffsarten (`attack_mode`): `flood` (viele parallele Anfragen), `long` (sehr hoher Output,
  hält KV-Cache), `big_n` (großes `n`, der Hebel aus der CVE), `combined`.
- Gemessen am Opfer: TTFT, TPOT, E2E, Goodput, Fehler und Abbrüche – ohne vs. mit Angriff,
  als Faktor (z. B. „TTFT p95 ×4,5", „Goodput 100 % → 0 %").
- **n-Grenze:** Der Test prüft per Leiter (2 … 100000), bis zu welchem `n` der Server annimmt.
  vLLM begrenzt `n` nur global (VLLM_MAX_N_SEQUENCES, Standard 16384; Empfehlung 64–128),
  nicht je Mandant – ein hoher angenommener Wert wird als Warnung ausgewiesen.
- **Präfix-Verdrängung:** Ein gecachter Opfer-Präfix wird vor und während des Angriffs gemessen.
  Fallen die `cached_tokens`, hat der Störnachbar die Blöcke des Opfers aus dem Cache verdrängt.
  Fehlt `cached_tokens`, lässt sich Verdrängung nicht von Warteschlange trennen – der Test sagt
  das dann offen (ein TTFT-Anstieg allein beweist keine Verdrängung).

Ampel aus Verteidigersicht: rot = Opfer stark beeinträchtigt. Grün (geringe Beeinträchtigung)
deutet darauf, dass eine Begrenzung je Mandant im Gateway oder getrennte Instanzen greifen.
Für einen Vorher-Nachher-Nachweis einer Gegenmaßnahme eignet sich die Vergleichsansicht.


Gegen den mitgelieferten Mock (`--block-size`, `--no-prefix-caching`, `--ignore-cache-salt`,
`--no-cached-tokens`) sind absolute Cohens-d-Werte unrealistisch hoch, weil der Mock kaum
Zeitrauschen hat; die AUC und der Sweep sind aussagekräftig. An einem echten vLLM liegt d im
realistischen Bereich und der Lastabfall wird sichtbar.

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
