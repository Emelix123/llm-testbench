<script>
  import { onMount } from 'svelte'
  import { api } from '../lib/api.js'
  let scen = $state([])
  onMount(async () => { scen = await api.get('/api/ops/scenarios').catch(() => []) })
</script>

<h1>Anleitung</h1>
<p>Die Testplattform misst, wie gut verschiedene Konfigurationen von <b>vLLM</b> (Inferenz-Engine) und <b>LiteLLM</b> (Gateway)
  mehrere Mandanten voneinander trennen – und was das an Leistung kostet. Dafür gibt es sieben fertige Szenarien
  (K0 = ungeschützt, K1–K6 = je eine Schutzmaßnahme bzw. alle zusammen). Alles lässt sich auf der Seite
  <a href="#/steuerung">Steuerung</a> bedienen; diese Anleitung geht die Schritte der Reihe nach durch.</p>

<div class="panel steps">
  <a href="#/steuerung">1 · Prüfen</a><span>→</span><a href="#/steuerung">2 · Einrichten</a><span>→</span>
  <a href="#/steuerung">3 · Gesamtlauf</a><span>→</span><a href="#/konfigurationen">4 · Auswerten</a>
</div>

<h2>0 · Testplattform starten</h2>
<div class="panel stack">
  <div><b>Auf dem eigenen Rechner (Linux oder WSL):</b> im Ordner des Repos <code>./start.sh</code> ausführen und
    <a href="http://localhost:8080">http://localhost:8080</a> öffnen. Beenden mit Strg+C.</div>
  <div><b>Auf einem Server:</b></div>
  <pre class="code">git clone https://github.com/Emelix123/llm-testbench.git
cd llm-testbench
./start.sh --hintergrund          # läuft weiter, auch wenn die SSH-Sitzung endet</pre>
  <div>Dann auf dem <i>eigenen</i> Rechner einen SSH-Tunnel öffnen und im Browser <code>http://localhost:8080</code> aufrufen:</div>
  <pre class="code">ssh -L 8080:localhost:8080 nutzer@server</pre>
  <div class="small muted">Die Oberfläche lauscht absichtlich nur auf 127.0.0.1, denn sie kann Docker-Container starten. Beenden auf dem
    Server: <code>./start.sh --stopp</code>. Fehlt Python 3.11+, besorgt <code>start.sh</code> es selbst (über uv, ohne root).</div>
  <div><b>Voraussetzungen:</b> Docker mit Compose v2 · NVIDIA-Treiber und NVIDIA Container Toolkit · GPU mit mindestens 4 GB
    (K5 braucht 8 GB) · rund 50 GB freier Platz · Internet für den ersten Download (oder Images und Modell vorab übertragen,
    siehe <code>docs/SERVER.md</code>).</div>
</div>

<h2>1 · Systemprüfung</h2>
<div class="panel stack">
  <div>Auf <a href="#/steuerung">Steuerung</a> prüft die Plattform beim Öffnen automatisch: Docker, Compose, GPU, ob Docker die GPU in
    Container durchreicht, Images, Modell, Speicherplatz, Einrichtung und freie Ports.</div>
  <ul>
    <li><span class="c-ok">✔</span> passt · <span class="c-warn">!</span> Hinweis (z. B. Images fehlen noch – das erledigt Schritt 2) ·
      <span class="c-fail">✘</span> muss behoben werden, bevor es weitergeht. Unter jedem Punkt steht, was zu tun ist.</li>
    <li><b>Gründlich (GPU im Container)</b> startet kurz einen Container mit GPU – der sichere Nachweis, dass das
      NVIDIA Container Toolkit funktioniert. Geht erst, wenn die Images geladen sind.</li>
  </ul>
</div>

<h2>2 · Einrichten (einmalig je Rechner)</h2>
<div class="panel stack">
  <ol>
    <li><b>Einrichten</b> – erzeugt Schlüssel und Passwörter (<code>deploy/.env</code>, <code>deploy/secrets/</code>, nie im Git),
      passt die GPU-Speicherwerte an die GPU an und legt die Endpunkt-Profile mit den Keys der drei Mandanten an
      (A = Opfer, B = Angreifer, C = Last).</li>
    <li><b>Images & Modell laden</b> – rund 40 GB (vLLM allein 36 GB). Die Ausgabe zeigt den Fortschritt; danach erneut prüfen.</li>
  </ol>
  <div class="small muted">Der KV-Cache ist in allen Szenarien fest (<code>KV_CACHE_MEMORY</code> in <code>deploy/.env</code>), damit Messungen
    auf Laptop und Server vergleichbar bleiben. Wer die ganze GPU nutzen will, setzt den Wert dort leer – dann sind die Ergebnisse
    aber nicht mehr mit anderen Rechnern vergleichbar.</div>
