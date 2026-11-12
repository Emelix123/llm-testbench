"""Synthetische Prompts mit exakter Token-Länge.

Der Tokenizer des Modells (Hugging Face `tokenizers`) wird über den Hub-Namen
oder einen lokalen Pfad zu tokenizer.json geladen. Ist er nicht erreichbar,
fällt das Tool auf eine kalibrierte Schätzung zurück (Zeichen pro Token aus
usage.prompt_tokens) und schreibt das in config.json. Die tatsächliche Länge
steht in jedem Fall aus usage in requests.jsonl.
"""
from __future__ import annotations

import random
import threading
from pathlib import Path

from . import storage

# Neutrales Vokabular (de/en gemischt), rundet beim Tokenisieren stabil.
_WORDS = (
    "the of and to in is that for it as was with be by on not he this are or his from at which "
    "but have an they you were her she there been one all we their has would when if so no will "
    "Haus Stadt Wasser Zeit Arbeit Jahr Weg Welt Leben Hand Tag Auge Frage Kind Land Bericht Zahl "
    "Grund Teil Woche Monat Abend Morgen Licht Musik Fenster Garten Brief Reise Markt Wald Fluss "
    "river mountain system report number market window garden letter travel forest morning light "
    "value table data model server request result method process analysis design project network "
    "rot blau grün schnell langsam groß klein alt neu gut hell dunkel warm kalt leise laut offen "
    "red blue green fast slow large small old new good bright dark warm cold quiet loud open "
    "und oder aber weil wenn dann noch schon auch nur sehr mehr immer wieder hier dort heute "
    "Kapitel Abschnitt Vertrag Klausel Partei Leistung Frist Zahlung Kosten Anlage Ziffer Absatz"
).split()

_cache: dict[str, "PromptFactory"] = {}
_cache_lock = threading.Lock()


def load_tokenizer(name: str | None):
    """Gibt (tokenizer|None, info) zurück."""
    if not name:
        return None, "kein Tokenizer angegeben"
    try:
        from tokenizers import Tokenizer
    except ImportError:
        return None, "Paket tokenizers fehlt"
    p = Path(name).expanduser()
    try:
        if p.is_file():
            return Tokenizer.from_file(str(p)), f"Datei {p}"
        if p.is_dir() and (p / "tokenizer.json").exists():
            return Tokenizer.from_file(str(p / "tokenizer.json")), f"Datei {p / 'tokenizer.json'}"
        cached = storage.TOKENIZER_CACHE / name.replace("/", "__") / "tokenizer.json"
        if cached.exists():
            return Tokenizer.from_file(str(cached)), f"Cache {name}"
        try:
            from huggingface_hub import hf_hub_download
            token = storage.get_secret("HF_TOKEN")
            fp = hf_hub_download(name, "tokenizer.json", token=token)
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(Path(fp).read_bytes())
            return Tokenizer.from_file(str(cached)), f"Hugging Face {name}"
        except Exception as e:  # noqa: BLE001
            return None, f"Laden fehlgeschlagen ({type(e).__name__}: {str(e)[:160]})"
    except Exception as e:  # noqa: BLE001
        return None, f"Laden fehlgeschlagen ({type(e).__name__}: {str(e)[:160]})"


