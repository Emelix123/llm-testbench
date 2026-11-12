<script>
  import { onMount } from 'svelte'
  import { api, dt } from '../lib/api.js'
  import Light from '../lib/Light.svelte'
  let batches = $state([]), settings = $state(null)
  let fProfile = $state(''), fTest = $state(''), fFrom = $state(''), fTo = $state('')
  onMount(async () => { ;[batches, settings] = await Promise.all([api.get('/api/batches'), api.get('/api/settings')]) })
  const profiles = $derived([...new Set(batches.map((b) => b.profile_name).filter(Boolean))])
  const tests = $derived([...new Set(batches.flatMap((b) => b.runs.map((r) => r.test_name)).filter(Boolean))])
  const shown = $derived(batches.filter((b) =>
    (!fProfile || b.profile_name === fProfile) &&
    (!fTest || b.runs.some((r) => r.test_name === fTest)) &&
    (!fFrom || (b.started_at || '') >= fFrom) && (!fTo || (b.started_at || '').slice(0, 10) <= fTo)))
</script>

<h1>Ergebnisse</h1>
<div class="panel grid2">
  <label class="field"><span>Endpunkt</span><select bind:value={fProfile}><option value="">alle</option>{#each profiles as p}<option>{p}</option>{/each}</select></label>
  <label class="field"><span>Test</span><select bind:value={fTest}><option value="">alle</option>{#each tests as t}<option>{t}</option>{/each}</select></label>
  <label class="field"><span>von</span><input type="date" bind:value={fFrom} /></label>
  <label class="field"><span>bis</span><input type="date" bind:value={fTo} /></label>
</div>
<div class="panel tablewrap" style="margin-top:.8rem">
  <table>
    <thead><tr><th></th><th>Datum</th><th>Endpunkt</th><th>Tests</th><th>Status</th></tr></thead>
    <tbody>
      {#each shown as b}
        <tr>
          <td><Light verdict={b.verdict} show={settings?.traffic_light} /></td>
          <td><a href={'#/bericht?runs=' + b.runs.map((r) => r.id).join(',')}>{dt(b.started_at)}</a></td>
          <td>{b.profile_name}</td>
          <td>{#each b.runs as r, i}{i ? ', ' : ''}<a href={'#/bericht?runs=' + r.id}>{r.test_name}</a>{/each}</td>
          <td class="small">{[...new Set(b.runs.map((r) => r.status))].join(', ')}</td>
        </tr>
      {:else}
        <tr><td colspan="5" class="muted">Keine Läufe.</td></tr>
      {/each}
    </tbody>
  </table>
</div>
