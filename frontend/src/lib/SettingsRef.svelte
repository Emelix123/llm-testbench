<script>
  let { settings, open = false } = $props()
  let show = $state(open)
  const has = $derived(settings && ((settings.vllm?.length || 0) + (settings.litellm?.length || 0) > 0))
</script>

{#if has}
  <div class="sref">
    <button class="head" onclick={() => (show = !show)} aria-expanded={show}>
      <span class="grow"><b>Relevante Einstellungen</b> <span class="muted">vLLM &amp; LiteLLM</span></span>
      <span class="chev">{show ? '▴' : '▾'}</span>
    </button>
    {#if show}
      <div class="body">
        {#each [['vLLM (Inferenz-Engine)', settings.vllm], ['LiteLLM (Gateway)', settings.litellm]] as [title, list]}
          {#if list?.length}
            <div class="col">
              <h4>{title}</h4>
              <table>
                <tbody>
                  {#each list as s}
                    <tr><td class="k"><code>{s.name}</code></td><td class="w">{s.wirkung}</td></tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        {/each}
      </div>
    {/if}
  </div>
{/if}

<style>
  .sref { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; margin: .4rem 0 1rem; background: var(--bg); }
  .head { width: 100%; border: none; background: var(--panel); color: var(--text); text-align: left;
    padding: .55rem .9rem; display: flex; align-items: center; gap: .5rem; cursor: pointer; font-size: .95rem; }
  .muted { color: var(--muted); font-size: .85rem; }
  .chev { color: var(--muted); }
  .body { padding: .3rem .9rem .8rem; display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  @media (max-width: 760px) { .body { grid-template-columns: 1fr; } }
  h4 { margin: .6rem 0 .3rem; font-size: .85rem; color: var(--accent); }
  table { width: 100%; border-collapse: collapse; }
  td { padding: .28rem .3rem; border-bottom: 1px solid var(--line); vertical-align: top; font-size: .85rem; }
  td.k { white-space: normal; width: 42%; }
  code { font-size: .8rem; background: var(--accent-weak); padding: .05rem .3rem; border-radius: 4px; word-break: break-word; }
  .w { color: var(--text); }
</style>
