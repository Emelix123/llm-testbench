# LLM-Inferenz-Testplattform

Lokales Test-Tool: zeigt per Knopfdruck, wie viele Anfragen eine vLLM-Maschine hinter LiteLLM
wirklich gleichzeitig rechnet, wie viele Nutzer sie trägt, was 1 Mio. Tokens kostet und ob die
Konfiguration stimmt. Läuft komplett auf dem Laptop – kein Server, keine Datenbank.

## Start

```bash
./start.sh          # Windows: start.bat
```

Dann <http://localhost:8080> öffnen. Voraussetzung: Python ≥ 3.11 (empfohlen 3.12; unter WSL mit
Ubuntu 22.04 ist nur 3.10 installiert → `docs/WSL-SETUP.md`).
Das gebaute Frontend liegt in `frontend/dist`; npm braucht man nur, um es neu zu bauen.

### Ohne GPU ausprobieren

```bash
./mock/start_mock.sh --slots 24 --speed 4
```

Startet einen Mock, der vLLM + LiteLLM nachbildet (feste Slots, KV-Cache, Prefill/Decode-Zeiten,
Prefix-Cache, LiteLLM-Header, Tool Calling, JSON-Schema, Stop, Seed, Logprobs; mit `--vision` Bilder,
mit `--reasoning` Denk-Teil, mit `--response-cache` ein fälschlich aktiver Antwort-Cache; `--block-size`, `--no-prefix-caching`, `--ignore-cache-salt`, `--no-cached-tokens` für den Prefix-Cache-Seitenkanal; `--salt-per-key` bildet den Mandanten-Hook des Gateways nach (Salt je API-Key, Client-Salt wird ignoriert); `--max-n` für die n-Grenze bei der Ressourcenverdrängung).
Der Mock ist bewusst einfach: Qualitätswerte gegen ihn sagen nichts aus. Endpunkt-Profil: URL `http://127.0.0.1:4000`, Modell `mock-llm`,
beliebiger Key. Der Parallelitäts-Test sollte dann 24 messen.

## Mandantentrennung messen: Szenarien K0–K6 (Bachelorarbeit)

`deploy/` enthält sieben Docker-Compose-Konfigurationen von vLLM + LiteLLM, die sich nur in den
Schutzmaßnahmen der Arbeit unterscheiden (K0 Standard … K4 voll gehärtet nach vLLM-Security-Doku,
K5 dedizierte Instanzen, K6 Wasserzeichen). Details und Befunde: [`deploy/README.md`](deploy/README.md),
WSL-Einrichtung: [`docs/WSL-SETUP.md`](docs/WSL-SETUP.md).

```bash
cd deploy && ./scenario.sh init && ./scenario.sh profile && ./scenario.sh up K0
```

Dann Suite **„Konfigurationsvergleich (je Szenario)“** mit Profil „Lokal (LiteLLM, Mandant A)“ starten,
für jedes Szenario wiederholen. Die Seite **Konfigurationen** zeigt Sicherheit und Leistung aller
Szenarien nebeneinander (Δ % zur Referenz, Diagramme) und exportiert die Tabelle als **LaTeX** (booktabs)
oder CSV. Das laufende Szenario erkennt die Testplattform selbst (`deploy/.active-scenario.json`).

### Kennzahlen der Arbeit → Test

