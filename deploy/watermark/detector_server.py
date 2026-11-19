"""Wasserzeichen-Detektor (KI-VO Art. 50 Abs. 2) als kleiner HTTP-Dienst für die Testplattform.

Nutzt den Gumbel-Detektor von vLLM (vllm.v1.watermarking) mit demselben geheimen Schlüssel wie die
Engine. Läuft ohne GPU (CPU-Pfad der Philox-PRF). Nur für den Betreiber: an 127.0.0.1 gebunden, nie
für Mandanten erreichbar – wer den Schlüssel bzw. diesen Dienst hat, kann Texte als KI-erzeugt
nachweisen (und gezielt Gegenproben bauen).

  POST /detect  {"text": "..."}  ->  {is_watermarked, p_value, score, num_scored_tokens, num_tokens}
  GET  /health
"""
from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoTokenizer
from vllm.v1.watermarking import GumbelWatermarkDetector

KEY_FILE = os.environ.get("WATERMARK_KEY_FILE", "/run/secrets/watermark_key")
with open(KEY_FILE, encoding="utf-8") as fh:
    KEY = int(fh.read().strip())
CONTEXT_WIDTH = int(os.environ.get("WATERMARK_CONTEXT_WIDTH", "4"))
THRESHOLD = float(os.environ.get("WATERMARK_P_THRESHOLD", "0.01"))

tok = AutoTokenizer.from_pretrained(os.environ["MODEL"])
det = GumbelWatermarkDetector(key=KEY, context_width=CONTEXT_WIDTH, p_value_threshold=THRESHOLD, prf="philox")
app = FastAPI(title="llmtb-watermark-detector", docs_url=None, redoc_url=None, openapi_url=None)


class Req(BaseModel):
    text: str


@app.get("/health")
def health():
    return {"ok": True, "context_width": CONTEXT_WIDTH, "p_value_threshold": THRESHOLD}


@app.post("/detect")
def detect(r: Req):
    ids = tok.encode(r.text, add_special_tokens=False)
    if len(ids) <= CONTEXT_WIDTH:
        return {"is_watermarked": False, "p_value": 1.0, "score": None, "num_scored_tokens": 0, "num_tokens": len(ids)}
    d = det.detect(ids)
    return {"is_watermarked": bool(d.is_watermarked), "p_value": float(d.p_value),
            "score": float(d.score) if d.score is not None else None,
            "num_scored_tokens": int(d.num_scored_tokens), "num_tokens": len(ids)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="warning")
