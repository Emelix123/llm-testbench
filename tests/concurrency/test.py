"""Parallelitäts-Test: Wellen von N gleichzeitigen Anfragen je Kontextlänge."""
from __future__ import annotations

import asyncio
import math

from app import stats
from app.loadprofiles import max_context

# Annahme für die Dauer-Schätzung: ~30 Tokens/s pro Anfrage unter Last, ~64 echte Plätze.
_TPS, _SLOTS = 30.0, 64


def estimate(params):
    out = params["output_tokens"]
    per_round = out / _TPS + 3
    total = 60
    for _ctx in params["context_lengths"]:
        for n in params["levels"]:
            total += per_round * math.ceil(n / _SLOTS) + params["pause_s"]
    return total


async def run(ctx):
    p = ctx.params
    levels = sorted(int(x) for x in p["levels"])
    clens = sorted(int(x) for x in p["context_lengths"])
    limit = max_context(ctx) - p["output_tokens"] - 64
    skipped = [c for c in clens if c > limit]
    clens = [c for c in clens if c <= limit]
    if skipped:
        ctx.note(f"Kontextlängen {skipped} übersprungen: größer als max. Kontext ({max_context(ctx)}) "
                 "minus Output. Max. Kontext im Endpunkt-Profil anpassen.")
    await ctx.warmup(n=p["warmup"])
    total_waves = len(levels) * len(clens)
    done = 0
    for c in clens:
        for n in levels:
            if ctx.cancelled:
                return
            ctx.progress(done / max(1, total_waves), f"Kontext {c}, N = {n}")
            stage = f"{c}:{n}"
            ctx.stage({"stage": stage, "context_len": c, "concurrency": n, "event": "start"})

            def make(worker, rng, c=c):
                return ctx.chat_body(ctx.factory.make(c, rng), p["output_tokens"]), {"context_len": c}

            # Alle N Anfragen gleichzeitig vorbereiten, dann gleichzeitig senden,
            # damit die Prompt-Erzeugung den Start nicht staffelt.
            bodies = [make(i, ctx.rng) for i in range(n)]
            await asyncio.gather(*(ctx.send(b, {**m, "stage": stage, "concurrency": n, "worker": i})
                                   for i, (b, m) in enumerate(bodies)))
            ctx.stage({"stage": stage, "context_len": c, "concurrency": n, "event": "end"})
            done += 1
            await asyncio.sleep(p["pause_s"])
    ctx.progress(1.0, "fertig")


# ------------------------------------------------------------------ Auswertung

def sustained_overlap(intervals, min_ms=1000.0):
    """Höchste Zahl gleichzeitig strömender Antworten, die mindestens min_ms lang gehalten wurde."""
    ev = []
    for a, b in intervals:
        if a is None or b is None or b <= a:
            continue
        ev += [(a, 1), (b, -1)]
    if not ev:
        return 0
    ev.sort(key=lambda x: (x[0], x[1]))
    held = {}
    cur, last_t = 0, ev[0][0]
    for t, d in ev:
        if cur > 0:
            held[cur] = held.get(cur, 0) + (t - last_t)
        cur += d
        last_t = t
    best = 0
    for k in sorted(held):
        # Zeit, in der mindestens k gleichzeitig liefen
        if sum(v for kk, v in held.items() if kk >= k) >= min_ms:
            best = k
    return best


def ttft_jump(recs):
    """Sucht den Sprung in der sortierten TTFT-Verteilung. Gibt Anzahl vor dem Sprung oder None zurück."""
    ok = [r for r in recs if stats.is_ok(r) and r.get("t_first_token") is not None]
    if len(ok) < 4:
        return None
    t = sorted(stats.ttft(r) for r in ok)
    decode = [(r["t_last_token"] - r["t_first_token"]) for r in ok if r.get("t_last_token")]
    dec_med = stats.pct(decode, 50) or 0
    thr = max(1000.0, 0.3 * dec_med)
    best_i, best_gap = None, 0
    for i in range(len(t) - 1):
        g = t[i + 1] - t[i]
        if g > thr and g > best_gap:
            best_i, best_gap = i + 1, g
    return best_i