| Kennzahl (Arbeit) | Abschnitt | Test | Feld |
|---|---|---|---|
| TTFT, TPOT als Leistungsmetriken | 1.2.2 | Leistungskosten der Isolation | `ttft_p50_ms`, `tpot_p50_ms`, Durchsatz, Cache-Treffer |
| Leistungsverlust je Gegenmaßnahme | 1.4.5 / Ausblick | Konfigurationen (Δ % zu K0) | alle Leistungswerte |
| AUC Treffer/Fehlschlag (0,5 = Raten), 95%-KI | 1.3.4, CVE-2025-46570 | Prefix-Cache-Seitenkanal | `prefix_auc`, `prefix_auc_ci` |
| Cohens d ruhig vs. unter Last (Abu Bakar: 0,78 → 0,21) | 1.3.4 | Prefix-Cache-Seitenkanal, `background_load` | `prefix_d`, `prefix_d_load` |
| Granularität = Blockgröße | 1.2.3, 1.3.4 | Prefix-Cache-Seitenkanal, Sweep | AUC/`cached_tokens` je Übereinstimmung |
| Gemeinsame Cache-Identität im Gateway (KeyPooling) | 1.3.4 | Prefix-Test mit getrennten Keys, Härtung | `prefix_separate_keys`, „Mandant B trifft …“ |
| Ressourcenverdrängung, Slowdown S_N | 1.3.4 | Ressourcenverdrängung | `slowdown_sn`, Goodput, Präfix-Verdrängung |
| Fairness-Metrik (Jain über normierte Leistung) | 1.2.5 | Ressourcenverdrängung | `jain_index` |
| n-Grenze (CVE-2026-34756) | 1.3.4 | Ressourcenverdrängung, Härtung | `n_largest_accepted` |
| Absturz/Codeausführung (CVE-2026-55514/-54234/-22778) | 1.3.4 | Härtung (Feature-Flags, Version) | Prüfpunkte |
| Interne Schnittstellen nicht erreichbar | 1.3.3 | Härtung | Engine direkt, `/vllm/*`, Header |
| Kennzeichnung ≥ 200 Tokens (KI-VO Art. 50) | 1.4.4 | Wasserzeichen | `wm_detect_rate`, `wm_fpr`, Opt-out |

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
  matrix.py         Konfigurationsvergleich K0–K6 (Kennzahl-Definitionen, neuester Lauf je Szenario)
  media.py          Testbilder
tests/<name>/       ein Ordner pro Test: manifest.json + test.py (run, summarize, estimate)
frontend/           Svelte + Vite + ECharts
mock/               Mock-Endpunkt zum Ausprobieren
deploy/             Szenarien K0–K6 (Docker Compose: vLLM + LiteLLM + Postgres), Mandanten-Hook, Detektor
docs/               WSL-Einrichtung
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
    `headline` (ein Satz Klartext), `charts`, `tables`, optional `kpis` und `matrix` (Kennzahlen für den
    Konfigurationsvergleich; Beschriftung/Richtung in `backend/app/matrix.py`). Jederzeit neu ausführbar.
  - Mandanten: `ctx.send(body, meta, tenant="angreifer")` sendet mit dem Key von Mandant B (`ctx.has_tenant()`
    prüft, ob das Profil ihn hat; sonst Haupt-Key).
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
| Sicherheit (Mandantentrennung) | Leistungskosten der Isolation | Mehrmandanten-Last (gemeinsamer + eigener System-Prompt) je Laststufe: TTFT, TPOT, Durchsatz, Cache-Treffer, 429 – Grundlage des Konfigurationsvergleichs |
| | Prefix-Cache-Seitenkanal (CVE-2025-46570) | Timing- und cached_tokens-Seitenkanal mit getrennten Mandanten-Keys; AUC/Cohens d je Einstellung (cache_salt, Caching, Blockgröße, Last) |
| | Ressourcenverdrängung / Noisy Neighbor (CVE-2026-34756) | Opfer-Latenz mit/ohne Störnachbar (eigene Keys); Slowdown S_N, Jain-Fairness, Präfix-Verdrängung; n-Grenze |
| | Härtung & Angriffsfläche | 22–28 Prüfpunkte nach vLLM-Security-Doku (je nach Szenario): Engine direkt erreichbar, `/vllm/*`-Durchreichung, Betriebsendpunkte, n, prompt_embeds, Video/SSRF, Cache-Trennung, Orakel, Header, Gateway-Verwaltung, Version/CVEs |
| | Wasserzeichen (KI-VO Art. 50) | Erkennungsrate je Länge, Fehlalarme, Opt-out, greedy (braucht den Detektor aus `deploy/`) |
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
| `granularity_sweep` | Sweep über die Präfix-Übereinstimmung: zeigt, dass Treffer nur an **Blockgrenzen** entstehen (ein Angreifer muss je einen ganzen Block raten; vLLM 0.34 nutzt standardmäßig 64 Tokens je Block, das vLLM-Paper 16) |

