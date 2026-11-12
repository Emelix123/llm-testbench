"""Konfiguration & Funktionen: ein Prüfpunkt je Funktion, jeder läuft durch."""
from __future__ import annotations

import asyncio
import json
import re
import time

import jsonschema

from app import media, stats
from app.client import measure_network_latency, message_of, simple_get
from app.loadprofiles import max_context

CRITICAL = {"tool_calling", "structured_output", "chat_template", "streaming_format", "litellm_cache",
            "context_limit", "litellm_passthrough"}
LABELS = {
    "tool_calling": "Tool Calling", "structured_output": "Structured Output", "reasoning": "Reasoning",
    "chat_template": "Chat-Template", "context_limit": "Kontextgrenze", "needle": "Nadel im Heuhaufen",
    "multimodal": "Multimodal", "parameters": "Parameter", "streaming_format": "Streaming-Format",
    "cancel": "Abbruch", "litellm_passthrough": "LiteLLM-Durchreichung", "litellm_cache": "LiteLLM-Antwort-Cache",
    "fingerprint": "Steckbrief",
}
SPECIAL = re.compile(r"<\|[^|>]{1,40}\|>|</s>|<s>|\[/?INST\]|<\|eot_id\|>|<start_of_turn>|<end_of_turn>")

TOOLS = [
    {"type": "function", "function": {
        "name": "get_weather", "description": "Aktuelles Wetter für eine Stadt",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "Stadt"},
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
            "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "get_time", "description": "Uhrzeit in einer Zeitzone",
        "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": ["timezone"]}}},
]
TOOL_SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in TOOLS}

PERSON_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "alter": {"type": "integer", "minimum": 0},
                   "interessen": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                   "aktiv": {"type": "boolean"}},
    "required": ["name", "alter", "interessen", "aktiv"], "additionalProperties": False,
}


def estimate(params):
    return 30 + 30 * len(params["checks"])


def _short(x, n=240):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    s = (s or "").replace("\n", " ")
    return s[:n] + (" …" if len(s) > n else "")


def _valid_calls(calls):
    errs = []
    for c in calls or []:
        fn = c.get("function") or {}
        try:
            a = json.loads(fn.get("arguments") or "")
        except json.JSONDecodeError:
            errs.append(f"{fn.get('name')}: Argumente kein gültiges JSON")
            continue
        sch = TOOL_SCHEMAS.get(fn.get("name"))
        if sch is None:
            errs.append(f"unbekanntes Tool {fn.get('name')}")
            continue
        try:
            jsonschema.validate(a, sch)
        except jsonschema.ValidationError as e:
            errs.append(f"{fn.get('name')}: {e.message}")
    return errs


def _msg(ctx, text, system=None):
    m = [{"role": "user", "content": text}]
    if system:
        m.insert(0, {"role": "system", "content": system})
    return m


# ------------------------------------------------------------------ Prüfpunkte

