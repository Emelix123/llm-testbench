"""lm-evaluation-harness als Unterprozess gegen den Endpunkt."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys

from app import stats
from app.quality import reference_compare


def estimate(params):
    return 120 + len(params["tasks"].split(",")) * (params["limit"] or 1000) * 1.0


def _cmd():
    exe = shutil.which("lm_eval") or shutil.which("lm-eval")
    if exe:
        return [exe]
    try:
        import lm_eval  # noqa: F401
        return [sys.executable, "-m", "lm_eval"]
    except ImportError:
        return None


async def run(ctx):
    p = ctx.params
    cmd = _cmd()
    if cmd is None:
        ctx.note('lm-evaluation-harness ist nicht installiert. Installation: pip install "lm-eval[api]" '
                 "(in der .venv der Testplattform), dann den Test erneut starten.")
        ctx.extra_config["lmeval"] = {"installed": False}
        return
    out_dir = ctx.dir / "lm_eval"
    base = ctx.ep.base_url.rstrip("/")
    url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    margs = f"model={ctx.ep.model},base_url={url},num_concurrent={p['num_concurrent']},max_retries=1,tokenized_requests=False"
    args = cmd + ["--model", "local-chat-completions", "--model_args", margs, "--tasks", p["tasks"],
                  "--apply_chat_template", "--output_path", str(out_dir)]
    if p["limit"]:
        args += ["--limit", str(p["limit"])]
    env = dict(os.environ)
    if ctx.ep.api_key:
        env["OPENAI_API_KEY"] = ctx.ep.api_key  # nur an den Unterprozess, nie in Ergebnisse
    ctx.extra_config["lmeval"] = {"installed": True, "args": [a for a in args if "key" not in a.lower()]}
    ctx.progress(0.02, "lm-eval startet")
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env)
    log = (ctx.dir / "lm_eval.log").open("w", encoding="utf-8")
    try:
        while True:
            if ctx.cancelled:
                proc.terminate()
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            t = line.decode("utf-8", "replace").rstrip()
            log.write(t + "\n")
            if "%" in t and "|" in t:
                ctx.progress(0.5, t[-80:])
        await proc.wait()
    finally:
        log.close()
    ctx.extra_config["lmeval"]["returncode"] = proc.returncode
    res = sorted(out_dir.rglob("results*.json"), key=lambda f: f.stat().st_mtime)
    if res:
        data = json.loads(res[-1].read_text(encoding="utf-8"))
        ctx.stage({"lmeval_results": data.get("results", {})})
    else:
        ctx.note("lm-eval hat keine Ergebnisdatei geschrieben – Details in lm_eval.log im Laufordner.")
    ctx.progress(1.0, "fertig")


def summarize(recs, stages, cfg, settings):
    lm = cfg.get("lmeval") or {}
    if not lm.get("installed", True):
        return {"verdict": None, "headline": 'Nicht ausgeführt: lm-evaluation-harness fehlt (pip install "lm-eval[api]").',
                "kpis": {}, "charts": [], "tables": []}
    results = next((s["lmeval_results"] for s in stages if "lmeval_results" in s), None)
    if not results:
        return {"verdict": "rot", "headline": "lm-eval lieferte keine Ergebnisse (siehe lm_eval.log).",
                "kpis": {}, "charts": [], "tables": []}
    scores = {}
    for task, metrics in results.items():
        for k, v in metrics.items():
            if isinstance(v, (int, float)) and "stderr" not in k and k != "alias" and not k.startswith("sample"):
                scores[f"{task} {k.split(',')[0]} ({k.split(',')[1] if ',' in k else ''})"] = 100 * v if v <= 1 else v
    cmp = reference_compare("quality_lmeval", cfg.get("run_id"), scores, cfg["params"]["max_drop_pts"])
    head = "; ".join(f"{k}: {stats.fmt_num(v, 1)}" for k, v in list(scores.items())[:4]) + ". " + cmp["text"]
    return {"verdict": cmp["verdict"], "headline": head, "scores": scores, "kpis": {}, "charts": [],
            "tables": [{"title": "Ergebnisse und Referenz", "columns": ["Metrik", "Punktzahl", "Referenz", "Abweichung"],
                        "rows": cmp["rows"]}]}