Der Granularitäts-Sweep reproduziert den CVE-Befund: unterhalb einer Blockgröße bleibt die AUC bei ~0,5,
ab einem vollen Block steigt sie; `cached_tokens` steigt blockweise. Gemessen an vLLM 0.34 (K0): 0–32
übereinstimmende Tokens ohne Treffer, ab 64 Tokens `cached_tokens` = 64 – die Standard-Blockgröße ist dort 64.

**Verwundbar vs. gelöst (Kriterium der Arbeit, Tab. 3.4):** Zur AUC wird ein 95%-Konfidenzintervall
per Bootstrap berechnet. Schließt das Intervall 0,5 ein, ist der Seitenkanal nicht signifikant von
Raten unterscheidbar → **nicht verwundbar (gelöst)**. Liegt es deutlich über 0,5 → **verwundbar**.
Gemessen gegen den Mock: ohne Salt AUC 1,00 (KI 1,00–1,00, verwundbar), mit `cache_salt` pro Mandant
AUC ~0,58 (KI schließt 0,5 ein, gelöst).

Mit cache_salt pro Mandant oder abgeschaltetem Prefix-Caching fällt die AUC auf ~0,5 zurück
(grün). So lässt sich belegen, ob eine Gegenmaßnahme greift – ohne und mit Last.

Beide Sicherheitstests zeigen in der Oberfläche ein Panel **„So funktioniert der Angriff"** (auf der
Test-Auswahl und im Bericht): Ablauf Schritt für Schritt mit Rollen Opfer/Angreifer, gemessenem
Signal, Maß, der Unterscheidung verwundbar/gelöst und den Originalquellen (CVE-2025-46570 /
GHSA-4qjh-9fv9-r85r, InputSnatch arXiv:2411.18191, Song u. a.; CVE-2026-34756, vLLM Security Docs).

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
- **Slowdown-Faktor S_N** (Arbeit, Gl. 6.1): Ende-zu-Ende-Verlangsamung des Opfers (E2E p95 mit/ohne
  Angriff). Mit Ratenbegrenzung je Mandant kleiner als ohne – der Vergleich zeigt, ob sie greift.
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
| Konfigurationsvergleich (je Szenario) | Härtung, Prefix-Seitenkanal, Leistungskosten, Wasserzeichen, Ressourcenverdrängung | ca. 15–20 min |
| Abnahme | alles zusammen, plus Prüfbericht als HTML und PDF in `data/reports/` | mehrere Stunden |

Den Prüfbericht gibt es für jeden Lauf auch über die Knöpfe auf der Ergebnisseite. Das PDF erzeugt
ein installierter Chrome, Edge oder Chromium im Headless-Modus (Pfad notfalls über `LLMTB_CHROME`);
ohne Browser bleibt der HTML-Bericht, der sich drucken lässt.

## Ergebnisse, Vergleich, Kosten

- Ergebnisseite: vier Kennzahlen, Karte je Test mit Ampel und Klartext, Details auf Klick, ZIP-Export.
- Kostenrechner mit Reglern für Auslastung, Lesezeit und Maschinenkosten.
- Vergleich: zwei Läufe desselben Tests nebeneinander, Diagramme überlagert, Differenzen (inkl. Sicherheits-/Leistungskennzahlen mit Δ %).
- Konfigurationen: alle Szenarien K0–K6 in einer Matrix – Bilanz (Ampel je Sicherheitstest + Leistungsverlust), Tabellen je Schutzziel, Δ-Diagramme, Export LaTeX/CSV.
- Absichtlich provozierte Ablehnungen (Grenzwerte, Kontextgrenze, Abbruch-Test) zählen nicht zur Fehlerquote.

