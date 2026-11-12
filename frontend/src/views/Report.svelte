<script>
  import { onMount } from 'svelte'
  import { api, num, dt } from '../lib/api.js'
  import Light from '../lib/Light.svelte'
  import Chart from '../lib/Chart.svelte'
  import CostCalc from '../lib/CostCalc.svelte'
  let { query } = $props()
  let rep = $state(null), error = $state(''), open = $state({}), refs = $state({}), pdfBusy = $state(false)

  onMount(async () => {
    try {
      ;[rep, refs] = await Promise.all([api.get('/api/report?runs=' + encodeURIComponent(query.runs || '')), api.get('/api/references')])
    } catch (e) { error = e.message }
  })
  async function setRef(c, on) {
    refs = await api.put('/api/references/' + c.test, { run_id: on ? c.run_id : null })
    rep = await api.get('/api/report?runs=' + encodeURIComponent(query.runs || ''))
  }
  async function pdf() {
    pdfBusy = true; error = ''
    try {
      const r = await fetch('/api/report/pdf?runs=' + encodeURIComponent(query.runs || ''))
      if (!r.ok) throw new Error((await r.json()).detail)
      const url = URL.createObjectURL(await r.blob())
      const a = Object.assign(document.createElement('a'), { href: url, download: 'pruefbericht.pdf' })
      a.click(); URL.revokeObjectURL(url)
    } catch (e) { error = 'PDF: ' + e.message }
    pdfBusy = false
  }
  const k = $derived(rep?.kpis || {})
  const tl = $derived(rep?.traffic_light)
  const first = $derived(rep?.cards?.[0]?.config)
</script>

