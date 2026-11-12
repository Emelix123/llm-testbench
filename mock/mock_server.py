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
args, _ = ap.parse_known_args()

app = FastAPI()
S = args.speed
state = {"active": 0, "kv_used": 0}
cond = asyncio.Condition()
prefill_lock = asyncio.Lock()
prefix_cache: dict[str, int] = {}
recent = deque()

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
    text = prompt_text(body)
    p_tok = ntok(text) + 12  # Chat-Template
    max_tokens = int(body.get("max_tokens") or 256)
    if p_tok + max_tokens > args.max_context:
        return JSONResponse({"error": {"message": f"This model's maximum context length is {args.max_context} tokens. "
                                                  f"However, you requested {p_tok + max_tokens} tokens.", "code": "400"}},
                            400, headers=ll_headers())
    allowed = {"model", "messages", "max_tokens", "temperature", "stream", "stream_options", "ignore_eos",
               "min_tokens", "seed", "stop", "top_p", "n", "logprobs", "tools", "tool_choice", "response_format"}
    force = bool(body.get("ignore_eos")) or int(body.get("min_tokens") or 0) >= max_tokens
    n_out = max_tokens if force else max(1, min(max_tokens, int(random.gauss(max_tokens * 0.6, max_tokens * 0.2))))
    need = p_tok + n_out
    unknown = set(body) - allowed
    stream = body.get("stream", False)
    include_usage = (body.get("stream_options") or {}).get("include_usage", False)
    cached = cached_prefix(text)
    rid = f"chatcmpl-{random.getrandbits(48):x}"

    async def gen():
        # Auf freien Slot und KV-Platz warten (Warteschlange wie in vLLM)
        async with cond:
            await cond.wait_for(lambda: state["active"] < args.slots and state["kv_used"] + need <= args.kv_tokens)
            state["active"] += 1
            state["kv_used"] += need
        try:
            async with prefill_lock:
                await asyncio.sleep(max(0, p_tok - cached) / args.prefill_tps / S)
            for i in range(n_out):
                step = (args.step_ms + args.step_ms_per_seq * state["active"]) / 1000 / S
                await asyncio.sleep(step)
                tok = random.choice(WORDS) + " "
                chunk = {"id": rid, "object": "chat.completion.chunk", "model": args.model,
                         "choices": [{"index": 0, "delta": {"content": tok},
                                      "finish_reason": ("length" if i == n_out - 1 and force else
                                                        ("stop" if i == n_out - 1 else None))}]}
                if i == 0:
                    chunk["choices"][0]["delta"]["role"] = "assistant"
                yield f"data: {json.dumps(chunk)}\n\n"
            if include_usage:
                u = {"id": rid, "object": "chat.completion.chunk", "model": args.model, "choices": [],
                     "usage": {"prompt_tokens": p_tok, "completion_tokens": n_out, "total_tokens": p_tok + n_out,
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
    content = ""
    async for line in gen():
        if line.startswith("data: {"):
            obj = json.loads(line[6:])
            for ch in obj.get("choices", []):
                content += ch["delta"].get("content", "")
    return JSONResponse({"id": rid, "object": "chat.completion", "model": args.model,
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": "length" if force else "stop"}],
                         "usage": {"prompt_tokens": p_tok, "completion_tokens": n_out,
                                   "total_tokens": p_tok + n_out,
                                   "prompt_tokens_details": {"cached_tokens": cached}}}, headers=headers)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
