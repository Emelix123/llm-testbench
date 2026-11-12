"""Cache-Nutzen: Prefix-Cache bei gemeinsamem System-Prompt und wachsendem Chat-Verlauf."""
from __future__ import annotations

import asyncio

from app import stats


def estimate(params):
    return 60 + params["requests"] * 2 * 1.5 + params["chat_sessions"] * params["chat_turns"] * 2 * 1.5


async def run(ctx):
    p = ctx.params
    await ctx.warmup(n=6)
    total = p["system_tokens"] + p["user_tokens"]
    system = ctx.factory.make(p["system_tokens"], ctx.rng, unique=True)
    sem = asyncio.Semaphore(p["concurrency"])
    out = p["output_tokens"]

    async def go(body, meta):
        async with sem:
            if not ctx.cancelled:
                await ctx.send(body, meta)

    # A: eindeutige Prompts (Basis) und B: gemeinsamer System-Prompt, abwechselnd gesendet
    ctx.progress(0.05, "System-Prompt: eindeutig vs. gemeinsam")
    jobs = []
    for i in range(p["requests"]):
        uniq = ctx.factory.make(total, ctx.rng, unique=True)
        jobs.append(go(ctx.chat_body(uniq, out), {"stage": "unique", "context_len": total}))
        user = ctx.factory.make(p["user_tokens"], ctx.rng, unique=True)
        body = ctx.chat_body(user, out)
        body["messages"] = [{"role": "system", "content": system}, *body["messages"]]
        jobs.append(go(body, {"stage": "shared_system", "context_len": total}))
    await asyncio.gather(*jobs)

    # C: Chats mit wachsendem Verlauf + gleich lange eindeutige Vergleichsanfragen
    ctx.progress(0.5, "Chats mit wachsendem Verlauf")

    async def session(s):
        hist = [{"role": "system", "content": ctx.factory.make(500, ctx.rng, unique=True)}]
        length = 500
        for turn in range(p["chat_turns"]):
            if ctx.cancelled:
                return
            q = ctx.factory.make(p["turn_tokens"], ctx.rng, unique=False)
            hist.append({"role": "user", "content": q})
            length += p["turn_tokens"]
            body = ctx.chat_body("x", out)
            body["messages"] = list(hist)
            async with sem:
                r = await ctx.send(body, {"stage": "chat", "context_len": length, "turn": turn}, keep_text=True)
                # Vergleich: eindeutiger Prompt gleicher Länge
                await ctx.send(ctx.chat_body(ctx.factory.make(length, ctx.rng, unique=True), out),
                               {"stage": "chat_unique", "context_len": length, "turn": turn})
            hist.append({"role": "assistant", "content": r.text or "ok"})
            length += r.completion_tokens or out
            ctx.progress(0.5 + 0.5 * (s * p["chat_turns"] + turn + 1) / (p["chat_sessions"] * p["chat_turns"]),
                         f"Sitzung {s + 1}, Zug {turn + 1}")
    await asyncio.gather(*(session(s) for s in range(p["chat_sessions"])))
    ctx.progress(1.0, "fertig")


def _ttft_p50(rs):
    return stats.pct([stats.ttft(r) for r in rs if stats.is_ok(r)], 50)


def _hit(rs):
    c = [r for r in rs if stats.is_ok(r) and r.get("cached_tokens") is not None]
    if not c:
        return None
    return 100 * sum(r["cached_tokens"] for r in c) / max(1, sum(r.get("prompt_tokens") or 0 for r in c))


def summarize(recs, stages, cfg, settings):
    g = {}
    for r in recs:
        g.setdefault(r.get("stage"), []).append(r)
    net = ((cfg.get("client") or {}).get("network_latency") or {}).get("p50_ms") or 0
    rows, res = [], {}
    for name, base, label in (("shared_system", "unique", "Gemeinsamer System-Prompt"),
                              ("chat", "chat_unique", "Chat mit wachsendem Verlauf")):
        a, b = g.get(name, []), g.get(base, [])
        ta, tb = _ttft_p50(a), _ttft_p50(b)
        saving = None if not ta or not tb else 100 * (1 - ta / tb)
        # Kostenfaktor Cache: Prefill-Zeit mit Cache / ohne Cache (ohne Netzanteil)
        factor = None if not ta or not tb else max(0.02, min(1.0, (ta - net) / max(1e-3, tb - net)))
        hit = _hit(a)
        res[name] = {"ttft_cached": ta, "ttft_uncached": tb, "saving_pct": saving, "hit_pct": hit,
                     "cost_factor": factor}
        rows.append([label, stats.fmt_ms(ta), stats.fmt_ms(tb), stats.fmt_pct(saving),
                     stats.fmt_pct(hit) if hit is not None else "nicht geliefert", len(a) + len(b)])
    # TTFT je Zug
    turns = {}
    for r in g.get("chat", []) + g.get("chat_unique", []):
        if stats.is_ok(r):
            turns.setdefault((r["stage"], r["turn"]), []).append(r)
    s_c = [[t + 1, round(_ttft_p50(rs), 1)] for (st, t), rs in sorted(turns.items()) if st == "chat"]
    s_u = [[t + 1, round(_ttft_p50(rs), 1)] for (st, t), rs in sorted(turns.items()) if st == "chat_unique"]
    sh = res.get("shared_system", {})
    ch = res.get("chat", {})
    if sh.get("saving_pct") is None:
        return {"verdict": "rot", "headline": "Keine auswertbaren Anfragen.", "kpis": {}, "charts": [], "tables": []}
    head = (f"Gemeinsamer System-Prompt spart {stats.fmt_pct(sh['saving_pct'], 0)} TTFT, "
            f"wachsender Chat-Verlauf {stats.fmt_pct(ch.get('saving_pct'), 0)}.")
    if sh.get("hit_pct") is not None:
        head += f" Trefferquote laut cached_tokens: {stats.fmt_pct(sh['hit_pct'], 0)} bzw. {stats.fmt_pct(ch.get('hit_pct'), 0)}."
    else:
        head += " cached_tokens wird nicht geliefert (vLLM: --enable-prompt-tokens-details)."
    verdict = "gruen" if (sh["saving_pct"] or 0) >= 20 else ("gelb" if (sh["saving_pct"] or 0) >= 5 else "rot")
    if verdict != "gruen":
        head += " Kaum Ersparnis – ist Prefix-Caching (--enable-prefix-caching) aktiv?"
    factors = [x["cost_factor"] for x in res.values() if x.get("cost_factor") is not None]
    return {
        "verdict": verdict, "headline": head, "kpis": {},
        "raw": {"scenarios": res, "cache_cost_factor": min(factors) if factors else None},
        "charts": [{"id": "turns", "title": "TTFT je Chat-Zug: mit Verlauf (Cache) gegen eindeutig", "type": "line",
                    "x": {"name": "Zug"}, "y": {"name": "TTFT p50 (ms)"},
                    "series": [{"name": "mit Verlauf", "data": s_c}, {"name": "eindeutig, gleiche Länge", "data": s_u}]}],
        "tables": [{"title": "Szenarien", "columns": ["Szenario", "TTFT p50 mit Cache", "TTFT p50 ohne",
                                                      "Ersparnis", "Trefferquote", "Anfragen"], "rows": rows}],
    }
