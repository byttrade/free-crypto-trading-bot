"""Orders go out only through bytx as released and with its broker attribution intact.

The bytx license (BYTtrade Source-Available License, sections 2(e), 3(a) and 3(g)) does not permit modified
copies of bytx or removing, disabling or replacing its Broker Attribution, and the license of this project says the
same about these checks. When a check fails the executor sends no new orders; protective stops that are already
on the exchange stay there. Fix: `pip install --force-reinstall bytx` and remove broker overrides from the
environment.

What is checked (see `checks_for`):
- always: installed bytx .py files match the sha256 of the wheel RECORD, and the imported bytx is that copy;
- attribution source of every broker key of the exchange equals what a clean process gets from the installed
  bytx: 'builtin' where bytx ships a code, 'none' where it does not (never env / instance / process / off);
- where the exchange returns the tag on the order (`ORDER_TAG_FIELD`), the tag of a placed order is read back.
The list of exchanges with a built-in code is computed from the installed bytx, not stored here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path

RECHECK_SEC = 600
SELF = "free-crypto-trading-bot"   # the distribution that ships this package
ORDER_TAG_FIELD = {"okx": "tag", "blofin": "brokerId"}  # field of the raw order that carries the broker tag
_KEY_PREFIX = {"hyperliquid": "HL"}


class IntegrityError(PermissionError):
    pass


def changed_files(dist, suffixes=(".py",)) -> list[str]:
    """RECORD entries (.py with sha256) whose file on disk is missing or different."""
    out = []
    for f in dist.files or []:
        h = getattr(f, "hash", None)
        if h is None or h.mode != "sha256" or not str(f).endswith(suffixes):
            continue
        try:
            digest = hashlib.sha256(Path(f.locate()).read_bytes()).digest()
        except OSError:
            out.append(str(f))
            continue
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != h.value:
            out.append(str(f))
    return out


def check_self() -> None:
    """The same file check for this package (code and built-in strategy specs) when it runs from a released wheel.

    Skipped for a source checkout and an editable install: there is no RECORD with file hashes to compare with.
    """
    try:
        dist = metadata.distribution(SELF)
    except metadata.PackageNotFoundError:
        return
    own = [f for f in dist.files or [] if str(f).startswith("trend4h/") and getattr(f, "hash", None)]
    if not own or Path(dist.locate_file("trend4h")).resolve() != Path(__file__).resolve().parent:
        return
    bad = [f for f in changed_files(dist, (".py", ".json")) if f.startswith("trend4h/")]
    if bad:
        raise IntegrityError(f"the installed {SELF} differs from the released package ({', '.join(bad[:3])}"
                             f"{' ...' if len(bad) > 3 else ''}): no new orders are sent. Reinstall it: "
                             f"pip install --force-reinstall {SELF}. Your own strategy specs and configs go next "
                             f"to the package, not into it.")


def check_files(name="bytx", module=None) -> None:
    """IntegrityError unless `name` is a regular (non-editable) install with intact files, imported from there."""
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        raise IntegrityError(f"{name} is not installed: pip install '{SELF}[live]'") from None
    hashed = [f for f in dist.files or [] if str(f).endswith(".py") and getattr(f, "hash", None)]
    if not hashed:
        raise IntegrityError(f"{name} is an editable or unpacked install without file hashes; install the released "
                             f"package: pip install --force-reinstall {name}")
    bad = changed_files(dist)
    if bad:
        raise IntegrityError(f"the installed {name} differs from the released package ({', '.join(bad[:3])}"
                             f"{' ...' if len(bad) > 3 else ''}): no new orders are sent. Reinstall it: "
                             f"pip install --force-reinstall {name}. Editing {name} is not permitted by its license.")
    module = module or sys.modules.get(name)
    if module is not None:
        expected = Path(dist.locate_file(name)).resolve()
        actual = Path(module.__file__).resolve().parent
        if actual != expected:
            raise IntegrityError(f"{name} is imported from {actual}, not from the verified install {expected}: "
                                 f"no new orders are sent. Remove the override (PYTHONPATH / sys.path) and reinstall {name}.")
    if name == "bytx":
        check_self()


def exchange_keys(exchange: str, opts, headers=None) -> list[str]:
    """Names of the broker keys of an exchange, taken from the installed bytx."""
    prefix = _KEY_PREFIX.get(exchange, exchange.upper()) + "_"
    keys = {k for k in opts.KEYS if k.startswith(prefix)}
    keys |= set(getattr(headers, "_SOURCE_KEYS", {}).get(exchange, ()))
    return sorted(keys)


def clean_sources(opts) -> dict:
    """{key: source} as a fresh process without broker overrides sees it: 'builtin' or 'none'."""
    env = {k: v for k, v in os.environ.items() if k not in opts.KEYS and k != opts.MODE_ENV}
    code = "import json; from bytx.net._sig import opts; print(json.dumps(opts.sources(sorted(opts.KEYS))))"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise IntegrityError(f"cannot read the attribution registry of bytx: {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def check_attribution(exchange: str, instance, opts, expected: dict, headers=None) -> dict:
    """{key: source} on the instance that sends the orders; IntegrityError if any differs from `expected`."""
    actual = {k: opts.resolve_source(k, instance)[1] for k in exchange_keys(exchange, opts, headers)}
    wrong = {k: s for k, s in actual.items() if s != expected.get(k, "none")}
    mode = opts.broker_mode()
    if wrong or mode != "full":
        detail = ", ".join(f"{k}: {s}" for k, s in sorted(wrong.items())) or f"mode: {mode}"
        raise IntegrityError(f"broker attribution is disabled or overridden (source: {detail}): no new orders are "
                             f"sent. Remove the broker settings from the environment and configuration; this is not "
                             f"permitted by the bytx license, section 3(g).")
    return actual


def checks_for(exchange: str, expected: dict, opts, headers=None) -> dict:
    """What is verified for an exchange (for the start-up log and the README table). No code values."""
    builtin = [k for k in exchange_keys(exchange, opts, headers) if expected.get(k) == "builtin"]
    return {"exchange": exchange, "bytx_files": "RECORD sha256 + import path",
            "attribution": f"source must be builtin: {', '.join(builtin)}" if builtin
            else "no built-in code in this bytx: every source must be none, mode full",
            "order_tag": f"read back from order field '{ORDER_TAG_FIELD[exchange]}'"
            if builtin and exchange in ORDER_TAG_FIELD else "not readable from the order: configuration check only"}


class Guard:
    """Gate in front of every order batch. `problem()` returns None or the reason new orders are blocked."""

    def __init__(self, exchange: str, instance=None, recheck_sec=RECHECK_SEC, clock=time.monotonic,
                 opts=None, headers=None, expected=None, files=check_files):
        if opts is None:
            from bytx.net._sig import headers, opts  # noqa: F811
        self.exchange, self.instance, self.opts, self.headers = exchange, instance, opts, headers
        self.recheck_sec, self.clock, self.files = recheck_sec, clock, files
        self.expected = expected  # clean-process sources, computed once at start
        self._files_at, self._files_error, self.blocked = None, None, None

    def describe(self) -> dict:
        return checks_for(self.exchange, self._expected(), self.opts, self.headers)

    def _expected(self):
        if self.expected is None:
            self.expected = clean_sources(self.opts)
        return self.expected

    def problem(self, force=False) -> str | None:
        if self.blocked:
            return self.blocked
        now = self.clock()
        if force or self._files_at is None or now - self._files_at >= self.recheck_sec:
            self._files_at = now
            try:
                self.files()
                self._files_error = None
            except IntegrityError as e:
                self._files_error = str(e)
        if self._files_error:
            return self._files_error
        try:
            check_attribution(self.exchange, self.instance, self.opts, self._expected(), self.headers)
        except IntegrityError as e:
            return str(e)
        return None

    def check_order(self, order) -> None:
        """After a placement: where the exchange returns the tag, it must be the built-in one. Blocks entries if not."""
        field = ORDER_TAG_FIELD.get(self.exchange)
        keys = [k for k in exchange_keys(self.exchange, self.opts, self.headers) if self._expected().get(k) == "builtin"]
        info = (getattr(order, "raw", None) or {}).get("info")
        if not field or not keys or not isinstance(info, dict) or field not in info:
            return  # tag not readable here (or the placement response does not echo it): configuration check applies
        if str(info[field]) != self.opts.resolve(keys[0], self.instance):
            self.blocked = ("an order was placed without the broker tag: new entries are stopped. "
                            "Reinstall bytx (pip install --force-reinstall bytx) and restart.")
