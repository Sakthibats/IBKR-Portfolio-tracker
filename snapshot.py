"""
Portfolio snapshots — keep the last good /api/portfolio pull around so the
numbers survive a gateway logout.

  * Local: every snapshot is stored in flex_trades.db (portfolio_snapshots).
  * Cloud: the snapshot is encrypted (AES-256-GCM, key from a PBKDF2-hashed
    passphrase) and PUT to the Cloudflare Pages Function, which keeps it in
    KV. Cloudflare only ever sees ciphertext; the browser decrypts it.

Cloud upload is enabled by three .env values:
    CF_SNAPSHOT_URL=https://<project>.pages.dev/api/snapshot
    CF_UPLOAD_TOKEN=<same value as the Pages secret UPLOAD_TOKEN>
    SNAPSHOT_PASSPHRASE=<what you type on the phone to unlock>
"""

import base64
import json
import os
import sqlite3

import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import flex

PBKDF2_ITERATIONS = 600_000
UPLOAD_TIMEOUT = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    payload  TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Local store
# ---------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(flex.DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def save_local(portfolio: dict) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (taken_at, payload) VALUES (?, ?)",
            (portfolio["lastUpdated"], json.dumps(portfolio)),
        )


def load_latest() -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT payload FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return json.loads(row[0]) if row else None


# ---------------------------------------------------------------------------
# Cloud publish
# ---------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def encrypt(portfolio: dict, passphrase: str) -> dict:
    """Encrypt the payload into the envelope the browser's WebCrypto can open."""
    salt = os.urandom(16)
    iv   = os.urandom(12)
    key  = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS,
    ).derive(passphrase.encode("utf-8"))
    # AESGCM appends the 16-byte tag to the ciphertext — the layout WebCrypto expects
    ciphertext = AESGCM(key).encrypt(iv, json.dumps(portfolio).encode("utf-8"), None)
    return {
        "v":          1,
        "kdf":        "PBKDF2-SHA256",
        "iterations": PBKDF2_ITERATIONS,
        "salt":       _b64(salt),
        "iv":         _b64(iv),
        "ciphertext": _b64(ciphertext),
        "takenAt":    portfolio["lastUpdated"],   # plaintext so the lock screen can show the age
    }


def publish(portfolio: dict) -> dict | None:
    """Upload the encrypted snapshot. Returns None when cloud sync isn't configured."""
    url        = os.getenv("CF_SNAPSHOT_URL")
    token      = os.getenv("CF_UPLOAD_TOKEN")
    passphrase = os.getenv("SNAPSHOT_PASSPHRASE")
    if not (url and token and passphrase):
        return None

    try:
        resp = requests.put(
            url,
            json=encrypt(portfolio, passphrase),
            headers={"Authorization": f"Bearer {token}"},
            timeout=UPLOAD_TIMEOUT,
        )
        resp.raise_for_status()
        return {"ok": True}
    except requests.RequestException as e:
        return {"ok": False, "error": str(e)}