## Endpunkt-Profil

URL von LiteLLM, Modell, Tokenizer (Hugging-Face-Name oder Pfad zu `tokenizer.json`), max. Kontext,
API-Key und die Häkchen „Vision-Modell“ und „Reasoning-Modell“ (steuern Multimodal- und Reasoning-Prüfung).

Für die Sicherheitstests zusätzlich (aufklappbar „Sicherheitstests“): API-Keys für **Mandant B (Angreifer)**
und **Mandant C (Last)** – der Haupt-Key ist Mandant A (Opfer) –, die direkte Engine-URL (sollte nicht
erreichbar sein) und die URL des Wasserzeichen-Detektors. `deploy/scenario.sh profile` füllt das aus.
Mit getrennten Keys prüfen die Tests die Trennung **im Gateway**; mit einem Key nur die Engine.

## vLLM- und LiteLLM-Einstellungen je Test

Jeder Test nennt die Stellschrauben an vLLM (Inferenz-Engine) und LiteLLM (Gateway), die sein
Ergebnis beeinflussen. In der Oberflaeche steht die Liste je Test unter „Einstellungen“ (Test-Auswahl
und Bericht). vLLM-Flags sind versionsabhaengig – vor dem Einsatz die Doku der eingesetzten Version pruefen.

### Leistung & Kapazität

**Schnellcheck**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--enable-prefix-caching / --no-enable-prefix-caching` | Prefix-Cache an/aus (in neuem vLLM meist standardmäßig an). |
| vLLM | `--enable-prompt-tokens-details` | Liefert prompt_tokens_details.cached_tokens im usage; sonst fehlt der Cache-Wert. |
| vLLM | `--max-model-len` | Beworbene/erzwungene Kontextlänge. |
| LiteLLM | `x-litellm-* Antwort-Header` | Overhead, Retries, Fallbacks, Rate-Limits – vom Tool ausgelesen. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Rate-/Parallelitäts-Grenzen je Mandant; 429 vom Gateway statt GPU-Limit. |
| LiteLLM | `litellm_settings.drop_params / additional_drop_params` | Ob unbekannte Parameter (ignore_eos, min_tokens, cache_salt) verworfen oder an vLLM durchgereicht werden. |
| LiteLLM | `litellm_settings.cache / cache_params` | Antwort-Cache von LiteLLM – sollte für Messungen AUS sein, sonst verfälscht er TTFT und Qualität. |
| LiteLLM | `stream_options.include_usage-Passthrough` | Muss durchgereicht werden, damit usage im Stream ankommt. |

**Parallelitäts-Test**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--max-num-batched-tokens` | Tokens je Scheduler-Schritt; begrenzt gleichzeitiges Prefill. |
| vLLM | `--gpu-memory-utilization` | Anteil GPU-Speicher für den KV-Cache → wie viele/lange Anfragen gleichzeitig passen. |
| vLLM | `--enable-chunked-prefill` | Prefill großer Prompts in Stücken; glättet TTFT unter Last. |
| vLLM | `--kv-cache-dtype` | z. B. fp8 vergrößert den KV-Cache → mehr Parallelität. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Rate-/Parallelitäts-Grenzen je Mandant; 429 vom Gateway statt GPU-Limit. |
| LiteLLM | `num_retries, fallbacks, allowed_fails, cooldown_time` | Stille Wiederholungen/Fallbacks (x-litellm-attempted-retries/-fallbacks). |

