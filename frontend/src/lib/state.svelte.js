import { api } from './api.js'

export const live = $state({ batch: null, log: [], connected: false })
let es = null

export function connect() {
  if (es) es.close()
  es = new EventSource('/api/events')
  live.connected = true
  es.addEventListener('batch', (e) => { live.batch = JSON.parse(e.data); if (live.batch.state !== 'laeuft') close() })
  es.addEventListener('run', (e) => {
    const r = JSON.parse(e.data)
    if (!live.batch) return
    const i = live.batch.runs.findIndex((x) => x.index === r.index)
    if (i >= 0) live.batch.runs[i] = r
  })
  es.addEventListener('progress', (e) => {
    const p = JSON.parse(e.data)
    const r = live.batch?.runs.find((x) => x.run_id === p.run_id)
    if (r) { r.progress = p.progress; r.text = p.text }
  })
  es.addEventListener('live', (e) => { if (live.batch) live.batch.live = JSON.parse(e.data) })
  es.addEventListener('log', (e) => { live.log = [...live.log, JSON.parse(e.data).msg].slice(-50) })
  es.addEventListener('idle', () => close())
  es.onerror = () => { close(); setTimeout(refresh, 3000) }
}
function close() { es?.close(); es = null; live.connected = false }

export async function refresh() {
  const b = await api.get('/api/batch/current').catch(() => null)
  live.batch = b
  if (b && b.state === 'laeuft' && !es) connect()
}
