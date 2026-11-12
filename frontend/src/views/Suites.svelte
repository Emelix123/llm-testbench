<script>
  import { onMount } from 'svelte'
  import { api } from '../lib/api.js'
  let suites = $state([]), tests = $state([])
  const load = async () => { ;[suites, tests] = await Promise.all([api.get('/api/suites'), api.get('/api/tests')]) }
  onMount(load)
  const name = (id) => tests.find((t) => t.id === id)?.name ?? id
  async function del(s) { if (confirm(`Suite „${s.name}“ löschen?`)) { await api.del('/api/suites/' + s.id); load() } }
</script>

<h1>Suiten</h1>
<p class="muted">Fertige Zusammenstellungen. Eigene Suiten speichert man auf der Seite <a href="#/tests">Tests</a>.</p>
{#each suites as s}
  <div class="panel row" style="margin-top:.6rem">
    <div class="grow">
      <b>{s.name}</b> {#if s.duration}<span class="muted small">· {s.duration}</span>{/if} {#if s.builtin}<span class="badge">fertig</span>{/if}
      <div class="small muted">{s.items.map((i) => name(i.test)).join(', ') || 'Tests folgen in späteren Phasen'}</div>
      {#if s.note}<div class="small muted">{s.note}</div>{/if}
    </div>
    <a class="btn" href={'#/tests?suite=' + s.id}>Verwenden</a>
    {#if !s.builtin}<button class="danger" onclick={() => del(s)}>Löschen</button>{/if}
  </div>
{/each}
<p class="small muted" style="margin-top:1rem">Konfiguration, Dauerlast und Abnahme kommen mit den Phasen 3–5.</p>
