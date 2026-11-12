async function req(method, url, body) {
  const r = await fetch(url, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) {
    let msg = r.statusText
    try { msg = (await r.json()).detail || msg } catch {}
    throw new Error(msg)
  }
  return r.json()
}
export const api = {
  get: (u) => req('GET', u),
  post: (u, b) => req('POST', u, b ?? {}),
  put: (u, b) => req('PUT', u, b),
  del: (u) => req('DELETE', u),
}

const nf = (d) => new Intl.NumberFormat('de-DE', { maximumFractionDigits: d, minimumFractionDigits: d })
export const num = (x, d = 0) => (x === null || x === undefined ? '–' : nf(d).format(x))
export function dur(s) {
  if (s == null) return '–'
  s = Math.round(s)
  if (s < 60) return `${s} s`
  const m = Math.round(s / 60)
  if (m < 60) return `ca. ${m} min`
  const h = Math.floor(m / 60), r = m % 60
  return `ca. ${h} h${r ? ' ' + r + ' min' : ''}`
}
export function dt(iso) {
  if (!iso) return '–'
  const d = new Date(iso)
  return d.toLocaleString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}
export const verdictText = { gruen: 'im Ziel', gelb: 'Hinweise', rot: 'außerhalb Ziel' }
