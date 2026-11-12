<script>
  import { onMount } from 'svelte'
  import { api } from '../lib/api.js'
  let s = $state(null), saved = $state(false)
  onMount(async () => { s = await api.get('/api/settings') })
  async function save() { s = await api.put('/api/settings', $state.snapshot(s)); saved = true; setTimeout(() => (saved = false), 2000) }
</script>

<h1>Einstellungen</h1>
{#if s}
  <div class="panel stack">
    <h3>Zielwerte (SLOs)</h3>
    <p class="small muted">Standardwerte, solange die echten Zielwerte offen sind. Die Ampel bewertet nur nach dem Lauf und bricht nie etwas ab. Ergebnisse werden beim nächsten Öffnen mit den neuen Werten neu berechnet.</p>
    <div class="grid2">
      <label class="field"><span>TTFT p95 höchstens (ms)</span><input type="number" bind:value={s.slo.ttft_p95_ms} /></label>
      <label class="field"><span>ITL p95 höchstens (ms) – {Math.round(1000 / (s.slo.itl_p95_ms || 1))} Tokens/s je Nutzer</span><input type="number" bind:value={s.slo.itl_p95_ms} /></label>
      <label class="field"><span>Fehlerquote höchstens (%)</span><input type="number" step="0.1" bind:value={s.slo.error_rate_pct} /></label>
    </div>
    <label class="row"><input type="checkbox" bind:checked={s.traffic_light} /> Ampel anzeigen (aus: nur Zahlen)</label>
  </div>
  <div class="panel stack" style="margin-top:.8rem">
    <h3>Kostenannahmen</h3>
    <div class="grid2">
      <label class="field"><span>Maschinenkosten (€/h, Abschreibung + Betrieb)</span><input type="number" step="0.01" bind:value={s.cost.machine_eur_per_h} /></label>
      <label class="field"><span>Leistungsaufnahme (kW)</span><input type="number" step="0.01" bind:value={s.cost.power_kw} /></label>
      <label class="field"><span>Strompreis (€/kWh)</span><input type="number" step="0.01" bind:value={s.cost.power_eur_per_kwh} /></label>
      <label class="field"><span>Auslastung über den Tag: {s.cost.utilization_pct} %</span><input type="range" min="5" max="100" bind:value={s.cost.utilization_pct} /></label>
      <label class="field"><span>Lesezeit zwischen zwei Anfragen eines Nutzers: {s.cost.read_time_s} s</span><input type="range" min="0" max="300" bind:value={s.cost.read_time_s} /></label>
    </div>
  </div>
  <div class="panel stack" style="margin-top:.8rem">
    <h3>Testrechner</h3>
    <div class="grid2">
      <label class="field"><span>CPU-Warnung ab (%)</span><input type="number" bind:value={s.client.cpu_warn_pct} /></label>
      <label class="field"><span>Timeout je Anfrage (s)</span><input type="number" bind:value={s.client.request_timeout_s} /></label>
    </div>
  </div>
  <div class="row" style="margin-top:1rem"><button class="primary" onclick={save}>Speichern</button>{#if saved}<span class="muted">gespeichert</span>{/if}</div>
{/if}
