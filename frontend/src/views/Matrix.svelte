<script>
  import { onMount } from 'svelte'
  import { api, num, dt } from '../lib/api.js'
  import Light from '../lib/Light.svelte'
  import Chart from '../lib/Chart.svelte'

  let data = $state(null), base = $state(''), since = $state(''), msg = $state(''), loading = $state(false)
  const groups = $derived(data?.groups || [])
  const B = $derived(groups.find((g) => g.key === base))

  async function load() {
    loading = true
    try {
      data = await api.get('/api/matrix' + (since ? '?since=' + since : ''))
      if (!groups.find((g) => g.key === base)) base = (groups.find((g) => g.scenario === 'K0') || groups[0])?.key || ''
    } finally { loading = false }
  }
  onMount(load)

  const M = (key) => data?.metrics.find((m) => m.key === key)
  const metricsOf = (area) => (data?.metrics || []).filter((m) => m.area === area && groups.some((g) => g.values[m.key] != null))

  function fmt(m, v) {
    if (v === null || v === undefined) return '–'
    if (typeof v === 'boolean') return v ? 'ja' : 'nein'
    if (typeof v !== 'number') return String(v)
    return num(v, m?.digits ?? 2) + (m?.unit && m.unit !== '' ? (m.unit === '×' ? '×' : ' ' + m.unit) : '')
  }
  // Differenz zur Referenz in % und ob sie besser/schlechter ist (± 2 % gilt als gleich)
  function delta(m, v, b) {
    if (typeof v !== 'number' || typeof b !== 'number' || b === 0 || !m) return null
    const pct = ((v - b) / Math.abs(b)) * 100
    let cls = 'same'
    if (m.better && Math.abs(pct) > 2) cls = (m.better === 'lower' ? pct < 0 : pct > 0) ? 'good' : 'bad'
    return { pct, cls }
  }
  const dtxt = (d) => (d ? (d.pct > 0 ? '+' : '') + num(d.pct, 1) + ' %' : '')
  const verdictOf = (g, test) => g.runs[test]?.verdict
  const runLink = (g, test) => (g.runs[test] ? '#/bericht?runs=' + g.runs[test].id : null)
  const short = (g) => g.scenario || g.profile_name
  // Maßnahmen kommen als Liste oder (ältere Läufe) als Text "[a, b]"
  const massn = (g) => {
    const m = g.massnahmen
    const list = Array.isArray(m) ? m : String(m || '').replace(/^\[|\]$/g, '').split(',').map((x) => x.trim()).filter(Boolean)
    return list.length ? list.join(', ').replace(/_/g, ' ') : (g.scenario ? 'keine Maßnahme' : '')
  }

  // Bilanz: Sicherheit je Bereich + Leistungsverlust gegenüber der Referenz
  const BILANZ = [
    ['prefix_cache_leak', 'Seitenkanal', 'prefix_auc'],
    ['resource_contention', 'Noisy Neighbor', 'slowdown_sn'],
    ['hardening_check', 'Härtung', 'hardening_score'],
    ['watermark', 'Wasserzeichen', 'wm_detect_rate'],
  ]
  const PERF = ['output_tps', 'ttft_p50_ms', 'tpot_p50_ms']
  // Bilanz: zusätzlich die Kosten (3 Mandanten / viele Mandanten mit gemeinsamer Vorlage)
  const BILANZ_PERF = [...PERF, 'cost_per_mtok_eur', 'salt_cost_nmax_eur']
  const BILANZ_LABEL = { cost_per_mtok_eur: '€/1 Mio. Tok', salt_cost_nmax_eur: '€/1 Mio. Tok, max. Mandanten' }
  const nmax = $derived(Math.max(0, ...groups.map((g) => g.values.salt_tenants_max || 0)))
  const costChart = $derived(groups.some((g) => g.values.cost_per_mtok_eur != null || g.values.salt_cost_nmax_eur != null) ? {
    id: 'cost', type: 'bar', title: 'Kosten je 1 Mio. Output-Tokens (€)', categories: groups.map(short), labels: true,
    y: { name: '€ je 1 Mio. Output-Tokens' },
    series: [{ name: '3 Mandanten (eingeschwungen)', data: groups.map((g) => g.values.cost_per_mtok_eur ?? null) },
             { name: `${nmax || 'viele'} Mandanten, gemeinsame Vorlage`, data: groups.map((g) => g.values.salt_cost_nmax_eur ?? null) }],
  } : null)
  const coldChart = $derived(groups.some((g) => g.values.salt_new_tenant_ttft_ms != null) ? {
    id: 'cold', type: 'bar', title: 'Neuer Mandant: Wartezeit auf das erste Token (ms)', categories: groups.map(short), labels: true,
    y: { name: 'ms' },
    series: [{ name: 'TTFT 1. Anfrage', data: groups.map((g) => g.values.salt_new_tenant_ttft_ms ?? null) },
             { name: 'davon Aufschlag', data: groups.map((g) => g.values.salt_new_tenant_extra_ms ?? null) }],
  } : null)

  const deltaChart = $derived.by(() => {
    if (!B || !groups.length) return null
    const cats = groups.map(short)
    return {
      id: 'delta', type: 'bar', title: `Leistung gegenüber ${short(B)} (Δ %)`, categories: cats, labels: true, labelSuffix: ' %',
      y: { name: 'Δ % zur Referenz' },
      series: PERF.map((k) => ({ name: M(k)?.label || k,
        data: groups.map((g) => { const d = delta(M(k), g.values[k], B.values[k]); return d ? Math.round(d.pct * 10) / 10 : null }) })),
      markLines: [{ y: 0, name: '' }],
    }
  })
  const aucChart = $derived(groups.some((g) => g.values.prefix_auc != null) ? {
    id: 'auc', type: 'bar', title: 'AUC des Prefix-Cache-Seitenkanals (0,5 = Raten)', categories: groups.map(short), labels: true,
    y: { name: 'AUC' }, series: [{ name: 'AUC ruhig', data: groups.map((g) => g.values.prefix_auc ?? null) }],
    markLines: [{ y: 0.5, name: 'Raten' }],
  } : null)
  const snChart = $derived(groups.some((g) => g.values.slowdown_sn != null) ? {
    id: 'sn', type: 'bar', title: 'Verfügbarkeit: Slowdown S_N und Jain-Index', categories: groups.map(short), labels: true,
    y: { name: 'Faktor / Index' }, series: [{ name: 'Slowdown S_N', data: groups.map((g) => g.values.slowdown_sn ?? null) },
                                          { name: 'Jain-Index J', data: groups.map((g) => g.values.jain_index ?? null) }],
    markLines: [{ y: 1, name: '' }],
  } : null)

  // ------------------------------------------------------------------ Export
  // CSV/LaTeX erzeugt das Backend (/api/matrix/export) – dieselbe Funktion schreibt die Dateien des Gesamtlaufs.
  let exp = $state({ csv: '', latex: '' })
  async function loadExport() {
    const q = (since ? '&since=' + since : '') + (base ? '&base=' + encodeURIComponent(base) : '')
    try {
      const [c, l] = await Promise.all([api.text('/api/matrix/export?format=csv' + q), api.text('/api/matrix/export?format=latex' + q)])
      exp = { csv: c, latex: l }
    } catch { exp = { csv: '', latex: '' } }
  }
  $effect(() => { data; base; since; loadExport() })
  async function copy(kind) {
    const text = kind === 'csv' ? exp.csv : exp.latex
    try { await navigator.clipboard.writeText(text); msg = (kind === 'csv' ? 'CSV' : 'LaTeX') + ' in die Zwischenablage kopiert.' } catch { msg = 'Kopieren nicht möglich – Datei herunterladen.' }
    setTimeout(() => (msg = ''), 3000)
  }
  function download(kind) {
    const text = kind === 'csv' ? '\ufeff' + exp.csv : exp.latex
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([text], { type: kind === 'csv' ? 'text/csv' : 'text/plain' }))
    a.download = kind === 'csv' ? 'konfigurationsvergleich.csv' : 'konfigurationsvergleich.tex'
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }
</script>

