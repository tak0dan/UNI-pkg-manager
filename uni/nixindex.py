"""Searchable index of nixpkgs, built from the channel's published metadata.

`nix search` evaluates all of nixpkgs, needs a running nix-daemon and takes
minutes on a cold cache, so a failure looks exactly like "no such package".
Instead (as nixorcist does) the index comes from
``channels.nixos.org/<channel>/packages.json.br``: no evaluation, no daemon,
and unfree packages such as steam are listed like any other.

The decompressed file is ~400 MB; parsing it with json.load needs several GB
of memory, so it is streamed one package at a time into a SQLite file under
``$XDG_CACHE_HOME/uni/nix/`` and rebuilt when older than the cache TTL.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

CHANNEL_URL = "https://channels.nixos.org/{channel}/packages.json.br"
# `nixpkgs#foo` resolves through the default flake registry to nixpkgs-unstable.
DEFAULT_CHANNEL = "nixpkgs-unstable"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
  attr TEXT PRIMARY KEY, pname TEXT, version TEXT, description TEXT, unfree INTEGER
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class NixIndexError(Exception):
    """The index could not be built (network, decoder, malformed data)."""


def _decompressed_chunks(blob: bytes, size: int = 1 << 20) -> Iterator[str]:
    try:
        import brotli  # type: ignore
    except ImportError:
        brotli = None
    if brotli is not None:
        dec = brotli.Decompressor()
        for i in range(0, len(blob), size):
            yield dec.process(blob[i:i + size]).decode("utf-8")
        return
    if not shutil.which("brotli"):
        raise NixIndexError("the channel metadata is brotli-compressed and no brotli decoder is installed "
                          "(install the 'brotli' package or the Python module)")
    proc = subprocess.Popen(["brotli", "-d", "-c"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def feed():  # write from a thread so a full stdout pipe cannot deadlock us
        try:
            proc.stdin.write(blob)
        finally:
            proc.stdin.close()

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    while chunk := proc.stdout.read(size):
        yield chunk.decode("utf-8", errors="replace")
    writer.join()
    if proc.wait():
        raise NixIndexError("brotli could not decompress the channel metadata")


def iter_packages(chunks: Iterator[str]) -> Iterator[tuple[str, dict]]:
    """Yield (attribute, info) from the ``"packages"`` object of packages.json, incrementally."""
    decoder = json.JSONDecoder()
    buf = ""
    chunks = iter(chunks)

    def more() -> bool:
        nonlocal buf
        nxt = next(chunks, None)
        if nxt is None:
            return False
        buf += nxt
        return True

    # Find the opening brace of the packages object.
    while (at := buf.find('"packages"')) < 0:
        if not more():
            raise NixIndexError("no 'packages' object in channel metadata")
    while (brace := buf.find("{", at)) < 0:
        if not more():
            raise NixIndexError("truncated channel metadata")
    pos = brace + 1
    while True:
        # Skip whitespace and commas between entries; stop at the closing brace.
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos < len(buf):
                break
            buf, pos = buf[pos:], 0
            if not more():
                raise NixIndexError("truncated channel metadata")
        if buf[pos] == "}":
            return
        try:
            key, end = decoder.raw_decode(buf, pos)
            start = buf.index(":", end) + 1
            while buf[start] in " \t\r\n":
                start += 1
            value, end = decoder.raw_decode(buf, start)
        except (ValueError, IndexError):  # entry not fully read yet
            buf, pos = buf[pos:], 0
            if not more():
                raise NixIndexError("truncated channel metadata")
            continue
        yield key, value
        pos = end
        if pos > (1 << 22):  # drop consumed text now and then
            buf, pos = buf[pos:], 0


def _row(attr: str, info: dict) -> tuple:
    meta = info.get("meta") or {}
    lic = meta.get("license")
    lics = lic if isinstance(lic, list) else [lic]
    unfree = any(isinstance(item, dict) and item.get("free") is False for item in lics)
    desc = meta.get("description") or ""
    return (attr, info.get("pname") or "", info.get("version") or "", str(desc), int(unfree))


class NixIndex:
    def __init__(self, cache: Path, channel: str = DEFAULT_CHANNEL, ttl_hours: float = 24, fetch=None):
        self.channel = channel
        self.path = Path(cache) / f"{channel}.sqlite"
        self.ttl = ttl_hours * 3600
        self.fetch = fetch or self._download
        self._db: sqlite3.Connection | None = None

    def _download(self) -> bytes:
        url = CHANNEL_URL.format(channel=self.channel)
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                return resp.read()
        except OSError as exc:
            raise NixIndexError(f"could not download {url}: {exc}") from exc

    def _fetched_at(self, db: sqlite3.Connection) -> float:
        try:
            row = db.execute("SELECT value FROM meta WHERE key='fetched'").fetchone()
        except sqlite3.Error:
            return 0.0
        return float(row[0]) if row else 0.0

    def build(self) -> int:
        blob = self.fetch()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".building")
        tmp.unlink(missing_ok=True)
        db = sqlite3.connect(tmp)
        try:
            db.executescript(_SCHEMA)
            batch, count = [], 0
            for attr, info in iter_packages(_decompressed_chunks(blob)):
                batch.append(_row(attr, info))
                if len(batch) >= 5000:
                    db.executemany("INSERT OR REPLACE INTO packages VALUES (?,?,?,?,?)", batch)
                    count, batch = count + len(batch), []
            db.executemany("INSERT OR REPLACE INTO packages VALUES (?,?,?,?,?)", batch)
            count += len(batch)
            if not count:
                raise NixIndexError("channel metadata listed no packages")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('fetched', ?)", (str(time.time()),))
            db.commit()
        except BaseException:
            db.close()
            tmp.unlink(missing_ok=True)
            raise
        db.close()
        tmp.replace(self.path)
        return count

    def db(self) -> sqlite3.Connection:
        """The index, built or refreshed as needed. A stale index is used if a refresh fails."""
        if self._db is not None:
            return self._db
        fresh = False
        if self.path.is_file():
            with sqlite3.connect(self.path) as probe:
                fresh = time.time() - self._fetched_at(probe) < self.ttl
        if not fresh:
            try:
                self.build()
            except NixIndexError:
                if not self.path.is_file():
                    raise
        self._db = sqlite3.connect(self.path)
        return self._db

    def get(self, attr: str) -> tuple | None:
        return self.db().execute("SELECT * FROM packages WHERE attr=?", (attr,)).fetchone()

    def search(self, term: str, limit: int = 100) -> list[tuple]:
        """Matches on attribute, pname or description; exact, then prefix, then substring."""
        t = term.lower()
        like = f"%{t}%"
        return self.db().execute(
            "SELECT * FROM packages WHERE lower(attr) LIKE ?1 OR lower(pname) LIKE ?1 "
            "OR lower(description) LIKE ?1 "
            "ORDER BY (lower(attr) = ?2 OR lower(pname) = ?2) DESC, "
            "lower(attr) LIKE ?3 DESC, lower(attr) LIKE ?1 DESC, length(attr) LIMIT ?4",
            (like, t, f"{t}%", limit),
        ).fetchall()
