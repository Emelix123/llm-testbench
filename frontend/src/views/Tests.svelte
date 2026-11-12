<script>
  import { onMount } from 'svelte'
  import { api, dur } from '../lib/api.js'
  import ParamField from '../lib/ParamField.svelte'
  import { connect, live } from '../lib/state.svelte.js'
  let { query } = $props()
  const clone = (x) => (x === undefined ? undefined : JSON.parse(JSON.stringify(x)))

  let tests = $state([]), suites = $state([]), profiles = $state([])
  let profileId = $state(''), selected = $state({}), params = $state({}), expanded = $state({})
  let estimate = $state(null), error = $state(''), suiteName = $state(''), suite = $state(null), makeReport = $state(false)

  onMount(async () => {
    ;[tests, suites, profiles] = await Promise.all([api.get('/api/tests'), api.get('/api/suites'), api.get('/api/profiles')])
    for (const t of tests) params[t.id] = Object.fromEntries(t.params.map((p) => [p.key, clone(p.default)]))
    profileId = localStorage.getItem('profile') || profiles[0]?.id || ''
    if (!profiles.find((p) => p.id === profileId)) profileId = profiles[0]?.id || ''
    if (query.suite) applySuite(query.suite)
  })

  const categories = $derived([...new Set(tests.map((t) => t.category))])
  const items = $derived(tests.filter((t) => selected[t.id]).map((t) => ({ test: t.id, params: $state.snapshot(params[t.id]) })))

  $effect(() => {
    const body = { items }
    if (!items.length) { estimate = null; return }
    const h = setTimeout(async () => { estimate = await api.post('/api/estimate', body) }, 200)
    return () => clearTimeout(h)
  })

  function applySuite(id) {
    const s = suites.find((x) => x.id === id)
    suite = s || null
    makeReport = !!s?.report
    if (!s) return
    selected = {}
    for (const it of s.items) {
      selected[it.test] = true
      if (it.params) Object.assign(params[it.test], it.params)
    }
  }

  async function start() {
    error = ''
    localStorage.setItem('profile', profileId)
    try {
      await api.post('/api/batch', { profile_id: profileId, items, label: suite?.id || '', make_report: makeReport })
      live.log = []
      connect()
      location.hash = '#/live'
    } catch (e) { error = e.message }
  }
  async function saveSuite() {
    if (!suiteName) return
    await api.post('/api/suites', { name: suiteName, items })
    suites = await api.get('/api/suites'); suiteName = ''
  }
  function reset(t) { params[t.id] = Object.fromEntries(t.params.map((p) => [p.key, clone(p.default)])) }
</script>

<h1>Test starten</h1>

<div class="panel stack">
  <div class="grid2">
    <label class="field"><span>1. Endpunkt-Profil</span>
      <select bind:value={profileId}>
        {#each profiles as p}<option value={p.id}>{p.name} – {p.model}</option>{/each}
      </select>
    </label>
    <label class="field"><span>2. Suite übernehmen (optional)</span>
      <select value={suite?.id || ''} onchange={(e) => applySuite(e.target.value)}>
        <option value="">– einzelne Tests wählen –</option>
        {#each suites as s}<option value={s.id}>{s.name}{s.duration ? ` (${s.duration})` : ''}</option>{/each}
      </select>
    </label>
  </div>
  {#if !profiles.length}<p class="notice">Noch kein Endpunkt. <a href="#/endpunkte">Profil anlegen</a></p>{/if}
</div>

{#each categories as cat}
  <h2>{cat}</h2>
  {#each tests.filter((t) => t.category === cat) as t}
    <div class="panel test" class:on={selected[t.id]}>
      <div class="row">
        <label class="pick grow">
          <input type="checkbox" bind:checked={selected[t.id]} />
          <span class="grow"><b>{t.name}</b><br /><span class="small muted">{t.description}</span></span>
        </label>
        <button class="link" onclick={() => (expanded[t.id] = !expanded[t.id])}>{expanded[t.id] ? 'Parameter zu' : 'Parameter'}</button>
      </div>
      {#if expanded[t.id] && params[t.id]}
        <div class="grid2" style="margin-top:.8rem">
          {#each t.params as p}
            <div style={p.type === 'multi' ? 'grid-column: 1 / -1' : ''}><ParamField param={p} bind:value={params[t.id][p.key]} /></div>
          {/each}
        </div>
        <button class="link small" style="margin-top:.5rem" onclick={() => reset(t)}>Standardwerte</button>
      {/if}
    </div>
  {/each}
{/each}

<div class="panel startbar">
  <div class="grow">
    <div>3. Geschätzte Gesamtdauer: <b>{estimate ? dur(estimate.total_s) : '–'}</b></div>
    <label class="row small"><input type="checkbox" bind:checked={makeReport} /> am Ende Prüfbericht (HTML + PDF) erzeugen</label>
    {#if estimate}<div class="small muted">{estimate.items.map((i) => `${tests.find((t) => t.id === i.test)?.name}: ${dur(i.seconds)}`).join(' · ')}</div>{/if}
    {#if error}<div class="err">{error}</div>{/if}
  </div>
  <div class="row">
    <input style="width:12rem" placeholder="als Suite speichern …" bind:value={suiteName} />
    <button onclick={saveSuite} disabled={!items.length || !suiteName}>Speichern</button>
  </div>
  <button class="primary big" onclick={start} disabled={!items.length || !profileId || (live.batch && live.batch.state === 'laeuft')}>4. Test starten</button>
</div>

<style>
  .test { margin-top: .6rem; }
  .pick { display: flex; gap: .7rem; align-items: flex-start; cursor: pointer; }
  .pick input { margin-top: .3rem; }
  .test.on { border-color: var(--accent); }
  .startbar { position: sticky; bottom: 1rem; margin-top: 1.5rem; display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
</style>
