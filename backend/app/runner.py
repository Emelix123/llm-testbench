"""Testläufe: Modulsystem, RunContext mit Lastgenerator, Batch-Ausführung, Live-Events.

Ein Batch ist eine Auswahl von Tests, die nacheinander laufen. Jeder Test
bekommt einen eigenen Ordner data/runs/<datum>_<uhrzeit>_<test>/.
Tests laufen immer vollständig durch; abbrechen kann man nur per Knopf.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import sys
import time
import traceback
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import storage, stats
from .client import Endpoint, StreamResult, make_client, measure_network_latency, now_ns, simple_get
from .prompts import get_factory
from .sysmon import SleepInhibitor, cpu, network_kind

TESTS_DIR = storage.ROOT / "tests"


# ======================================================================= Module

class TestModule:
    def __init__(self, path: Path):
        self.path = path
        self.manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        self.id = self.manifest.get("id") or path.name
        spec = importlib.util.spec_from_file_location(f"llmtb_tests.{self.id}", path / "test.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.mod = mod

    def defaults(self) -> dict:
        return {p["key"]: p.get("default") for p in self.manifest.get("params", [])}

    def estimate_s(self, params: dict) -> float:
        fn = getattr(self.mod, "estimate", None)
        if fn:
            try:
                return float(fn({**self.defaults(), **params}))
            except Exception:  # noqa: BLE001
                pass
        return float(self.manifest.get("estimated_duration_s", 60))

    def public(self) -> dict:
        m = dict(self.manifest)
        m["id"] = self.id
        m["estimated_duration_s"] = self.estimate_s({})
        return m


def load_modules() -> dict[str, TestModule]:
    mods = {}
    for d in sorted(TESTS_DIR.iterdir()):
        if (d / "manifest.json").exists() and (d / "test.py").exists():
            try:
                m = TestModule(d)
                mods[m.id] = m
            except Exception:  # noqa: BLE001
                traceback.print_exc()
    return mods


# ======================================================================= Live

class Live:
    """Live-Werte für die Oberfläche: Parallelität, Tokens/s, Fehler."""

    def __init__(self) -> None:
        self.inflight = 0
        self.errors = 0
        self.requests = 0
        self._chunks: deque[float] = deque()

    def chunk(self) -> None:
        self._chunks.append(time.monotonic())

    def tps(self, window: float = 5.0) -> float:
        cut = time.monotonic() - window
        while self._chunks and self._chunks[0] < cut:
            self._chunks.popleft()
        return len(self._chunks) / window

    def snapshot(self) -> dict:
        return {"inflight": self.inflight, "tokens_per_s": round(self.tps(), 1),
                "errors": self.errors, "requests": self.requests, "client_cpu_pct": cpu.value}


# ======================================================================= Context

class RunContext:
    def __init__(self, *, run_id: str, run_dir: Path, test: TestModule, params: dict,
                 profile: dict, settings: dict, batch: "Batch"):
        self.run_id = run_id
        self.dir = run_dir
        self.test = test
        self.params = params
        self.profile = profile
        self.settings = settings
        self.batch = batch
        self.seed = int(params.get("seed") or random.randrange(1, 2**31))
        self.rng = random.Random(self.seed)
        self.ep = Endpoint(profile["base_url"], profile["model"],
                           storage.get_secret(profile.get("api_key_name")))
        self.timeout_s = float(settings["client"]["request_timeout_s"])
        self.client = make_client(timeout_s=self.timeout_s)
        self.factory = get_factory(profile.get("tokenizer"))
        self.t0_ns = now_ns()
        self.writer = storage.JsonlWriter(run_dir / "requests.jsonl")
        self.progress_writer = storage.JsonlWriter(run_dir / "progress.jsonl")
        self.cancel = batch.cancel_event
        self.live = batch.live
        caps = profile.get("capabilities") or {}
        self.force_output = caps.get("force_output_length", True)
        self.notes: list[str] = []
        self.extra_config: dict = {}
        self._req_seq = 0
        self._cpu_warn = settings["client"]["cpu_warn_pct"]

    # ------------------------------------------------------------ Rückmeldung
    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def progress(self, frac: float, text: str = "") -> None:
        self.batch.set_progress(self.run_id, max(0.0, min(1.0, frac)), text)

    def stage(self, info: dict) -> None:
        info = {"t": round((now_ns() - self.t0_ns) / 1e6, 1), **info}
        self.progress_writer.write(info)
        self.batch.emit("stage", {"run_id": self.run_id, **info})

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)
            self.batch.emit("log", {"run_id": self.run_id, "msg": msg})

    # ------------------------------------------------------------ Anfragen
    def chat_body(self, prompt: str, max_tokens: int, force: bool = True, **extra) -> dict:
        body: dict[str, Any] = {
            "model": self.ep.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }
        if force and self.force_output:
            body["ignore_eos"] = True
            body["min_tokens"] = max_tokens
        body.update(extra)
        return body

    async def send(self, body: dict, meta: dict | None = None, *, record: bool = True,
                   keep_text: bool = False) -> StreamResult:
        meta = meta or {}
        self.live.inflight += 1
        cpu_at_send = cpu.value
        try:
            res = await _stream_with_live(self, body, keep_text)
        finally:
            self.live.inflight -= 1
        self.live.requests += 1
        if not res.ok:
            self.live.errors += 1
        if record:
            self._req_seq += 1
            row = {
                "seq": self._req_seq,
                "profile": meta.get("profile"),
                "stage": meta.get("stage"),
                "context_len": meta.get("context_len"),
                **{k: v for k, v in meta.items() if k not in ("profile", "stage", "context_len")},
                "t_send": res.t_send,
                "t_first_token": res.t_first_token,
                "t_last_token": res.t_last_token,
                "t_end": round((now_ns() - self.t0_ns) / 1e6, 3),
                "itl_ms": res.itl_ms,
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
                "reasoning_tokens": res.reasoning_tokens,
                "cached_tokens": res.cached_tokens,
                "status": res.status,
                "error_type": res.error_type,
                "error_msg": (res.error_msg or None) and res.error_msg[:300],
                **res.headers,
                "client_cpu_pct": cpu_at_send,
                "cpu_high": cpu_at_send >= self._cpu_warn,
            }
            self.writer.write(row)
        return res

    async def warmup(self, n: int = 10, input_tokens: int = 500, output_tokens: int = 64,
                     concurrency: int = 10) -> None:
        """Aufwärmen (nicht gewertet) + Kalibrierung von Template-Overhead und ignore_eos."""
        self.progress(0.0, "Aufwärmen")
        # Kalibrierung: eine Anfrage mit max_tokens=1
        sample = self.factory.make(400, self.rng)
        res = await self.send(self.chat_body(sample, 1, force=False), record=False)
        if res.ok and res.prompt_tokens:
            self.factory.calibrate(sample, res.prompt_tokens)
        # ignore_eos / min_tokens durchgereicht?
        if self.force_output:
            p = self.factory.make(input_tokens, self.rng)
            r = await self.send(self.chat_body(p, 32), record=False)
            if r.status == 400:
                self.force_output = False
                self.note("LiteLLM lehnt ignore_eos/min_tokens ab. Output-Länge wird nicht erzwungen; "
                          "gerechnet wird mit den tatsächlichen Längen.")
            elif r.ok and r.completion_tokens is not None and r.completion_tokens < 32:
                self.force_output = False
                self.note(f"ignore_eos/min_tokens wirkt nicht (32 angefordert, {r.completion_tokens} erhalten). "
                          "Gerechnet wird mit den tatsächlichen Längen.")
        sem = asyncio.Semaphore(concurrency)

        async def one():
            async with sem:
                if self.cancelled:
                    return
                await self.send(self.chat_body(self.factory.make(input_tokens, self.rng), output_tokens),
                                record=False)
        await asyncio.gather(*(one() for _ in range(n)))
        self.extra_config["prompt_factory"] = self.factory.info()
        self.extra_config["force_output_length"] = self.force_output

    async def closed_load(
        self,
        concurrency: int,
        make_request: Callable[[int, random.Random], tuple[dict, dict]],
        *,
        duration_s: float | None = None,
        total: int | None = None,
        on_done: Callable[[StreamResult], None] | None = None,
    ) -> None:
        """Geschlossene Last: `concurrency` Arbeiter senden ohne Pause nacheinander.

        Mit `duration_s` werden nach Ablauf keine neuen Anfragen mehr gestartet,
        laufende werden zu Ende gemessen. Mit `total` werden genau so viele
        Anfragen gesendet (bei total == concurrency: alle gleichzeitig).
        """
        deadline = time.monotonic() + duration_s if duration_s else None
        counter = {"n": 0}

        async def worker(i: int):
            wrng = random.Random(self.rng.getrandbits(64))
            while not self.cancelled:
                if deadline and time.monotonic() >= deadline:
                    return
                if total is not None:
                    if counter["n"] >= total:
                        return
                    counter["n"] += 1
                body, meta = make_request(i, wrng)
                meta = {**meta, "worker": i}
                r = await self.send(body, meta)
                if on_done:
                    on_done(r)

        await asyncio.gather(*(worker(i) for i in range(concurrency)))

    async def close(self) -> None:
        self.writer.close()
        self.progress_writer.close()
        await self.client.aclose()


async def _stream_with_live(ctx: RunContext, body: dict, keep_text: bool) -> StreamResult:
    from . import client as client_mod
    return await client_mod.stream_chat(ctx.client, ctx.ep, body, ctx.t0_ns, timeout_s=ctx.timeout_s,
                                        keep_text=keep_text, cancel_event=ctx.cancel,
                                        on_chunk=ctx.live.chunk)


# ======================================================================= Batch

class Batch:
    def __init__(self, profile: dict, items: list[dict], modules: dict[str, TestModule], label: str = ""):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.profile = profile
        self.items = items              # [{"test": id, "params": {...}}]
        self.modules = modules
        self.label = label
        self.cancel_event = asyncio.Event()
        self.live = Live()
        self.state = "laeuft"
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.finished_at: str | None = None
        self.runs: list[dict] = []      # [{run_id, test, name, progress, text, status}]
        self._subs: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.inhibitor = SleepInhibitor()

    # ------------------------------------------------------------ Events
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def emit(self, kind: str, data: dict) -> None:
        msg = {"type": kind, **data}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def set_progress(self, run_id: str, frac: float, text: str) -> None:
        for r in self.runs:
            if r["run_id"] is not None and r["run_id"] == run_id:
                r["progress"] = round(frac, 4)
                r["text"] = text
        self.emit("progress", {"run_id": run_id, "progress": round(frac, 4), "text": text})

    def snapshot(self) -> dict:
        return {
            "batch_id": self.id, "state": self.state, "label": self.label,
            "profile": {"id": self.profile["id"], "name": self.profile.get("name")},
            "started_at": self.started_at, "finished_at": self.finished_at,
            "runs": self.runs, "live": self.live.snapshot(),
        }

    # ------------------------------------------------------------ Ablauf
    async def execute(self) -> None:
        method = self.inhibitor.acquire()
        ticker = asyncio.create_task(self._tick())
        try:
            for i, item in enumerate(self.items):
                mod = self.modules[item["test"]]
                params = {**mod.defaults(), **(item.get("params") or {})}
                self.runs.append({"index": i, "run_id": None, "test": mod.id, "name": mod.manifest.get("name"),
                                  "progress": 0.0, "text": "wartet", "status": "wartet",
                                  "estimated_s": mod.estimate_s(params)})
            for item, run in zip(self.items, self.runs):
                if self.cancel_event.is_set():
                    run["status"] = "abgebrochen"
                    run["text"] = "nicht gestartet"
                    continue
                await self._run_one(self.modules[item["test"]], item.get("params") or {}, run, method)
            self.state = "abgebrochen" if self.cancel_event.is_set() else "fertig"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.state = "fehler"
            self.emit("log", {"msg": f"Interner Fehler: {e}"})
        finally:
            ticker.cancel()
            self.inhibitor.release()
            self.finished_at = datetime.now().isoformat(timespec="seconds")
            self.emit("batch", self.snapshot())

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.emit("live", self.live.snapshot())

    async def _run_one(self, mod: TestModule, params_in: dict, run: dict, inhibit_method) -> None:
        params = {**mod.defaults(), **params_in}
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        rd = storage.RUNS / f"{ts}_{mod.id}"
        n = 1
        while rd.exists():
            n += 1
            rd = storage.RUNS / f"{ts}_{mod.id}-{n}"
        rd.mkdir(parents=True)
        run["run_id"] = rd.name
        settings = storage.get_settings()
        run["status"] = "laeuft"
        self.emit("run", run)
        ctx = RunContext(run_id=run["run_id"], run_dir=rd, test=mod, params=params,
                         profile=self.profile, settings=settings, batch=self)
        net = await measure_network_latency(ctx.client, ctx.ep)
        fingerprint = await endpoint_fingerprint(ctx)
        cfg = {
            "test": mod.id, "test_name": mod.manifest.get("name"), "category": mod.manifest.get("category"),
            "batch_id": self.id, "batch_label": self.label, "batch_index": run["index"],
            "params": params, "seed": ctx.seed, "tool_version": storage.TOOL_VERSION,
            "profile": {k: v for k, v in self.profile.items() if k != "has_key"},
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "laeuft",
            "client": {"network_latency": net, "network_kind": network_kind(),
                       "sleep_inhibit": inhibit_method, "python": sys.version.split()[0]},
            "endpoint_fingerprint": fingerprint,
            "slo": settings["slo"],
        }
        storage.write_json(rd / "config.json", cfg)
        status = "fertig"
        try:
            await mod.mod.run(ctx)
            if ctx.cancelled:
                status = "abgebrochen"
        except asyncio.CancelledError:
            status = "abgebrochen"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status = "fehler"
            ctx.note(f"Testfehler: {type(e).__name__}: {e}")
        finally:
            await ctx.close()
        cfg.update({"status": status, "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "notes": ctx.notes, **ctx.extra_config})
        storage.write_json(rd / "config.json", cfg)
        try:
            summarize_run(rd)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        run["status"] = status
        run["progress"] = 1.0 if status == "fertig" else run["progress"]
        run["text"] = {"fertig": "fertig", "abgebrochen": "abgebrochen", "fehler": "Fehler"}[status]
        self.emit("run", run)


async def endpoint_fingerprint(ctx: RunContext) -> dict:
    """Steckbrief: /v1/models, LiteLLM-Header (ohne Werte von Keys)."""
    st, data, dur, headers, err = await simple_get(ctx.client, ctx.ep, "/v1/models")
    models = None
    if isinstance(data, dict):
        models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
    hdr = {}
    if headers is not None:
        hdr = {k.lower(): v for k, v in headers.items()
               if k.lower().startswith(("x-litellm", "x-ratelimit", "server"))
               and "key" not in k.lower()}
    return {"models_status": st, "models": models, "headers": hdr, "error": err,
            "model_listed": (ctx.ep.model in models) if models else None}


# ======================================================================= Auswertung

_modules_cache: dict[str, TestModule] | None = None


def modules() -> dict[str, TestModule]:
    global _modules_cache
    if _modules_cache is None:
        _modules_cache = load_modules()
    return _modules_cache


def summarize_run(rd: Path) -> dict:
    cfg = storage.read_json(rd / "config.json", {})
    mod = modules().get(cfg.get("test"))
    settings = storage.get_settings()
    if mod is None:
        summ = {"verdict": None, "headline": "Testmodul nicht gefunden", "kpis": {}}
    else:
        recs = list(storage.iter_requests(rd))
        stages = list(storage.iter_jsonl(rd / "progress.jsonl"))
        summ = mod.mod.summarize(recs, stages, cfg, settings)
    summ.setdefault("kpis", {})
    summ["tool_version"] = storage.TOOL_VERSION
    summ["computed_at"] = datetime.now().isoformat(timespec="seconds")
    summ["status"] = cfg.get("status")
    summ["notes"] = cfg.get("notes") or []
    cpu_hi = 0
    for r in storage.iter_requests(rd):
        cpu_hi += 1 if r.get("cpu_high") else 0
    summ["cpu_high_requests"] = cpu_hi
    if not settings.get("traffic_light", True):
        summ["verdict_hidden"] = True
    storage.write_json(rd / "summary.json", summ)
    return summ


def get_summary(rd: Path, fresh: bool = False) -> dict:
    sp = rd / "summary.json"
    if not fresh and sp.exists():
        s = storage.read_json(sp, {})
        newer_settings = storage.SETTINGS.exists() and storage.SETTINGS.stat().st_mtime > sp.stat().st_mtime
        if s.get("tool_version") == storage.TOOL_VERSION and not newer_settings:
            return s
    return summarize_run(rd)


def batch_report(run_ids: list[str]) -> dict:
    """Die vier Kennzahlen über alle Tests eines Batches + Karten pro Test."""
    settings = storage.get_settings()
    cards, kpi = [], {"real_parallelism": None, "max_users": None, "eur_per_m": None,
                      "error_rate_pct": None}
    total, failed = 0, 0
    for rid in run_ids:
        rd = storage.run_dir(rid)
        cfg = storage.read_json(rd / "config.json", {})
        s = get_summary(rd)
        for r in storage.iter_requests(rd):
            total += 1
            failed += 0 if stats.is_ok(r) else 1
        k = s.get("kpis") or {}
        for key in ("real_parallelism", "max_users", "eur_per_m"):
            if k.get(key) is not None:
                cur = kpi[key]
                # Mehrere Tests liefern denselben Wert (z. B. mehrere Lastprofile): konservativ.
                if cur is None or (key != "eur_per_m" and k[key]["value"] < cur["value"]):
                    kpi[key] = k[key]
        cards.append({"run_id": rid, "test": cfg.get("test"), "name": cfg.get("test_name"),
                      "status": cfg.get("status"), "verdict": s.get("verdict"),
                      "headline": s.get("headline"), "summary": s, "config": _public_cfg(cfg)})
    cards.sort(key=lambda c: (c["config"].get("batch_index") is None, c["config"].get("batch_index") or 0,
                              c["run_id"]))
    if total:
        kpi["error_rate_pct"] = {"value": round(100 * failed / total, 2),
                                 "text": f"{failed} von {total} Anfragen fehlgeschlagen"}
    return {"kpis": kpi, "cards": cards, "traffic_light": settings.get("traffic_light", True),
            "settings": settings}


def _public_cfg(cfg: dict) -> dict:
    c = dict(cfg)
    prof = dict(c.get("profile") or {})
    prof.pop("api_key", None)
    c["profile"] = prof
    return c


# ======================================================================= Manager

class Manager:
    def __init__(self) -> None:
        self.current: Batch | None = None

    def start(self, profile: dict, items: list[dict], label: str = "") -> Batch:
        if self.current and self.current.state == "laeuft":
            raise RuntimeError("Es läuft bereits ein Test.")
        mods = modules()
        for it in items:
            if it["test"] not in mods:
                raise ValueError(f"Unbekannter Test: {it['test']}")
        b = Batch(profile, items, mods, label)
        self.current = b
        b.task = asyncio.create_task(b.execute())
        return b

    def cancel(self) -> bool:
        if self.current and self.current.state == "laeuft":
            self.current.cancel_event.set()
            return True
        return False


manager = Manager()

