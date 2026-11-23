"""Encrypted SQLite (SQLCipher 4, AES-256) for every database in data/: jobhunter.db, vectors.db, the queues and
backups. The key is a random 256-bit value kept in the OS keychain as DB_KEY (see secrets.py).

- `connect(path)` opens a database with the key. A plain (unencrypted) database found at that path is encrypted
  in place first, after checking that every table has the same row count.
- Running from source without the sqlcipher3 package (or with JOBHUNTER_PLAINTEXT_DB=1, used by the tests)
  falls back to the standard sqlite3 module. The packaged app refuses to start without encryption.
- Lose the key and the data is gone: the Configuration page shows a recovery key to put in a password manager.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import sqlcipher3.dbapi2 as sqlite  # type: ignore
    HAVE_CIPHER = True
except ImportError:  # pragma: no cover - depends on install
    import sqlite3 as sqlite  # type: ignore
    HAVE_CIPHER = False

PLAIN_HEADER = b"SQLite format 3\x00"
Row = sqlite.Row
IntegrityError = sqlite.IntegrityError
OperationalError = sqlite.OperationalError
DatabaseError = sqlite.DatabaseError
Connection = sqlite.Connection


def enabled() -> bool:
    return HAVE_CIPHER and os.environ.get("JOBHUNTER_PLAINTEXT_DB") != "1"


def require() -> None:
    """The packaged app calls this at start: never silently run unencrypted."""
    if not HAVE_CIPHER:
        raise RuntimeError("Database encryption (sqlcipher3) is missing from this build.")


def db_key(create: bool = True) -> str:
    from . import secrets
    k = secrets.get("DB_KEY")
    if not k and create:
        k = os.urandom(32).hex()
        secrets.set("DB_KEY", k)
        if secrets.get("DB_KEY") != k:
            raise RuntimeError("Could not save the database key in the OS keychain.")
        log.info("created a new database key in the OS keychain")
    if not k:
        raise RuntimeError("No database key in the OS keychain (DB_KEY). Restore it from your recovery key.")
    return k


def _keyed(conn, key: str) -> None:
    conn.execute(f"PRAGMA key = \"x'{key}'\"")


def is_plain(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == PLAIN_HEADER
    except (FileNotFoundError, IsADirectoryError):
        return False


def _counts(conn) -> dict[str, int]:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}


def encrypt_file(path: Path, key: str | None = None) -> dict:
    """Turn a plain SQLite file into an encrypted one in place. The plain file is removed only after the
    encrypted copy opens with the key and every table has the same row count."""
    path = Path(path)
    key = key or db_key()
    tmp = path.with_name(path.name + ".enc-tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite.connect(str(path), timeout=30)
    src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = _counts(src)
    uv = src.execute("PRAGMA user_version").fetchone()[0]
    src.execute(f"ATTACH DATABASE ? AS enc KEY \"x'{key}'\"", (str(tmp),))
    src.execute("SELECT sqlcipher_export('enc')")
    src.execute(f"PRAGMA enc.user_version = {int(uv)}")
    src.execute("DETACH DATABASE enc")
    src.close()
    chk = sqlite.connect(str(tmp))
    _keyed(chk, key)
    after = _counts(chk)
    chk.close()
    if before != after:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"encryption check failed for {path.name}: {before} != {after}")
    for side in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        side.unlink(missing_ok=True)
    os.replace(tmp, path)
    log.info("encrypted %s (%d tables)", path.name, len(after))
    return {"path": str(path), "tables": len(after), "rows": sum(after.values())}


def connect(path: str | Path, **kw):
    """sqlite3.connect() with the keychain key. Same arguments as sqlite3.connect."""
    path = str(path)
    if not enabled() or path == ":memory:":
        return sqlite.connect(path, **kw)
    key = db_key()
    if is_plain(Path(path)):
        encrypt_file(Path(path), key)
    conn = sqlite.connect(path, **kw)
    _keyed(conn, key)
    try:
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except DatabaseError as e:
        conn.close()
        raise RuntimeError(f"{Path(path).name} cannot be opened with the key in the OS keychain "
                           f"(wrong or missing DB_KEY). Restore the recovery key on the Configuration page.") from e
    return conn


def copy(src_conn, dst: Path) -> Path:
    """Consistent copy of an open database to `dst` (encrypted with the same key when encryption is on)."""
    dst = Path(dst)
    dst.unlink(missing_ok=True)
    if enabled():
        src_conn.execute(f"ATTACH DATABASE ? AS bak KEY \"x'{db_key()}'\"", (str(dst),))
        src_conn.execute("SELECT sqlcipher_export('bak')")
        uv = src_conn.execute("PRAGMA main.user_version").fetchone()[0]
        src_conn.execute(f"PRAGMA bak.user_version = {int(uv)}")
        src_conn.execute("DETACH DATABASE bak")
    else:
        out = sqlite.connect(str(dst))
        with out:
            src_conn.backup(out)
        out.close()
    return dst


def status(data_dir: Path) -> list[dict]:
    """Which .db files in data/ are encrypted (for the Configuration page)."""
    out = []
    for p in sorted(Path(data_dir).glob("*.db")):
        out.append({"name": p.name, "encrypted": not is_plain(p) and p.stat().st_size > 0, "bytes": p.stat().st_size})
    return out