**Kapazitäts-Sweep**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--max-num-batched-tokens` | Tokens je Scheduler-Schritt; begrenzt gleichzeitiges Prefill. |
| vLLM | `--gpu-memory-utilization` | Anteil GPU-Speicher für den KV-Cache → wie viele/lange Anfragen gleichzeitig passen. |
| vLLM | `--enable-chunked-prefill` | Prefill großer Prompts in Stücken; glättet TTFT unter Last. |
| vLLM | `--scheduling-policy {fcfs,priority}` | Planungsstrategie; fcfs lässt die Warteschlange bei Überlast wachsen. |
| vLLM | `--max-model-len` | Beworbene/erzwungene Kontextlänge. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Rate-/Parallelitäts-Grenzen je Mandant; 429 vom Gateway statt GPU-Limit. |
| LiteLLM | `litellm_settings.drop_params / additional_drop_params` | Ob unbekannte Parameter (ignore_eos, min_tokens, cache_salt) verworfen oder an vLLM durchgereicht werden. |
| LiteLLM | `request_timeout / timeout` | Timeout je Anfrage im Gateway (wichtig bei langen Prefills/Überlast). |

**Rate-Test (offene Last)**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--max-num-batched-tokens` | Tokens je Scheduler-Schritt; begrenzt gleichzeitiges Prefill. |
| vLLM | `--scheduling-policy {fcfs,priority}` | Planungsstrategie; fcfs lässt die Warteschlange bei Überlast wachsen. |
| vLLM | `--gpu-memory-utilization` | Anteil GPU-Speicher für den KV-Cache → wie viele/lange Anfragen gleichzeitig passen. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Rate-/Parallelitäts-Grenzen je Mandant; 429 vom Gateway statt GPU-Limit. |
| LiteLLM | `request_timeout / timeout` | Timeout je Anfrage im Gateway (wichtig bei langen Prefills/Überlast). |

**Kontextlängen-Matrix**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-model-len` | Beworbene/erzwungene Kontextlänge. |
| vLLM | `--rope-scaling` | Kontextverlängerung über die native Länge hinaus. |
| vLLM | `--enable-chunked-prefill` | Prefill großer Prompts in Stücken; glättet TTFT unter Last. |
| vLLM | `--max-num-batched-tokens` | Tokens je Scheduler-Schritt; begrenzt gleichzeitiges Prefill. |
| vLLM | `--block-size` | Tokens je KV-Block (z. B. 16); bestimmt die Granularität von Cache-Treffern. |
| LiteLLM | `request_timeout / timeout` | Timeout je Anfrage im Gateway (wichtig bei langen Prefills/Überlast). |
| LiteLLM | `context_window_fallbacks` | Ausweichmodell bei zu langem Kontext. |

**Cache-Nutzen**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--enable-prefix-caching / --no-enable-prefix-caching` | Prefix-Cache an/aus (in neuem vLLM meist standardmäßig an). |
| vLLM | `--block-size` | Tokens je KV-Block (z. B. 16); bestimmt die Granularität von Cache-Treffern. |
| vLLM | `--enable-prompt-tokens-details` | Liefert prompt_tokens_details.cached_tokens im usage; sonst fehlt der Cache-Wert. |
| vLLM | `--gpu-memory-utilization` | Anteil GPU-Speicher für den KV-Cache → wie viele/lange Anfragen gleichzeitig passen. |
| LiteLLM | `litellm_settings.cache / cache_params` | Antwort-Cache von LiteLLM – sollte für Messungen AUS sein, sonst verfälscht er TTFT und Qualität. |
| LiteLLM | `cache_salt-Passthrough (Hook)` | Salt verhindert Cache-Treffer; für den Nutzen-Test ohne Salt messen. |

**Kosten je Token-Art**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--enable-prefix-caching / --no-enable-prefix-caching` | Prefix-Cache an/aus (in neuem vLLM meist standardmäßig an). |
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--enable-chunked-prefill` | Prefill großer Prompts in Stücken; glättet TTFT unter Last. |
| vLLM | `--kv-cache-dtype` | z. B. fp8 vergrößert den KV-Cache → mehr Parallelität. |
| LiteLLM | `litellm_settings.drop_params / additional_drop_params` | Ob unbekannte Parameter (ignore_eos, min_tokens, cache_salt) verworfen oder an vLLM durchgereicht werden. |

### Konfiguration & Funktionen

