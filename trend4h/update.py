"""Daily auto-update of bytx inside the allowed version range, without disturbing trading.

Order: remember the current version -> install the new one into this environment -> verify it in a separate
process (files against the wheel RECORD, import, exchange object without network, attribution sources) ->
restart the executor. Any failure: the previous version is put back and verified, the reason is logged, the same
version is not tried again for a day. Protective stops on the exchange are never touched. Only bytx (and what its
installer pulls in) is updated; trend-4h itself is not.

Note: an auto-update installs code from PyPI into a process that holds exchange keys. Set "auto_update" to
"notify" or "off" in the config if you prefer to update by hand.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from importlib import metadata
from pathlib import Path

from .live import STEP, Lock

BYTX_MIN, BYTX_BELOW = (0, 2, 2), (0, 3)   # allowed range: >=0.2.2,<0.3 (same as in pyproject.toml)
CHECK_EVERY = 24 * 3600
AFTER_BAR_SEC = 300                        # the bar must have been processed at least this long ago
_VERIFY = ("import json; from trend4h import integrity; integrity.check_files('bytx'); import bytx; "
           "from bytx.net._sig import opts; bytx.create_exchange('bybit'); "
           "print(json.dumps({'version': bytx.__version__, 'sources': opts.sources(sorted(opts.KEYS))}))")


def parse(version: str):
    """'0.2.10' -> (0, 2, 10); None for pre-releases and anything unusual."""
    parts = version.split(".")
    return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else None


def pick(versions) -> tuple[str | None, str | None]:
    """(newest version inside the allowed range, newest version at or above its upper bound)."""
    parsed = sorted((v, s) for s in versions if (v := parse(s)))
    inside = [s for v, s in parsed if BYTX_MIN <= v and v[:len(BYTX_BELOW)] < BYTX_BELOW]
    beyond = [s for v, s in parsed if v[:len(BYTX_BELOW)] >= BYTX_BELOW]
    return (inside[-1] if inside else None), (beyond[-1] if beyond else None)


def pypi_versions() -> list[str]:
    with urllib.request.urlopen("https://pypi.org/pypi/bytx/json", timeout=20) as r:
        return list(json.load(r)["releases"])


def pip_install(version: str) -> None:
    """Install bytx==version into this environment; RuntimeError with the installer's last lines on failure."""
    if importlib.util.find_spec("pip"):
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", f"bytx=={version}"]
    elif shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", sys.executable, f"bytx=={version}"]
    else:
        raise RuntimeError("neither pip nor uv is available in this environment: update bytx by hand")
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if out.returncode:
        raise RuntimeError(f"installer failed: {(out.stderr or out.stdout).strip()[-300:]}")


def verify_install(version: str) -> dict:
    """Check the freshly installed bytx in a separate process; returns its attribution sources."""
    out = subprocess.run([sys.executable, "-c", _VERIFY], capture_output=True, text=True, timeout=180)
    if out.returncode:
        raise RuntimeError(f"verification failed: {out.stderr.strip()[-300:]}")
    info = json.loads(out.stdout.strip().splitlines()[-1])
    if info["version"] != version:
        raise RuntimeError(f"installed version is {info['version']}, expected {version}")
    return info["sources"]


class Updater:
    def __init__(self, mode="on", margin_min=60, state_dir="state", log=print, versions=pypi_versions,
                 install=pip_install, verify=verify_install, current=lambda: metadata.version("bytx"),
                 clock=time.time):
        self.mode, self.margin = mode, max(60, int(margin_min)) * 60
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "update.json"
        self.log, self.versions, self.install, self.verify, self.current, self.clock = log, versions, install, verify, current, clock
        self.st = json.loads(self.file.read_text()) if self.file.exists() else {"checked_at": 0, "available": None, "failed": {}}
        self.force = False  # "update now" from the panel: skips the daily timer, never the safe window

    def _save(self):
        self.file.write_text(json.dumps(self.st))

    def status(self) -> dict:
        return {"mode": self.mode, "current": self.current(), "available": self.st.get("available"),
                "incompatible": self.st.get("incompatible"), "checked_at": self.st.get("checked_at")}

    def check(self) -> str | None:
        """Ask the index once a day; returns the version to install, if any."""
        now = self.clock()
        if self.mode == "off":
            return None
        if self.force or now - self.st["checked_at"] >= CHECK_EVERY:
            try:
                inside, beyond = pick(self.versions())
            except Exception as e:
                self.log("update_check_failed", error=str(e)[:200])
                inside, beyond = self.st.get("available"), self.st.get("incompatible")
            cur = parse(self.current())
            newer = inside if inside and cur and parse(inside) > cur else None
            if beyond and beyond != self.st.get("incompatible"):
                self.log("update_incompatible", version=beyond,
                         note="a newer incompatible bytx is available: update trend-4h")
            if newer and newer != self.st.get("available"):
                self.log("update_available", current=self.current(), version=newer)
            self.st.update(checked_at=now, available=newer, incompatible=beyond)
            self._save()
        v = self.st.get("available")
        if not v or self.mode != "on" or now - self.st["failed"].get(v, 0) < CHECK_EVERY:
            return None
        return v

    def safe_window(self, executor) -> str | None:
        """None if it is safe to update now, otherwise why not."""
        now = self.clock()
        to_close = STEP / 1000 - now % (STEP / 1000)
        if executor is not None and executor.busy:
            return "an exchange request is in flight"
        if to_close < self.margin:
            return f"the next 4h bar closes in {int(to_close // 60)} min (need {self.margin // 60})"
        if executor is not None and now - executor.last_bar_done < AFTER_BAR_SEC:
            return "the last bar was processed less than 5 minutes ago"
        return None

    def run(self, executor=None) -> str | None:
        """One pass. Returns 'restart' after a successful update (the caller must restart before any new order)."""
        version = self.check()
        if not version:
            self.force = False
            return None
        wait = self.safe_window(executor)
        if wait:
            if self.force:
                self.log("update_postponed", version=version, reason=wait)
            return None
        self.force = False
        try:
            lock = Lock(self.dir / "update.lock")
        except RuntimeError:
            return None  # another process (live / panel) is updating
        try:
            old = self.current()
            self.log("update_start", current=old, version=version)
            try:
                before = self.verify(old)
                self.install(version)
                after = self.verify(version)
                lost = sorted(k for k, s in before.items() if s == "builtin" and after.get(k) != "builtin")
                if lost:
                    raise RuntimeError(f"attribution sources changed after the update: {', '.join(lost)}")
            except Exception as e:
                self.st["failed"][version] = self.clock()
                self._save()
                self.log("update_failed", version=version, error=str(e)[:300])
                try:
                    self.install(old)
                    self.verify(old)
                    self.log("update_rolled_back", version=old)
                except Exception as e2:  # the integrity gate of the executor decides whether trading continues
                    self.log("update_rollback_failed", version=old, error=str(e2)[:300])
                return None
            self.st["available"] = None
            self._save()
            self.log("update_done", version=version, note="restarting the executor")
            return "restart"
        finally:
            lock.release()
