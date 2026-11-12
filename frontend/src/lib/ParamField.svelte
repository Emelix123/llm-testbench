<script>
  let { param, value = $bindable() } = $props()
  let text = $state(Array.isArray(value) ? value.join(', ') : '')

  function setList(t) {
    text = t
    value = t.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean).map(Number).filter((x) => !Number.isNaN(x))
  }
  function toggle(v) {
    const s = new Set(value || [])
    s.has(v) ? s.delete(v) : s.add(v)
    value = param.options.map((o) => o.value).filter((x) => s.has(x))
  }
</script>

<label class="field">
  <span>{param.label}{#if param.help} · {param.help}{/if}</span>
  {#if param.type === 'int' || param.type === 'float'}
    <input type="number" step={param.type === 'int' ? 1 : 'any'} min={param.min} max={param.max}
      value={value ?? ''} placeholder="leer"
      oninput={(e) => (value = e.target.value === '' ? null : Number(e.target.value))} />
  {:else if param.type === 'int_list'}
    <input value={text} oninput={(e) => setList(e.target.value)} placeholder="z. B. 8, 16, 32" />
  {:else if param.type === 'bool'}
    <input type="checkbox" bind:checked={value} />
  {:else if param.type === 'multi'}
    <div class="multi">
      {#each param.options as o}
        <label><input type="checkbox" checked={(value || []).includes(o.value)} onchange={() => toggle(o.value)} /> {o.label}</label>
      {/each}
    </div>
  {:else}
    <input bind:value />
  {/if}
</label>

<style>
  .multi { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: .2rem .8rem; font-size: .9rem; }
  .multi label { display: flex; gap: .4rem; align-items: center; }
</style>
