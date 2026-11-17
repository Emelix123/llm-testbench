<script>
  let { explain, open = true } = $props()
  let show = $state(open)
  const roleLabel = { system: 'System', opfer: 'Opfer', angreifer: 'Angreifer' }
</script>

{#if explain}
  <div class="exp">
    <button class="head" onclick={() => (show = !show)} aria-expanded={show}>
      <span class="grow"><b>So funktioniert der Angriff</b>{#if explain.cve} <span class="cve">{explain.cve}</span>{/if}</span>
      <span class="chev">{show ? '▴' : '▾'}</span>
    </button>
    {#if show}
      <div class="body">
        {#if explain.kurz}<p class="lead">{explain.kurz}</p>{/if}

        {#if explain.ablauf?.length}
          <ol class="steps">
            {#each explain.ablauf as s, i}
              <li>
                <span class="num">{i + 1}</span>
                <span class="role {s.rolle}">{roleLabel[s.rolle] ?? s.rolle}</span>
                <span class="txt">{s.text}</span>
              </li>
            {/each}
          </ol>
        {/if}

        <div class="grid">
          {#if explain.signal}<div class="box"><h4>Gemessenes Signal</h4><p>{explain.signal}</p></div>{/if}
          {#if explain.mass}<div class="box"><h4>Maß</h4><p>{explain.mass}</p></div>{/if}
        </div>

        <div class="grid">
          {#if explain.verwundbar}<div class="box v"><h4><i></i> Verwundbar</h4><p>{explain.verwundbar}</p></div>{/if}
          {#if explain.geloest}<div class="box g"><h4><i></i> Nicht verwundbar (gelöst)</h4><p>{explain.geloest}</p></div>{/if}
        </div>

        {#if explain.quellen?.length}
          <div class="src">
            <h4>Quellen</h4>
            <ul>
              {#each explain.quellen as q}
                <li>{#if q.url}<a href={q.url} target="_blank" rel="noreferrer">{q.ref}</a>{:else}<b>{q.ref}</b>{/if}{#if q.text} – {q.text}{/if}</li>
              {/each}
            </ul>
          </div>
        {/if}
      </div>
    {/if}
  </div>
{/if}

<style>
  .exp { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; margin: .4rem 0 1rem; background: var(--bg); }
  .head { width: 100%; border: none; background: var(--accent-weak); color: var(--text); text-align: left;
    padding: .6rem .9rem; display: flex; align-items: center; gap: .5rem; cursor: pointer; font-size: .95rem; }
  .cve { font-size: .78rem; color: var(--muted); font-family: ui-monospace, monospace; }
  .chev { color: var(--muted); }
  .body { padding: .9rem; }
  .lead { margin: 0 0 .9rem; font-size: 1rem; }
  .steps { list-style: none; margin: 0 0 1rem; padding: 0; display: flex; flex-direction: column; gap: .5rem; }
  .steps li { display: grid; grid-template-columns: auto auto 1fr; gap: .5rem; align-items: start; }
  .num { width: 1.4rem; height: 1.4rem; border-radius: 50%; background: var(--accent); color: #fff;
    display: flex; align-items: center; justify-content: center; font-size: .8rem; font-weight: 700; flex: none; }
  .role { font-size: .72rem; font-weight: 700; padding: .12rem .45rem; border-radius: 99px; white-space: nowrap;
    align-self: start; margin-top: .05rem; }
  .role.system { background: #e5e7eb; color: #374151; }
  .role.opfer { background: #dbeafe; color: #1e40af; }
  .role.angreifer { background: #fee2e2; color: #b91c1c; }
  .txt { font-size: .92rem; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem; margin-bottom: .6rem; }
  @media (max-width: 640px) { .grid { grid-template-columns: 1fr; } }
  .box { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: .55rem .75rem; }
  .box h4 { margin: 0 0 .25rem; font-size: .82rem; display: flex; align-items: center; gap: .35rem; }
  .box p { margin: 0; font-size: .88rem; }
  .box i { width: .7rem; height: .7rem; border-radius: 50%; display: inline-block; }
  .box.v { border-color: var(--red); } .box.v i { background: var(--red); } .box.v h4 { color: var(--red); }
  .box.g { border-color: var(--green); } .box.g i { background: var(--green); } .box.g h4 { color: var(--green); }
  .src { font-size: .84rem; }
  .src h4 { margin: .2rem 0 .3rem; }
  .src ul { margin: 0; padding-left: 1.1rem; }
  .src li { margin-bottom: .2rem; }
</style>
