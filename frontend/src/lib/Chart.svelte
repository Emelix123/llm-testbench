<script>
  import * as echarts from 'echarts'
  import { onMount } from 'svelte'
  let { spec } = $props()
  let el

  const palette = ['#2563eb', '#ea580c', '#16a34a', '#9333ea', '#0891b2', '#db2777', '#ca8a04', '#64748b']

  function option(s) {
    const dark = matchMedia('(prefers-color-scheme: dark)').matches
    const fg = dark ? '#c9ced8' : '#3b4250'
    const grid = dark ? '#2a2f3a' : '#e8eaee'
    const axis = (a = {}) => ({
      type: a.log ? 'log' : 'value', name: a.name, nameLocation: 'middle', nameGap: 32,
      axisLabel: { color: fg, formatter: (v) => v.toLocaleString('de-DE') }, nameTextStyle: { color: fg },
      splitLine: { lineStyle: { color: grid } }, axisLine: { lineStyle: { color: grid } }, scale: true,
    })
    const series = s.series.map((x, i) => ({
      name: x.name, type: s.type === 'scatter' ? 'scatter' : 'line', data: x.data.filter((d) => d[0] != null && d[1] != null),
      showSymbol: true, symbolSize: 6, smooth: false,
      lineStyle: { width: 2, type: x.dashed ? 'dashed' : 'solid' },
      color: x.dashed ? '#94a3b8' : palette[i % palette.length],
    }))
    if (s.markLines?.length && series.length) {
      series[0].markLine = {
        symbol: 'none', silent: true, lineStyle: { color: '#dc2626', type: 'dashed' },
        label: { formatter: '{b}', color: '#dc2626' },
        data: s.markLines.map((m) => ({ yAxis: m.y, name: m.name })),
      }
    }
    return {
      color: palette, animation: false,
      tooltip: { trigger: 'axis', valueFormatter: (v) => (typeof v === 'number' ? v.toLocaleString('de-DE') : v) },
      legend: { top: 0, textStyle: { color: fg } },
      grid: { left: 64, right: 24, top: 40, bottom: 48 },
      xAxis: axis(s.x), yAxis: { ...axis(s.y), nameGap: 48 },
      series,
    }
  }

  onMount(() => {
    const chart = echarts.init(el)
    chart.setOption(option(spec))
    const ro = new ResizeObserver(() => chart.resize())
    ro.observe(el)
    return () => { ro.disconnect(); chart.dispose() }
  })
</script>

<div class="chart">
  <h3>{spec.title}</h3>
  <div bind:this={el} style="height: 320px; width: 100%"></div>
</div>
