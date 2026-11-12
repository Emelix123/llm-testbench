<script>
  import { verdictText } from './api.js'
  let { verdict, show = true, label = false } = $props()
</script>

{#if show && verdict}
  <span class="light {verdict}" title={verdictText[verdict]}>
    <i></i>{#if label}{verdictText[verdict]}{/if}
  </span>
{/if}

<style>
  .light { display: inline-flex; align-items: center; gap: .35rem; font-size: .82rem; font-weight: 600; }
  i { width: .8rem; height: .8rem; border-radius: 50%; display: inline-block; flex: none; }
  .gruen i { background: var(--green); } .gruen { color: var(--green); }
  .gelb i { background: var(--yellow); } .gelb { color: var(--yellow); }
  .rot i { background: var(--red); } .rot { color: var(--red); }
</style>
