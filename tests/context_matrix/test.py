"""Kontextlängen-Matrix: gleiche Parallelität, steigende Input-Länge."""
from __future__ import annotations

import asyncio

from app import stats
from app.loadprofiles import max_context


def estimate(params):
    return 60 + len(params["input_lengths"]) * (params["stage_duration_s"] + 20)


def _lengths(ctx):
    limit = max_context(ctx) - ctx.params["output_tokens"] - 64
    out, skipped = [], []
    for x in ctx.params["input_lengths"]:
        x = int(x) or limit
        (out if x <= limit else skipped).append(x)
    if skipped:
        ctx.note(f"Längen {skipped} übersprungen: über dem max. Kontext ({max_context(ctx)}).")
    return sorted(set(out))


async def run(ctx):
    p = ctx.params
    await ctx.warmup(n=p["warmup"])
    lens = _lengths(ctx)
    # Prefill-Messung: je Länge 3 Anfragen einzeln mit max_tokens=1
    for i, L in enumerate(lens):
        if ctx.cancelled:
            return
        ctx.progress(i / (2 * len(lens)), f"Prefill {L}")
        for _ in range(3):
            await ctx.send(ctx.chat_body(ctx.factory.make(L, ctx.rng), 1, force=False),
                           {"stage": f"prefill:{L}", "context_len": L})
    for i, L in enumerate(lens):
        if ctx.cancelled:
            return
        ctx.progress(0.5 + i / (2 * len(lens)), f"Input {L}, {p['concurrency']} parallel")
        stage = f"ctx:{L}"
        ctx.stage({"stage": stage, "context_len": L, "event": "start"})

        def mk(w, rng, L=L, stage=stage):
            return (ctx.chat_body(ctx.factory.make(L, rng), p["output_tokens"]),
                    {"stage": stage, "context_len": L, "concurrency": p["concurrency"]})
        await ctx.closed_load(p["concurrency"], mk, duration_s=p["stage_duration_s"])
        await asyncio.sleep(2)
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    slo = settings["slo"]
    pre, load = {}, {}
    for r in recs:
        st = str(r.get("stage", ""))
        if st.startswith("prefill:"):
            pre.setdefault(int(st[8:]), []).append(r)
        elif st.startswith("ctx:"):
            load.setdefault(int(st[4:]), []).append(r)
    # Prefill-Geschwindigkeit: Anstieg der TTFT über die Input-Länge
    pts = []
    for L, rs in sorted(pre.items()):
        t = stats.pct([stats.ttft(r) for r in rs if stats.is_ok(r)], 50)
        n = stats.pct([r.get("prompt_tokens") for r in rs if stats.is_ok(r)], 50)
        if t is not None and n:
            pts.append((n, t))
    prefill_tps = None
    if len(pts) >= 2:
        slope = stats.slope_per_hour([(n, t) for n, t in pts])  # ms pro Token × 3600
        if slope and slope > 0:
            prefill_tps = 1000 / (slope / 3600)
    rows, s_ttft, s_conc, s_tps, per = [], [], [], [], {}
    for L, rs in sorted(load.items()):
        agg = stats.aggregate(rs, slo)
        ov = stats.sustained_overlap([(r.get("t_first_token"), r.get("t_last_token")) for r in rs if stats.is_ok(r)])
        per[L] = {"ttft_p95": agg["ttft_ms"]["p95"], "served": ov, "meets_slo": agg["meets_slo"]}
        s_ttft.append([L, agg["ttft_ms"]["p95"]])
        s_conc.append([L, ov])
        s_tps.append([L, agg["output_tps"]])
        rows.append([stats.fmt_num(L), agg["requests"], ov, stats.fmt_ms(agg["ttft_ms"]["p50"]),
                     stats.fmt_ms(agg["ttft_ms"]["p95"]), stats.fmt_ms(agg["itl_ms"]["p95"]),
                     stats.fmt_num(agg["output_tps"]), stats.fmt_num(agg["input_tps"]), agg["stalled_requests"],
                     stats.fmt_pct(agg["error_rate_pct"]), "✔" if agg["meets_slo"] else "✘"])
    if not per:
        return {"verdict": "rot", "headline": "Keine auswertbaren Stufen.", "kpis": {}, "charts": [], "tables": []}
    conc = cfg["params"]["concurrency"]
    ok_l = [L for L, v in per.items() if v["meets_slo"]]
    parts = [f"{L // 1000}k: {v['served']} gleichzeitig, TTFT p95 {stats.fmt_ms(v['ttft_p95'])}" for L, v in sorted(per.items())]
    head = f"Bei {conc} parallelen Anfragen – " + "; ".join(parts) + "."
    if prefill_tps:
        head += f" Prefill ≈ {stats.fmt_num(prefill_tps)} Tokens/s."
    verdict = "gruen" if len(ok_l) == len(per) else ("gelb" if ok_l else "rot")
    return {
        "verdict": verdict, "headline": head, "kpis": {},
        "raw": {"prefill_tps": prefill_tps, "per_length": per},
        "charts": [
            {"id": "ttft", "title": "TTFT p95 je Input-Länge", "type": "line", "x": {"name": "Input-Tokens", "log": True},
             "y": {"name": "TTFT p95 (ms)", "log": True}, "series": [{"name": "TTFT p95", "data": s_ttft}],
             "markLines": [{"y": slo["ttft_p95_ms"], "name": "Ziel"}]},
            {"id": "served", "title": "Gleichzeitig bediente Anfragen je Input-Länge", "type": "line",
             "x": {"name": "Input-Tokens", "log": True}, "y": {"name": "gleichzeitig"},
             "series": [{"name": "gemessen", "data": s_conc},
                        {"name": f"angefragt ({conc})", "dashed": True, "data": [[L, conc] for L, _ in s_conc]}]},
            {"id": "prefill", "title": "TTFT einer einzelnen Anfrage über die Input-Länge (Prefill)", "type": "line",
             "x": {"name": "Input-Tokens"}, "y": {"name": "TTFT (ms)"},
             "series": [{"name": "TTFT p50", "data": [[n, round(t, 1)] for n, t in pts]}]},
        ],
        "tables": [{"title": "Kontextlängen", "columns": ["Input", "Anfragen", "gleichzeitig", "TTFT p50", "TTFT p95",
                                                          "ITL p95", "Output Tok/s", "Input Tok/s", "Hänger",
                                                          "Fehler", "Ziel"], "rows": rows}],
    }
