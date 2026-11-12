<script>
  import { onMount } from 'svelte'
  import { live, refresh } from './lib/state.svelte.js'
  import Overview from './views/Overview.svelte'
  import Endpoints from './views/Endpoints.svelte'
  import Tests from './views/Tests.svelte'
  import Suites from './views/Suites.svelte'
  import Results from './views/Results.svelte'
  import Report from './views/Report.svelte'
  import Compare from './views/Compare.svelte'
  import Settings from './views/Settings.svelte'
  import Live from './views/Live.svelte'

  const menu = [
    ['', 'Übersicht'], ['endpunkte', 'Endpunkte'], ['tests', 'Tests'], ['suiten', 'Suiten'],
    ['ergebnisse', 'Ergebnisse'], ['vergleich', 'Vergleich'], ['einstellungen', 'Einstellungen'],
  ]
  const views = { '': Overview, endpunkte: Endpoints, tests: Tests, suiten: Suites, ergebnisse: Results,
    bericht: Report, vergleich: Compare, einstellungen: Settings, live: Live }

  let route = $state(parse())
  let open = $state(false)
  function parse() {
    const h = location.hash.replace(/^#\/?/, '')
    const [path, q] = h.split('?')
    return { path: path || '', query: Object.fromEntries(new URLSearchParams(q || '')) }
  }
  onMount(() => {
    const on = () => { route = parse(); open = false; window.scrollTo(0, 0) }
    addEventListener('hashchange', on)
    refresh()
    return () => removeEventListener('hashchange', on)
  })
  const View = $derived(views[route.path] || Overview)
  const running = $derived(live.batch && live.batch.state === 'laeuft')
</script>

<header>
  <button class="burger" aria-label="Menü" onclick={() => (open = !open)}>
    <span></span><span></span><span></span>
  </button>
  <a class="brand" href="#/">LLM-Inferenz-Testplattform</a>
  <div class="grow"></div>
  {#if running}
    <a class="runpill" href="#/live"><i></i> Test läuft – {live.batch.live?.inflight ?? 0} parallel</a>
  {:else}
    <a class="btn primary-link" href="#/tests">Neuer Test</a>
  {/if}
</header>

{#if open}
  <div class="scrim" onclick={() => (open = false)} role="presentation"></div>
{/if}
<nav class:open>
  {#each menu as [p, label]}
    <a href="#/{p}" class:active={route.path === p}>{label}</a>
  {/each}
  {#if running}<a href="#/live" class:active={route.path === 'live'}>Laufender Test</a>{/if}
</nav>

<main>
  {#key route.path + JSON.stringify(route.query)}
    <View query={route.query} />
  {/key}
</main>

<style>
  header {
    position: sticky; top: 0; z-index: 20; display: flex; align-items: center; gap: .8rem;
    padding: .6rem 1rem; background: var(--panel); border-bottom: 1px solid var(--line);
  }
  .brand { font-weight: 700; color: var(--text); text-decoration: none; }
  .burger { width: 40px; height: 36px; padding: 8px; flex-direction: column; justify-content: space-between; display: flex; }
  .burger span { display: block; height: 2px; background: var(--text); border-radius: 2px; width: 100%; }
  nav {
    position: fixed; top: 0; left: 0; bottom: 0; width: 250px; background: var(--panel); z-index: 40;
    border-right: 1px solid var(--line); padding: 4rem .6rem 1rem; transform: translateX(-100%); transition: transform .2s;
    display: flex; flex-direction: column; gap: .15rem;
  }
  nav.open { transform: none; }
  nav a { padding: .55rem .8rem; border-radius: 8px; text-decoration: none; color: var(--text); }
  nav a:hover { background: var(--bg); }
  nav a.active { background: var(--accent-weak); color: var(--accent); font-weight: 600; }
  .scrim { position: fixed; inset: 0; background: rgba(0,0,0,.25); z-index: 30; }
  main { max-width: 1180px; margin: 0 auto; padding: 1.4rem 1rem 4rem; }
  .runpill { display: inline-flex; gap: .5rem; align-items: center; text-decoration: none; font-weight: 600; font-size: .9rem;
    color: var(--accent); background: var(--accent-weak); padding: .35rem .8rem; border-radius: 99px; }
  .runpill i { width: .55rem; height: .55rem; border-radius: 50%; background: var(--accent); animation: p 1.2s infinite; }
  @keyframes p { 50% { opacity: .25 } }
  .primary-link { background: var(--accent); color: #fff; border-color: var(--accent); font-weight: 600; }
</style>
