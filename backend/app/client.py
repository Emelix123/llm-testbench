"""HTTP-Client gegen LiteLLM (OpenAI-kompatibel), mit Zeitmessung pro Stream-Chunk.

Regeln aus dem Plan:
- monotone Uhr (perf_counter_ns), Zeitstempel in ms relativ zum Laufbeginn
- keine automatischen Wiederholungen, eigenes Timeout pro Anfrage
- Token-Zahlen nur aus usage, nie aus Chunks geschätzt
- LiteLLM-Header werden pro Anfrage mitgeschrieben
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

LITELLM_HEADERS = {
    "x-litellm-overhead-duration-ms": "litellm_overhead_ms",
    "x-litellm-attempted-retries": "litellm_retries",
    "x-litellm-attempted-fallbacks": "litellm_fallbacks",
    "x-ratelimit-remaining-requests": "ratelimit_remaining_requests",
    "x-ratelimit-remaining-tokens": "ratelimit_remaining_tokens",
}


def now_ns() -> int:
    return time.perf_counter_ns()


def _num(v: str | None):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else round(f, 3)
    except ValueError:
        return None


def parse_litellm_headers(h: httpx.Headers) -> dict:
    return {field_: _num(h.get(name)) for name, field_ in LITELLM_HEADERS.items()}


def make_client(max_connections: int = 1024, timeout_s: float = 600) -> httpx.AsyncClient:
    # Eine Verbindung pro paralleler Anfrage, HTTP/1.1, keine Retries.
    limits = httpx.Limits(max_connections=max_connections,
                          max_keepalive_connections=max_connections)
    return httpx.AsyncClient(
        limits=limits,
        timeout=httpx.Timeout(timeout_s, connect=30),
        transport=httpx.AsyncHTTPTransport(retries=0, limits=limits),
        follow_redirects=False,
    )


_URL_SUFFIXES = ("/chat/completions", "/completions", "/models", "/embeddings", "/responses")


def normalize_base_url(url: str) -> str:
    """Basis-URL ohne /v1 und ohne Endpunkt-Pfad.

    Akzeptiert https://host, https://host/v1 und https://host/v1/chat/completions
    (häufig so aus der Doku kopiert) und liefert jeweils https://host.
    """
    u = (url or "").strip().rstrip("/")
    for suf in _URL_SUFFIXES:
        if u.endswith(suf):
            u = u[: -len(suf)].rstrip("/")
            break
    if u.endswith("/v1"):
        u = u[:-3]
    return u.rstrip("/")


@dataclass
class Endpoint:
    base_url: str
    model: str
    api_key: str | None = None

    def __post_init__(self):
        self.base_url = normalize_base_url(self.base_url)

    @property
    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def url(self, path: str) -> str:
        return self.base_url + path


@dataclass
class StreamResult:
    t_send: float = 0.0             # ms relativ zum Laufbeginn
    t_first_token: float | None = None
    t_last_token: float | None = None
    itl_ms: list[float] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    status: int | None = None
    error_type: str | None = None
    error_msg: str | None = None
    headers: dict = field(default_factory=dict)
    raw_headers: dict = field(default_factory=dict)
    text: str = ""
    reasoning_text: str = ""
    finish_reason: str | None = None
    saw_done: bool = False
    usage_in_stream: bool = False
    chunks: int = 0
    tool_calls: list = field(default_factory=list)   # zusammengesetzt aus Stream-Deltas
    raw_chunks: list = field(default_factory=list)   # nur mit keep_chunks
    format_issues: list = field(default_factory=list)
    logprobs_seen: bool = False
    data_after_done: bool = False

    @property
    def ok(self) -> bool:
        return self.error_type is None and self.status == 200


def _classify_status(status: int) -> str:
    if status == 429:
        return "429"
    if status >= 500:
        return "5xx"
    if status == 408:
        return "timeout"
    return "4xx"


async def stream_chat(
    client: httpx.AsyncClient,
    ep: Endpoint,
    body: dict,
    t0_ns: int,
    timeout_s: float = 600,
    keep_text: bool = False,
    cancel_event: asyncio.Event | None = None,
    on_chunk=None,
    keep_chunks: bool = False,
) -> StreamResult:
    """Sendet eine Streaming-Chat-Anfrage und misst jeden Chunk."""
    body = dict(body)
    body["stream"] = True
    body.setdefault("stream_options", {"include_usage": True})
    body.setdefault("model", ep.model)
    r = StreamResult()
    ms = lambda ns: round((ns - t0_ns) / 1e6, 3)  # noqa: E731
    last_tok_ns: int | None = None
    send_ns = now_ns()
    r.t_send = ms(send_ns)
    try:
        async with asyncio.timeout(timeout_s):
            async with client.stream("POST", ep.url("/v1/chat/completions"),
                                     headers=ep.headers, json=body) as resp:
                r.status = resp.status_code
                r.headers = parse_litellm_headers(resp.headers)
                r.raw_headers = {k.lower(): v for k, v in resp.headers.items()
                                 if k.lower().startswith(("x-litellm", "x-ratelimit", "llm_provider"))}
                if resp.status_code != 200:
                    raw = (await resp.aread())[:2000].decode("utf-8", "replace")
                    r.error_type = _classify_status(resp.status_code)
                    r.error_msg = raw
                    return r
                async for line in resp.aiter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        r.error_type = "abbruch"
                        return r
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        if not line.startswith((":", "event:", "id:", "retry:")) and len(r.format_issues) < 5:
                            r.format_issues.append(f"Zeile ohne data-Präfix: {line[:80]!r}")
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        r.saw_done = True
                        continue  # weiterlesen, um Daten nach [DONE] zu erkennen
                    if r.saw_done:
                        r.data_after_done = True
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        if len(r.format_issues) < 5:
                            r.format_issues.append(f"kein gültiges JSON: {payload[:80]!r}")
                        continue
                    if keep_chunks and len(r.raw_chunks) < 5000:
                        r.raw_chunks.append(obj)
                    if "error" in obj:
                        err = obj["error"]
                        code = err.get("code") if isinstance(err, dict) else None
                        r.error_type = _classify_status(int(code)) if str(code).isdigit() else "stream_error"
                        r.error_msg = json.dumps(err)[:2000]
                        return r
                    r.chunks += 1
                    t = now_ns()
                    got_token = False
                    for ch in obj.get("choices") or []:
                        delta = ch.get("delta") or {}
                        c = delta.get("content")
                        rc = delta.get("reasoning_content") or delta.get("reasoning")
                        tc = delta.get("tool_calls")
                        if c:
                            got_token = True
                            if keep_text:
                                r.text += c
                        if rc:
                            got_token = True
                            if keep_text:
                                r.reasoning_text += rc
                        if tc:
                            got_token = True
                            for d in tc:
                                idx = d.get("index", 0)
                                while len(r.tool_calls) <= idx:
                                    r.tool_calls.append({"id": None, "type": "function",
                                                         "function": {"name": "", "arguments": ""}})
                                cur = r.tool_calls[idx]
                                if d.get("id"):
                                    cur["id"] = d["id"]
                                fn = d.get("function") or {}
                                if fn.get("name"):
                                    cur["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    cur["function"]["arguments"] += fn["arguments"]
                        if ch.get("logprobs"):
                            r.logprobs_seen = True
                        if ch.get("finish_reason"):
                            r.finish_reason = ch["finish_reason"]
                    if got_token:
                        if on_chunk is not None:
                            on_chunk()
                        if r.t_first_token is None:
                            r.t_first_token = ms(t)
                        elif last_tok_ns is not None:
                            r.itl_ms.append(round((t - last_tok_ns) / 1e6, 3))
                        last_tok_ns = t
                    u = obj.get("usage")
                    if u:
                        r.usage_in_stream = True
                        r.prompt_tokens = u.get("prompt_tokens")
                        r.completion_tokens = u.get("completion_tokens")
                        ctd = u.get("completion_tokens_details") or {}
                        r.reasoning_tokens = ctd.get("reasoning_tokens")
                        ptd = u.get("prompt_tokens_details") or {}
                        r.cached_tokens = ptd.get("cached_tokens")
                if last_tok_ns is not None:
                    r.t_last_token = ms(last_tok_ns)
    except (TimeoutError, httpx.TimeoutException):
        r.error_type = "timeout"
        r.error_msg = f"kein Abschluss nach {timeout_s:.0f} s"
    except asyncio.CancelledError:
        r.error_type = "abbruch"
        raise
    except httpx.HTTPError as e:
        r.error_type = "verbindung"
        r.error_msg = f"{type(e).__name__}: {e}"[:500]
    if last_tok_ns is not None and r.t_last_token is None:
        r.t_last_token = ms(last_tok_ns)
    return r


async def simple_get(client: httpx.AsyncClient, ep: Endpoint, path: str, auth: bool = True,
                     timeout_s: float = 15) -> tuple[int | None, Any, float, httpx.Headers | None, str | None]:
    """GET mit Zeitmessung. Gibt (status, json|text, dauer_ms, headers, fehler) zurück."""
    t = now_ns()
    try:
        resp = await client.get(ep.url(path), headers=ep.headers if auth else {},
                                timeout=timeout_s)
        dur = (now_ns() - t) / 1e6
        try:
            data = resp.json()
        except ValueError:
            data = resp.text[:2000]
        return resp.status_code, data, dur, resp.headers, None
    except httpx.HTTPError as e:
        return None, None, (now_ns() - t) / 1e6, None, f"{type(e).__name__}: {e}"


async def measure_network_latency(client: httpx.AsyncClient, ep: Endpoint, n: int = 5) -> dict:
    """Mini-Anfragen ohne LLM-Aufruf: Anteil des Netzwerks an der TTFT.

    Bevorzugt LiteLLMs /health/liveliness (ohne Auth, ohne Modellaufruf),
    sonst /v1/models.
    """
    samples, path_used = [], None
    for path, auth in (("/health/liveliness", False), ("/v1/models", True)):
        st, _, dur, _, err = await simple_get(client, ep, path, auth=auth)
        if err is None and st is not None and st < 500 and st != 404:
            path_used = path
            samples.append(dur)
            for _ in range(n - 1):
                st, _, dur, _, err = await simple_get(client, ep, path, auth=auth)
                if err is None:
                    samples.append(dur)
            break
    if not samples:
        return {"ok": False, "path": None, "p50_ms": None, "samples_ms": []}
    s = sorted(samples)
    return {"ok": True, "path": path_used, "p50_ms": round(s[len(s) // 2], 2),
            "min_ms": round(s[0], 2), "samples_ms": [round(x, 2) for x in samples]}


async def chat_once(client: httpx.AsyncClient, ep: Endpoint, body: dict, timeout_s: float = 600) -> dict:
    """Nicht-streamende Anfrage. Gibt status, json, text, headers, dauer_ms, fehler zurück."""
    body = dict(body)
    body.setdefault("model", ep.model)
    body["stream"] = False
    t = now_ns()
    try:
        resp = await client.post(ep.url("/v1/chat/completions"), headers=ep.headers, json=body,
                                 timeout=timeout_s)
        dur = (now_ns() - t) / 1e6
        try:
            data = resp.json()
        except ValueError:
            data = None
        return {"status": resp.status_code, "json": data, "text": resp.text[:4000],
                "headers": {k.lower(): v for k, v in resp.headers.items()}, "ms": dur, "error": None}
    except httpx.HTTPError as e:
        return {"status": None, "json": None, "text": "", "headers": {}, "ms": (now_ns() - t) / 1e6,
                "error": f"{type(e).__name__}: {e}"[:300]}


def message_of(res: dict) -> dict:
    try:
        return res["json"]["choices"][0]["message"] or {}
    except (TypeError, KeyError, IndexError):
        return {}
