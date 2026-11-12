"""Schnellcheck: eine Liste mit Haken pro Punkt und Referenzwerte für TTFT und Tokens/s."""
from __future__ import annotations

from app import stats
from app.client import measure_network_latency, simple_get


def estimate(params):
    return 30 + params["single_user_requests"] * (5 + params["output_tokens"] / 40)


def _check(ctx, key, label, ok, detail="", warn=False):
    item = {"key": key, "label": label, "ok": bool(ok), "warn": bool(warn) and not ok, "detail": detail}
    ctx.stage({"check": item})
    return item


async def run(ctx):
    p = ctx.params
    checks = 0
    total = 10 + p["single_user_requests"]

    def step(text):
        nonlocal checks
        checks += 1
        ctx.progress(checks / total, text)

    # 1. Erreichbarkeit + Modell
    st, data, dur, headers, err = await simple_get(ctx.client, ctx.ep, "/v1/models")
    _check(ctx, "reachable", "Endpunkt erreichbar", st is not None and st < 500, err or f"HTTP {st}, {dur:.0f} ms")
    models = [m.get("id") for m in data.get("data", [])] if isinstance(data, dict) else []
    _check(ctx, "model", f"Modell „{ctx.ep.model}“ in /v1/models", ctx.ep.model in models,
           ", ".join(models[:10]) or (str(data)[:200] if data else "keine Antwort"))
    step("Modelle")

    # 2. Netzlatenz
    net = await measure_network_latency(ctx.client, ctx.ep, n=10)
    _check(ctx, "net", "Netzlatenz gemessen", net["ok"],
           f"p50 {net['p50_ms']} ms, min {net.get('min_ms')} ms über {net['path']}" if net["ok"] else "nicht messbar",
           warn=True)
    step("Netzlatenz")

    # 3. Streaming + usage + Header
    sample = ctx.factory.make(400, ctx.rng)
    r = await ctx.send(ctx.chat_body(sample, 16, force=False), {"stage": "probe", "context_len": 400},
                       keep_text=True)
    _check(ctx, "stream", "Streaming liefert Tokens", r.ok and r.t_first_token is not None,
           f"HTTP {r.status}, {r.chunks} Chunks" + (f", Fehler: {r.error_msg[:200]}" if r.error_msg else ""))
    _check(ctx, "done", "Stream endet mit [DONE]", r.saw_done, "" if r.saw_done else "kein [DONE] gesehen", warn=True)
    _check(ctx, "usage", "usage im Stream (stream_options.include_usage)", r.usage_in_stream,
           f"prompt_tokens={r.prompt_tokens}, completion_tokens={r.completion_tokens}" if r.usage_in_stream
           else "fehlt – Token-Zählung nicht möglich")
    if r.ok and r.prompt_tokens:
        ctx.factory.calibrate(sample, r.prompt_tokens)
    hdr = r.raw_headers
    _check(ctx, "litellm_headers", "LiteLLM-Header vorhanden (x-litellm-overhead-duration-ms)",
           r.headers.get("litellm_overhead_ms") is not None,
           ", ".join(f"{k}={v}" for k, v in sorted(hdr.items()) if "key" not in k)[:400] or "keine", warn=True)
    rl = {k: v for k, v in hdr.items() if k.startswith("x-ratelimit")}
    _check(ctx, "key_limits", "Key-Limits auslesbar (x-ratelimit-*)", bool(rl),
           ", ".join(f"{k}={v}" for k, v in sorted(rl.items())) or "keine Rate-Limit-Header – Limits unbekannt",
           warn=True)
    step("Streaming")

    # 4. cached_tokens: zweimal derselbe Prompt
    shared = ctx.factory.make(1200, ctx.rng)
    await ctx.send(ctx.chat_body(shared, 4, force=False), {"stage": "probe_cache", "context_len": 1200})
    r2 = await ctx.send(ctx.chat_body(shared, 4, force=False), {"stage": "probe_cache", "context_len": 1200})
    if r2.cached_tokens is None:
        _check(ctx, "cached", "cached_tokens wird geliefert", False,
               "Feld fehlt. vLLM mit --enable-prompt-tokens-details starten, sonst ist der Cache-Nutzen nicht messbar.",
               warn=True)
    else:
        _check(ctx, "cached", "cached_tokens wird geliefert", True,
               f"zweite Anfrage: {r2.cached_tokens} von {r2.prompt_tokens} Tokens aus dem Cache")
    step("Cache")

    # 5. ignore_eos / min_tokens
    pr = ctx.factory.make(200, ctx.rng)
    r3 = await ctx.send(ctx.chat_body(pr, 64, force=True), {"stage": "probe_eos", "context_len": 200})
    forced = r3.ok and r3.completion_tokens == 64
    _check(ctx, "ignore_eos", "Output-Länge erzwingbar (ignore_eos / min_tokens)", forced,
           f"HTTP {r3.status}, 64 angefordert, {r3.completion_tokens} erhalten"
           + (f" – {r3.error_msg[:160]}" if r3.error_msg else ""), warn=True)
    from app import storage
    storage.update_profile_capabilities(ctx.profile["id"], {"force_output_length": bool(forced)})
    ctx.force_output = bool(forced)
    step("ignore_eos")

    # 6. Messung mit 1 Nutzer
    for i in range(p["single_user_requests"]):
        if ctx.cancelled:
            return
        text = ctx.factory.make(p["input_tokens"], ctx.rng)
        await ctx.send(ctx.chat_body(text, p["output_tokens"]),
                       {"stage": "single_user", "profile": "referenz", "context_len": p["input_tokens"]})
        step(f"1 Nutzer: {i + 1}/{p['single_user_requests']}")
    ctx.extra_config["prompt_factory"] = ctx.factory.info()


def summarize(recs, stages, cfg, settings):
    checks = [s["check"] for s in stages if "check" in s]
    single = [r for r in recs if r.get("stage") == "single_user"]
    agg = stats.aggregate(single, settings["slo"]) if single else None
    hard_fail = [c for c in checks if not c["ok"] and not c["warn"]]
    warns = [c for c in checks if c["warn"]]
    verdict = "rot" if hard_fail else ("gelb" if warns else "gruen")
    passed = sum(1 for c in checks if c["ok"])
    if agg and agg["ok"]:
        head = (f"{passed} von {len(checks)} Punkten bestanden. Referenz mit 1 Nutzer: "
                f"TTFT p50 {stats.fmt_ms(agg['ttft_ms']['p50'])}, "
                f"{stats.fmt_num(agg['tokens_per_s_user_p50'], 1)} Tokens/s.")
    else:
        head = f"{passed} von {len(checks)} Punkten bestanden."
    tables = [{"title": "Prüfpunkte", "columns": ["", "Punkt", "Detail"],
               "rows": [["✔" if c["ok"] else ("!" if c["warn"] else "✘"), c["label"], c["detail"]] for c in checks]}]
    if agg:
        tables.append({"title": "Referenzmessung mit 1 Nutzer", "columns": ["Wert", "p50", "p95", "p99"],
                       "rows": [["TTFT", *(stats.fmt_ms(agg["ttft_ms"][k]) for k in ("p50", "p95", "p99"))],
                                ["ITL", *(stats.fmt_ms(agg["itl_ms"][k]) for k in ("p50", "p95", "p99"))],
                                ["E2E", *(stats.fmt_ms(agg["e2e_ms"][k]) for k in ("p50", "p95", "p99"))]]})
    return {"verdict": verdict, "headline": head, "checks": checks, "reference": agg,
            "kpis": {}, "charts": [], "tables": tables}
