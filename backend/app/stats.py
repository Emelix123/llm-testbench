"""Kennzahlen aus Rohdaten. Zeiten immer als p50/p95/p99, nie als Durchschnitt."""
from __future__ import annotations

import math
from collections import Counter
from typing import Iterable


def pct(values: Iterable[float], p: float) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return v[int(k)]
    return v[f] + (v[c] - v[f]) * (k - f)


def pcts(values: list[float]) -> dict:
    return {f"p{p}": _r(pct(values, p)) for p in (50, 95, 99)}


def _r(x, nd=1):
    return None if x is None else round(x, nd)


def ttft(r: dict) -> float | None:
    if r.get("t_first_token") is None:
        return None
    return r["t_first_token"] - r["t_send"]


def e2e(r: dict) -> float | None:
    end = r.get("t_last_token")
    if end is None:
        return None
    return end - r["t_send"]


def tpot(r: dict) -> float | None:
    """Mittlere Zeit pro Output-Token einer Anfrage (aus usage, nicht aus Chunks)."""
    n = r.get("completion_tokens")
    if not n or n < 2 or r.get("t_first_token") is None or r.get("t_last_token") is None:
        return None
    return (r["t_last_token"] - r["t_first_token"]) / (n - 1)


def is_ok(r: dict) -> bool:
    return r.get("error_type") is None and r.get("status") == 200


def max_overlap(intervals: list[tuple[float, float]]) -> int:
    """Höchste Zahl gleichzeitig offener Intervalle."""
    ev = []
    for a, b in intervals:
        if a is None or b is None or b < a:
            continue
        ev.append((a, 1))
        ev.append((b, -1))
    ev.sort(key=lambda x: (x[0], x[1]))
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


def aggregate(recs: list[dict], slo: dict | None = None) -> dict:
    """Standard-Kennzahlen für eine Menge von Anfragen (z. B. eine Laststufe)."""
    n = len(recs)
    ok = [r for r in recs if is_ok(r)]
    errs = Counter(r.get("error_type") or "ok" for r in recs)
    errs.pop("ok", None)
    ttfts = [ttft(r) for r in ok]
    e2es = [e2e(r) for r in ok]
    tpots = [tpot(r) for r in ok]
    itls = [x for r in ok for x in (r.get("itl_ms") or [])]
    compact = not itls and any(r.get("itl_summary") for r in ok)
    if compact:
        # Dauertests: ITL-Perzentile näherungsweise aus den Zusammenfassungen je Anfrage
        itl_p = {"p50": _r(pct([r["itl_summary"]["p50"] for r in ok if r.get("itl_summary")], 50)),
                 "p95": _r(pct([r["itl_summary"]["p95"] for r in ok if r.get("itl_summary")], 50)),
                 "p99": _r(pct([r["itl_summary"]["p95"] for r in ok if r.get("itl_summary")], 95))}
    out_tok = sum(r.get("completion_tokens") or 0 for r in ok)
    in_tok = sum(r.get("prompt_tokens") or 0 for r in ok)
    cached = [r.get("cached_tokens") for r in ok if r.get("cached_tokens") is not None]
    if recs:
        t_start = min(r["t_send"] for r in recs)
        ends = [r.get("t_end") or r.get("t_last_token") or r["t_send"] for r in recs]
        window_s = max(1e-3, (max(ends) - t_start) / 1000)
    else:
        window_s = None
    tpot_vals = [x for x in tpots if x]
    # Hänger: lange Lücke mitten in der Antwort (Hinweis auf vollen KV-Cache / Preemption)
    itl_med = pct(itls, 50) or 0
    stall_thr = max(500.0, 10 * itl_med)
    if compact:
        stall_thr = max(500.0, 10 * (itl_p["p50"] or 0))
        stalled_req = sum(1 for r in ok if (r.get("itl_summary") or {}).get("max", 0) > stall_thr)
        stalls = stalled_req
    else:
        stalls = sum(1 for r in ok for x in (r.get("itl_ms") or []) if x > stall_thr)
        stalled_req = sum(1 for r in ok if any(x > stall_thr for x in (r.get("itl_ms") or [])))
    per_user_tps = [1000 / x for x in tpot_vals if x > 0]
    res = {
        "requests": n,
        "ok": len(ok),
        "errors": dict(errs),
        "error_rate_pct": _r(100 * (n - len(ok)) / n, 2) if n else None,
        "ttft_ms": pcts(ttfts),
        "itl_ms": itl_p if compact else pcts(itls),
        "tpot_ms": pcts(tpot_vals),
        "e2e_ms": pcts(e2es),
        "tokens_per_s_user_p50": _r(pct(per_user_tps, 50)),
        "tokens_per_s_user_p5": _r(pct(per_user_tps, 5)),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cached_tokens": sum(cached) if cached else None,
        "window_s": _r(window_s, 2),
        "output_tps": _r(out_tok / window_s) if window_s else None,
        "input_tps": _r(in_tok / window_s) if window_s else None,
        "rps": _r(len(ok) / window_s, 3) if window_s else None,
        "litellm_overhead_ms": pcts([r.get("litellm_overhead_ms") for r in ok
                                     if r.get("litellm_overhead_ms") is not None]),
        "litellm_retries": sum(r.get("litellm_retries") or 0 for r in recs),
        "litellm_fallbacks": sum(r.get("litellm_fallbacks") or 0 for r in recs),
        "client_cpu_max": max((r.get("client_cpu_pct") or 0 for r in recs), default=None),
        "stalls": stalls,
        "stalled_requests": stalled_req,
        "max_streaming": max_overlap([(r.get("t_first_token"), r.get("t_last_token")) for r in ok]),
    }
    if slo:
        good = 0
        for r in ok:
            t, tp = ttft(r), tpot(r)
            if t is not None and t <= slo["ttft_p95_ms"] and (tp is None or tp <= slo["itl_p95_ms"]):
                good += 1
        res["goodput_pct"] = _r(100 * good / n, 1) if n else None
        res["meets_slo"] = meets_slo(res, slo)
    return res


