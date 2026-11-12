<script>
  import { onMount } from 'svelte'
  import { api, dt, num } from '../lib/api.js'
  import Light from '../lib/Light.svelte'
  import Chart from '../lib/Chart.svelte'
  let { query } = $props()
  let runs = $state([]), a = $state(query.a || ''), b = $state(query.b || ''), A = $state(null), B = $state(null)

  onMount(async () => { runs = await api.get('/api/runs'); load() })
  const runA = $derived(runs.find((r) => r.id === a))
  const candidatesB = $derived(runs.filter((r) => r.id !== a && (!runA || r.test === runA.test)))
  async function load() {
    A = a ? await api.get('/api/runs/' + a).catch(() => null) : null
    B = b ? await api.get('/api/runs/' + b).catch(() => null) : null
    history.replaceState(null, '', `#/vergleich?a=${a}&b=${b}`)
  }
  const label = (r) => `${dt(r.started_at)} · ${r.profile_name} · ${r.test_name}`

  // Diagramme mit gleicher id überlagern: Serien als "A: …" und "B: …"
  const charts = $derived.by(() => {
    if (!A || !B) return []
    const cb = Object.fromEntries((B.summary.charts || []).map((c) => [c.id, c]))
    return (A.summary.charts || []).filter((c) => cb[c.id]).map((c) => ({
      ...c, series: [...c.series.filter((s) => !s.dashed).map((s) => ({ ...s, name: 'A: ' + s.name })),
                     ...cb[c.id].series.filter((s) => !s.dashed).map((s) => ({ ...s, name: 'B: ' + s.name }))],
    }))
  })
  const scores = $derived.by(() => {
    if (!A || !B) return []
    const sa = A.summary.scores || {}, sb = B.summary.scores || {}
    return [...new Set([...Object.keys(sa), ...Object.keys(sb)])].map((k) => [k, sa[k], sb[k]])
  })
  function kpiRows(s) {
    const k = s.kpis || {}
    return [['Echte Parallelität', k.real_parallelism?.value], ['Max. Nutzer', k.max_users?.value],
      ['€ / 1 Mio. Tokens', k.eur_per_m?.value], ['€ Input / Output (Split)', k.eur_split ? `${num(k.eur_split.input, 3)} / ${num(k.eur_split.output, 2)}` : null]]
  }
</script>

<h1>Vergleich</h1>
<div class="panel grid2">
  <label class="field"><span>Lauf A</span>
    <select bind:value={a} onchange={() => { if (runA && runs.find((r) => r.id === b)?.test !== runA.test) b = ''; load() }}>
      <option value="">– wählen –</option>{#each runs as r}<option value={r.id}>{label(r)}</option>{/each}
    </select></label>
  <label class="field"><span>Lauf B (gleicher Test)</span>
    <select bind:value={b} onchange={load}>
      <option value="">– wählen –</option>{#each candidatesB as r}<option value={r.id}>{label(r)}</option>{/each}
    </select></label>
</div>

{#if A && B}
  <div class="cols">
    {#each [['A', A], ['B', B]] as [n, R]}
      <div class="panel">
        <div class="row"><b class="grow">{n}: {R.config.profile?.name} · {R.config.profile?.model}</b><Light verdict={R.summary.verdict} label /></div>
        <div class="small muted">{dt(R.config.started_at)} · {R.config.test_name} · Seed {R.config.seed}</div>
        <p>{R.summary.headline}</p>
      </div>
    {/each}
  </div>

  {#if kpiRows(A.summary).some((r) => r[1] != null) || kpiRows(B.summary).some((r) => r[1] != null)}
    <h2>Kennzahlen</h2>
    <div class="panel tablewrap"><table>
      <thead><tr><th>Kennzahl</th><th class="num">A</th><th class="num">B</th><th class="num">B − A</th></tr></thead>
      <tbody>{#each kpiRows(A.summary) as [name, va], i}
        {@const vb = kpiRows(B.summary)[i][1]}
        {#if va != null || vb != null}
          <tr><td>{name}</td><td class="num">{typeof va === 'number' ? num(va, Number.isInteger(va) ? 0 : 2) : va ?? '–'}</td><td class="num">{typeof vb === 'number' ? num(vb, Number.isInteger(vb) ? 0 : 2) : vb ?? '–'}</td>
            <td class="num">{typeof va === 'number' && typeof vb === 'number' ? num(vb - va, Number.isInteger(vb - va) ? 0 : 2) : ''}</td></tr>
        {/if}
      {/each}</tbody>
    </table></div>
  {/if}

  {#if scores.length}
    <h2>Punktzahlen</h2>
    <div class="panel tablewrap"><table>
      <thead><tr><th>Testset</th><th class="num">A</th><th class="num">B</th><th class="num">B − A</th></tr></thead>
      <tbody>{#each scores as [k, va, vb]}
        <tr><td>{k}</td><td class="num">{num(va, 1)}</td><td class="num">{num(vb, 1)}</td>
          <td class="num" class:err={va != null && vb != null && vb - va < -1}>{va != null && vb != null ? num(vb - va, 1) : ''}</td></tr>
      {/each}</tbody>
    </table></div>
  {/if}

  {#if charts.length}
    <h2>Diagramme (A und B überlagert)</h2>
    <div class="panel stack">{#each charts as ch}<Chart spec={ch} />{/each}</div>
  {/if}

  <h2>Tabellen</h2>
  {#each A.summary.tables || [] as t, i}
    {@const tb = (B.summary.tables || []).find((x) => x.title === t.title)}
    <div class="cols">
      {#each [['A', t], ['B', tb]] as [n, T]}
        <div class="panel tablewrap">
          <h3>{n}: {t.title}</h3>
          {#if T}<table><thead><tr>{#each T.columns as col}<th>{col}</th>{/each}</tr></thead>
            <tbody>{#each T.rows.slice(0, 40) as row}<tr>{#each row as cell}<td>{cell}</td>{/each}</tr>{/each}</tbody></table>
          {:else}<p class="muted">nicht vorhanden</p>{/if}
        </div>
      {/each}
    </div>
  {/each}
{:else}
  <p class="muted" style="margin-top:1rem">Zwei Läufe desselben Tests wählen, z. B. vor und nach einer Konfigurationsänderung oder zwei Maschinen.</p>
{/if}

<style>
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; margin-top: .8rem; }
  @media (max-width: 800px) { .cols { grid-template-columns: 1fr; } }
</style>