</div>

<h2>3 · Messen</h2>
<div class="panel stack">
  <div><b>Empfohlen: Gesamtlauf.</b> Auf <a href="#/steuerung">Steuerung</a> unter „3 · Gesamtlauf“:</div>
  <ol>
    <li>Szenarien wählen – vorausgewählt sind alle, die auf diese GPU passen.</li>
    <li>Suite „Konfigurationsvergleich (je Szenario)“ lassen. Sie misst Härtung, Prefix-Cache-Seitenkanal, Leistungskosten der
      Isolation, Kosten der Cache-Trennung (Salting), Wasserzeichen und Ressourcenverdrängung (Noisy Neighbor).</li>
    <li>Wiederholungen: <b>1</b> für einen schnellen Überblick, <b>3</b> für die Zahlen der Arbeit (Mittelwert ± Streuung).</li>
    <li><b>Gesamtlauf starten.</b> Dauer: etwa 3 min Start + 15–17 min Messung je Szenario und Wiederholung – bei sechs Szenarien
      und einer Wiederholung rund 2 h, mit drei Wiederholungen rund 5 h. Der Browser darf zu; der Lauf geht weiter, solange die Testplattform selbst läuft. Den laufenden Test zeigt „Laufender Test“ im Menü.</li>
  </ol>
  <div>Ohne Browser, z. B. in <code>tmux</code> auf dem Server: <code>./deploy/scenario.sh run-all -n 3</code>
    (Auswahl: <code>-s K0,K1,K4</code>; Stand: <code>--status</code>; Abbruch: <code>--abbrechen</code>).</div>
  <div><b>Einzeln:</b> unter „Einzelnes Szenario“ eines starten, dann unter <a href="#/suiten">Suiten</a> oder <a href="#/tests">Tests</a>
    mit dem Profil „Lokal (LiteLLM, Mandant A)“ messen. Jeder Lauf wird automatisch dem laufenden Szenario zugeordnet.</div>
</div>

<h2>4 · Auswerten</h2>
<div class="panel stack">
  <ul>
    <li><a href="#/konfigurationen">Konfigurationen</a>: alle Szenarien nebeneinander – Bilanz mit Ampeln, Leistung in Δ % zu K0,
      AUC des Seitenkanals, Slowdown und Jain-Index, Tabellen je Bereich. Export als LaTeX-Tabelle (booktabs) und CSV.</li>
    <li>Nach jedem Gesamtlauf liegen die Dateien zusätzlich in <code>data/exports/&lt;Datum&gt;/</code> (Download auch auf der Seite Steuerung):
      <code>zusammenfassung.md</code> (Mittelwert ± Standardabweichung je Szenario, Δ % zu K0), <code>konfigurationsvergleich.tex</code>/<code>.csv</code>,
      <code>laeufe.csv</code> (jeder einzelne Lauf).</li>
    <li><b>Kosten:</b> Die Bilanz auf <a href="#/konfigurationen">Konfigurationen</a> zeigt neben Durchsatz und Wartezeit die
      Kosten je 1 Mio. Output-Tokens als Δ % zu K0. Der Abschnitt „Kosten der Cache-Trennung“ zeigt, was das Salting kostet:
      wie lange ein <i>neuer</i> Mandant auf eine Vorlage wartet, die ein anderer schon berechnet hat, und was bei 1 bis 30
      Mandanten mit gemeinsamer Vorlage passiert (Cache-Treffer, Wartezeit, Durchsatz, €). Die Preisannahmen (Maschine €/h,
      Strom, Auslastung) stehen unter <a href="#/einstellungen">Einstellungen</a> und gelten rückwirkend.</li>
    <li><a href="#/ergebnisse">Ergebnisse</a>: jeder Lauf mit Diagrammen und Erklärung, wie der Angriff funktioniert.</li>
  </ul>
</div>