async def check_tool_calling(ctx):
    sub, ex = [], None
    base = {"messages": _msg(ctx, "Wie ist das Wetter in Berlin? Nutze das passende Werkzeug."),
            "tools": TOOLS, "max_tokens": 300, "temperature": 0}

    async def case(name, body, expect):
        r = await ctx.once(body, {"stage": "tool_calling", "case": name}, record=True)
        m = message_of(r)
        calls = m.get("tool_calls") or []
        ok, why = expect(r, m, calls)
        sub.append((name, ok, why))
        return r, m, calls

    def need_call(n=1, name=None):
        def f(r, m, calls):
            if r["status"] != 200:
                return False, f"HTTP {r['status']}: {_short(r['text'], 120)}"
            if len(calls) < n:
                return False, f"{len(calls)} Aufrufe statt ≥ {n}; content: {_short(m.get('content'), 80)}"
            if name and calls[0]["function"]["name"] != name:
                return False, f"falsches Tool {calls[0]['function']['name']}"
            e = _valid_calls(calls)
            return (not e), "; ".join(e) or ", ".join(f"{c['function']['name']}({c['function']['arguments']})" for c in calls)
        return f

    r, m, calls = await case("auto", {**base, "tool_choice": "auto"}, need_call())
    ex = calls or m.get("content")
    await case("required", {**base, "messages": _msg(ctx, "Hallo!"), "tool_choice": "required"}, need_call())
    await case("bestimmtes Tool", {**base, "tool_choice": {"type": "function", "function": {"name": "get_time"}}},
               need_call(name="get_time"))
    await case("none", {**base, "tool_choice": "none"},
               lambda r, m, c: (r["status"] == 200 and not c and bool(m.get("content")),
                                f"{len(c)} Aufrufe, content: {_short(m.get('content'), 80)}"))
    await case("parallel", {**base, "messages": _msg(ctx, "Wie ist das Wetter in Berlin und in Hamburg? Rufe das Werkzeug für beide Städte auf."),
                            "tool_choice": "auto"}, need_call(2))
    # Streaming
    sr = await ctx.send({**base, "tool_choice": "auto"}, {"stage": "tool_calling", "case": "stream"}, keep_text=True)
    e = _valid_calls(sr.tool_calls)
    sub.append(("Streaming", sr.ok and bool(sr.tool_calls) and not e,
                "; ".join(e) or (", ".join(f"{c['function']['name']}({c['function']['arguments']})" for c in sr.tool_calls)
                                 or f"keine tool_calls im Stream ({sr.error_type or ''})")))
    # zweiter Schritt mit Tool-Ergebnis
    first = calls[0] if calls else {"id": "call_1", "type": "function",
                                    "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'}}
    msgs = base["messages"] + [{"role": "assistant", "content": None, "tool_calls": [first]},
                               {"role": "tool", "tool_call_id": first.get("id") or "call_1",
                                "content": json.dumps({"temperatur": 21, "himmel": "sonnig"})}]
    r2 = await ctx.once({"messages": msgs, "tools": TOOLS, "max_tokens": 200, "temperature": 0},
                        {"stage": "tool_calling", "case": "tool_result"}, record=True)
    m2 = message_of(r2)
    sub.append(("Schritt mit Tool-Ergebnis", r2["status"] == 200 and bool(m2.get("content")),
                _short(m2.get("content") or r2["text"], 120)))
    ok = all(x[1] for x in sub)
    return ok, "; ".join(f"{n}: {'✔' if o else '✘ ' + w}" for n, o, w in sub), ex, sub


async def check_structured_output(ctx):
    n = ctx.params["structured_requests"]
    body = {"messages": _msg(ctx, "Erfinde eine Person und gib sie als JSON zurück."), "max_tokens": 300,
            "temperature": 0.7, "response_format": {"type": "json_schema", "json_schema": {
                "name": "person", "schema": PERSON_SCHEMA, "strict": True}}}
    res = await asyncio.gather(*(ctx.once(body, {"stage": "structured_output"}, record=True) for _ in range(n)))
    good, errs, ex = 0, [], None
    for r in res:
        c = message_of(r).get("content")
        try:
            obj = json.loads(c or "")
            jsonschema.validate(obj, PERSON_SCHEMA)
            good += 1
            ex = ex or obj
        except Exception as e:  # noqa: BLE001
            errs.append(f"HTTP {r['status']}: {type(e).__name__}: {_short(str(e) or c, 100)}")
    return good == n, f"{good} von {n} Antworten gültig nach Schema" + (f"; z. B. {errs[0]}" if errs else ""), ex, None


async def check_reasoning(ctx):
    q = _msg(ctx, "Was ist 17 mal 23? Denke kurz nach und nenne dann nur das Ergebnis.")
    on = await ctx.once({"messages": q, "max_tokens": 800, "temperature": 0}, {"stage": "reasoning", "case": "an"}, record=True)
    m = message_of(on)
    rc = m.get("reasoning_content") or m.get("reasoning")
    content = m.get("content") or ""
    leaked = bool(re.search(r"</?think>", content))
    off = await ctx.once({"messages": q, "max_tokens": 800, "temperature": 0,
                          "chat_template_kwargs": {"enable_thinking": False}},
                         {"stage": "reasoning", "case": "aus"}, record=True)
    m_off = message_of(off)
    rc_off = m_off.get("reasoning_content") or m_off.get("reasoning")
    is_reasoning = bool(rc) or leaked or bool((ctx.profile.get("capabilities") or {}).get("reasoning"))
    if not is_reasoning:
        return None, "kein Reasoning-Modell erkannt (kein reasoning_content, kein Denktext) – nicht anwendbar", content, None
    parts = [f"Denk-Teil getrennt: {'ja' if rc else 'nein'}", f"Denktext im content: {'ja' if leaked else 'nein'}",
             f"abschaltbar (enable_thinking=false): {'ja' if off['status'] == 200 and not rc_off else 'nein'}"]
    ok = bool(rc) and not leaked and off["status"] == 200 and not rc_off
    return ok, "; ".join(parts), {"reasoning_content": _short(rc or "", 160), "content": _short(content, 120)}, None


async def check_chat_template(ctx):
    sub = []
    r = await ctx.once({"messages": _msg(ctx, "Wie geht es dir heute?", system="Antworte nur mit dem Wort BANANE."),
                        "max_tokens": 20, "temperature": 0}, {"stage": "chat_template"}, record=True)
    c1 = message_of(r).get("content") or ""
    sub.append(("System-Prompt wirkt", "banane" in c1.lower(), _short(c1, 80)))
    msgs = [{"role": "user", "content": "Ich heiße Mira."}, {"role": "assistant", "content": "Hallo Mira, schön dich kennenzulernen."},
            {"role": "user", "content": "Danke. Erzähl mir etwas über das Meer."},
            {"role": "assistant", "content": "Das Meer bedeckt etwa 70 Prozent der Erde."},
            {"role": "user", "content": "Wie heiße ich?"}]
    r2 = await ctx.once({"messages": msgs, "max_tokens": 30, "temperature": 0}, {"stage": "chat_template"}, record=True)
    c2 = message_of(r2).get("content") or ""
    sub.append(("Verlauf mit mehreren Schritten", "mira" in c2.lower(), _short(c2, 80)))
    r3 = await ctx.once({"messages": _msg(ctx, "Schreibe drei Sätze über Bäume."), "max_tokens": 120, "temperature": 0},
                        {"stage": "chat_template"}, record=True)
    c3 = message_of(r3).get("content") or ""
    sp = SPECIAL.findall(c1 + c2 + c3)
    sub.append(("keine Spezial-Tokens im Output", not sp, ", ".join(sorted(set(sp))) or "keine"))
    return all(x[1] for x in sub), "; ".join(f"{n}: {'✔' if o else '✘'} ({w})" for n, o, w in sub), c2, sub


async def check_context_limit(ctx):
    adv = max_context(ctx)

    async def probe(n):
        body = ctx.chat_body(ctx.factory.make(n, ctx.rng), 1, force=False)
        r = await ctx.once(body, {"stage": "context_limit", "context_len": n, "expected_error": True}, record=True)
        return r

    steps, last_ok, first_fail, fail_r = [], None, None, None
    for frac in (0.5, 0.75, 0.9, 0.97, 1.02, 1.1):
        n = int(adv * frac)
        r = await probe(n)
        steps.append(f"{n}: HTTP {r['status']}")
        if r["status"] == 200:
            last_ok = n
        else:
            first_fail, fail_r = n, r
            break
    if first_fail is None:
        return False, f"keine Ablehnung bis {int(adv * 1.1)} Tokens – beworbener Kontext {adv} stimmt nicht oder wird nicht geprüft. " + ", ".join(steps), None, None
    lo, hi = last_ok or 0, first_fail
    while hi - lo > max(64, adv * 0.005):
        mid = (lo + hi) // 2
        r = await probe(mid)
        if r["status"] == 200:
            lo = mid
        else:
            hi, fail_r = mid, r
    clean = fail_r["status"] == 400 and bool(re.search(r"context|maximum|token|länge|length", fail_r["text"], re.I))
    close = abs(lo - adv) <= max(256, adv * 0.02)
    detail = (f"Grenze ≈ {lo} Tokens (beworben {adv}); Ablehnung mit HTTP {fail_r['status']}"
              f"{' und verständlicher Meldung' if clean else ' – Meldung unklar'}")
    return clean and close, detail, _short(fail_r["text"], 200), None


async def check_needle(ctx):
    n = ctx.params["needle_context"] or min(int(max_context(ctx) * 0.9), max_context(ctx) - 300)
    sub, ex = [], None
    for depth in (0.25, 0.5, 1.0):
        code = "".join(ctx.rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        fact = f" Der geheime Code lautet {code}. "
        q = " Frage: Wie lautet der geheime Code? Antworte nur mit dem Code."
        hay = ctx.factory.make(max(100, n - 60), ctx.rng, unique=True)
        pos = int(len(hay) * depth)
        pos = hay.rfind(" ", 0, pos) if depth < 1 else len(hay)
        text = hay[:pos] + fact + hay[pos:] + q
        r = await ctx.once({"messages": _msg(ctx, text), "max_tokens": 30, "temperature": 0},
                           {"stage": "needle", "depth": depth, "context_len": n}, record=True)
        c = message_of(r).get("content") or r["text"]
        ok = r["status"] == 200 and code in (c or "")
        sub.append((f"{int(depth * 100)} %", ok, _short(c, 60)))
        ex = ex or c
    return all(x[1] for x in sub), f"Kontext {n} Tokens – " + "; ".join(f"{d}: {'gefunden' if o else 'nicht gefunden'} ({w})" for d, o, w in sub), ex, sub


async def check_multimodal(ctx):
    if not (ctx.profile.get("capabilities") or {}).get("vision"):
        return None, "Endpunkt-Profil ist nicht als Vision-Modell markiert – nicht anwendbar", None, None
    sub = []

    def img(c, size=64):
        return {"type": "image_url", "image_url": {"url": media.data_url(size, size, c)}}

    async def ask(content, colors, name):
        r = await ctx.once({"messages": [{"role": "user", "content": content}], "max_tokens": 60, "temperature": 0},
                           {"stage": "multimodal", "case": name}, record=True)
        c = message_of(r).get("content") or r["text"]
        ok = r["status"] == 200 and all(media.mentions(c, col) for col in colors)
        sub.append((name, ok, _short(c, 80)))
    await ask([{"type": "text", "text": "Welche Farbe hat dieses Bild? Antworte mit einem Wort."}, img("rot")], ["rot"], "ein Bild")
    await ask([{"type": "text", "text": "Welche Farben haben diese beiden Bilder, in Reihenfolge?"}, img("blau"), img("grün")],
              ["blau", "grün"], "mehrere Bilder")
    await ask([{"type": "text", "text": "Welche Farbe hat dieses Bild?"}, img("gelb", 2048)], ["gelb"], "großes Bild (2048²)")
    return all(x[1] for x in sub), "; ".join(f"{n}: {'✔' if o else '✘'} ({w})" for n, o, w in sub), None, sub


async def check_parameters(ctx):
    sub = []
    q = _msg(ctx, "Beschreibe in zwei Sätzen einen Herbsttag.")
    rs = [await ctx.once({"messages": q, "max_tokens": 60, "temperature": 0}, {"stage": "parameters"}, record=True) for _ in range(3)]
    cs = [message_of(r).get("content") for r in rs]
    sub.append(("temperature 0 → gleiche Antworten", len(set(cs)) == 1 and cs[0] is not None, f"{len(set(cs))} verschiedene"))
    s1 = await ctx.once({"messages": q, "max_tokens": 60, "temperature": 0.9, "seed": 42}, {"stage": "parameters"}, record=True)
    s2 = await ctx.once({"messages": q, "max_tokens": 60, "temperature": 0.9, "seed": 42}, {"stage": "parameters"}, record=True)
    sub.append(("seed → gleiche Antwort", message_of(s1).get("content") == message_of(s2).get("content") and s1["status"] == 200,
                "gleich" if message_of(s1).get("content") == message_of(s2).get("content") else "verschieden"))
    st = await ctx.once({"messages": _msg(ctx, "Zähle von 1 bis 20, durch Komma getrennt, ohne weiteren Text."),
                         "max_tokens": 80, "temperature": 0, "stop": ["7,"]}, {"stage": "parameters"}, record=True)
    c = message_of(st).get("content") or ""
    fr = ((st.get("json") or {}).get("choices") or [{}])[0].get("finish_reason")
    sub.append(("stop", st["status"] == 200 and "7," not in c and "9" not in c and fr == "stop", f"{_short(c, 40)} (finish: {fr})"))
    mt = await ctx.once({"messages": q, "max_tokens": 7, "temperature": 0}, {"stage": "parameters"}, record=True)
    ct = ((mt.get("json") or {}).get("usage") or {}).get("completion_tokens")
    sub.append(("max_tokens", mt["status"] == 200 and ct is not None and ct <= 7, f"7 angefordert, {ct} erhalten"))
    lp = await ctx.once({"messages": q, "max_tokens": 10, "temperature": 0, "logprobs": True, "top_logprobs": 2},
                        {"stage": "parameters"}, record=True)
    has = bool(((lp.get("json") or {}).get("choices") or [{}])[0].get("logprobs"))
    sub.append(("logprobs", lp["status"] == 200 and has, "geliefert" if has else f"fehlt (HTTP {lp['status']})"))
    return all(x[1] for x in sub), "; ".join(f"{n}: {'✔' if o else '✘'} ({w})" for n, o, w in sub), cs[0], sub


async def check_streaming_format(ctx):
    r = await ctx.send({"messages": _msg(ctx, "Nenne drei Farben."), "max_tokens": 40, "temperature": 0},
                       {"stage": "streaming_format"}, keep_text=True, keep_chunks=True)
    issues = list(r.format_issues)
    if not r.saw_done:
        issues.append("kein [DONE]")
    if r.data_after_done:
        issues.append("Daten nach [DONE]")
    ch = r.raw_chunks
    if any(c.get("object") != "chat.completion.chunk" for c in ch):
        issues.append("object ≠ chat.completion.chunk")
    if len({c.get("id") for c in ch}) > 1:
        issues.append("wechselnde id")
    u_idx = [i for i, c in enumerate(ch) if c.get("usage")]
    if not u_idx:
        issues.append("keine usage im Stream")
    elif u_idx[-1] != len(ch) - 1:
        issues.append("usage nicht im letzten Chunk")
    if not any((c.get("choices") or [{}])[0].get("finish_reason") for c in ch if c.get("choices")):
        issues.append("kein finish_reason")
    return not issues and r.ok, f"{len(ch)} Chunks; " + ("; ".join(issues) if issues else "Format korrekt"), r.text, None


async def check_cancel(ctx):
    net = await measure_network_latency(ctx.client, ctx.ep, n=2)

    async def short(tag):
        out = []
        for _ in range(5):
            r = await ctx.send(ctx.chat_body(ctx.factory.make(500, ctx.rng), 50), {"stage": "cancel", "case": tag})
            if r.ok and r.t_first_token is not None:
                out.append(r.t_first_token - r.t_send)
        return stats.pct(out, 50)
    base = await short("vorher")
    ev = asyncio.Event()
    n = ctx.params["cancel_requests"]
    long_tasks = [asyncio.create_task(ctx.send(ctx.chat_body(ctx.factory.make(2000, ctx.rng), 4000),
                                                {"stage": "cancel", "case": "lang", "expected_error": True}, cancel_event=ev)) for _ in range(n)]
    await asyncio.sleep(3)
    ev.set()
    res = await asyncio.gather(*long_tasks)
    aborted = sum(1 for r in res if r.error_type == "abbruch")
    after = await short("nachher")
    ok = base is not None and after is not None and after <= 1.5 * base + 150
    return ok, (f"TTFT p50 vorher {stats.fmt_ms(base)}, direkt nach Abbruch von {aborted} Anfragen {stats.fmt_ms(after)}"
                f" (Netz {net.get('p50_ms')} ms)"), None, None


async def check_litellm_passthrough(ctx):
    sub = []
    r = await ctx.send({"messages": _msg(ctx, "Sag Hallo."), "max_tokens": 200, "ignore_eos": True, "temperature": 0.7},
                       {"stage": "litellm_passthrough", "case": "ignore_eos"})
    sub.append(("ignore_eos", r.ok and r.completion_tokens == 200, f"200 angefordert, {r.completion_tokens} erhalten, HTTP {r.status}"))
    r = await ctx.send({"messages": _msg(ctx, "Sag Hallo."), "max_tokens": 150, "min_tokens": 150, "temperature": 0.7},
                       {"stage": "litellm_passthrough", "case": "min_tokens"})
    sub.append(("min_tokens", r.ok and r.completion_tokens == 150, f"150 angefordert, {r.completion_tokens} erhalten, HTTP {r.status}"))
    sub.append(("stream_options.include_usage", r.usage_in_stream, "usage im Stream" if r.usage_in_stream else "fehlt"))
    return all(x[1] for x in sub), "; ".join(f"{n}: {'✔' if o else '✘'} ({w})" for n, o, w in sub), None, sub


async def check_litellm_cache(ctx):
    body = {"messages": _msg(ctx, f"Schreibe einen Satz über die Zahl {ctx.rng.randrange(10**9)}."),
            "max_tokens": 60, "temperature": 0}
    a = await ctx.once(body, {"stage": "litellm_cache"}, record=True)
    b = await ctx.once(body, {"stage": "litellm_cache"}, record=True)
    same = message_of(a).get("content") == message_of(b).get("content")
    fast = b["ms"] < 0.3 * a["ms"]
    hdr = any(k.startswith("x-litellm-cache") for k in b["headers"])
    cached = (same and fast) or hdr
    return not cached, (f"1. Antwort {a['ms']:.0f} ms, 2. Antwort {b['ms']:.0f} ms, identisch: {'ja' if same else 'nein'}"
                        f"{', Cache-Header vorhanden' if hdr else ''} → Antwort-Cache {'AN' if cached else 'aus'}"), None, None


async def check_fingerprint(ctx):
    st, data, _, headers, err = await simple_get(ctx.client, ctx.ep, "/v1/models")
    models = [m.get("id") for m in data.get("data", [])] if isinstance(data, dict) else []
    st2, key, _, _, _ = await simple_get(ctx.client, ctx.ep, "/key/info")
    key_info = None
    if st2 == 200 and isinstance(key, dict):
        info = key.get("info") or key
        key_info = {k: info.get(k) for k in ("key_alias", "tpm_limit", "rpm_limit", "max_parallel_requests",
                                             "max_budget", "spend", "models", "expires") if k in info}
    hdr = {k.lower(): v for k, v in (headers or {}).items() if k.lower().startswith(("x-litellm", "x-ratelimit"))}
    ctx.extra_config["key_info"] = key_info
    detail = (f"Modelle: {', '.join(models) or '–'}; Key-Limits: "
              f"{json.dumps(key_info, ensure_ascii=False) if key_info else 'über /key/info nicht abrufbar'}; Header: {', '.join(sorted(hdr)) or '–'}")
    return st == 200 and ctx.ep.model in models, detail, {"models": models, "key_info": key_info}, None


CHECKS = {k: globals()[f"check_{k}"] for k in LABELS}


async def run(ctx):
    sel = [c for c in ctx.params["checks"] if c in CHECKS]
    ctx.progress(0.0, "Aufwärmen")
    sample = ctx.factory.make(400, ctx.rng)
    r = await ctx.send(ctx.chat_body(sample, 1, force=False), record=False)
    if r.ok and r.prompt_tokens:
        ctx.factory.calibrate(sample, r.prompt_tokens)
    for i, key in enumerate(sel):
        if ctx.cancelled:
            return
        ctx.progress(i / len(sel), LABELS[key])
        t = time.monotonic()
        try:
            ok, detail, example, sub = await CHECKS[key](ctx)
        except Exception as e:  # noqa: BLE001 – Prüfpunkt scheitert, der Test läuft weiter
            ok, detail, example, sub = False, f"Prüfung abgebrochen: {type(e).__name__}: {_short(str(e), 200)}", None, None
        ctx.stage({"check": {"key": key, "label": LABELS[key], "ok": ok, "detail": detail,
                             "example": _short(example, 400) if example is not None else None,
                             "sub": [list(x) for x in sub] if sub else None,
                             "seconds": round(time.monotonic() - t, 1)}})
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    checks = [s["check"] for s in stages if "check" in s]
    passed = [c for c in checks if c["ok"] is True]
    failed = [c for c in checks if c["ok"] is False]
    na = [c for c in checks if c["ok"] is None]
    crit = [c for c in failed if c["key"] in CRITICAL]
    verdict = "rot" if crit else ("gelb" if failed else "gruen")
    head = f"{len(passed)} von {len(checks) - len(na)} Prüfpunkten bestanden"
    if na:
        head += f", {len(na)} nicht anwendbar"
    head += "." + (f" Nicht bestanden: {', '.join(c['label'] for c in failed)}." if failed else "")
    rows = [["✔" if c["ok"] else ("–" if c["ok"] is None else "✘"), c["label"], c["detail"], c.get("example") or ""]
            for c in checks]
    return {"verdict": verdict, "headline": head, "checks": checks, "kpis": {}, "charts": [],
            "tables": [{"title": "Prüfpunkte", "columns": ["", "Prüfpunkt", "Ergebnis", "Beispielantwort"], "rows": rows}]}
