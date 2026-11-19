"""LiteLLM-Hook: Mandantentrennung im Gateway (Bachelorarbeit, Abschn. 1.3.4 „Gemeinsame Cache-Identität
im Gateway“ / KeyPooling).

vLLM kennt keine Mandanten. Ein Präfix-Block wird allein an Tokens und Präfix erkannt – nicht am
Mandanten. Laut vLLM-Security-Doku muss deshalb ein authentifizierendes Gateway `cache_salt` je
Mandant setzen (zufälliges Geheimnis ~256 Bit, KEINE vorhersagbare ID). Genau das macht dieser Hook:

  1. Mandant = Team der LiteLLM-Virtual-Key (Fallback: User, sonst Hash des Keys).
  2. cache_salt = HMAC-SHA256(Geheimnis, Mandant), base64url (43 Zeichen, ohne '/', da vLLM '/' ablehnt).
     Vom Client gesendete cache_salt-Werte werden verworfen – der Mandant darf ihn nicht wählen.
  3. Optional: gefährliche, vom Client steuerbare vLLM-Felder entfernen (prompt_embeds →
     CVE-2026-55514, kv_transfer_params, mm_processor_kwargs, priority, watermarking=false …).
  4. Optional: n je Mandant begrenzen (CVE-2026-34756; vLLM begrenzt n nur global).
  5. Optional (Szenario K5): jeden Mandanten fest auf seine eigene vLLM-Instanz leiten.

Gesteuert über Umgebungsvariablen (siehe deploy/compose/*.yaml):
  TENANT_SALT_MODE      off | per_tenant | per_request        (Standard: off)
  TENANT_STRIP_PARAMS   1 = gefährliche Felder entfernen       (Standard: 0)
  TENANT_MAX_N          größtes erlaubtes n, 0 = keine Grenze  (Standard: 0)
  TENANT_ROUTING        JSON {"mandant-a": "llm-mandant-a", …} (Standard: leer)
  TENANT_HIDE_INTERNALS 1 = interne Angaben in Antwort-Headern überschreiben (x-litellm-model-api-base
                        verrät sonst die Adresse der Engine, z. B. http://vllm:8000/v1)
  CACHE_SALT_SECRET_FILE  Datei mit dem HMAC-Geheimnis (Docker-Secret)

Hinweis: Der Hook läuft nur für LLM-Aufrufe. Durchreich-Routen wie /vllm/{pfad} umgehen ihn –
deshalb erhalten die Mandanten-Keys in den gehärteten Szenarien eine Routen-Allowlist
(allowed_routes, siehe bootstrap_tenants.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("llmtb.tenant_isolation")

# Vom Client steuerbare vLLM-Felder, die ein Mandant nicht setzen darf.
STRIP_FIELDS = (
    "cache_salt",            # Mandant darf den Salt nicht wählen
    "prompt_embeds",         # CVE-2026-55514 (Absturz der Engine); nur für vertrauenswürdige Nutzer
    "kv_transfer_params",    # P2P/Disaggregation: laut Doku am Proxy entfernen/prüfen
    "mm_processor_kwargs",   # Ressourcenverbrauch der Multimodal-Vorverarbeitung
    "media_io_kwargs",
    "priority",              # Vordrängeln bei --scheduling-policy priority
    "watermarking",          # Opt-out vom Wasserzeichen (KI-VO Art. 50)
    "chat_template",         # eigenes Template (vLLM lehnt es standardmäßig ab)
    "vllm_xargs",            # beliebige Zusatzargumente an Sampling/Logits-Prozessoren
)
LLM_CALL_TYPES = {"completion", "acompletion", "text_completion", "atext_completion"}


def _load_secret() -> bytes:
    path = os.environ.get("CACHE_SALT_SECRET_FILE", "/run/secrets/cache_salt_secret")
    try:
        with open(path, "rb") as fh:
            raw = fh.read().strip()
    except OSError:
        raw = b""
    if len(raw) < 32:
        # Ohne Geheimnis wäre der Salt vorhersagbar (= wertlos). Lieber laut scheitern.
        if os.environ.get("TENANT_SALT_MODE", "off") == "per_tenant":
            raise RuntimeError(f"cache_salt-Geheimnis fehlt oder ist zu kurz ({path}). ./scenario.sh init ausführen.")
    return raw


class TenantIsolation(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self.salt_mode = os.environ.get("TENANT_SALT_MODE", "off").strip().lower()
        self.strip = os.environ.get("TENANT_STRIP_PARAMS", "0") == "1"
        self.max_n = int(os.environ.get("TENANT_MAX_N", "0") or 0)
        self.routing: dict[str, str] = json.loads(os.environ.get("TENANT_ROUTING") or "{}")
        self.hide = os.environ.get("TENANT_HIDE_INTERNALS", "0") == "1"
        self.secret = _load_secret() if self.salt_mode == "per_tenant" else b""
        log.warning("[llmtb] Mandanten-Hook aktiv: salt=%s strip=%s max_n=%s routing=%s hide=%s",
                    self.salt_mode, self.strip, self.max_n or "-", sorted(self.routing) or "-", self.hide)

    # ------------------------------------------------------------------ Hilfen
    @staticmethod
    def tenant_of(u) -> str:
        for attr in ("team_id", "user_id"):
            v = getattr(u, attr, None)
            if v:
                return str(v)
        tok = getattr(u, "token", None) or getattr(u, "api_key", None) or "anonym"
        return "key-" + hashlib.sha256(str(tok).encode()).hexdigest()[:16]

    def salt_for(self, tenant: str) -> str:
        if self.salt_mode == "per_request":
            return secrets.token_urlsafe(32)
        mac = hmac.new(self.secret, b"llmtb-cache-salt-v1\x00" + tenant.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()   # 43 Zeichen, nur [A-Za-z0-9_-]

    @staticmethod
    def _pop_everywhere(data: dict, field: str) -> bool:
        hit = data.pop(field, None) is not None
        for k in ("extra_body", "optional_params"):
            sub = data.get(k)
            if isinstance(sub, dict) and sub.pop(field, None) is not None:
                hit = True
        return hit

    # ------------------------------------------------------------------ Hook
    async def async_pre_call_hook(self, user_api_key_dict, cache, data: dict, call_type):
        if call_type not in LLM_CALL_TYPES:
            return data
        tenant = self.tenant_of(user_api_key_dict)

        if self.strip:
            removed = [f for f in STRIP_FIELDS if self._pop_everywhere(data, f)]
            if removed:
                log.info("[llmtb] %s: entfernt %s", tenant, removed)

        if self.max_n:
            try:
                n = int(data.get("n") or 1)
            except (TypeError, ValueError):
                n = 1
            if n > self.max_n:
                raise HTTPException(status_code=400,
                                    detail=f"n={n} überschreitet die Mandantengrenze n<={self.max_n}")

        if self.salt_mode in ("per_tenant", "per_request"):
            self._pop_everywhere(data, "cache_salt")
            eb = data.get("extra_body")
            if not isinstance(eb, dict):
                eb = {}
            eb["cache_salt"] = self.salt_for(tenant)
            data["extra_body"] = eb

        if self.routing:
            target = self.routing.get(tenant)
            if not target:
                raise HTTPException(status_code=403, detail="Für diesen Mandanten ist keine Instanz konfiguriert")
            data["model"] = target
        return data


    async def async_post_call_response_headers_hook(self, data, user_api_key_dict, response,
                                                    request_headers=None, litellm_call_info=None):
        # Rückgabewerte überschreiben LiteLLMs Standard-Header (custom_headers.update(...)).
        if not self.hide:
            return None
        return {"x-litellm-model-api-base": "-", "x-litellm-model-id": "-"}


proxy_handler_instance = TenantIsolation()
