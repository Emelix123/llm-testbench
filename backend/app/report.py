"""Prüfbericht als eigenständige HTML-Datei (Diagramme als SVG) und als PDF.

PDF: über einen installierten Chrome/Chromium/Edge im Headless-Modus oder, falls
vorhanden, über Playwright. Ohne beides bleibt die HTML-Datei (druckbar).
"""
from __future__ import annotations

import asyncio
import html
import math
import os
import platform
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import stats, storage

PALETTE = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#0891b2", "#db2777", "#ca8a04", "#64748b"]
VERDICT = {"gruen": ("#16a34a", "im Ziel"), "gelb": ("#d79b00", "Hinweise"), "rot": ("#dc2626", "außerhalb Ziel")}
e = html.escape


# ------------------------------------------------------------------ SVG-Diagramm

def _ticks(lo, hi, log):
    if log:
        a, b = math.floor(math.log10(lo)), math.ceil(math.log10(hi))
        return [10 ** k for k in range(a, b + 1)]
    if hi == lo:
        hi = lo + 1
    raw = (hi - lo) / 5
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=raw)
    start = math.floor(lo / step) * step
    return [start + i * step for i in range(int((hi - start) / step) + 2)]


def svg_chart(spec: dict, w: int = 760, h: int = 300) -> str:
    series = [s for s in spec.get("series", []) if any(p[0] is not None and p[1] is not None for p in s["data"])]
    if not series:
        return ""
    xlog, ylog = spec.get("x", {}).get("log"), spec.get("y", {}).get("log")
    pts = [(x, y) for s in series for x, y in s["data"] if x is not None and y is not None]
    ml = [m["y"] for m in spec.get("markLines", []) or []]
    xs = [x for x, _ in pts if not xlog or x > 0]
    ys = [y for _, y in pts if not ylog or y > 0] + [m for m in ml if not ylog or m > 0]
    if not xs or not ys:
        return ""
    x0, x1 = min(xs), max(xs)
    y0, y1 = (min(ys), max(ys)) if ylog else (min(0, min(ys)), max(ys) * 1.05 or 1)
    xt, yt = _ticks(x0, x1, xlog), _ticks(y0, y1, ylog)
    x0, x1 = (min(x0, xt[0]), max(x1, xt[-1])) if xlog else (x0, x1 if x1 > x0 else x0 + 1)
    y0, y1 = min(y0, yt[0]), max(y1, yt[-1])
    L, R, T, B = 70, 20, 30, 45

    def fx(x):
        a, b, v = (math.log10(x0), math.log10(x1), math.log10(max(x, 1e-9))) if xlog else (x0, x1, x)
        return L + (v - a) / ((b - a) or 1) * (w - L - R)

    def fy(y):
        a, b, v = (math.log10(y0), math.log10(y1), math.log10(max(y, 1e-9))) if ylog else (y0, y1, y)
        return h - B - (v - a) / ((b - a) or 1) * (h - T - B)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" font-family="sans-serif" font-size="11">']
    for t in yt:
        if y0 <= t <= y1:
            out.append(f'<line x1="{L}" x2="{w - R}" y1="{fy(t):.1f}" y2="{fy(t):.1f}" stroke="#e5e7eb"/>'
                       f'<text x="{L - 6}" y="{fy(t) + 4:.1f}" text-anchor="end" fill="#555">{e(stats.fmt_num(t, 0 if t >= 10 else 1))}</text>')
    for t in xt:
        if x0 <= t <= x1:
            out.append(f'<text x="{fx(t):.1f}" y="{h - B + 16}" text-anchor="middle" fill="#555">{e(stats.fmt_num(t, 0 if t >= 10 else 1))}</text>')
    out.append(f'<line x1="{L}" x2="{w - R}" y1="{h - B}" y2="{h - B}" stroke="#999"/>')
    out.append(f'<text x="{(L + w - R) / 2}" y="{h - 6}" text-anchor="middle" fill="#333">{e(spec.get("x", {}).get("name", ""))}</text>')
    out.append(f'<text x="14" y="{(T + h - B) / 2}" text-anchor="middle" fill="#333" transform="rotate(-90 14 {(T + h - B) / 2})">{e(spec.get("y", {}).get("name", ""))}</text>')
    for m in spec.get("markLines", []) or []:
        if y0 <= m["y"] <= y1:
            out.append(f'<line x1="{L}" x2="{w - R}" y1="{fy(m["y"]):.1f}" y2="{fy(m["y"]):.1f}" stroke="#dc2626" stroke-dasharray="5 4"/>'
                       f'<text x="{w - R}" y="{fy(m["y"]) - 4:.1f}" text-anchor="end" fill="#dc2626">{e(m.get("name", ""))}</text>')
    lx = L
    for i, s in enumerate(series):
        col = "#94a3b8" if s.get("dashed") else PALETTE[i % len(PALETTE)]
        p = [(fx(x), fy(y)) for x, y in s["data"] if x is not None and y is not None
             and (not xlog or x > 0) and (not ylog or y > 0)]
        if not p:
            continue
        d = " ".join(f"{a:.1f},{b:.1f}" for a, b in p)
        dash = ' stroke-dasharray="6 4"' if s.get("dashed") else ""
        out.append(f'<polyline points="{d}" fill="none" stroke="{col}" stroke-width="2"{dash}/>')
        out += [f'<circle cx="{a:.1f}" cy="{b:.1f}" r="2.6" fill="{col}"/>' for a, b in p]
        out.append(f'<rect x="{lx}" y="8" width="10" height="10" fill="{col}"/><text x="{lx + 14}" y="17">{e(s["name"])}</text>')
        lx += 24 + 7 * len(s["name"])
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------ HTML