{#if error}<p class="err">{error}</p>{/if}
{#if rep}
  <div class="row">
    <h1 class="grow">Ergebnis</h1>
    <span class="muted small">{first?.profile?.name} · {first?.profile?.model} · {dt(first?.started_at)}</span>
  </div>
  <div class="row" style="margin-bottom:.8rem">
    <a class="btn" href={'/api/report/html?runs=' + encodeURIComponent(query.runs || '')} target="_blank">Prüfbericht (HTML)</a>
    <button onclick={pdf} disabled={pdfBusy}>{pdfBusy ? 'PDF wird erzeugt …' : 'Prüfbericht (PDF)'}</button>
    <a class="btn" href={'#/vergleich?a=' + (rep.cards[0]?.run_id || '')}>Mit anderem Lauf vergleichen</a>
  </div>

  <div class="kpis">
    <div class="panel kpi">
      <span>Echte Parallelität</span>
      <b>{k.real_parallelism ? (k.real_parallelism.prefix || '') + num(k.real_parallelism.value) : '–'}</b>
      <small>{k.real_parallelism ? k.real_parallelism.unit : 'Parallelitäts-Test nicht im Lauf'}</small>
      {#if k.real_parallelism?.text}<small class="muted">{k.real_parallelism.text}</small>{/if}
    </div>
    <div class="panel kpi">
      <span>Max. Nutzer bei den Zielwerten</span>
      <b>{k.max_users ? num(k.max_users.value) : '–'}</b>
      <small>{k.max_users ? k.max_users.unit : 'Kapazitäts-Sweep nicht im Lauf oder kein Stufe im Ziel'}</small>
    </div>
    <div class="panel kpi">
      <span>€ pro 1 Mio. Tokens</span>
      <b>{k.eur_per_m ? num(k.eur_per_m.value, 2) + ' €' : '–'}</b>
      <small>{k.eur_per_m ? k.eur_per_m.unit : 'aus dem Kapazitäts-Sweep'}</small>
      {#if k.eur_split}<small class="muted">Input {num(k.eur_split.input, 3)} € · Output {num(k.eur_split.output, 2)} € · Cache {num(k.eur_split.cache, 3)} €</small>{/if}
    </div>
    <div class="panel kpi">
      <span>Fehlerquote</span>
      <b class:err={k.error_rate_pct?.value > 0}>{k.error_rate_pct ? num(k.error_rate_pct.value, 2) + ' %' : '–'}</b>
      <small>{k.error_rate_pct?.text ?? ''}</small>
    </div>
  </div>
  {#if k.max_users || k.eur_per_m}
    <p class="small muted">{k.max_users?.text}<br />{k.eur_per_m?.text}</p>
  {/if}

  <CostCalc cards={rep.cards} settings={rep.settings} />

  {#each rep.cards as c}
    <div class="panel card">
      <button class="cardhead" onclick={() => (open[c.run_id] = !open[c.run_id])}>
        <Light verdict={c.verdict} show={tl} label />
        <div class="grow">
          <b>{c.name}</b>{#if c.status !== 'fertig'} <span class="badge">{c.status}</span>{/if}
          <div>{c.headline}</div>
          {#if c.summary.slo_text}<div class="small muted">{c.summary.slo_text}</div>{/if}
        </div>
        <span class="muted">{open[c.run_id] ? '▴' : '▾ Details'}</span>
      </button>
      {#if open[c.run_id]}
        <div class="details stack">
          {#each c.summary.notes || [] as n}<div class="notice">{n}</div>{/each}
          {#if c.summary.cpu_high_requests}<div class="notice">{c.summary.cpu_high_requests} Anfragen wurden bei Laptop-CPU ≥ 80 % gesendet – diese Messungen sind markiert, der Laptop könnte gebremst haben.</div>{/if}
          {#if c.summary.users_note}<p class="small muted">{c.summary.users_note}</p>{/if}
          {#each c.summary.charts || [] as ch}<Chart spec={ch} />{/each}
          {#each c.summary.tables || [] as t}
            <h3>{t.title}</h3>
            <div class="tablewrap"><table>
              <thead><tr>{#each t.columns as col}<th>{col}</th>{/each}</tr></thead>
              <tbody>{#each t.rows as row}<tr>{#each row as cell}<td class:num={typeof cell === 'number' || /^[\d.,\s]+( ms| s| %)?$/.test(String(cell))}>{cell}</td>{/each}</tr>{/each}</tbody>
            </table></div>
          {/each}
          <h3>Rahmen</h3>
          <table class="small"><tbody>
            <tr><td>Netzlatenz</td><td>{c.config.client?.network_latency?.p50_ms ?? '–'} ms (p50, {c.config.client?.network_latency?.path ?? '–'}) · Verbindung: {c.config.client?.network_kind} (empfohlen: LAN-Kabel)</td></tr>
            <tr><td>Prompts</td><td>{c.config.prompt_factory ? `${c.config.prompt_factory.tokenizer}; ${c.config.prompt_factory.exact ? 'exakte Länge' : 'geschätzte Länge'}; Template-Overhead ${c.config.prompt_factory.template_overhead} Tokens` : '–'}</td></tr>
            <tr><td>Output-Länge erzwungen</td><td>{c.config.force_output_length === undefined ? '–' : c.config.force_output_length ? 'ja (ignore_eos/min_tokens)' : 'nein – tatsächliche Längen'}</td></tr>
            <tr><td>Seed</td><td>{c.config.seed}</td></tr>
            <tr><td>Modelle laut /v1/models</td><td>{c.config.endpoint_fingerprint?.models?.join(', ') ?? '–'}</td></tr>
            <tr><td>Ruhezustand verhindert</td><td>{c.config.client?.sleep_inhibit ?? 'nein'}</td></tr>
          </tbody></table>
          <div class="row"><a class="btn" href="/api/runs/{c.run_id}/export">Export als ZIP</a>
            {#if c.summary.scores}
              {#if refs[c.test] === c.run_id}
                <span class="badge">Referenzlauf</span><button class="link small" onclick={() => setRef(c, false)}>Referenz entfernen</button>
              {:else}
                <button onclick={() => setRef(c, true)}>Als Referenz markieren</button>
              {/if}
            {/if}<span class="small muted">Ordner data/runs/{c.run_id}</span></div>
        </div>
      {/if}
    </div>
  {/each}
{/if}

<style>
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: .8rem; margin-bottom: .6rem; }
  .kpi { display: flex; flex-direction: column; gap: .1rem; }
  .kpi span { font-size: .85rem; color: var(--muted); }
  .kpi b { font-size: 2.1rem; font-variant-numeric: tabular-nums; line-height: 1.2; }
  .card { margin-top: .8rem; padding: 0; }
  .cardhead { width: 100%; border: none; background: none; text-align: left; padding: 1rem 1.2rem; display: flex; gap: 1rem; align-items: flex-start; }
  .details { padding: 0 1.2rem 1.2rem; }
</style>
