"""User authentication stored independently from the reliability database."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

ROLES = ("viewer", "editor", "admin")


class AccessControl:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('viewer','editor','admin')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                username = os.environ.get("GREMLIN_ADMIN_USERNAME", "admin")
                pin = os.environ.get("GREMLIN_ADMIN_PIN", "1336")
                conn.execute(
                    "INSERT INTO users(username,password_hash,role) VALUES (?,?, 'admin')",
                    (username, generate_password_hash(pin)),
                )

    def authenticate(self, username: str, pin: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id,username,password_hash,role FROM users WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], pin):
            return {"id": row["id"], "username": row["username"], "role": row["role"]}
        return None

    def list_users(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT id,username,role,created_at,updated_at FROM users ORDER BY username")]

    def save_user(self, user_id: int | None, username: str, pin: str, role: str) -> None:
        username = username.strip()
        if not username or role not in ROLES or (user_id is None and not pin):
            raise ValueError("Username, a valid role, and a PIN for new users are required.")
        with self._connect() as conn:
            if user_id is None:
                conn.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,?)", (username, generate_password_hash(pin), role))
            elif pin:
                conn.execute("UPDATE users SET username=?,password_hash=?,role=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (username, generate_password_hash(pin), role, user_id))
            else:
                conn.execute("UPDATE users SET username=?,role=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (username, role, user_id))

    def delete_user(self, user_id: int, current_user_id: int) -> None:
        if user_id == current_user_id:
            raise ValueError("You cannot remove the account currently in use.")
        with self._connect() as conn:
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
