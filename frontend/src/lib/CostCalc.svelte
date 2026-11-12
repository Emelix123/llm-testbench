<script>
  import { num } from './api.js'
  // cards: Karten der Ergebnisseite; settings: gespeicherte Kostenannahmen (Startwerte der Regler)
  let { cards, settings } = $props()
  let c = $state({ ...settings.cost })

  const sweep = $derived(cards.find((x) => x.test === 'capacity_sweep')?.summary?.profiles || null)
  const split = $derived(cards.find((x) => x.test === 'cost_split')?.summary?.raw || null)
  const eurH = $derived(Number(c.machine_eur_per_h) + Number(c.power_kw) * Number(c.power_eur_per_kwh))
  const perM = (tps) => (tps ? (eurH / (tps * 3600 * (c.utilization_pct / 100))) * 1e6 : null)
  const users = (b) => (b.e2e_s ? (b.concurrency * (b.e2e_s + Number(c.read_time_s))) / b.e2e_s : null)
</script>

{#if sweep || split}
  <div class="panel stack calc">
    <h3>Kostenrechner</h3>
    <p class="small muted">Die Regler ändern nur diese Ansicht. Dauerhaft ändert man die Annahmen unter <a href="#/einstellungen">Einstellungen</a>.</p>
    <div class="grid2">
      <label class="field"><span>Auslastung über den Tag: {c.utilization_pct} %</span><input type="range" min="5" max="100" bind:value={c.utilization_pct} /></label>
      <label class="field"><span>Lesezeit zwischen zwei Anfragen: {c.read_time_s} s</span><input type="range" min="0" max="300" bind:value={c.read_time_s} /></label>
      <label class="field"><span>Maschine €/h</span><input type="number" step="0.01" bind:value={c.machine_eur_per_h} /></label>
      <label class="field"><span>Leistungsaufnahme (kW)</span><input type="number" step="0.01" bind:value={c.power_kw} /></label>
      <label class="field"><span>Strompreis (€/kWh)</span><input type="number" step="0.01" bind:value={c.power_eur_per_kwh} /></label>
    </div>
    {#if split}
      <div class="kp">
        <div><span>Input</span><b>{num(perM(split.input_tps), 3)} €</b></div>
        <div><span>Output</span><b>{num(perM(split.output_tps), 2)} €</b></div>
        <div><span>Cache-Treffer</span><b>{num(perM(split.input_tps) * split.cache_cost_factor, 3)} €</b></div>
        <div class="muted small">je 1 Mio. Tokens, aus Prefill {num(split.input_tps)} Tok/s und Decode {num(split.output_tps)} Tok/s</div>
      </div>
    {/if}
    {#if sweep}
      <table>
        <thead><tr><th>Lastprofil</th><th class="num">parallel im Ziel</th><th class="num">aktive Nutzer</th><th class="num">€ / 1 Mio. Tokens (gesamt)</th><th class="num">€ / 1 Mio. Output-Tokens</th></tr></thead>
        <tbody>
          {#each Object.values(sweep) as p}
            <tr><td>{p.name}</td>
              {#if p.best}
                <td class="num">{p.best.concurrency}</td><td class="num">{num(users(p.best))}</td>
                <td class="num">{num(perM((p.best.output_tps || 0) + (p.best.input_tps || 0)), 3)} €</td>
                <td class="num">{num(perM(p.best.output_tps), 2)} €</td>
              {:else}<td colspan="4" class="muted">keine Stufe im Ziel</td>{/if}
            </tr>
          {/each}
        </tbody>
      </table>
      <p class="small muted">Aktive Nutzer ≈ Parallelität × (Antwortzeit + Lesezeit) / Antwortzeit. Parallele Anfragen sind nicht gleich Nutzer.</p>
    {/if}
  </div>
{/if}

<style>
  .calc { margin-top: .8rem; }
  .kp { display: flex; gap: 1.2rem; align-items: flex-end; flex-wrap: wrap; }
  .kp div { display: flex; flex-direction: column; }
  .kp span { font-size: .8rem; color: var(--muted); }
  .kp b { font-size: 1.3rem; }
</style>