**Konfiguration & Funktionen**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--enable-auto-tool-choice + --tool-call-parser {hermes,mistral,llama3_json,…}` | Tool Calling: Parser passend zum Modell. |
| vLLM | `--guided-decoding-backend {auto,xgrammar,guidance,outlines}` | Structured Output: erzwingt gültiges JSON nach Schema. |
| vLLM | `--reasoning-parser {deepseek_r1,qwen3,…}` | Trennt den Denk-Teil in reasoning_content. |
| vLLM | `--chat-template / --chat-template-content-format` | Chat-Template: System-Prompt und Verlauf. |
| vLLM | `--max-model-len` | Beworbene/erzwungene Kontextlänge. |
| vLLM | `--limit-mm-per-prompt` | Bilder je Prompt (Multimodal). |
| vLLM | `--seed, --max-logprobs` | Determinismus bei temperature 0 bzw. logprobs. |
| LiteLLM | `litellm_settings.drop_params / additional_drop_params` | Ob unbekannte Parameter (ignore_eos, min_tokens, cache_salt) verworfen oder an vLLM durchgereicht werden. |
| LiteLLM | `litellm_settings.cache / cache_params` | Antwort-Cache von LiteLLM – sollte für Messungen AUS sein, sonst verfälscht er TTFT und Qualität. |
| LiteLLM | `allowed_openai_params` | Erlaubt ignore_eos/min_tokens/cache_salt in der Durchreichung. |
| LiteLLM | `--served-model-name / /key/info` | Steckbrief: Modellname und Key-Limits. |

### Stabilität & Betrieb

**Dauerlast**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--gpu-memory-utilization` | Anteil GPU-Speicher für den KV-Cache → wie viele/lange Anfragen gleichzeitig passen. |
| vLLM | `--scheduling-policy {fcfs,priority}` | Planungsstrategie; fcfs lässt die Warteschlange bei Überlast wachsen. |
| vLLM | `--preemption-mode {recompute,swap}` | Wie vLLM bei Speichermangel unterbrochene Anfragen behandelt. |
| vLLM | `--swap-space` | CPU-Auslagerung für den KV-Cache. |
| vLLM | `--max-num-queued-reqs` | Begrenzt wartende + laufende Anfragen zusammen. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Rate-/Parallelitäts-Grenzen je Mandant; 429 vom Gateway statt GPU-Limit. |
| LiteLLM | `request_timeout / timeout` | Timeout je Anfrage im Gateway (wichtig bei langen Prefills/Überlast). |
| LiteLLM | `num_retries, fallbacks, allowed_fails, cooldown_time` | Stille Wiederholungen/Fallbacks (x-litellm-attempted-retries/-fallbacks). |

**Lastspitze**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--gpu-memory-utilization` | Anteil GPU-Speicher für den KV-Cache → wie viele/lange Anfragen gleichzeitig passen. |
| vLLM | `--scheduling-policy {fcfs,priority}` | Planungsstrategie; fcfs lässt die Warteschlange bei Überlast wachsen. |
| vLLM | `--preemption-mode {recompute,swap}` | Wie vLLM bei Speichermangel unterbrochene Anfragen behandelt. |
| vLLM | `--max-num-queued-reqs` | Begrenzt wartende + laufende Anfragen zusammen. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Rate-/Parallelitäts-Grenzen je Mandant; 429 vom Gateway statt GPU-Limit. |
| LiteLLM | `request_timeout / timeout` | Timeout je Anfrage im Gateway (wichtig bei langen Prefills/Überlast). |
| LiteLLM | `num_retries, fallbacks, allowed_fails, cooldown_time` | Stille Wiederholungen/Fallbacks (x-litellm-attempted-retries/-fallbacks). |

**Überlast**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--gpu-memory-utilization` | Anteil GPU-Speicher für den KV-Cache → wie viele/lange Anfragen gleichzeitig passen. |
| vLLM | `--scheduling-policy {fcfs,priority}` | Planungsstrategie; fcfs lässt die Warteschlange bei Überlast wachsen. |
| vLLM | `--preemption-mode {recompute,swap}` | Wie vLLM bei Speichermangel unterbrochene Anfragen behandelt. |
| vLLM | `--max-num-queued-reqs` | Begrenzt wartende + laufende Anfragen zusammen. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Rate-/Parallelitäts-Grenzen je Mandant; 429 vom Gateway statt GPU-Limit. |
| LiteLLM | `request_timeout / timeout` | Timeout je Anfrage im Gateway (wichtig bei langen Prefills/Überlast). |
| LiteLLM | `num_retries, fallbacks, allowed_fails, cooldown_time` | Stille Wiederholungen/Fallbacks (x-litellm-attempted-retries/-fallbacks). |

