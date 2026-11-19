<script>
  import { onMount } from 'svelte'
  import { api } from '../lib/api.js'
  let profiles = $state([]), form = $state(null), error = $state(''), checks = $state({}), busy = $state({})
  const empty = () => ({ name: '', base_url: 'http://localhost:4000', model: '', tokenizer: '', api_key: '', api_key_name: '', max_context: 32768, capabilities: {},
    tenant_key_values: { angreifer: '', last: '' }, engine_url: '', watermark_detector_url: '' })
  function edit(p) { form = { ...p, api_key: '', capabilities: { ...(p.capabilities || {}) }, tenant_key_values: { angreifer: '', last: '' } } }
  const tk = (p, r) => (p.tenant_keys?.[r] ? (p.tenant_has_key?.[r] ? 'hinterlegt' : 'fehlt') : null)
  async function load() { profiles = await api.get('/api/profiles') }
  onMount(load)
  async function save() {
    error = ''
    try { await api.post('/api/profiles', $state.snapshot(form)); form = null; await load() } catch (e) { error = e.message }
  }
  async function check(p) {
    busy[p.id] = true; checks[p.id] = null
    try { checks[p.id] = await api.post(`/api/profiles/${p.id}/check`) } catch (e) { checks[p.id] = { ok: false, items: [{ label: 'Fehler', ok: false, detail: e.message }] } }
    busy[p.id] = false
  }
  async function del(p) { if (confirm(`Profil „${p.name}“ löschen? Der Key bleibt in secrets.env.`)) { await api.del(`/api/profiles/${p.id}`); load() } }
</script>

<div class="row"><h1 class="grow">Endpunkte</h1><button class="primary" onclick={() => (form = empty())}>Profil anlegen</button></div>

{#if form}
  <div class="panel stack">
    <h3>{form.id ? 'Profil bearbeiten' : 'Neues Profil'}</h3>
    <div class="grid2">
      <label class="field"><span>Name</span><input bind:value={form.name} placeholder="z. B. GPU-Box 1 – Qwen" disabled={!!form.id} /></label>
      <label class="field"><span>LiteLLM-URL (Basis, ohne /v1/chat/completions)</span><input bind:value={form.base_url} placeholder="https://llm.example.de" /></label>
      <label class="field"><span>Modell (wie in /v1/models)</span><input bind:value={form.model} /></label>
      <label class="field"><span>Tokenizer (Hugging-Face-Name oder Pfad zu tokenizer.json)</span><input bind:value={form.tokenizer} placeholder="Qwen/Qwen2.5-72B-Instruct" /></label>
      <label class="field"><span>Max. Kontext (Tokens)</span><input type="number" bind:value={form.max_context} /></label>
      <label class="field"><span>API-Key {form.id ? '(leer = unverändert)' : ''}</span><input type="password" bind:value={form.api_key} autocomplete="off" /></label>
    </div>
    <details class="small" open={!!(form.tenant_keys || form.engine_url)}>
      <summary><b>Sicherheitstests (Mandantentrennung)</b> – weitere Mandanten-Keys, Engine- und Detektor-URL</summary>
      <p class="muted">Der API-Key oben gehört zu Mandant A (Opfer). Mit eigenen Keys für Mandant B (Angreifer) und C (Last) prüfen
        die Sicherheitstests, ob das Gateway die Mandanten trennt. <code>deploy/scenario.sh profile</code> trägt das automatisch ein.</p>
      <div class="grid2">
        <label class="field"><span>API-Key Mandant B – Angreifer {form.tenant_keys?.angreifer ? '(leer = unverändert)' : ''}</span><input type="password" bind:value={form.tenant_key_values.angreifer} autocomplete="off" /></label>
        <label class="field"><span>API-Key Mandant C – Last {form.tenant_keys?.last ? '(leer = unverändert)' : ''}</span><input type="password" bind:value={form.tenant_key_values.last} autocomplete="off" /></label>
        <label class="field"><span>Direkte Engine-URL (sollte NICHT erreichbar sein)</span><input bind:value={form.engine_url} placeholder="http://127.0.0.1:8000" /></label>
        <label class="field"><span>Wasserzeichen-Detektor (nur Betreiber)</span><input bind:value={form.watermark_detector_url} placeholder="http://127.0.0.1:8090" /></label>
      </div>
    </details>
    <div class="row">
      <label class="row"><input type="checkbox" bind:checked={form.capabilities.vision} /> Vision-Modell (Bilder)</label>
      <label class="row"><input type="checkbox" bind:checked={form.capabilities.reasoning} /> Reasoning-Modell (Denk-Teil)</label>
    </div>
    <p class="small muted">Der Key wird nur in <code>data/secrets.env</code> gespeichert, unter dem Namen {form.api_key_name || '…_API_KEY'} – nie in Ergebnissen oder Exporten.</p>
    {#if error}<p class="err">{error}</p>{/if}
    <div class="row"><button class="primary" onclick={save}>Speichern</button><button onclick={() => (form = null)}>Abbrechen</button></div>
  </div>
{/if}

{#each profiles as p}
  <div class="panel stack" style="margin-top: .8rem">
    <div class="row">
      <div class="grow">
        <h3>{p.name}</h3>
        <div class="small muted">{p.base_url} · {p.model} · Tokenizer: {p.tokenizer || '– (Schätzung)'} · max. {p.max_context ?? '?'} Tokens{p.capabilities?.vision ? ' · Vision' : ''}{p.capabilities?.reasoning ? ' · Reasoning' : ''} · Key: {p.has_key ? 'hinterlegt' : 'fehlt'}{tk(p, 'angreifer') ? ` · Mandant B: ${tk(p, 'angreifer')}` : ''}{tk(p, 'last') ? ` · Mandant C: ${tk(p, 'last')}` : ''}</div>
      </div>
      <button onclick={() => check(p)} disabled={busy[p.id]}>{busy[p.id] ? 'Prüfe …' : 'Verbindung prüfen'}</button>
      <button onclick={() => edit(p)}>Bearbeiten</button>
      <button class="danger" onclick={() => del(p)}>Löschen</button>
    </div>
    {#if checks[p.id]}
      <table>
        <tbody>
        {#each checks[p.id].items as c}
          <tr><td style="width:1.5rem">{c.ok ? '✔' : '✘'}</td><td>{c.label}</td><td class="small muted">{c.detail}</td></tr>
        {/each}
        </tbody>
      </table>
    {/if}
  </div>
{/each}