def summarize(recs, stages, cfg, settings):
    slo = settings["slo"]
    by = {}
    for r in recs:
        st = r.get("stage")
        if not st or ":" not in str(st):
            continue
        by.setdefault(st, []).append(r)
    clens = sorted({int(s.split(":")[0]) for s in by})
    rows, per_ctx, chart_ttft, chart_overlap, chart_sorted = [], {}, [], [], []
    any_429 = 0
    for c in clens:
        pts_p50, pts_p95, pts_ov = [], [], []
        best, limit_hit, jump_at_n = 0, False, None
        levels = sorted(int(s.split(":")[1]) for s in by if int(s.split(":")[0]) == c)
        for n in levels:
            rs = by[f"{c}:{n}"]
            agg = stats.aggregate(rs, slo)
            ov = sustained_overlap([(r.get("t_first_token"), r.get("t_last_token")) for r in rs if stats.is_ok(r)])
            jump = ttft_jump(rs)
            n429 = agg["errors"].get("429", 0)
            any_429 += n429
            best = max(best, ov)
            if jump is not None and jump < n and jump_at_n is None:
                jump_at_n = n
                limit_hit = True
            if ov < n * 0.9:
                limit_hit = True
            pts_p50.append([n, agg["ttft_ms"]["p50"]])
            pts_p95.append([n, agg["ttft_ms"]["p95"]])
            pts_ov.append([n, ov])
            rows.append([c, n, ov, jump if jump is not None else "–",
                         stats.fmt_ms(agg["ttft_ms"]["p50"]), stats.fmt_ms(agg["ttft_ms"]["p95"]),
                         stats.fmt_ms(agg["itl_ms"]["p95"]), stats.fmt_num(agg["output_tps"]),
                         agg["ok"], n429, agg["requests"] - agg["ok"] - n429])
        per_ctx[c] = {"real_parallelism": best, "limit_reached": limit_hit, "jump_at_n": jump_at_n,
                      "max_tested": max(levels) if levels else None}
        chart_ttft += [{"name": f"{c // 1000}k p50", "data": pts_p50}, {"name": f"{c // 1000}k p95", "data": pts_p95}]
        chart_overlap.append({"name": f"{c // 1000}k", "data": pts_ov})
        if levels:
            biggest = by[f"{c}:{levels[-1]}"]
            ts = sorted(x for x in (stats.ttft(r) for r in biggest if stats.is_ok(r)) if x is not None)
            chart_sorted.append({"name": f"{c // 1000}k, N={levels[-1]}",
                                 "data": [[i + 1, round(v, 1)] for i, v in enumerate(ts)]})

    def label(c):
        d = per_ctx[c]
        v = d["real_parallelism"]
        return f"{'≥ ' if not d['limit_reached'] else ''}{v} bei {c // 1000}k"

    if not per_ctx:
        return {"verdict": "rot", "headline": "Keine auswertbaren Wellen.", "kpis": {}, "charts": [], "tables": []}
    first = per_ctx[clens[0]]
    head = "Echte Parallelität: " + ", ".join(label(c) for c in clens) + " Kontext."
    if any_429:
        head += f" {any_429}× HTTP 429: LiteLLM begrenzt, nicht die GPU."
    total = len([r for r in recs if ":" in str(r.get("stage"))])
    failed = len([r for r in recs if ":" in str(r.get("stage")) and not stats.is_ok(r)])
    err_rate = 100 * failed / total if total else 0
    verdict = "rot" if err_rate > max(slo["error_rate_pct"], 5) else ("gelb" if any_429 or err_rate > slo["error_rate_pct"] else "gruen")
    kpis = {
        "real_parallelism": {
            "value": first["real_parallelism"],
            "prefix": "" if first["limit_reached"] else "≥ ",
            "unit": f"bei {clens[0] // 1000}k Kontext",
            "text": " · ".join(label(c) for c in clens),
        },
    }
    return {
        "verdict": verdict, "headline": head, "kpis": kpis, "per_context": per_ctx,
        "charts": [
            {"id": "overlap", "title": "Gleichzeitig strömende Antworten (≥ 1 s gehalten)", "type": "line",
             "x": {"name": "gesendete Anfragen N", "log": True}, "y": {"name": "gleichzeitig"},
             "series": chart_overlap + [{"name": "Ideal (= N)", "dashed": True,
                                         "data": [[n, n] for n in sorted({int(s.split(':')[1]) for s in by})]}]},
            {"id": "ttft", "title": "TTFT je Parallelität", "type": "line",
             "x": {"name": "N", "log": True}, "y": {"name": "TTFT (ms)", "log": True}, "series": chart_ttft},
            {"id": "sorted", "title": "TTFT je Anfrage, sortiert (größte Welle) – der Sprung zeigt die Grenze",
             "type": "line", "x": {"name": "Anfrage (sortiert)"}, "y": {"name": "TTFT (ms)"}, "series": chart_sorted},
        ],
        "tables": [{"title": "Wellen", "columns": ["Kontext", "N", "gleichzeitig", "Sprung nach", "TTFT p50",
                                                    "TTFT p95", "ITL p95", "Output Tok/s", "ok", "429", "andere Fehler"],
                    "rows": rows}],
    }