<h2>Die Szenarien</h2>
<div class="panel tablewrap"><table>
  <thead><tr><th>ID</th><th>Konfiguration</th><th>Was sie zeigt</th></tr></thead>
  <tbody>{#each scen as s}
    <tr><td><b>{s.id}</b></td><td>{s.name}{#if !s.available}<div class="small c-warn">{s.reason}</div>{/if}</td>
      <td class="small">{s.beschreibung}{#if s.erwartung}<div><b>Erwartung:</b> {s.erwartung}</div>{/if}</td></tr>
  {/each}</tbody>
</table></div>

<h2>Gute Messungen</h2>
<div class="panel"><ul>
  <li>Während der Messung nichts anderes auf der GPU laufen lassen; Laptop am Netzteil (die Plattform verhindert Standby).</li>
  <li>Für die Arbeit 3 Wiederholungen je Szenario; alle Szenarien im selben Gesamtlauf messen (gleiche Bedingungen).</li>
  <li>Die Seite „Konfigurationen“ nimmt je Szenario den <i>neuesten</i> Lauf; mit „nur Läufe seit“ lassen sich alte Probeläufe ausblenden.</li>
  <li>Auf WSL zusätzlich die Hinweise in <code>docs/WSL-SETUP.md</code> beachten (Speicher, Energiesparplan).</li>
</ul></div>

<h2>Wenn etwas nicht klappt</h2>
<div class="panel tablewrap"><table>
  <thead><tr><th>Meldung</th><th>Lösung</th></tr></thead>
  <tbody>
    <tr><td>Szenario startet nicht, „Free memory … less than desired GPU memory utilization“ oder „out of memory“</td>
      <td>Andere GPU-Programme beenden; in der Wartung „GPU-Werte neu berechnen“.</td></tr>
    <tr><td>LiteLLM startet nicht, „password authentication failed“</td>
      <td>Gateway-DB stammt von einer früheren Einrichtung: Wartung → „Zurücksetzen“, dann erneut starten.</td></tr>
    <tr><td>„GPU im Container“ rot</td><td>NVIDIA Container Toolkit installieren:
      <code>sudo nvidia-ctk runtime configure --runtime=docker</code>, Docker neu starten.</td></tr>
    <tr><td>Port 4000/8000/8090 belegt</td><td>Anderes Programm oder fremden Container beenden (<code>docker ps</code>).</td></tr>
    <tr><td>Kosten der Cache-Trennung: „Nur 3 Mandanten-Keys vorhanden“</td>
      <td>Die Pool-Mandanten fehlen (ältere Einrichtung): „Einrichten“ erneut ausführen, dann das Szenario neu starten.</td></tr>
    <tr><td>Gesamtlauf teilweise fehlgeschlagen</td><td>Erneut starten und nur die fehlenden Szenarien wählen – ausgewertet wird je Szenario der neueste Lauf.</td></tr>
    <tr><td>„Steuerung nur lokal oder per SSH-Tunnel“</td><td>Die Seite über <code>http://localhost:8080</code> öffnen (auf Servern per SSH-Tunnel).</td></tr>
  </tbody>
</table></div>

<h2>Dasselbe im Terminal</h2>
<div class="panel"><pre class="code">./start.sh [--hintergrund | --stopp]     Testplattform starten/stoppen
./deploy/scenario.sh check                Systemprüfung
./deploy/scenario.sh init                 Einrichten
./deploy/scenario.sh pull                 Images & Modell laden
./deploy/scenario.sh run-all -n 3         Gesamtlauf (alle passenden Szenarien, 3 Wiederholungen)
./deploy/scenario.sh up K1 | down         ein Szenario starten / stoppen
./deploy/scenario.sh status | logs vllm   Zustand, Logs
./deploy/scenario.sh reset                Gateway-DB und Compile-Cache löschen</pre></div>

<style>
  .steps { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; font-weight: 600; }
  .steps span { color: var(--muted); }
  .code { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: .6rem .8rem; font-size: .82rem; overflow-x: auto; margin: 0; }
  ul, ol { margin: .2rem 0; padding-left: 1.3rem; }
  li + li { margin-top: .3rem; }
  .c-ok { color: var(--green); font-weight: 700; } .c-warn { color: var(--yellow); font-weight: 700; } .c-fail { color: var(--red); font-weight: 700; }
</style>