CSS = """
body{font-family:Inter,system-ui,sans-serif;color:#16181d;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.45;font-size:14px}
h1{font-size:1.6rem;margin:0}h2{font-size:1.2rem;margin:2rem 0 .5rem;border-bottom:2px solid #e5e7eb;padding-bottom:.3rem}
h3{font-size:1rem;margin:1.2rem 0 .3rem}.muted{color:#5f6673}.small{font-size:.85rem}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:.6rem;margin:1rem 0}
.kpi{border:1px solid #e3e6eb;border-radius:8px;padding:.6rem .8rem}.kpi span{font-size:.8rem;color:#5f6673;display:block}
.kpi b{font-size:1.6rem;display:block}.kpi small{color:#5f6673}
table{border-collapse:collapse;width:100%;font-size:.82rem;margin:.3rem 0 .8rem}th,td{text-align:left;padding:.25rem .4rem;border-bottom:1px solid #e5e7eb;vertical-align:top}
th{color:#5f6673}.light{display:inline-block;width:.8rem;height:.8rem;border-radius:50%;vertical-align:middle;margin-right:.3rem}
.card{border:1px solid #e3e6eb;border-radius:8px;padding:.8rem 1rem;margin:.8rem 0;page-break-inside:avoid}
.note{background:#fff6dc;border-left:3px solid #d79b00;padding:.3rem .6rem;margin:.3rem 0;font-size:.85rem}
.overview td:first-child{white-space:nowrap}
@media print{body{margin:0;max-width:none}.card{break-inside:avoid}h2{break-after:avoid}}
"""


def _kpi(label, value, sub):
    return f'<div class="kpi"><span>{e(label)}</span><b>{e(value)}</b><small>{e(sub or "")}</small></div>'


