"""
Voltra Retail inventory API.
Reads Postgres credentials from a file rendered by the Vault Agent Injector.
The sidecar renews the dynamic lease and rewrites the file; this process
polls that file and reconnects without a pod restart.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg
from fastapi import FastAPI, HTTPException

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("voltra")

CREDS_PATH = Path(os.environ.get("CREDS_PATH", "/vault/secrets/db-creds"))
DB_HOST = os.environ.get("DB_HOST", "postgres-postgresql.pg-database.svc.cluster.local")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "appdb")
POLL_SECONDS = float(os.environ.get("CREDS_POLL_SECONDS", "2"))


_lock = threading.Lock()
_conn: psycopg.Connection | None = None
_username = "unknown"
_password = ""
_last_reload: datetime | None = None
_seen_usernames: list[str] = []


def _parse_creds(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        return data["username"], data["password"]
    user = password = ""
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key in {"username", "user", "db_user", "DB_USER"}:
            user = value
        elif key in {"password", "db_password", "DB_PASS"}:
            password = value
    if not user or not password:
        raise ValueError("credential file missing username or password")
    return user, password


def _connect(username: str, password: str) -> psycopg.Connection:
    conn = psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=username,
        password=password,
        connect_timeout=8,
        autocommit=True,
    )
    return conn


def reload_if_changed(force: bool = False) -> bool:
    """Return True if a new credential set was applied."""
    global _conn, _username, _password, _last_reload

    if not CREDS_PATH.exists():
        return False

    username, password = _parse_creds(CREDS_PATH.read_text(encoding="utf-8"))

    with _lock:
        unchanged = username == _username and password == _password and _conn is not None
        if unchanged and not force:
            return False
        old = _username
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = _connect(username, password)
        _username = username
        _password = password
        _last_reload = datetime.now(timezone.utc)
        if username not in _seen_usernames:
            _seen_usernames.append(username)
        log.info("database session ready user=%s previous=%s", username, old)
        return True


def poll_credentials() -> None:
    while True:
        try:
            reload_if_changed()
        except Exception:
            log.exception("credential reload failed")
        time.sleep(POLL_SECONDS)


@contextmanager
def db() -> Iterator[psycopg.Connection]:
    with _lock:
        if _conn is None:
            raise HTTPException(status_code=503, detail="database credentials not loaded yet")
        conn = _conn
    try:
        yield conn
    except psycopg.OperationalError:
        log.warning("query failed; forcing credential reload")
        reload_if_changed(force=True)
        raise


app = FastAPI(title="Voltra Retail", version="1.0.0")


@app.on_event("startup")
def startup() -> None:
    deadline = time.time() + 90
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            if CREDS_PATH.exists():
                reload_if_changed(force=True)
                break
        except Exception as exc:
            last_error = exc
            log.warning("waiting for usable credentials: %s", exc)
        time.sleep(1)
    else:
        raise RuntimeError(f"credentials not available at {CREDS_PATH}: {last_error}")

    thread = threading.Thread(target=poll_credentials, name="vault-creds-poll", daemon=True)
    thread.start()


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "database": "connected", "username": _username}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/inventory")
def inventory() -> dict[str, Any]:
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT sku, name, stock, price_cents FROM products ORDER BY sku"
            ).fetchall()
        return {
            "store": "Voltra Retail",
            "queried_as": _username,
            "items": [
                {"sku": r[0], "name": r[1], "stock": r[2], "price_cents": r[3]} for r in rows
            ],
        }
    except Exception:
        raise


@app.get("/credential-status")
def credential_status() -> dict[str, Any]:
    """Safe for demos: username and rotation history, never the password."""
    return {
        "active_username": _username,
        "password_present": bool(_password),
        "last_reload_utc": _last_reload.isoformat() if _last_reload else None,
        "usernames_seen_this_process": _seen_usernames,
        "creds_path": str(CREDS_PATH),
        "creds_file_exists": CREDS_PATH.exists(),
        "ttl_hint": "Vault database role default_ttl is 120s; injector renews before expiry",
    }