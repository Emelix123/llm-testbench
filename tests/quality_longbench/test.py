"""LongBench v2: Langkontext-Multiple-Choice über den Endpunkt."""
from __future__ import annotations

import asyncio
import json
import random
import re

from app import stats, storage
from app.client import message_of
from app.loadprofiles import max_context
from app.quality import reference_compare

TEMPLATE = """Please read the following text and answer the question below.

<text>
{context}
</text>

What is the correct answer to this question: {question}
Choices:
(A) {A}
(B) {B}
(C) {C}
(D) {D}

Format your response as follows: "The correct answer is (insert answer here)"."""


def estimate(params):
    return 120 + params["questions"] * 40 / max(1, params["concurrency"])


def load_dataset(ctx):
    cache = storage.DATA / "datasets" / "longbench_v2.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    from huggingface_hub import hf_hub_download
    ctx.progress(0.01, "lade LongBench v2 (einmalig, ca. 450 MB)")
    fp = hf_hub_download("THUDM/LongBench-v2", "data.json", repo_type="dataset", token=storage.get_secret("HF_TOKEN"))
    cache.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(open(fp, encoding="utf-8").read())
    cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


async def run(ctx):
    p = ctx.params
    try:
        data = await asyncio.to_thread(load_dataset, ctx)
    except Exception as e:  # noqa: BLE001
        ctx.note(f"LongBench v2 konnte nicht geladen werden ({type(e).__name__}: {str(e)[:200]}). "
                 "Internetzugang zu huggingface.co nötig, oder Datei nach data/datasets/longbench_v2.json legen.")
        return
    budget = max_context(ctx) - 200
    rng = random.Random(p["seed"])
    rng.shuffle(data)
    picked = []
    for item in data:
        n = ctx.factory.count(item["context"]) if len(item["context"]) < budget * 8 else budget * 2
        if n <= budget or p["truncate"]:
            picked.append(item)
        if len(picked) >= p["questions"]:
            break
    sem = asyncio.Semaphore(p["concurrency"])
    done = {"n": 0}

    def truncate(text):
        if ctx.factory.count(text) <= budget - 400:
            return text, False
        keep = int(len(text) * (budget - 400) / max(1, ctx.factory.count(text)))
        return text[:keep // 2] + "\n…\n" + text[-keep // 2:], True

    async def one(item):
        async with sem:
            if ctx.cancelled:
                return
            ctxt, cut = truncate(item["context"])
            prompt = TEMPLATE.format(context=ctxt, question=item["question"], A=item["choice_A"], B=item["choice_B"],
                                     C=item["choice_C"], D=item["choice_D"])
            r = await ctx.once({"messages": [{"role": "user", "content": prompt}], "max_tokens": 128, "temperature": 0},
                               {"stage": "longbench", "id": item["_id"]}, record=True)
            c = message_of(r).get("content") or ""
            m = re.search(r"correct answer is \(?([A-D])\)?", c) or re.search(r"\(([A-D])\)", c)
            pred = m.group(1) if m else None
            ctx.stage({"item": {"id": item["_id"], "difficulty": item.get("difficulty"), "length": item.get("length"),
                                "domain": item.get("domain"), "pred": pred, "answer": item["answer"],
                                "ok": pred == item["answer"], "gekuerzt": cut, "status": r["status"]}})
            done["n"] += 1
            ctx.progress(done["n"] / len(picked), f"{done['n']} von {len(picked)} Fragen")
    await asyncio.gather(*(one(i) for i in picked))


def summarize(recs, stages, cfg, settings):
    items = [s["item"] for s in stages if "item" in s]
    if not items:
        return {"verdict": None, "headline": "Nicht ausgeführt (Datensatz nicht verfügbar) – siehe Hinweise.",
                "kpis": {}, "charts": [], "tables": []}
    acc = 100 * sum(i["ok"] for i in items) / len(items)
    scores = {"LongBench v2 gesamt": acc}
    for key in ("length", "difficulty"):
        g = {}
        for i in items:
            g.setdefault(i.get(key), []).append(i["ok"])
        for k, v in g.items():
            scores[f"LongBench v2 {key}={k}"] = 100 * sum(v) / len(v)
    cmp = reference_compare("quality_longbench", cfg.get("run_id"), scores, cfg["params"]["max_drop_pts"])
    cut = sum(i["gekuerzt"] for i in items)
    head = (f"Trefferquote {stats.fmt_num(acc, 1)} % über {len(items)} Fragen (Zufall 25 %), {cut} Kontexte gekürzt. "
            + cmp["text"])
    return {"verdict": cmp["verdict"], "headline": head, "scores": scores, "kpis": {}, "charts": [],
            "tables": [{"title": "Ergebnisse und Referenz", "columns": ["Teilmenge", "Trefferquote", "Referenz", "Abweichung"],
                        "rows": cmp["rows"]}]}
