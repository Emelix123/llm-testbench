<script>
  import { onMount } from 'svelte'
  import { api, dur, dt } from '../lib/api.js'
  import Light from '../lib/Light.svelte'

  let check = $state(null), checking = $state(false), checkErr = $state('')
  let scen = $state([]), st = $state(null), job = $state(null), g = $state(null), status = $state(null)
  let suites = $state([]), exportsList = $state([])
  let sel = $state('K0'), err = $state(''), glErr = $state('')
  let glSel = $state({}), reps = $state(1), suite = $state('konfigurationsvergleich'), keep = $state(false)
  let suiteS = $state(null), logSvc = $state(''), logText = $state(''), consoleEl = $state(null)
  let lastJobState = null, timer = null

  const SYM = { ok: '✔', warn: '!', fail: '✘', info: '·' }
  const busy = $derived(job?.state === 'laeuft')
  const glRunning = $derived(g?.state === 'laeuft')
  const testRunning = $derived(!!status?.running && !glRunning)
  const locked = $derived(busy || glRunning || testRunning)
  const S = $derived(scen.find((s) => s.id === sel))
  const chosen = $derived(scen.filter((s) => glSel[s.id] && s.available).map((s) => s.id))
  const eta = $derived(suiteS != null ? chosen.length * (180 + reps * suiteS) : null)
  const active = $derived(st?.active)

  async function runCheck(gpu = false) {
    checking = true; checkErr = ''
    try { check = await api.get('/api/ops/check' + (gpu ? '?gpu=1' : '')) } catch (e) { checkErr = e.message } finally { checking = false }
  }
  async function poll() {
    clearTimeout(timer)
    try {
      ;[job, g, st, status] = await Promise.all([api.get('/api/ops/job?tail=500'), api.get('/api/gesamtlauf'), api.get('/api/ops/status'), api.get('/api/status')])
      if (lastJobState === 'laeuft' && job?.state !== 'laeuft' && ['init', 'pull', 'gpu', 'reset'].includes(job?.action)) runCheck()
      if (lastJobState === 'laeuft' && job?.state !== 'laeuft') loadExports()
      lastJobState = job?.state
      setTimeout(() => consoleEl && (consoleEl.scrollTop = consoleEl.scrollHeight), 0)
    } catch {}
    timer = setTimeout(poll, busy || glRunning || status?.running ? 2000 : 8000)
  }
  async function loadExports() { exportsList = await api.get('/api/exports').catch(() => []) }
  async function estimate() {
    const s = suites.find((x) => x.id === suite)
    suiteS = s ? (await api.post('/api/estimate', { items: s.items })).total_s : null
  }
  onMount(() => {
    runCheck()
    ;(async () => {
      ;[scen, suites] = await Promise.all([api.get('/api/ops/scenarios'), api.get('/api/suites')])
      glSel = Object.fromEntries(scen.map((s) => [s.id, s.available]))
      estimate()
    })()
    loadExports()
    poll()
    return () => clearTimeout(timer)
  })

  async function act(action, extra = {}) {
    err = ''
    if (action === 'reset' && !confirm('Zurücksetzen stoppt alles und löscht die Gateway-Datenbank (Mandanten-Keys werden beim nächsten Start neu angelegt) und den Compile-Cache. Messergebnisse bleiben. Fortfahren?')) return
    try { job = await api.post('/api/ops/run', { action, ...extra, confirm: action === 'reset' }); lastJobState = 'laeuft'; poll() } catch (e) { err = e.message }
  }
  async function startGl() {
    glErr = ''
    if (!chosen.length) { glErr = 'Bitte mindestens ein Szenario wählen.'; return }
    try { g = await api.post('/api/gesamtlauf', { scenarios: chosen, repetitions: Number(reps), suite, keep_running: keep }); poll() } catch (e) { glErr = e.message }
  }
  async function cancelGl() { if (confirm('Gesamtlauf abbrechen? Der laufende Test wird beendet; fertige Messungen bleiben erhalten.')) { await api.post('/api/gesamtlauf/cancel'); poll() } }
  async function showLogs(svc) {
    logSvc = svc; logText = 'Lade …'
    try { logText = await (await fetch('/api/ops/logs?service=' + svc + '&tail=300')).text() } catch (e) { logText = e.message }
  }
  const stepCls = (s) => ({ fertig: 'ok', teilweise: 'warn', fehler: 'fail', abgebrochen: 'warn', startet: 'run', misst: 'run' })[s] || ''
  const elapsed = (from) => (from ? (Date.now() - new Date(from).getTime()) / 1000 : 0)
</script>