def render_html(rep: dict, title: str = "Prüfbericht") -> str:
    cards, k = rep["cards"], rep["kpis"]
    tl = rep.get("traffic_light", True)
    first = (cards[0]["config"] if cards else {}) or {}
    prof = first.get("profile") or {}
    s = rep["settings"]
    slo, cost = s["slo"], s["cost"]
    started = min((c["config"].get("started_at") or "" for c in cards), default="")
    finished = max((c["config"].get("finished_at") or "" for c in cards), default="")

    def light(v):
        if not tl or v not in VERDICT:
            return ""
        return f'<span class="light" style="background:{VERDICT[v][0]}"></span>'
    rp = k.get("real_parallelism")
    mu, eu, er = k.get("max_users"), k.get("eur_per_m"), k.get("error_rate_pct")
    sp = k.get("eur_split")
    h = [f"<!doctype html><html lang='de'><head><meta charset='utf-8'><title>{e(title)}</title><style>{CSS}</style></head><body>",
         f"<h1>{e(title)}</h1>",
         f"<p class='muted'>Endpunkt <b>{e(prof.get('name') or '')}</b> · Modell {e(prof.get('model') or '')} · "
         f"{e(prof.get('base_url') or '')}<br>Testzeitraum {e(started.replace('T', ' '))} – {e(finished.replace('T', ' '))} · "
         f"erstellt {datetime.now():%d.%m.%Y %H:%M} · Tool-Version {e(storage.TOOL_VERSION)}</p>",
         "<div class='kpis'>",
         _kpi("Echte Parallelität", (rp.get("prefix", "") + stats.fmt_num(rp["value"])) if rp else "–", rp.get("text") if rp else "nicht gemessen"),
         _kpi("Max. Nutzer bei den Zielwerten", stats.fmt_num(mu["value"]) if mu else "–", mu.get("unit") if mu else "nicht gemessen"),
         _kpi("€ pro 1 Mio. Tokens", (stats.fmt_num(eu["value"], 2) + " €") if eu else "–",
              (f"Input {stats.fmt_num(sp['input'], 3)} € · Output {stats.fmt_num(sp['output'], 2)} € · Cache {stats.fmt_num(sp['cache'], 3)} €"
               if sp else (eu.get("unit") if eu else "nicht gemessen"))),
         _kpi("Fehlerquote", (stats.fmt_num(er["value"], 2) + " %") if er else "–", er.get("text") if er else ""),
         "</div>",
         f"<p class='small muted'>Zielwerte: TTFT p95 ≤ {e(stats.fmt_ms(slo['ttft_p95_ms']))}, ITL p95 ≤ {e(stats.fmt_ms(slo['itl_p95_ms']))}, "
         f"Fehlerquote ≤ {e(stats.fmt_pct(slo['error_rate_pct']))}. Kosten: {e(stats.fmt_num(cost['machine_eur_per_h'], 2))} €/h + "
         f"{e(stats.fmt_num(cost['power_kw'], 2))} kW × {e(stats.fmt_num(cost['power_eur_per_kwh'], 2))} €/kWh, Auslastung "
         f"{e(stats.fmt_num(cost['utilization_pct']))} %, Lesezeit {e(stats.fmt_num(cost['read_time_s']))} s. "
         "Parallele Anfragen sind nicht gleich Nutzer: aktive Nutzer ≈ Parallelität × (Antwortzeit + Lesezeit) / Antwortzeit.</p>",
         "<h2>Übersicht</h2><table class='overview'><tr><th>Test</th><th>Ergebnis</th></tr>"]
    for c in cards:
        h.append(f"<tr><td>{light(c['verdict'])}{e(c['name'] or '')}</td><td>{e(c['headline'] or '')}"
                 f"{' <i>(' + e(c['status']) + ')</i>' if c['status'] != 'fertig' else ''}</td></tr>")
    h.append("</table>")
    for c in cards:
        sm, cfg = c["summary"], c["config"]
        h.append(f"<div class='card'><h2 style='margin-top:0'>{light(c['verdict'])}{e(c['name'] or '')}</h2>"
                 f"<p>{e(c['headline'] or '')}</p>")
        if sm.get("slo_text"):
            h.append(f"<p class='small muted'>{e(sm['slo_text'])}</p>")
        for n in sm.get("notes") or []:
            h.append(f"<div class='note'>{e(n)}</div>")
        for ch in sm.get("charts") or []:
            svg = svg_chart(ch)
            if svg:
                h.append(f"<h3>{e(ch['title'])}</h3>{svg}")
        for t in sm.get("tables") or []:
            if not t["rows"]:
                continue
            h.append(f"<h3>{e(t['title'])}</h3><table><tr>" + "".join(f"<th>{e(str(x))}</th>" for x in t["columns"]) + "</tr>")
            for row in t["rows"]:
                h.append("<tr>" + "".join(f"<td>{e(str(x))}</td>" for x in row) + "</tr>")
            h.append("</table>")
        cl = cfg.get("client") or {}
        net = cl.get("network_latency") or {}
        h.append(f"<p class='small muted'>Lauf {e(c['run_id'])} · Seed {e(str(cfg.get('seed')))} · Netzlatenz p50 "
                 f"{e(str(net.get('p50_ms')))} ms · Verbindung {e(str(cl.get('network_kind')))} · Parameter "
                 f"{e(', '.join(f'{k}={v}' for k, v in (cfg.get('params') or {}).items()))}</p></div>")
    fp = first.get("endpoint_fingerprint") or {}
    h.append("<h2>Steckbrief des Endpunkts</h2><table>"
             f"<tr><td>Modelle (/v1/models)</td><td>{e(', '.join(fp.get('models') or []) or '–')}</td></tr>"
             f"<tr><td>LiteLLM-Header</td><td>{e(', '.join(f'{a}={b}' for a, b in (fp.get('headers') or {}).items()) or '–')}</td></tr>"
             f"<tr><td>Prompts</td><td>{e(str((first.get('prompt_factory') or {}).get('tokenizer', '–')))}</td></tr>"
             "</table><p class='small muted'>Alle Werte stammen aus dem, was der Testrechner sieht: Stream-Zeitstempel, "
             "usage-Felder und LiteLLM-Header. GPU-Leistung, Temperatur und VRAM sind ohne Serverzugriff nicht verfügbar.</p>"
             "</body></html>")
    return "".join(h)


