<script>
  import { onMount } from 'svelte'
  import { api, num, dur } from '../lib/api.js'
  import { live, refresh, connect } from '../lib/state.svelte.js'
  onMount(async () => { await refresh(); if (live.batch?.state === 'laeuft') connect() })
  const b = $derived(live.batch)
  const report = $derived(b ? '#/bericht?runs=' + b.runs.filter((r) => r.run_id).map((r) => r.run_id).join(',') : '')
  async function cancel() { if (confirm('Test wirklich abbrechen? Bereits gemessene Anfragen bleiben gespeichert.')) await api.post('/api/batch/cancel') }
</script>

<h1>Laufender Test</h1>
{#if !b}
  <p class="muted">Kein Test aktiv. <a href="#/tests">Test starten</a></p>
{:else}
  <div class="panel stack">
    <div class="row">
      <div class="grow">Endpunkt <b>{b.profile.name}</b> · Status <b>{b.state === 'laeuft' ? 'läuft' : b.state}</b></div>
      {#if b.state === 'laeuft'}<button class="danger" onclick={cancel}>Abbrechen</button>{:else}<a class="btn" href={report}>Ergebnis ansehen</a>{/if}
    </div>
    <div class="kpis">
      <div><span>aktuelle Parallelität</span><b>{num(b.live?.inflight)}</b></div>
      <div><span>Tokens/s gesamt (live, ≈)</span><b>{num(b.live?.tokens_per_s)}</b></div>
      <div><span>Fehler bisher</span><b class:err={b.live?.errors}>{num(b.live?.errors)}</b><small>von {num(b.live?.requests)} Anfragen</small></div>
      <div><span>CPU Laptop</span><b class:err={b.live?.client_cpu_pct >= 80}>{num(b.live?.client_cpu_pct)} %</b></div>
    </div>
    <p class="small muted">Der Browser darf geschlossen werden – der Test läuft im Backend weiter. Fehler werden gezählt, der Test bricht deswegen nicht ab.</p>
  </div>
  <h2>Fortschritt</h2>
  <div class="panel stack">
    {#each b.runs as r}
      <div>
        <div class="row"><b class="grow">{r.name}</b><span class="small muted">{r.text} · geschätzt {dur(r.estimated_s)}</span></div>
        <div class="bar"><div style="width:{Math.round((r.progress || 0) * 100)}%"></div></div>
      </div>
    {/each}
  </div>
  {#if live.log.length}
    <h2>Hinweise</h2>
    <div class="panel small">{#each live.log as l}<div>{l}</div>{/each}</div>
  {/if}
{/if}

<style>
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .8rem; }
  .kpis div { background: var(--bg); border-radius: 8px; padding: .7rem .9rem; display: flex; flex-direction: column; }
  .kpis span { font-size: .8rem; color: var(--muted); }
  .kpis b { font-size: 1.6rem; font-variant-numeric: tabular-nums; }
  .kpis small { color: var(--muted); }
</style>
