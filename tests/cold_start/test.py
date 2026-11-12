"""Kaltstart: erste Anfrage nach einer Pause gegen warme Anfragen."""
from __future__ import annotations

import asyncio
import time

from app import stats


def estimate(params):
    return params["pause_min"] * 60 + 2 * params["warm_requests"] * 5 + 20


async def run(ctx):
    p = ctx.params

    async def batch(tag, n):
        for _ in range(n):
            if ctx.cancelled:
                return
            await ctx.send(ctx.chat_body(ctx.factory.make(p["input_tokens"], ctx.rng), p["output_tokens"]),
                           {"stage": tag, "context_len": p["input_tokens"]})
    ctx.progress(0.02, "warme Anfragen vorher")
    await batch("warm_vorher", p["warm_requests"])
    pause = p["pause_min"] * 60
    end = time.monotonic() + pause
    while time.monotonic() < end and not ctx.cancelled:
        left = end - time.monotonic()
        ctx.progress(0.05 + 0.85 * (1 - left / pause), f"Pause, noch {int(left // 60)}:{int(left % 60):02d} min")
        await asyncio.sleep(min(5, max(0.0, left)))
    if ctx.cancelled:
        return
    # neue Verbindung, damit auch der Verbindungsaufbau mitgemessen wird
    await ctx.client.aclose()
    from app.client import make_client
    ctx.client = make_client(timeout_s=ctx.timeout_s)
    ctx.progress(0.92, "erste Anfrage nach der Pause")
    await batch("kalt", 1)
    await batch("warm_nachher", p["warm_requests"])
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    def t(tag):
        return [stats.ttft(r) for r in recs if r.get("stage") == tag and stats.is_ok(r)]
    warm = t("warm_vorher") + t("warm_nachher")
    cold = t("kalt")
    if not cold or not warm:
        return {"verdict": "rot", "headline": "Kaltstart nicht messbar.", "kpis": {}, "charts": [], "tables": []}
    w50 = stats.pct(warm, 50)
    c = cold[0]
    factor = c / w50 if w50 else None
    head = (f"Erste Anfrage nach {stats.fmt_num(cfg['params']['pause_min'], 1)} min Pause: TTFT {stats.fmt_ms(c)}, "
            f"warm {stats.fmt_ms(w50)} (Faktor {stats.fmt_num(factor, 1)}).")
    verdict = "gruen" if (factor or 0) < 2 or c - w50 < 300 else ("gelb" if c < 5000 else "rot")
    return {"verdict": verdict, "headline": head, "kpis": {}, "raw": {"cold_ms": c, "warm_p50_ms": w50},
            "charts": [], "tables": [{"title": "Kaltstart", "columns": ["Messung", "TTFT"],
                                      "rows": [["kalt (erste Anfrage)", stats.fmt_ms(c)],
                                               ["warm vorher p50", stats.fmt_ms(stats.pct(t('warm_vorher'), 50))],
                                               ["warm nachher p50", stats.fmt_ms(stats.pct(t('warm_nachher'), 50))]]}]}