def meets_slo(agg: dict, slo: dict) -> bool:
    t = agg["ttft_ms"]["p95"]
    i = agg["itl_ms"]["p95"]
    if t is None:
        return False
    if t > slo["ttft_p95_ms"]:
        return False
    if i is not None and i > slo["itl_p95_ms"]:
        return False
    if (agg["error_rate_pct"] or 0) > slo["error_rate_pct"]:
        return False
    return True


def active_users(concurrency: float, e2e_s: float, read_s: float) -> float:
    """aktive Nutzer ≈ Parallelität · (Antwortzeit + Lesezeit) / Antwortzeit"""
    if not e2e_s:
        return 0
    return concurrency * (e2e_s + read_s) / e2e_s


def eur_per_million(tokens_per_s: float, cost: dict) -> float | None:
    if not tokens_per_s:
        return None
    eur_h = cost["machine_eur_per_h"] + cost["power_kw"] * cost["power_eur_per_kwh"]
    tokens_h = tokens_per_s * 3600 * cost["utilization_pct"] / 100
    if tokens_h <= 0:
        return None
    return eur_h / tokens_h * 1e6


def fmt_ms(x: float | None) -> str:
    if x is None:
        return "–"
    if x >= 1000:
        return f"{x / 1000:.1f} s".replace(".", ",")
    return f"{x:.0f} ms"


def fmt_num(x: float | None, nd: int = 0) -> str:
    if x is None:
        return "–"
    s = f"{x:,.{nd}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_pct(x: float | None, nd: int = 1) -> str:
    return "–" if x is None else fmt_num(x, nd) + " %"


def sustained_overlap(intervals, min_ms=1000.0):
    """Höchste Zahl gleichzeitig strömender Antworten, die mindestens min_ms lang gehalten wurde."""
    ev = []
    for a, b in intervals:
        if a is None or b is None or b <= a:
            continue
        ev += [(a, 1), (b, -1)]
    if not ev:
        return 0
    ev.sort(key=lambda x: (x[0], x[1]))
    held = {}
    cur, last_t = 0, ev[0][0]
    for t, d in ev:
        if cur > 0:
            held[cur] = held.get(cur, 0) + (t - last_t)
        cur += d
        last_t = t
    best = 0
    for k in sorted(held):
        # Zeit, in der mindestens k gleichzeitig liefen
        if sum(v for kk, v in held.items() if kk >= k) >= min_ms:
            best = k
    return best


def eur_per_hour(cost: dict) -> float:
    return cost["machine_eur_per_h"] + cost["power_kw"] * cost["power_eur_per_kwh"]


def slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    """Lineare Regression y über x (x in Sekunden) → Änderung pro Stunde."""
    pts = [(x, y) for x, y in points if x is not None and y is not None]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    return sxy / sxx * 3600


def windows(recs: list[dict], width_s: float) -> list[tuple[float, list[dict]]]:
    """Teilt Anfragen nach Sendezeit in Zeitfenster (Start des Fensters in s, Anfragen)."""
    if not recs:
        return []
    t0 = min(r["t_send"] for r in recs)
    out: dict[int, list] = {}
    for r in recs:
        out.setdefault(int((r["t_send"] - t0) / 1000 // width_s), []).append(r)
    return [(k * width_s, v) for k, v in sorted(out.items())]


def queue_growth(recs: list[dict]) -> dict:
    """Wächst die Warteschlange? Vergleicht TTFT im ersten und letzten Drittel einer Stufe."""
    ok = sorted((r for r in recs if is_ok(r) and r.get("t_first_token") is not None), key=lambda r: r["t_send"])
    if len(ok) < 6:
        return {"growing": None, "first_p50": None, "last_p50": None}
    k = len(ok) // 3
    a = pct([ttft(r) for r in ok[:k]], 50)
    b = pct([ttft(r) for r in ok[-k:]], 50)
    growing = b > 1.5 * a and b - a > 500
    return {"growing": growing, "first_p50": round(a, 1), "last_p50": round(b, 1)}
