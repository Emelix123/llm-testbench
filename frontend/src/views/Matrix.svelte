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
  function rowsForExport() {
    const ms = (data?.metrics || []).filter((m) => groups.some((g) => g.values[m.key] != null))
    return { ms, rows: groups.map((g) => [g.scenario || '', g.scenario_name || g.profile_name, ...ms.map((m) => g.values[m.key])]) }
  }
  function csv() {
    const { ms, rows } = rowsForExport()
    const esc = (v) => { const s = v === null || v === undefined ? '' : typeof v === 'boolean' ? (v ? 'ja' : 'nein') : typeof v === 'number' ? String(v).replace('.', ',') : String(v); return /[;"\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s }
    return [['Szenario', 'Konfiguration', ...ms.map((m) => m.label + (m.unit ? ` [${m.unit}]` : ''))], ...rows].map((r) => r.map(esc).join(';')).join('\n')
  }
  function latex() {
    const tex = (s) => String(s).replace(/([%&_#$])/g, '\\$1').replace(/×/g, '$\\times$').replace(/≥/g, '$\\geq$').replace(/–/g, '--')
      .replace(/^-(?=\d)/, '$-$')   // echtes Minuszeichen statt Bindestrich
    const cell = (k, g) => { const m = M(k), v = g.values[k]; return v == null ? '--' : tex(fmt(m, v)) }
    const dcell = (k, g) => { if (!B) return '--'; if (g.key === base) return 'Ref.'; const d = delta(M(k), g.values[k], B.values[k]); return d ? tex(dtxt(d)) : '--' }
    const lines = groups.map((g) => [tex(short(g)), tex(g.scenario_name || g.profile_name), cell('prefix_auc', g), cell('prefix_d', g), cell('slowdown_sn', g),
      cell('jain_index', g), cell('hardening_score', g), cell('wm_detect_rate', g), dcell('output_tps', g), dcell('ttft_p50_ms', g), dcell('tpot_p50_ms', g)].join(' & ') + ' \\\\')
    return ['% Erzeugt von der LLM-Inferenz-Testplattform (Konfigurationsvergleich), ' + new Date().toLocaleString('de-DE'),
      '% benötigt \\usepackage{booktabs}. Passt so auf A4 mit 2,5 cm Rand; sonst die tabular-Umgebung in',
      '% \\resizebox{\\textwidth}{!}{...} (Paket graphicx) einschließen.',
      '\\begin{table}[htbp]', '\\centering', '\\footnotesize', '\\setlength{\\tabcolsep}{2.5pt}',
      '\\begin{tabular}{llrrrrrrrrr}', '\\toprule',
      ' & & \\multicolumn{2}{c}{Seitenkanal} & \\multicolumn{2}{c}{Verfügbarkeit} & Härtung & WZ & \\multicolumn{3}{c}{$\\Delta$ zu ' + tex(B ? short(B) : 'Referenz') + '} \\\\',
      '\\cmidrule(lr){3-4}\\cmidrule(lr){5-6}\\cmidrule(lr){9-11}',
      'ID & Konfiguration & AUC & $d$ & $S_N$ & $J$ & bestanden & erkannt & Durchsatz & TTFT p50 & TPOT p50 \\\\', '\\midrule',
      ...lines, '\\bottomrule', '\\end{tabular}',
      '\\caption{Sicherheit und Leistungskosten der Konfigurationen (neuester Lauf je Test)}', '\\label{tab:konfigurationsvergleich}', '\\end{table}'].join('\n')
  }
  async function copy(kind) {
    const text = kind === 'csv' ? csv() : latex()
    try { await navigator.clipboard.writeText(text); msg = (kind === 'csv' ? 'CSV' : 'LaTeX') + ' in die Zwischenablage kopiert.' } catch { msg = 'Kopieren nicht möglich – Datei herunterladen.' }
    setTimeout(() => (msg = ''), 3000)
  }
  function download(kind) {
    const text = kind === 'csv' ? '\ufeff' + csv() : latex()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([text], { type: kind === 'csv' ? 'text/csv' : 'text/plain' }))
    a.download = kind === 'csv' ? 'konfigurationsvergleich.csv' : 'konfigurationsvergleich.tex'
    a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }
</script>

<div class="row"><h1 class="grow">Konfigurationen: Sicherheit gegen Leistung</h1>
  <button onclick={load} disabled={loading}>{loading ? 'Lade …' : 'Neu laden'}</button></div>

<p class="muted small">Je Konfiguration (Szenario K0–K6 aus <code>deploy/scenario.sh</code>, sonst Endpunkt-Profil) zählt der neueste fertige Lauf
  jedes Tests. Leistungswerte stammen aus „Leistungskosten der Isolation“ (Bezugs-Laststufe), Sicherheitswerte aus den Sicherheitstests.
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
      <li><code>cd deploy && ./scenario.sh init && ./scenario.sh profile</code> (einmalig)</li>
      <li><code>./scenario.sh up K0</code> – dann in der Testplattform mit dem Profil „Lokal (LiteLLM, Mandant A)“ die Suite
        <b>„Konfigurationsvergleich (je Szenario)“</b> starten.</li>
      <li>Für jedes weitere Szenario (K1 … K6) wiederholen. Das Szenario wird je Lauf automatisch erkannt.</li>
    </ol>
  </div>
{:else}
  <h2>Bilanz</h2>
  <div class="panel tablewrap"><table>
    <thead><tr><th>Konfiguration</th>{#each BILANZ as [, label]}<th>{label}</th>{/each}
      {#each PERF as k}<th class="num">{M(k)?.label} Δ</th>{/each}</tr></thead>
    <tbody>{#each groups as g}
      <tr class:ref={g.key === base}>
        <td><b>{short(g)}</b> {g.scenario_name || ''}<div class="small muted">{g.profile_name}{massn(g) ? ' · ' + massn(g) : ''}</div></td>
        {#each BILANZ as [test, , key]}
          <td>{#if g.runs[test]}<a href={runLink(g, test)} class="cell"><Light verdict={verdictOf(g, test)} /> {fmt(M(key), g.values[key])}</a>{:else}<span class="muted">–</span>{/if}</td>
        {/each}
        {#each PERF as k}
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
  </div>

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
              {@const d = g.key === base ? null : delta(m, g.values[m.key], B?.values[m.key])}
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
  <details class="panel" id="export-latex"><summary>LaTeX (booktabs)</summary><pre class="export">{latex()}</pre></details>
  <details class="panel" id="export-csv" style="margin-top:.6rem"><summary>CSV (Semikolon, Dezimalkomma – für Excel/LibreOffice)</summary><pre class="export">{csv()}</pre></details>
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
