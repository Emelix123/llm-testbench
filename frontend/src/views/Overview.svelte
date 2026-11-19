<script>
  import { onMount } from 'svelte'
  import { api, dt } from '../lib/api.js'
  import Light from '../lib/Light.svelte'
  let batches = $state(null), settings = $state(null), profiles = $state([])
  onMount(async () => {
    ;[batches, settings, profiles] = await Promise.all([api.get('/api/batches'), api.get('/api/settings'), api.get('/api/profiles')])
  })
  const link = (b) => '#/bericht?runs=' + b.runs.map((r) => r.id).join(',')
</script>

<h1>Übersicht</h1>
{#if batches && (profiles.length === 0 || batches.length === 0)}
  <div class="panel stack">
    <h3>Los geht's</h3>
    <p>Die <a href="#/anleitung">Anleitung</a> erklärt alles Schritt für Schritt. Kurz:</p>
    <ol>
      <li><a href="#/steuerung">Steuerung</a> → Systemprüfung ansehen, „Einrichten“ und „Images & Modell laden“ (einmalig).</li>
      <li>Dort einen <b>Gesamtlauf</b> starten – er misst alle Szenarien K0–K6 nacheinander.</li>
      <li>Ergebnisse auf <a href="#/konfigurationen">Konfigurationen</a> ansehen und als LaTeX/CSV exportieren.</li>
    </ol>
    <p class="small muted">Einen eigenen, entfernten Endpunkt testen: unter <a href="#/endpunkte">Endpunkte</a> ein Profil anlegen,
      dann unter <a href="#/tests">Tests</a> starten.</p>
  </div>
{/if}

<h2>Letzte Läufe</h2>
{#if !batches}
  <p class="muted">Lade …</p>
{:else if batches.length === 0}
  <p class="muted">Noch keine Läufe.</p>
{:else}
  <div class="panel tablewrap">
    <table>
      <thead><tr><th></th><th>Datum</th><th>Endpunkt</th><th>Tests</th><th>Ergebnis</th></tr></thead>
      <tbody>
        {#each batches.slice(0, 10) as b}
          <tr>
            <td><Light verdict={b.verdict} show={settings?.traffic_light} /></td>
            <td><a href={link(b)}>{dt(b.started_at)}</a></td>
            <td>{b.profile_name}</td>
            <td>{b.runs.map((r) => r.test_name).join(', ')}</td>
            <td class="small">{b.runs.map((r) => r.headline).filter(Boolean)[0] ?? ''}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}
