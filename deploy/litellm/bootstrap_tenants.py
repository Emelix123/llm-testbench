"""Legt die Test-Mandanten in LiteLLM an (Teams + Virtual Keys mit festen Werten aus deploy/.env).

Läuft als Einmal-Container im Szenario (docker compose run --rm tenant-setup). Bei jedem Szenario-
Wechsel werden die Keys gelöscht und mit den Grenzen des Szenarios neu angelegt – so tragen sie nie
Grenzen aus einem vorigen Szenario mit sich.

Umgebung:
  LITELLM_URL, LITELLM_MASTER_KEY
  TENANT_A_KEY / TENANT_B_KEY / TENANT_C_KEY
  TENANT_RPM, TENANT_TPM, TENANT_MAX_PARALLEL   0 = keine Grenze (Szenario K3/K4 setzen Werte)
  TENANT_ALLOWED_ROUTES  Komma-Liste erlaubter Routen; leer = LiteLLM-Standard (inkl. /vllm/*-Durchreichung!)
Nur Standardbibliothek, damit es im LiteLLM-Image ohne Zusatzpakete läuft.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
MASTER = os.environ["LITELLM_MASTER_KEY"]

TENANTS = [
    ("mandant-a", "Mandant A (Opfer)", "TENANT_A_KEY"),
    ("mandant-b", "Mandant B (Angreifer)", "TENANT_B_KEY"),
    ("mandant-c", "Mandant C (Last)", "TENANT_C_KEY"),
]


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {MASTER}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


def limit(name: str) -> int | None:
    v = int(os.environ.get(name, "0") or 0)
    return v if v > 0 else None


def main() -> int:
    for _ in range(90):
        st, _ = call("GET", "/health/readiness")
        if st == 200:
            break
        time.sleep(2)
    else:
        print("LiteLLM nicht bereit", file=sys.stderr)
        return 1

    routes = [r.strip() for r in os.environ.get("TENANT_ALLOWED_ROUTES", "").split(",") if r.strip()]
    lim = {"rpm_limit": limit("TENANT_RPM"), "tpm_limit": limit("TENANT_TPM"),
           "max_parallel_requests": limit("TENANT_MAX_PARALLEL")}
    scenario = os.environ.get("LLMTB_SCENARIO", "?")
    for team_id, alias, env in TENANTS:
        key = os.environ.get(env, "")
        if not key:
            print(f"{env} fehlt – {team_id} übersprungen", file=sys.stderr)
            continue
        call("POST", "/team/new", {"team_id": team_id, "team_alias": alias})  # existiert evtl. schon
        call("POST", "/key/delete", {"keys": [key]})
        body = {"key": key, "team_id": team_id, "key_alias": f"{team_id}-{scenario.lower()}",
                "metadata": {"llmtb_scenario": scenario}, **{k: v for k, v in lim.items() if v}}
        if routes:
            body["allowed_routes"] = routes
        st, resp = call("POST", "/key/generate", body)
        if st != 200:
            print(f"{team_id}: Key anlegen fehlgeschlagen ({st}): {resp}", file=sys.stderr)
            return 1
        shown = {k: v for k, v in body.items() if k not in ("key", "metadata")}
        print(f"[llmtb] {alias}: {json.dumps(shown, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
