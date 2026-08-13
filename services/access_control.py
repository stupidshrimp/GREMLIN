"""User authentication stored independently from the reliability database."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("viewer", "editor", "admin")
LOGIN_FAILURE_LIMIT = 5
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_LOCK_SECONDS = 15 * 60
DB_BUSY_TIMEOUT_SECONDS = 30


class AccessControl:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=DB_BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_SECONDS * 1000}")
        return conn

    def ensure_schema(self, *, initial_username: str | None = None, initial_pin: str | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('viewer','editor','admin')),
                    credential_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
            if "credential_version" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 1")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS login_attempts (
                    scope_key TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL,
                    window_started REAL NOT NULL,
                    locked_until REAL NOT NULL DEFAULT 0
                )
            """)
            if bool(initial_username) != bool(initial_pin):
                raise RuntimeError("GREMLIN_ADMIN_USERNAME and GREMLIN_ADMIN_PIN must be configured together.")
            if initial_username and initial_pin and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES (?,?, 'admin')",
                    (initial_username.strip(), generate_password_hash(initial_pin)),
                )

    def get_user(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id,username,role,credential_version FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def has_users(self) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("SELECT 1 FROM users LIMIT 1").fetchone())

    def authenticate(self, username: str, pin: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id,username,password_hash,role,credential_version FROM users WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], pin):
            return {key: row[key] for key in ("id", "username", "role", "credential_version")}
        return None

    def authenticate_limited(self, username: str, pin: str, client_key: str) -> tuple[dict | None, int]:
        """Authenticate with persistent per-account and per-client lockouts."""
        now = time.time()
        scopes = (f"account:{username.strip().casefold()}", f"client:{client_key}")
        with self._connect() as conn:
            # Serialize the complete read/check/increment sequence. A deferred
            # transaction would let parallel requests all read the same count
            # and then overwrite one another with the same next value, making
            # a batch of guesses count as one. BEGIN IMMEDIATE takes SQLite's
            # writer slot before the count is read, so every request observes
            # the preceding request's committed increment.
            conn.execute("BEGIN IMMEDIATE")
            attempts = {row["scope_key"]: row for row in conn.execute(
                "SELECT * FROM login_attempts WHERE scope_key IN (?,?)", scopes
            )}
            locked_until = max((float(row["locked_until"]) for row in attempts.values()), default=0)
            if locked_until > now:
                return None, max(1, int(locked_until - now + 0.999))
            row = conn.execute(
                "SELECT id,username,password_hash,role,credential_version FROM users WHERE username=?", (username,)
            ).fetchone()
            if row and check_password_hash(row["password_hash"], pin):
                conn.executemany("DELETE FROM login_attempts WHERE scope_key=?", ((scope,) for scope in scopes))
                return {key: row[key] for key in ("id", "username", "role", "credential_version")}, 0
            retry_after = 0
            for scope in scopes:
                previous = attempts.get(scope)
                in_window = previous and now - float(previous["window_started"]) < LOGIN_WINDOW_SECONDS
                count = int(previous["failure_count"]) if in_window else 0
                started = float(previous["window_started"]) if in_window else now
                count += 1
                lock_until = now + LOGIN_LOCK_SECONDS if count >= LOGIN_FAILURE_LIMIT else 0
                retry_after = max(retry_after, int(lock_until - now))
                conn.execute(
                    """INSERT INTO login_attempts(scope_key,failure_count,window_started,locked_until)
                       VALUES (?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
                       failure_count=excluded.failure_count, window_started=excluded.window_started,
                       locked_until=excluded.locked_until""",
                    (scope, count, started, lock_until),
                )
            return None, retry_after

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT id,username,role,created_at,updated_at FROM users ORDER BY username")]

    def save_user(self, user_id: int | None, username: str, pin: str, role: str) -> None:
        username = username.strip()
        if not username or role not in ROLES or (user_id is None and not pin):
            raise ValueError("Username, a valid role, and a PIN for new users are required.")
        with self._connect() as conn:
            # The last-admin count and the mutation are one serialized decision.
            # Without taking the writer slot first, two concurrent demotions can
            # both observe two admins and then leave the database with none.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
            if user_id and existing is None:
                raise ValueError("That user no longer exists.")
            if existing and existing["role"] == "admin" and role != "admin":
                if conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
                    raise ValueError("The last administrator cannot be demoted.")
            if user_id is None:
                conn.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)", (username, generate_password_hash(pin), role))
            elif pin:
                conn.execute("""UPDATE users SET username=?,password_hash=?,role=?,
                             credential_version=credential_version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                             (username, generate_password_hash(pin), role, user_id))
            else:
                conn.execute("UPDATE users SET username=?,role=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (username, role, user_id))

    def delete_user(self, user_id: int, current_user_id: int) -> None:
        if user_id == current_user_id:
            raise ValueError("You cannot remove the account currently in use.")
        with self._connect() as conn:
            # Serialize the invariant check with DELETE for the same reason as
            # save_user's admin-demotion path.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if row and row["role"] == "admin" and conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
                raise ValueError("The last administrator cannot be removed.")
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    def record_change(self, user: dict, action: str) -> None:
        """Stamp who performed a protected write and when it occurred."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(user_id,username,action) VALUES (?,?,?)",
                (user["id"], user["username"], action),
            )