<div class="row"><h1 class="grow">Steuerung</h1><a class="btn" href="#/anleitung">Anleitung</a></div>
<p class="muted">Szenarien K0–K6 starten, messen und auswerten – ohne Terminal. Jeder Knopf führt <code>deploy/scenario.sh</code>
  aus; die Ausgabe erscheint unten. Neu hier? Die <a href="#/anleitung">Anleitung</a> geht alles Schritt für Schritt durch.</p>

{#if glRunning}<div class="notice">Gesamtlauf läuft – Szenarien und Tests werden automatisch gesteuert. Manuelle Befehle sind solange gesperrt.</div>
{:else if testRunning}<div class="notice">Ein Test läuft (<a href="#/live">ansehen</a>). Szenario-Befehle sind gesperrt, damit die Messung nicht verfälscht wird.</div>{/if}

<!-- ------------------------------------------------------------------ 1 Prüfen -->
<h2>1 · Systemprüfung</h2>
<div class="panel stack">
  <div class="row">
    <div class="grow">{#if check}<b class={check.ok ? 'c-ok' : 'c-fail'}>{check.ok ? 'Bereit' : 'Nicht bereit'}</b> <span class="muted small">· {check.summary} · geprüft {dt(check.checked_at)}</span>{:else if checking}Prüfe … (Docker, GPU, Images – dauert einige Sekunden){/if}</div>
    <button onclick={() => runCheck()} disabled={checking}>Erneut prüfen</button>
    <button onclick={() => runCheck(true)} disabled={checking} title="Startet kurz einen Container mit GPU – sicherer Nachweis, dass Docker die GPU sieht">Gründlich (GPU im Container)</button>
  </div>
  {#if checkErr}<div class="err small">{checkErr}</div>{/if}
  {#if check}
    <div class="tablewrap"><table>
      <tbody>{#each check.items as it}
        <tr><td class="sym c-{it.status}">{SYM[it.status]}</td><td><b>{it.label}</b></td>
          <td>{it.detail}{#if it.fix && it.status !== 'ok'}<div class="small fix">→ {it.fix}</div>{/if}</td></tr>
      {/each}</tbody>
    </table></div>
  {/if}
</div>

<!-- ------------------------------------------------------------------ 2 Einrichten -->
<h2>2 · Einrichten <span class="muted small">(einmalig je Rechner)</span></h2>
<div class="panel grid2">
  <div class="stack">
    <div><b>Schlüssel, GPU-Werte, Profile</b> {#if check?.init_done && check?.profiles_done}<span class="c-ok">✔ erledigt</span>{/if}</div>
    <div class="small muted">Erzeugt <code>deploy/.env</code> mit Zufallsschlüsseln, die Geheimnisse in <code>deploy/secrets/</code>,
      passt die GPU-Speicherwerte an diese GPU an und legt die Endpunkt-Profile samt Mandanten-Keys an. Bestehende Schlüssel bleiben unverändert.</div>
    <div><button class="primary" onclick={() => act('init')} disabled={locked}>Einrichten</button></div>
  </div>
  <div class="stack">
    <div><b>Images & Modell laden</b> {#if check && !check.images_missing?.length && check.model_cached}<span class="c-ok">✔ vorhanden</span>{/if}</div>
    <div class="small muted">vLLM (~36 GB), LiteLLM, Postgres und das Modell (Qwen3-8B ~16 GB; auf kleinen GPUs Qwen3-0.6B ~1,5 GB). Einmalig; je nach Leitung 10–30 min.
      {#if check?.images_missing?.length}<br />Fehlt: {check.images_missing.join(', ')}{/if}</div>
    <div><button onclick={() => act('pull')} disabled={locked}>Images & Modell laden</button></div>
  </div>
</div>

<!-- ------------------------------------------------------------------ 3b Gesamtlauf -->
<h2>3 · Gesamtlauf <span class="muted small">(empfohlen: alle Szenarien automatisch nacheinander)</span></h2>
<div class="panel stack">
  <div class="small muted">Für jedes gewählte Szenario: starten → Suite messen (so oft wie angegeben) → nächstes Szenario. Am Ende
    wird gestoppt und exportiert (CSV, LaTeX, Zusammenfassung mit Mittelwert ± Streuung). Du kannst das Browserfenster schließen – der Lauf geht weiter,
    solange die Testplattform läuft (auf Servern: <code>./start.sh --hintergrund</code>).</div>
  <div class="scgrid">
    {#each scen as s}
      <label class="sc" class:off={!s.available} title={s.beschreibung}>
        <input type="checkbox" bind:checked={glSel[s.id]} disabled={!s.available || glRunning} />
        <span><b>{s.id}</b> {s.name}{#if !s.available}<span class="small c-warn"> – {s.reason}</span>{/if}</span>
      </label>
    {/each}
  </div>
  <div class="grid2">
    <label class="field"><span>Suite je Szenario</span>
      <select bind:value={suite} onchange={estimate} disabled={glRunning}>{#each suites as s}<option value={s.id}>{s.name}</option>{/each}</select></label>
    <label class="field"><span>Wiederholungen je Szenario (für Mittelwert ± Streuung: 3)</span>
      <input type="number" min="1" max="10" bind:value={reps} disabled={glRunning} /></label>
    <label class="field chk"><input type="checkbox" bind:checked={keep} disabled={glRunning} /> <span>letztes Szenario danach laufen lassen</span></label>
  </div>
  <div class="row">
    <button class="primary big" onclick={startGl} disabled={locked || !chosen.length}>Gesamtlauf starten</button>
    <span class="muted small">{`${chosen.length} Szenario(s)${eta ? ' · geschätzt ' + dur(eta) : ''}`}</span>
    {#if glErr}<span class="err small">{glErr}</span>{/if}
  </div>

  {#if g}
    <div class="glbox stack">
      <div class="row"><b class="grow">{`Gesamtlauf ${g.id} · ${g.state === 'laeuft' ? 'läuft' : g.state}${g.phase ? ' · ' + g.phase : ''}`}</b>
        {#if glRunning}<span class="small muted">{`seit ${dur(elapsed(g.started_at))} von geschätzt ${dur(g.estimate_s)}`}</span>
          <button class="danger" onclick={cancelGl}>Abbrechen</button>{/if}</div>
      <div class="tablewrap"><table>
        <thead><tr><th>Szenario</th><th>Status</th><th>Stand</th><th>Ergebnisse</th></tr></thead>
        <tbody>{#each g.steps as s}
          <tr><td><b>{s.scenario}</b></td><td class="c-{stepCls(s.status)}">{s.status}</td><td class="small">{s.text}
            {#if s.error_tail}<pre class="tail">{s.error_tail.join('\n')}</pre>{/if}</td>
            <td>{#each s.runs as r}{#if r.run_id}<a href={'#/bericht?runs=' + r.run_id} title={r.name || r.test}><Light verdict={r.verdict} /></a>{/if}{/each}</td></tr>
        {/each}</tbody>
      </table></div>
      {#if g.batch}
        <div class="small">{#each g.batch.runs as r}
          <div class="row"><span class="tname">{r.name || r.test}</span><div class="bar grow"><div style="width:{Math.round((r.progress || 0) * 100)}%"></div></div><span class="muted">{r.text}</span></div>
        {/each}</div>
      {/if}
      {#if g.error}<div class="err small">{g.error}</div>{/if}
      {#if g.export}
        <div class="small"><b>Ergebnisse</b> in <code>{g.export.dir}</code>:
          {#each g.export.files as f}<a class="dl" href={`/api/exports/${g.export.id}/${f}`}>{f}</a>{/each}
          · <a href="#/konfigurationen">Seite „Konfigurationen“</a></div>
      {/if}
    </div>
  {/if}
</div>

<!-- ------------------------------------------------------------------ 3a einzeln -->
<h2>Einzelnes Szenario <span class="muted small">(zum Ausprobieren oder für eigene Tests)</span></h2>
<div class="panel stack">
  <div class="row">
    <label class="field grow"><span>Szenario</span>
      <select bind:value={sel}>{#each scen as s}<option value={s.id} disabled={!s.available}>{s.id} – {s.name}{s.available ? '' : ' (' + s.reason + ')'}</option>{/each}</select></label>
    <button class="primary" onclick={() => act('up', { scenario: sel })} disabled={locked || !S?.available}>Starten</button>
    <button onclick={() => act('down')} disabled={locked}>Stoppen</button>
  </div>
  {#if S}<div class="small"><b>{S.titel}</b><div class="muted">{S.beschreibung}</div>
    {#if S.erwartung}<div><b>Erwartung:</b> {S.erwartung}</div>{/if}
    {#if S.massnahmen?.length}<div class="muted">Maßnahmen: {S.massnahmen.join(', ').replace(/_/g, ' ')}</div>{/if}</div>{/if}
  <div class="small">
    <b>Läuft gerade:</b> {active?.id ? `${active.id} – ${active.name} (seit ${dt(active.started_at)})`
      : busy && job?.action === 'up' ? 'wird gestartet …' : 'kein Szenario'}
    {#if st?.docker_error}<span class="err"> · Docker: {st.docker_error}</span>{/if}
  </div>
  {#if st?.containers?.length || st?.detector?.length}
    <div class="tablewrap"><table>
      <thead><tr><th>Dienst</th><th>Zustand</th><th>Ports</th><th></th></tr></thead>
      <tbody>{#each [...(st.containers || []), ...(st.detector || [])] as c}
        <tr><td>{c.service}</td><td class="c-{c.health === 'healthy' || (c.state === 'running' && !c.health) ? 'ok' : c.state === 'running' ? 'run' : 'warn'}">{c.status}</td>
          <td class="small">{c.ports}</td><td><button class="link" onclick={() => showLogs(c.service)}>Logs</button></td></tr>
      {/each}</tbody>
    </table></div>
  {/if}
  {#if active?.id}<div class="small muted">Jetzt unter <a href="#/tests">Tests</a> oder <a href="#/suiten">Suiten</a> mit dem Profil
    „Lokal (LiteLLM, Mandant A)“ messen. Das Szenario wird jedem Lauf automatisch zugeordnet.</div>{/if}
  {#if logSvc}<div><div class="row"><b class="grow">Logs: {logSvc}</b><button class="link" onclick={() => showLogs(logSvc)}>neu laden</button>
    <button class="link" onclick={() => (logSvc = '')}>schließen</button></div><pre class="console">{logText}</pre></div>{/if}
</div>

<!-- ------------------------------------------------------------------ Ausgabe -->
<h2>Ausgabe</h2>
<div class="panel stack">
  {#if job}
    <div class="row"><b class="grow">{job.label} · <span class="c-{job.state === 'fertig' ? 'ok' : job.state === 'laeuft' ? 'run' : 'fail'}">{job.state === 'laeuft' ? 'läuft …' : job.state}</span></b>
      <span class="small muted">{dt(job.started_at)}</span>
      {#if busy}<button class="danger" onclick={() => act('cancel')}>Befehl abbrechen</button>{/if}</div>
    <pre class="console" bind:this={consoleEl}>{job.lines.join('\n')}</pre>
  {:else}<span class="muted small">Noch kein Befehl ausgeführt.</span>{/if}
  {#if err}<div class="err small">{err}</div>{/if}
</div>

<!-- ------------------------------------------------------------------ Exporte -->
{#if exportsList.length}
  <h2>Frühere Gesamtläufe</h2>
  <div class="panel tablewrap"><table>
    <thead><tr><th>Gesamtlauf</th><th>Szenarien</th><th>Status</th><th>Dateien</th></tr></thead>
    <tbody>{#each exportsList as e}
      <tr><td>{dt(e.started_at)}<div class="small muted">{e.id}</div></td><td class="small">{(e.scenarios || []).join(', ') + (e.repetitions ? ` · ${e.repetitions}×` : '')}</td>
        <td class="small">{e.state || '–'}</td>
        <td class="small">{#each e.files as f}<a class="dl" href={`/api/exports/${e.id}/${f}`}>{f}</a>{/each}</td></tr>
    {/each}</tbody>
  </table></div>
{/if}

<!-- ------------------------------------------------------------------ Wartung -->
<h2>Wartung</h2>
<div class="panel row">
  <button onclick={() => act('detector-up')} disabled={locked} title="Wasserzeichen-Detektor allein starten (für Fehlalarm-Messungen in K0)">Detektor starten</button>
  <button onclick={() => act('detector-down')} disabled={locked}>Detektor stoppen</button>
  <button onclick={() => act('gpu')} disabled={locked} title="GPU-Speicherwerte in deploy/.env neu aus dem VRAM berechnen">GPU-Werte neu berechnen</button>
  <span class="grow"></span>
  <button class="danger" onclick={() => act('reset')} disabled={locked}>Zurücksetzen …</button>
</div>

<style>
  .sym { width: 1.4rem; font-weight: 700; text-align: center; }
  .c-ok { color: var(--green); } .c-warn { color: var(--yellow); } .c-fail { color: var(--red); } .c-info { color: var(--muted); }
  .c-run { color: var(--accent); font-weight: 600; }
  .fix { color: var(--muted); margin-top: .15rem; }
  .scgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: .3rem .8rem; }
  .sc { display: flex; gap: .45rem; align-items: flex-start; padding: .25rem 0; cursor: pointer; }
  .sc.off { color: var(--muted); cursor: not-allowed; }
  .chk { display: flex; gap: .45rem; align-items: center; align-self: end; padding-bottom: .5rem; }
  .chk span { margin: 0 !important; color: var(--text) !important; font-size: .9rem !important; }
  .glbox { border-top: 1px solid var(--line); padding-top: .8rem; }
  .tname { width: 14rem; }
  .console, .tail { background: var(--bg); border: 1px solid var(--line); border-radius: 8px; padding: .6rem .8rem; font-size: .78rem;
    max-height: 22rem; overflow: auto; white-space: pre-wrap; word-break: break-all; margin: .4rem 0 0; }
  .tail { max-height: 10rem; }
  .dl { margin-left: .6rem; white-space: nowrap; }
</style>
