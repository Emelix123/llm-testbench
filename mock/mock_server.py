"""Mock eines vLLM-Servers hinter LiteLLM – nur zum Ausprobieren und Testen des Tools.

Nachgebildet:
- feste Zahl Slots (max_num_seqs) und ein KV-Cache-Budget in Tokens
- Prefill nacheinander mit fester Geschwindigkeit, Decode-Schritt wird mit mehr Sequenzen langsamer
- Prefix-Cache (cached_tokens) für identische Anfänge
- LiteLLM-Header (Overhead, Retries, Rate-Limits), optionales Key-Limit (429)
- ignore_eos / min_tokens, usage im Stream, [DONE]
- Kontextgrenze mit HTTP 400

Start:  python mock/mock_server.py --port 4000 --slots 48
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import time
from collections import deque

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=4000)
ap.add_argument("--model", default="mock-llm")
ap.add_argument("--slots", type=int, default=48, help="max_num_seqs")
ap.add_argument("--kv-tokens", type=int, default=600_000, help="KV-Cache-Budget in Tokens")
ap.add_argument("--max-context", type=int, default=32768)
ap.add_argument("--prefill-tps", type=float, default=40_000)
ap.add_argument("--step-ms", type=float, default=12.0, help="Decode-Schritt bei 1 Sequenz")
ap.add_argument("--step-ms-per-seq", type=float, default=0.35)
ap.add_argument("--rpm-limit", type=int, default=0, help="Key-Limit Anfragen/Minute (0 = aus)")
ap.add_argument("--speed", type=float, default=1.0, help="Zeitraffer: >1 = schneller")
ap.add_argument("--key", default="", help="erwarteter API-Key (leer = keiner)")
ap.add_argument("--reasoning", action="store_true", help="Denk-Teil in reasoning_content liefern")
ap.add_argument("--vision", action="store_true", help="Bilder verstehen (VL-Modell)")
ap.add_argument("--response-cache", action="store_true", help="LiteLLM-Antwort-Cache simulieren (Fehlkonfiguration)")
args, _ = ap.parse_known_args()

app = FastAPI()
S = args.speed
state = {"active": 0, "kv_used": 0}
cond = asyncio.Condition()
prefill_lock = asyncio.Lock()
prefix_cache: dict[str, int] = {}
recent = deque()
response_cache: dict = {}

WORDS = ("Das Modell antwortet hier mit einem langen Beispieltext über Daten, Server und Messwerte, "
         "damit der Stream realistisch aussieht und viele Tokens enthält. ").split()


def ntok(text: str) -> int:
    return max(1, len(text) // 4)


def prompt_text(body) -> str:
    out = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out += [x.get("text", "") for x in c if isinstance(x, dict)]
    return "\n".join(out)


def cached_prefix(text: str) -> int:
    """Blockweise (je 64 Tokens = 256 Zeichen) gemeinsame Anfänge zählen."""
    hit = 0
    blk = 256
    for i in range(blk, len(text) + 1, blk):
        h = hashlib.blake2b(text[:i].encode(), digest_size=8).hexdigest()
        if h in prefix_cache:
            hit = i // 4
        else:
            break
    for i in range(blk, len(text) + 1, blk):
        prefix_cache[hashlib.blake2b(text[:i].encode(), digest_size=8).hexdigest()] = 1
    if len(prefix_cache) > 500_000:
        prefix_cache.clear()
    return hit


def ll_headers(remaining: int | None = None) -> dict:
    h = {"x-litellm-overhead-duration-ms": f"{random.uniform(1.5, 6):.2f}",
         "x-litellm-attempted-retries": "0", "x-litellm-attempted-fallbacks": "0",
         "x-litellm-model-id": "mock-deployment-1"}
    if args.rpm_limit:
        h["x-ratelimit-limit-requests"] = str(args.rpm_limit)
        h["x-ratelimit-remaining-requests"] = str(max(0, remaining or 0))
        h["x-ratelimit-remaining-tokens"] = "1000000"
    return h


def check_auth(req: Request):
    if not args.key:
        return None
    if req.headers.get("authorization") != f"Bearer {args.key}":
        return JSONResponse({"error": {"message": "Authentication Error, invalid key", "code": "401"}}, 401)
    return None



# ------------------------------------------------------------------ Antwortinhalt
import base64
import re
import struct
import zlib

COLORS = {"rot": (220, 30, 30), "grün": (30, 180, 60), "blau": (30, 60, 220), "gelb": (240, 220, 30),
          "schwarz": (10, 10, 10), "weiß": (245, 245, 245)}


def png_first_pixel(data: bytes):
    if not data.startswith(b"\x89PNG"):
        return None
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w = struct.unpack(">I", chunk[:4])[0]
        if typ == b"IDAT":
            idat += chunk
        pos += 12 + ln
    raw = zlib.decompressobj().decompress(idat, 64)
    return tuple(raw[1:4])


def color_name(rgb):
    return min(COLORS, key=lambda k: sum((a - b) ** 2 for a, b in zip(COLORS[k], rgb)))


def images_in(body):
    out = []
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            for x in c:
                if isinstance(x, dict) and x.get("type") == "image_url":
                    url = (x.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        try:
                            out.append(base64.b64decode(url.split(",", 1)[1]))
                        except Exception:  # noqa: BLE001
                            out.append(b"")
    return out


def instance(schema, rng, key=""):
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for comb in ("anyOf", "oneOf"):
        if comb in schema:
            return instance(schema[comb][0], rng, key)
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: instance(v, rng, k) for k, v in props.items()}
    if t == "array":
        n = max(schema.get("minItems", 1), 1)
        return [instance(schema.get("items", {"type": "string"}), rng, key) for _ in range(n)]
    if t == "integer":
        return max(schema.get("minimum", 0), 1)
    if t == "number":
        return float(max(schema.get("minimum", 0), 1.5))
    if t == "boolean":
        return True
    if key in ("city", "stadt", "ort"):
        return "Berlin"
    return "Beispiel"


def plan_answer(body, text, rng):
    """Gibt (reasoning_words, content_words|None, tool_calls) zurück. None = zufälliger Text."""
    msgs = body.get("messages", [])
    system = " ".join(m.get("content", "") for m in msgs if m.get("role") == "system" and isinstance(m.get("content"), str))
    last = msgs[-1] if msgs else {}
    reasoning = []
    ctk = body.get("chat_template_kwargs") or {}
    if args.reasoning and ctk.get("enable_thinking", True) and body.get("reasoning_effort") != "none":
        reasoning = "Ich überlege kurz, was gefragt ist , und antworte dann knapp .".split()
    tools = body.get("tools") or []
    choice = body.get("tool_choice", "auto")
    if tools and choice != "none" and last.get("role") != "tool":
        names = [t["function"]["name"] for t in tools]
        if isinstance(choice, dict):
            names = [choice["function"]["name"]]
        want = choice == "required" or isinstance(choice, dict) or re.search(r"Wetter|weather|Funktion|Tool", text, re.I)
        if want:
            cities = [c for c in ("Berlin", "Hamburg", "München") if c in text] or ["Berlin"]
            if body.get("parallel_tool_calls") is False:
                cities = cities[:1]
            fn = next((t["function"] for t in tools if t["function"]["name"] == names[0]), tools[0]["function"])
            calls = []
            for c in cities:
                a = instance(fn.get("parameters", {}), rng)
                for k in a:
                    if k in ("city", "stadt", "ort", "location"):
                        a[k] = c
                calls.append({"id": f"call_{rng.getrandbits(32):x}", "type": "function",
                              "function": {"name": fn["name"], "arguments": json.dumps(a, ensure_ascii=False)}})
            return reasoning, None, calls
    if last.get("role") == "tool":
        return reasoning, f"Laut Werkzeug: {str(last.get('content'))[:60]}".split(), []
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema":
        obj = instance((rf.get("json_schema") or {}).get("schema", {}), rng)
        return reasoning, [json.dumps(obj, ensure_ascii=False)], []
    if rf.get("type") == "json_object":
        return reasoning, [json.dumps({"antwort": "ok"})], []
    m = re.search(r"nur mit dem Wort (\w+)", system)
    if m:
        return reasoning, [m.group(1)], []
    if re.search(r"Wie heiße ich", text):
        n = re.search(r"Ich heiße (\w+)", text)
        return reasoning, f"Du heißt {n.group(1) if n else 'unbekannt'} .".split(), []
    if re.search(r"geheime Code", text) and re.search(r"Wie lautet der geheime Code", text):
        n = re.search(r"Der geheime Code lautet (\w+)", text)
        return reasoning, f"Der geheime Code lautet {n.group(1) if n else 'unbekannt'} .".split(), []
    if re.search(r"Bewerte", text):
        return reasoning, ["4"], []
    if re.search(r"Zähle von 1 bis", text):
        return reasoning, [f"{i}," for i in range(1, 30)], []
    imgs = images_in(body)
    if imgs:
        if not args.vision:
            return reasoning, "Ich sehe kein Bild .".split(), []
        cols = [color_name(png_first_pixel(b) or (0, 0, 0)) for b in imgs]
        return reasoning, ("Die Bilder sind " + ", ".join(cols) + " .").split(), []
    return reasoning, None, []


@app.get("/health/liveliness")
async def live():
    return "I'm alive!"


@app.get("/v1/models")
async def models(req: Request):
    if (e := check_auth(req)):
        return e
    return JSONResponse({"object": "list", "data": [{"id": args.model, "object": "model"}]},
                        headers=ll_headers(args.rpm_limit))


@app.post("/v1/chat/completions")
async def chat(req: Request):
    if (e := check_auth(req)):
        return e
    body = await req.json()
    now = time.monotonic()
    while recent and recent[0] < now - 60:
        recent.popleft()
    if args.rpm_limit and len(recent) >= args.rpm_limit:
        return JSONResponse({"error": {"message": "Rate limit exceeded", "code": "429"}}, 429,
                            headers=ll_headers(0))
    recent.append(now)
    if not body.get("messages"):
        return JSONResponse({"error": {"message": "messages must not be empty", "code": "400"}}, 400, headers=ll_headers())
    if body.get("max_tokens") is not None and int(body["max_tokens"]) < 1:
        return JSONResponse({"error": {"message": "max_tokens must be at least 1", "code": "400"}}, 400, headers=ll_headers())
    text = prompt_text(body)
    p_tok = ntok(text) + 12  # Chat-Template
    max_tokens = int(body.get("max_tokens") or 256)
    if p_tok + max_tokens > args.max_context:
        return JSONResponse({"error": {"message": f"This model's maximum context length is {args.max_context} tokens. "
                                                  f"However, you requested {p_tok + max_tokens} tokens.", "code": "400"}},
                            400, headers=ll_headers())
    allowed = {"model", "messages", "max_tokens", "temperature", "stream", "stream_options", "ignore_eos",
               "min_tokens", "seed", "stop", "top_p", "n", "logprobs", "top_logprobs", "tools", "tool_choice",
               "response_format", "parallel_tool_calls", "chat_template_kwargs", "reasoning_effort"}
    temp = body.get("temperature", 1.0)
    seed = body.get("seed")
    if temp == 0 or seed is not None:
        rng = random.Random(hashlib.blake2b((text + str(seed)).encode(), digest_size=8).hexdigest())
    else:
        rng = random.Random()
    n_choices = int(body.get("n") or 1)
    if n_choices > 8:
        return JSONResponse({"error": {"message": "n must be <= 8", "code": "400"}}, 400, headers=ll_headers())
    if args.vision is False and images_in(body):
        pass
    reasoning, fixed, tool_calls = plan_answer(body, text, rng)
    reasoning = reasoning[:max(0, max_tokens - 1)]
    max_tokens = max(1, max_tokens - len(reasoning))  # Denk-Tokens zählen in max_tokens mit (wie vLLM)
    force = bool(body.get("ignore_eos")) or int(body.get("min_tokens") or 0) >= max_tokens + len(reasoning)
    if fixed is not None and not force:
        words = [w + " " for w in fixed]
    else:
        n_rand = max_tokens if force else max(1, min(max_tokens, int(rng.gauss(max_tokens * 0.6, max_tokens * 0.2))))
        words = [w + " " for w in (fixed or [])] + [rng.choice(WORDS) + " " for _ in range(n_rand)]
    words = words[:max_tokens] if not force else (words + [rng.choice(WORDS) + " " for _ in range(max_tokens)])[:max_tokens]
    finish = "length" if len(words) >= max_tokens else "stop"
    stops = body.get("stop") or []
    if isinstance(stops, str):
        stops = [stops]
    if stops and not force:
        acc = ""
        for i, w in enumerate(words):
            hit = next((st for st in stops if st in acc + w), None)
            if hit:
                words = words[:i] + ([(acc + w).split(hit)[0][len(acc):]] if (acc + w).split(hit)[0][len(acc):] else [])
                finish = "stop"
                break
            acc += w
    if tool_calls:
        words, finish = [], "tool_calls"
    n_out = len(reasoning) + len(words) + 8 * len(tool_calls)
    need = p_tok + n_out
    unknown = set(body) - allowed
    stream = body.get("stream", False)
    include_usage = (body.get("stream_options") or {}).get("include_usage", False)
    want_lp = bool(body.get("logprobs"))
    cache_key = hashlib.blake2b(json.dumps(body, sort_keys=True).encode(), digest_size=8).hexdigest()
    cached = cached_prefix(text)
    rid = f"chatcmpl-{random.getrandbits(48):x}"

    def chunk_of(delta, fin=None):
        ch = {"index": 0, "delta": delta, "finish_reason": fin}
        if want_lp and delta.get("content"):
            ch["logprobs"] = {"content": [{"token": delta["content"], "logprob": -0.1, "top_logprobs": []}]}
        return {"id": rid, "object": "chat.completion.chunk", "created": int(time.time()), "model": args.model,
                "choices": [ch]}

    async def gen():
        async with cond:
            await cond.wait_for(lambda: state["active"] < args.slots and state["kv_used"] + need <= args.kv_tokens)
            state["active"] += 1
            state["kv_used"] += need
        try:
            async with prefill_lock:
                await asyncio.sleep(max(0, p_tok - cached) / args.prefill_tps / S)
            first = True
            seq = [("reasoning_content", w + " ") for w in reasoning] + [("content", w) for w in words]
            for i, (kind, tok) in enumerate(seq):
                await asyncio.sleep((args.step_ms + args.step_ms_per_seq * state["active"]) / 1000 / S)
                delta = {kind: tok}
                if first:
                    delta["role"] = "assistant"
                    first = False
                last_one = i == len(seq) - 1 and not tool_calls
                yield f"data: {json.dumps(chunk_of(delta, finish if last_one else None), ensure_ascii=False)}\n\n"
            for j, tc in enumerate(tool_calls):
                await asyncio.sleep(args.step_ms / 1000 / S)
                args_s = tc["function"]["arguments"]
                half = len(args_s) // 2
                d1 = {"tool_calls": [{"index": j, "id": tc["id"], "type": "function",
                                      "function": {"name": tc["function"]["name"], "arguments": args_s[:half]}}]}
                if first:
                    d1["role"] = "assistant"
                    first = False
                yield f"data: {json.dumps(chunk_of(d1), ensure_ascii=False)}\n\n"
                d2 = {"tool_calls": [{"index": j, "function": {"arguments": args_s[half:]}}]}
                yield f"data: {json.dumps(chunk_of(d2, 'tool_calls' if j == len(tool_calls) - 1 else None), ensure_ascii=False)}\n\n"
            if include_usage:
                u = {"id": rid, "object": "chat.completion.chunk", "model": args.model, "choices": [],
                     "usage": {"prompt_tokens": p_tok, "completion_tokens": n_out, "total_tokens": p_tok + n_out,
                               "completion_tokens_details": {"reasoning_tokens": len(reasoning)} if reasoning else None,
                               "prompt_tokens_details": {"cached_tokens": cached}}}
                yield f"data: {json.dumps(u)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            async with cond:
                state["active"] -= 1
                state["kv_used"] -= need
                cond.notify_all()

    rem = (args.rpm_limit - len(recent)) if args.rpm_limit else None
    headers = ll_headers(rem)
    if unknown:
        headers["x-mock-dropped-params"] = ",".join(sorted(unknown))
    if stream:
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)
    if args.response_cache and cache_key in response_cache:
        return JSONResponse(response_cache[cache_key], headers={**headers, "x-litellm-cache-key": cache_key})
    content, rc = "", ""
    async for line in gen():
        if line.startswith("data: {"):
            obj = json.loads(line[6:])
            for ch in obj.get("choices", []):
                content += ch["delta"].get("content", "") or ""
                rc += ch["delta"].get("reasoning_content", "") or ""
    msg = {"role": "assistant", "content": content.strip() if not tool_calls else None}
    if rc:
        msg["reasoning_content"] = rc.strip()
    if tool_calls:
        msg["tool_calls"] = tool_calls
    choices = [{"index": i, "message": msg, "finish_reason": finish,
                **({"logprobs": {"content": [{"token": w, "logprob": -0.1} for w in content.split()]}} if want_lp else {})}
               for i in range(n_choices)]
    out = {"id": rid, "object": "chat.completion", "created": int(time.time()), "model": args.model,
           "choices": choices,
           "usage": {"prompt_tokens": p_tok, "completion_tokens": n_out * n_choices,
                     "total_tokens": p_tok + n_out * n_choices,
                     "prompt_tokens_details": {"cached_tokens": cached}}}
    if args.response_cache:
        response_cache[cache_key] = out
    return JSONResponse(out, headers=headers)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