<div class="row"><h1 class="grow">Konfigurationen: Sicherheit gegen Leistung</h1>
  <button onclick={load} disabled={loading}>{loading ? 'Lade …' : 'Neu laden'}</button></div>

<p class="muted small">Je Konfiguration (Szenario K0–K6, siehe <a href="#/steuerung">Steuerung</a>; sonst Endpunkt-Profil) zählt der neueste fertige Lauf
  jedes Tests. Leistungswerte stammen aus „Leistungskosten der Isolation“ (Bezugs-Laststufe), die Kosten der Cache-Trennung
  (neuer Mandant, viele Mandanten mit gemeinsamer Vorlage) aus „Kosten der Cache-Trennung“, Sicherheitswerte aus den Sicherheitstests.
  Differenzen sind relativ zur gewählten Referenz; <span class="good">grün</span> = besser, <span class="bad">rot</span> = schlechter (± 2 % gilt als gleich).</p>

<div class="panel grid2">
  <label class="field"><span>Referenz (Δ % bezogen auf)</span>
    <select bind:value={base}>{#each groups as g}<option value={g.key}>{g.label} · {g.profile_name}</option>{/each}</select></label>
  <label class="field"><span>nur Läufe seit</span><input type="date" bind:value={since} onchange={load} /></label>
  <div class="field"><span class="muted small">Export (Tabelle für die Arbeit)</span>
    <div class="row"><button onclick={() => copy('latex')}>LaTeX kopieren</button><button onclick={() => download('latex')}>.tex</button>
      <button onclick={() => copy('csv')}>CSV kopieren</button><button onclick={() => download('csv')}>.csv</button></div>
    {#if msg}<div class="small muted">{msg}</div>{/if}</div>
</div>

{#if !groups.length}
  <div class="panel stack" style="margin-top:1rem">
    <h3>Noch keine Daten</h3>
    <ol class="small">
      <li>Auf <a href="#/steuerung">Steuerung</a> einrichten (einmalig) und einen <b>Gesamtlauf</b> starten – er misst alle Szenarien nacheinander.</li>
      <li>Oder einzeln: dort ein Szenario starten und unter <a href="#/suiten">Suiten</a> mit dem Profil „Lokal (LiteLLM, Mandant A)“ die Suite
        <b>„Konfigurationsvergleich (je Szenario)“</b> starten; für jedes Szenario wiederholen.</li>
      <li>Schritt für Schritt: <a href="#/anleitung">Anleitung</a>.</li>
    </ol>
  </div>
{:else}
  <h2>Bilanz</h2>
  <div class="panel tablewrap"><table>
    <thead><tr><th>Konfiguration</th>{#each BILANZ as [, label]}<th>{label}</th>{/each}
      {#each BILANZ_PERF as k}<th class="num">{BILANZ_LABEL[k] || M(k)?.label} Δ</th>{/each}</tr></thead>
    <tbody>{#each groups as g}
      <tr class:ref={g.key === base}>
        <td><b>{short(g)}</b> {g.scenario_name || ''}<div class="small muted">{g.profile_name}{massn(g) ? ' · ' + massn(g) : ''}</div></td>
        {#each BILANZ as [test, , key]}
          <td>{#if g.runs[test]}<a href={runLink(g, test)} class="cell"><Light verdict={verdictOf(g, test)} /> {fmt(M(key), g.values[key])}</a>{:else}<span class="muted">–</span>{/if}</td>
        {/each}
        {#each BILANZ_PERF as k}
          {@const d = g.key === base ? null : delta(M(k), g.values[k], B?.values[k])}
          <td class="num {d?.cls || ''}">{g.key === base ? 'Referenz' : (d ? dtxt(d) : '–')}</td>
        {/each}
      </tr>
    {/each}</tbody>
  </table></div>

  {#if deltaChart}<div class="panel" style="margin-top:.8rem"><Chart spec={deltaChart} /></div>{/if}
  <div class="cols">
    {#if aucChart}<div class="panel"><Chart spec={aucChart} /></div>{/if}
    {#if snChart}<div class="panel"><Chart spec={snChart} /></div>{/if}
    {#if costChart}<div class="panel"><Chart spec={costChart} /></div>{/if}
    {#if coldChart}<div class="panel"><Chart spec={coldChart} /></div>{/if}
  </div>
  {#if data.cost_note}<p class="small muted">{data.cost_note}.</p>{/if}

  {#each Object.entries(data.areas) as [area, title]}
    {@const ms = metricsOf(area)}
    {#if ms.length}
      <h2>{title}</h2>
      <div class="panel tablewrap"><table>
        <thead><tr><th>Konfiguration</th>{#each ms as m}<th class="num">{m.label}</th>{/each}<th>Lauf</th></tr></thead>
        <tbody>{#each groups as g}
          <tr class:ref={g.key === base}>
            <td><b>{short(g)}</b> <span class="small muted">{g.scenario_name || ''}</span></td>
            {#each ms as m}
              {@const d = g.key === base || m.delta === false ? null : delta(m, g.values[m.key], B?.values[m.key])}
              <td class="num">{fmt(m, g.values[m.key])}{#if d}<div class="small {d.cls}">{dtxt(d)}</div>{/if}</td>
            {/each}
            <td class="small">{#if g.runs[ms[0].test]}<a href={runLink(g, ms[0].test)}>{dt(g.runs[ms[0].test].started_at)}</a>{:else}–{/if}</td>
          </tr>
        {/each}</tbody>
      </table></div>
    {/if}
  {/each}

  <h2>Export-Vorschau</h2>
  <p class="small muted">Dasselbe wie die Knöpfe oben – zum Markieren und Kopieren, falls die Zwischenablage nicht erlaubt ist.
    Die LaTeX-Tabelle braucht <code>\usepackage{'{'}booktabs{'}'}</code>.</p>
  <details class="panel" id="export-latex"><summary>LaTeX (booktabs)</summary><pre class="export">{exp.latex}</pre></details>
  <details class="panel" id="export-csv" style="margin-top:.6rem"><summary>CSV (Semikolon, Dezimalkomma – für Excel/LibreOffice)</summary><pre class="export">{exp.csv}</pre></details>
{/if}

<style>
  .export { overflow-x: auto; font-size: .78rem; white-space: pre; margin: .6rem 0 0; }
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin-top: .8rem; }
  @media (max-width: 800px) { .cols { grid-template-columns: 1fr; } }
  .good { color: var(--green); font-weight: 600; }
  .bad { color: var(--red); font-weight: 600; }
  .same { color: var(--muted); }
  tr.ref td { background: var(--accent-weak); }
  .cell { display: inline-flex; align-items: center; gap: .35rem; text-decoration: none; color: var(--text); }
</style>
