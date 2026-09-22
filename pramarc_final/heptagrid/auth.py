"""Password-based account authentication for PRAMARC.

Features:
- Gmail account sign-up and login
- PBKDF2-SHA256 password hashing with per-user random salts
- persistent local JSON user store for the hackathon prototype
- opaque server-side sessions in HttpOnly/SameSite cookies
- a pre-seeded local demo account for quick judging/practice

The demo account can be changed with environment variables:
  PRAMARC_DEMO_EMAIL=pramarc.demo@gmail.com
  PRAMARC_DEMO_PASSWORD=Pramarc@123

For a real deployment, replace this small local user store with a managed identity
provider/database and serve the site over HTTPS.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
SESSION_TTL_SECONDS = 8 * 60 * 60
PBKDF2_ITERATIONS = 210_000
MIN_PASSWORD_LENGTH = 8


@dataclass
class SessionRecord:
    email: str
    expires_at: float


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class AuthManager:
    def __init__(self, user_store_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionRecord] = {}
        self._secret = os.getenv("PRAMARC_AUTH_SECRET") or secrets.token_hex(32)
        self.user_store_path = Path(user_store_path or (Path(__file__).resolve().parent / "data" / "users.json"))
        self.user_store_path.parent.mkdir(parents=True, exist_ok=True)
        self.demo_email = self.normalize_email(os.getenv("PRAMARC_DEMO_EMAIL", "pramarc.demo@gmail.com"))
        self.demo_password = os.getenv("PRAMARC_DEMO_PASSWORD", "Pramarc@123")
        self._ensure_store_and_demo_account()

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def _validate_email(email: str) -> None:
        if not EMAIL_RE.fullmatch(email):
            raise AuthError("Enter a valid Gmail address.", 400)
        if not email.endswith("@gmail.com"):
            raise AuthError("Please use a Gmail address for this prototype.", 400)

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", 400)
        if len(password) > 256:
            raise AuthError("Password is too long.", 400)

    @staticmethod
    def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return salt.hex(), digest.hex()

    @staticmethod
    def _verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
        try:
            salt = bytes.fromhex(salt_hex)
            _, candidate = AuthManager._hash_password(password, salt)
            return hmac.compare_digest(candidate, digest_hex)
        except (ValueError, TypeError):
            return False

    def _session_digest(self, token: str) -> str:
        return hmac.new(self._secret.encode(), token.encode(), hashlib.sha256).hexdigest()

    def _read_users(self) -> dict:
        if not self.user_store_path.exists():
            return {"users": {}}
        try:
            raw = json.loads(self.user_store_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("users"), dict):
                return {"users": {}}
            return raw
        except (json.JSONDecodeError, OSError):
            return {"users": {}}

    def _write_users(self, data: dict) -> None:
        tmp = self.user_store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.user_store_path)

    def _ensure_store_and_demo_account(self) -> None:
        with self._lock:
            data = self._read_users()
            users = data.setdefault("users", {})
            if self.demo_email not in users:
                salt, digest = self._hash_password(self.demo_password)
                users[self.demo_email] = {
                    "salt": salt,
                    "password_hash": digest,
                    "created_at": int(time.time()),
                    "demo": True,
                }
                self._write_users(data)
            elif not self.user_store_path.exists():
                self._write_users(data)

    def _new_session(self, email: str) -> str:
        raw_token = secrets.token_urlsafe(32)
        self._sessions[self._session_digest(raw_token)] = SessionRecord(
            email=email,
            expires_at=time.time() + SESSION_TTL_SECONDS,
        )
        return raw_token

    def signup(self, email: str, password: str, confirm_password: str) -> tuple[str, dict]:
        email = self.normalize_email(email)
        self._validate_email(email)
        self._validate_password(password)
        if password != confirm_password:
            raise AuthError("Passwords do not match.", 400)

        with self._lock:
            data = self._read_users()
            users = data.setdefault("users", {})
            if email in users:
                raise AuthError("An account with this Gmail already exists. Please log in.", 409)
            salt, digest = self._hash_password(password)
            users[email] = {
                "salt": salt,
                "password_hash": digest,
                "created_at": int(time.time()),
                "demo": False,
            }
            self._write_users(data)
            token = self._new_session(email)

        return token, {
            "ok": True,
            "email": email,
            "message": "Account created successfully.",
            "expires_in": SESSION_TTL_SECONDS,
        }

    def login(self, email: str, password: str) -> tuple[str, dict]:
        email = self.normalize_email(email)
        self._validate_email(email)
        if not password:
            raise AuthError("Enter your password.", 400)

        with self._lock:
            data = self._read_users()
            record = data.get("users", {}).get(email)
            if not record:
                # Perform a dummy hash to make unknown-user and wrong-password paths less distinguishable.
                self._hash_password(password, b"\x00" * 16)
                raise AuthError("Incorrect Gmail or password.", 401)
            if not self._verify_password(password, str(record.get("salt", "")), str(record.get("password_hash", ""))):
                raise AuthError("Incorrect Gmail or password.", 401)
            token = self._new_session(email)

        return token, {
            "ok": True,
            "email": email,
            "message": "Login successful.",
            "expires_in": SESSION_TTL_SECONDS,
        }

    def get_session(self, raw_token: Optional[str]) -> Optional[SessionRecord]:
        if not raw_token:
            return None
        now = time.time()
        digest = self._session_digest(raw_token)
        with self._lock:
            record = self._sessions.get(digest)
            if not record:
                return None
            if now > record.expires_at:
                self._sessions.pop(digest, None)
                return None
            return record

    def logout(self, raw_token: Optional[str]) -> None:
        if not raw_token:
            return
        with self._lock:
            self._sessions.pop(self._session_digest(raw_token), None)

    def account_exists(self, email: str) -> bool:
        email = self.normalize_email(email)
        with self._lock:
            return email in self._read_users().get("users", {})
