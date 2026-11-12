"""Testbilder ohne Zusatzpakete: einfarbige PNGs als data-URL."""
from __future__ import annotations

import base64
import struct
import zlib

COLORS = {
    "rot": ((220, 30, 30), ("rot", "red")),
    "grün": ((30, 180, 60), ("grün", "gruen", "green")),
    "blau": ((30, 60, 220), ("blau", "blue")),
    "gelb": ((240, 220, 30), ("gelb", "yellow")),
}


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def data_url(width: int, height: int, color: str) -> str:
    return "data:image/png;base64," + base64.b64encode(png(width, height, COLORS[color][0])).decode()


def mentions(text: str, color: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in COLORS[color][1])