class PromptFactory:
    def __init__(self, tokenizer_name: str | None):
        self.tok, self.tokenizer_info = load_tokenizer(tokenizer_name)
        self.exact = self.tok is not None
        self.chars_per_token = 4.0        # wird kalibriert, falls kein Tokenizer
        self.template_overhead = 0        # Tokens des Chat-Templates, kalibriert
        self._pool_ids: list[int] = []
        self._pool_text = ""
        rng = random.Random(1234)
        self._pool_text = " ".join(rng.choice(_WORDS) for _ in range(60000))
        if self.tok is not None:
            self._pool_ids = self.tok.encode(self._pool_text, add_special_tokens=False).ids

    # ------------------------------------------------------------- Zählen
    def count(self, text: str) -> int:
        if self.tok is not None:
            return len(self.tok.encode(text, add_special_tokens=False).ids)
        return max(1, round(len(text) / self.chars_per_token))

    def calibrate(self, sample_text: str, prompt_tokens_from_server: int) -> None:
        """Gleicht lokale Zählung mit usage.prompt_tokens ab (eine Nachricht, user-Rolle)."""
        if not prompt_tokens_from_server:
            return
        if self.tok is not None:
            self.template_overhead = max(0, prompt_tokens_from_server - self.count(sample_text))
        else:
            # Template-Overhead bei Schätzung grob mit 10 Tokens ansetzen.
            content_tokens = max(1, prompt_tokens_from_server - 10)
            self.template_overhead = 10
            self.chars_per_token = max(1.5, len(sample_text) / content_tokens)

    # ------------------------------------------------------------- Erzeugen
    def _filler(self, n_tokens: int, rng: random.Random) -> str:
        if n_tokens <= 0:
            return ""
        if self.tok is not None:
            pool = self._pool_ids
            start = rng.randrange(0, max(1, len(pool) - n_tokens - 1))
            ids = pool[start:start + n_tokens]
            while len(ids) < n_tokens:  # sehr lange Kontexte: Pool wiederholen
                ids = ids + pool[: n_tokens - len(ids)]
            text = self.tok.decode(ids)
            # Rundreise korrigieren, bis die Länge exakt passt.
            for _ in range(6):
                n = self.count(text)
                if n == n_tokens:
                    break
                ids2 = self.tok.encode(text, add_special_tokens=False).ids
                if n > n_tokens:
                    ids2 = ids2[:n_tokens]
                else:
                    extra = pool[: n_tokens - n]
                    ids2 = ids2 + extra
                text = self.tok.decode(ids2)
            return text
        n_chars = int(n_tokens * self.chars_per_token)
        start = rng.randrange(0, max(1, len(self._pool_text) - n_chars - 1))
        s = self._pool_text[start:start + n_chars]
        while len(s) < n_chars:
            s += " " + self._pool_text[: n_chars - len(s)]
        return s

    def unique_prefix(self, rng: random.Random) -> str:
        """Eindeutiger Zufallsanfang, damit der Prefix-Cache nicht mitmisst."""
        return f"[{rng.getrandbits(64):016x}-{rng.getrandbits(64):016x}] "

    def make(self, total_input_tokens: int, rng: random.Random, unique: bool = True,
             shared_prefix: str = "") -> str:
        """Ein user-Text, der zusammen mit dem Chat-Template auf total_input_tokens kommt."""
        prefix = (shared_prefix or "") + (self.unique_prefix(rng) if unique else "")
        instr = " Antworte ausführlich und schreibe einen langen, zusammenhängenden Text."
        target = total_input_tokens - self.template_overhead
        fixed = self.count(prefix + instr)
        body = self._filler(max(1, target - fixed), rng)
        text = prefix + body + instr
        if self.tok is not None:
            # Feinkorrektur an der Nahtstelle (Tokens können verschmelzen).
            for _ in range(4):
                diff = target - self.count(text)
                if diff == 0:
                    break
                if diff > 0:
                    body = body + self._filler(diff, rng)
                else:
                    ids = self.tok.encode(body, add_special_tokens=False).ids
                    body = self.tok.decode(ids[: max(1, len(ids) + diff)])
                text = prefix + body + instr
        return text

    def info(self) -> dict:
        return {
            "tokenizer": self.tokenizer_info,
            "exact": self.exact,
            "template_overhead": self.template_overhead,
            "chars_per_token": None if self.exact else round(self.chars_per_token, 3),
        }


def get_factory(tokenizer_name: str | None) -> PromptFactory:
    key = tokenizer_name or ""
    with _cache_lock:
        f = _cache.get(key)
        if f is None or (f.tok is None and tokenizer_name):
            f = PromptFactory(tokenizer_name)
            _cache[key] = f
        return f
