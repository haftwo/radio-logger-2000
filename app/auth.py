"""Password hashing and session helpers."""
import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request

from . import db


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310_000)
    return hmac.compare_digest(dk.hex(), digest)


def secret_key() -> str:
    path = os.path.join(db.CONFIG_DIR, "secret.key")
    if os.path.exists(path):
        return open(path).read().strip()
    key = secrets.token_urlsafe(48)
    os.makedirs(db.CONFIG_DIR, exist_ok=True)
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


def ensure_admin() -> None:
    """Create the first admin account from env vars when no users exist."""
    if db.q1("SELECT 1 FROM users"):
        return
    username = os.environ.get("RL_ADMIN_USER", "admin")
    password = os.environ.get("RL_ADMIN_PASSWORD", "changeme")
    db.ex("INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)",
          (username, hash_password(password), "admin", int(time.time())))
    print(f"[radio-logger] Created initial admin user '{username}'. Change the password in Admin > Users.")


def current_user(request: Request) -> dict | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.q1("SELECT id, username, role FROM users WHERE id=?", (uid,))


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def require_editor(request: Request) -> dict:
    """Signed-in users who may change song history: admins and viewers, not guests."""
    user = require_user(request)
    if user["role"] == "guest":
        raise HTTPException(403, "Guests have read-only access")
    return user


def guest_account() -> dict | None:
    """The built-in public guest account; it exists only while public guest access is switched on."""
    return db.q1("SELECT id, username, role FROM users WHERE auth='guest'")


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user