**Grenzwerte**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `VLLM_MAX_N_SEQUENCES (Umgebungsvariable)` | Obergrenze für n (Standard 16384; Empfehlung öffentlich 64–128). |
| vLLM | `--max-model-len` | Beworbene/erzwungene Kontextlänge. |
| vLLM | `--limit-mm-per-prompt` | Bilder je Prompt. |
| vLLM | `--max-num-batched-tokens` | Tokens je Scheduler-Schritt; begrenzt gleichzeitiges Prefill. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Rate-/Parallelitäts-Grenzen je Mandant; 429 vom Gateway statt GPU-Limit. |
| LiteLLM | `litellm_settings.drop_params / additional_drop_params` | Ob unbekannte Parameter (ignore_eos, min_tokens, cache_salt) verworfen oder an vLLM durchgereicht werden. |

**Kaltstart**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--load-format` | Format/Tempo des Modell-Ladens. |
| vLLM | `--enforce-eager` | Ohne CUDA-Graph-Aufwärmung – erste Anfragen langsamer. |
| vLLM | `--enable-prefix-caching / --no-enable-prefix-caching` | Prefix-Cache an/aus (in neuem vLLM meist standardmäßig an). |
| LiteLLM | `request_timeout / timeout` | Timeout je Anfrage im Gateway (wichtig bei langen Prefills/Überlast). |

### Qualität

**Qualität: eigene Kundenaufgaben**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--quantization {awq,gptq,fp8,…}` | Quantisierung – Hauptverdächtiger für Qualitätsverlust. |
| vLLM | `--dtype` | Rechen-Datentyp. |
| vLLM | `--kv-cache-dtype` | z. B. fp8 vergrößert den KV-Cache → mehr Parallelität. |
| vLLM | `--seed` | Reproduzierbarkeit. |
| LiteLLM | `litellm_settings.cache / cache_params` | Antwort-Cache von LiteLLM – sollte für Messungen AUS sein, sonst verfälscht er TTFT und Qualität. |

**Qualität: Konsistenz unter Last**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--enable-chunked-prefill` | Prefill großer Prompts in Stücken; glättet TTFT unter Last. |
| vLLM | `--seed` | Determinismus. |
| vLLM | `--kv-cache-dtype` | z. B. fp8 vergrößert den KV-Cache → mehr Parallelität. |
| LiteLLM | `litellm_settings.cache / cache_params` | Antwort-Cache von LiteLLM – sollte für Messungen AUS sein, sonst verfälscht er TTFT und Qualität. |

**Qualität: Tool Calling (BFCL-Stil)**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--enable-auto-tool-choice + --tool-call-parser` | Tool Calling korrekt konfiguriert. |
| vLLM | `--quantization {awq,gptq,fp8,…}` | Quantisierung – Hauptverdächtiger für Qualitätsverlust. |
| LiteLLM | `litellm_settings.drop_params / additional_drop_params` | Ob unbekannte Parameter (ignore_eos, min_tokens, cache_salt) verworfen oder an vLLM durchgereicht werden. |
| LiteLLM | `litellm_settings.cache / cache_params` | Antwort-Cache von LiteLLM – sollte für Messungen AUS sein, sonst verfälscht er TTFT und Qualität. |

