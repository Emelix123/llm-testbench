"""Schutz für den Laptop als Testrechner: CPU-Last und Ruhezustand."""
from __future__ import annotations

import asyncio
import os
import platform
import subprocess

import psutil


class CpuMonitor:
    def __init__(self) -> None:
        self.value: float = 0.0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            psutil.cpu_percent(interval=None)
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.value = psutil.cpu_percent(interval=None)


cpu = CpuMonitor()


class SleepInhibitor:
    """Verhindert den Ruhezustand, solange ein Lauf aktiv ist."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._win = False
        self.method: str | None = None

    def acquire(self) -> str | None:
        system = platform.system()
        try:
            if system == "Darwin":
                self._proc = subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())])
                self.method = "caffeinate"
            elif system == "Windows":
                import ctypes
                ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self._win = True
                self.method = "SetThreadExecutionState"
            elif system == "Linux":
                self._proc = subprocess.Popen(
                    ["systemd-inhibit", "--what=sleep:idle", "--who=llm-testbench",
                     "--why=Lasttest läuft", "sleep", "infinity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.method = "systemd-inhibit"
        except Exception:  # noqa: BLE001
            self.method = None
        return self.method

    def release(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
        if self._win:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:  # noqa: BLE001
                pass
            self._win = False


def network_kind() -> str:
    """Grobe Einordnung der aktiven Verbindung: LAN, WLAN oder VPN (Heuristik über Interface-Namen)."""
    try:
        stats = psutil.net_if_stats()
        up = [n for n, s in stats.items() if s.isup and not n.lower().startswith(("lo", "docker", "veth", "br-"))]
    except Exception:  # noqa: BLE001
        return "unbekannt"
    names = " ".join(up).lower()
    kinds = []
    if any(k in names for k in ("tun", "tap", "wg", "utun", "ppp", "vpn", "tailscale", "zt")):
        kinds.append("VPN")
    if any(k in names for k in ("wl", "wi-fi", "wlan", "airport")):
        kinds.append("WLAN")
    if any(k in names for k in ("eth", "en", "ethernet")) and "WLAN" not in kinds:
        kinds.append("LAN")
    return " + ".join(kinds) or "unbekannt"
