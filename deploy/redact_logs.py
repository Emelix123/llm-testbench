#!/usr/bin/env python3
"""Schwärzt Geheimnisse in Container-Logs: liest stdin, schreibt stdout.

Genutzt von `./scenario.sh save-logs` (auch vor jedem `down`). Ersetzt
  * alle geheimen Werte aus deploy/.env (Master-Key, Mandanten-Keys, Passwörter, Pool-Geheimnis, HF-Token)
    und deploy/secrets/* (cache_salt-Geheimnis, Wasserzeichen-Schlüssel) – auch wenn vLLM sie z. B. in
    „non-default args“ ausgibt,
  * vorsichtshalber alles, was wie ein API-Key aussieht (sk-…, Bearer …, hf_…) und den Schlüssel in einer
    Wasserzeichen-Konfiguration ("key": 123…).

Nur Standardbibliothek. Die Logs dürfen danach in den Export und in einen Anhang der Arbeit.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent
MASK = "<geschwärzt>"
SECRET_SUFFIXES = ("_KEY", "_SECRET", "_PASSWORD", "_TOKEN")


def secret_values() -> list[str]:
    vals: set[str] = set()
    env = DEPLOY / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k.strip().endswith(SECRET_SUFFIXES) and len(v) >= 6:
                vals.add(v)
    sec = DEPLOY / "secrets"
    if sec.is_dir():
        for p in sec.iterdir():
            try:
                v = p.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if len(v) >= 6:
                vals.add(v)
    return sorted(vals, key=len, reverse=True)


PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-" + MASK),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-.=]{6,}", re.I), r"\1" + MASK),
    (re.compile(r"\bhf_[A-Za-z0-9]{16,}"), "hf_" + MASK),
    (re.compile(r"""(["']key["']\s*:\s*)\d{6,}"""), r"\1" + MASK),
    (re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1" + MASK + "@"),
]


def main() -> None:
    exact = secret_values()
    exact_re = re.compile("|".join(re.escape(v) for v in exact)) if exact else None
    out = sys.stdout
    for line in sys.stdin:
        if exact_re is not None:
            line = exact_re.sub(MASK, line)
        for rx, rep in PATTERNS:
            line = rx.sub(rep, line)
        out.write(line)


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