**Qualität: lm-evaluation-harness**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--quantization {awq,gptq,fp8,…}` | Quantisierung – Hauptverdächtiger für Qualitätsverlust. |
| vLLM | `--max-model-len` | Beworbene/erzwungene Kontextlänge. |
| vLLM | `--dtype` | Rechen-Datentyp. |
| LiteLLM | `litellm_settings.cache / cache_params` | Antwort-Cache von LiteLLM – sollte für Messungen AUS sein, sonst verfälscht er TTFT und Qualität. |

**Qualität: LongBench v2 (Langkontext)**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--max-model-len` | Beworbene/erzwungene Kontextlänge. |
| vLLM | `--rope-scaling` | Langkontext über native Länge. |
| vLLM | `--quantization {awq,gptq,fp8,…}` | Quantisierung – Hauptverdächtiger für Qualitätsverlust. |
| LiteLLM | `litellm_settings.cache / cache_params` | Antwort-Cache von LiteLLM – sollte für Messungen AUS sein, sonst verfälscht er TTFT und Qualität. |
| LiteLLM | `request_timeout / timeout` | Timeout je Anfrage im Gateway (wichtig bei langen Prefills/Überlast). |

### Sicherheit (Mandantentrennung)

**Prefix-Cache-Seitenkanal (CVE-2025-46570)**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `--enable-prefix-caching / --no-enable-prefix-caching` | Die Schwachstelle selbst; Abschalten schließt den Seitenkanal (kostet aber den Cache-Nutzen). |
| vLLM | `--block-size` | Tokens je KV-Block (z. B. 16); bestimmt die Granularität von Cache-Treffern. |
| vLLM | `--enable-prompt-tokens-details` | Schaltet den cached_tokens-Oracle frei; Weglassen entzieht dem Angreifer dieses direkte Signal. |
| vLLM | `cache_salt (Request-Feld, ab vLLM 0.9.0)` | Fix: nur Anfragen mit gleichem Salt teilen Blöcke. |
| LiteLLM | `async_pre_call_hook` | Setzt cache_salt je Mandant – die eigentliche Gegenmaßnahme im Gateway. |
| LiteLLM | `allowed_openai_params / drop_params` | Entscheidet, ob cache_salt bis vLLM durchkommt (vgl. CVE-2026-105752). |
| LiteLLM | `Positivliste weitergeleiteter Endpunkte` | Den Präfix-Cache-Zählerendpunkt von vLLM NICHT weiterleiten. |

**Ressourcenverdrängung / Noisy Neighbor (CVE-2026-34756)**

| Ebene | Einstellung | Wirkung |
|---|---|---|
| vLLM | `VLLM_MAX_N_SEQUENCES` | Obergrenze für n (Standard 16384; Empfehlung 64–128) – gilt global, nicht je Mandant. |
| vLLM | `--max-num-seqs` | Max. gleichzeitig laufende Sequenzen = die echte Parallelität. |
| vLLM | `--max-num-batched-tokens` | Tokens je Scheduler-Schritt; begrenzt gleichzeitiges Prefill. |
| vLLM | `--gpu-memory-utilization` | Anteil GPU-Speicher für den KV-Cache → wie viele/lange Anfragen gleichzeitig passen. |
| vLLM | `--scheduling-policy {fcfs,priority}` | Planungsstrategie; fcfs lässt die Warteschlange bei Überlast wachsen. |
| vLLM | `--max-num-queued-reqs` | Begrenzt wartende + laufende Anfragen zusammen. |
| vLLM | `--preemption-mode {recompute,swap}` | Wie vLLM bei Speichermangel unterbrochene Anfragen behandelt. |
| LiteLLM | `Key/Team: rpm, tpm, max_parallel_requests` | Ratenbegrenzung je Mandant – die eigentliche Gegenmaßnahme. |
| LiteLLM | `max_budget, budget_duration` | Budget je Mandant. |
| LiteLLM | `gemeinsamer Zähler über Redis` | Sonst zählt jede Gateway-Instanz für sich und die Grenze greift nicht global. |

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