# ------------------------------------------------------------------ PDF

def find_browsers() -> list[str]:
    """Kandidaten für Headless-PDF, schlanke Headless-Shell zuerst."""
    cands = [os.environ.get("LLMTB_CHROME", "")]
    for n in ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
              "chrome", "msedge", "microsoft-edge"):
        cands.append(shutil.which(n) or "")
    if platform.system() == "Darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    if platform.system() == "Windows":
        for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")):
            cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                      os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    for root in ("/opt/pw-browsers", os.path.expanduser("~/.cache/ms-playwright")):
        if os.path.isdir(root):
            cands += sorted(str(p) for p in Path(root).rglob("chrome-headless-shell") if p.is_file())
            cands += sorted(str(p) for p in Path(root).rglob("chrome") if p.is_file())
    out = []
    for c in cands:
        if c and os.path.isfile(c) and c not in out:
            out.append(c)
    out.sort(key=lambda c: 0 if "headless-shell" in c else 1)
    return out


def html_to_pdf(html_path: Path, pdf_path: Path) -> str | None:
    """Gibt None bei Erfolg zurück, sonst eine Fehlermeldung."""
    browsers = find_browsers()
    if not browsers:
        return "Kein Chrome/Edge/Chromium gefunden – HTML-Bericht im Browser öffnen und als PDF drucken."
    errors = []
    for exe in browsers:
        pdf_path.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as prof:
            cmd = [exe, "--headless=new" if "headless-shell" not in exe else "--headless", "--no-sandbox",
                   "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                   "--disable-background-networking", "--disable-component-update", "--disable-sync",
                   f"--user-data-dir={prof}", "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}",
                   html_path.resolve().as_uri()]
            try:
                subprocess.run(cmd, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{Path(exe).name}: {type(ex).__name__}")
                continue
        if pdf_path.exists() and pdf_path.stat().st_size > 1000:
            return None
        errors.append(f"{Path(exe).name}: kein PDF")
    return "PDF-Erzeugung fehlgeschlagen (" + "; ".join(errors) + "). HTML-Bericht im Browser öffnen und als PDF drucken."


async def write_report(rep: dict, out_dir: Path, title: str = "Prüfbericht") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    hp, pp = out_dir / "pruefbericht.html", out_dir / "pruefbericht.pdf"
    hp.write_text(render_html(rep, title), encoding="utf-8")
    err = await asyncio.to_thread(html_to_pdf, hp, pp)
    return {"html": str(hp), "pdf": str(pp) if err is None else None, "pdf_error": err}
