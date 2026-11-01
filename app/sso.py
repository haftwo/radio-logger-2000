"""Microsoft Entra ID (Azure AD) single sign-on via OpenID Connect.

Authorization-code flow with PKCE, as a confidential client. The ID token is obtained
directly from Entra's token endpoint over TLS, so per OpenID Connect Core 3.1.3.7 TLS
server validation stands in for the signature check; issuer, tenant, audience, expiry
and nonce are all still verified.
"""
import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import db

AUTHORITY = "https://login.microsoftonline.com"
SETTING_KEYS = ("sso_enabled", "sso_tenant_id", "sso_client_id", "sso_client_secret", "sso_admin_role",
                "sso_admin_group", "sso_viewer_access", "sso_viewer_role", "sso_password_login",
                "sso_public_url", "sso_button_label")
DEFAULTS = {
    "sso_enabled": "0",
    "sso_tenant_id": "",
    "sso_client_id": "",
    "sso_client_secret": "",
    "sso_admin_role": "Admin",
    "sso_admin_group": "",
    "sso_viewer_access": "tenant",      # tenant = anyone in the tenant | role = needs the viewer role/group
    "sso_viewer_role": "Viewer",
    "sso_password_login": "all",        # all | admins (local admin accounts only, as break-glass)
    "sso_public_url": "",
    "sso_button_label": "Sign in with Microsoft",
}
ENV = {"sso_tenant_id": "RL_SSO_TENANT_ID", "sso_client_id": "RL_SSO_CLIENT_ID",
       "sso_client_secret": "RL_SSO_CLIENT_SECRET", "sso_admin_group": "RL_SSO_ADMIN_GROUP",
       "sso_public_url": "RL_PUBLIC_URL"}

_discovery: dict[str, tuple[float, dict]] = {}


def seed_from_env() -> None:
    """Environment variables pre-fill SSO settings on first start (handy for enterprise deploys)."""
    current = db.get_settings()
    for key, env in ENV.items():
        if os.environ.get(env) and not current.get(key):
            db.set_setting(key, os.environ[env])
    if os.environ.get("RL_SSO_ENABLED") and "sso_enabled" not in current:
        db.set_setting("sso_enabled", "1" if os.environ["RL_SSO_ENABLED"].lower() in ("1", "true", "yes") else "0")


def config() -> dict:
    s = db.get_settings()
    return {k: s.get(k, DEFAULTS[k]) for k in SETTING_KEYS}


def enabled() -> bool:
    c = config()
    return c["sso_enabled"] == "1" and bool(c["sso_tenant_id"] and c["sso_client_id"] and c["sso_client_secret"])


def redirect_uri(request) -> str:
    base = config()["sso_public_url"].rstrip("/") or str(request.base_url).rstrip("/")
    return base + "/auth/callback"


async def discovery(tenant: str) -> dict:
    hit = _discovery.get(tenant)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{AUTHORITY}/{tenant}/v2.0/.well-known/openid-configuration")
        r.raise_for_status()
        data = r.json()
    _discovery[tenant] = (time.time(), data)
    return data


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


async def login_url(request) -> str:
    c = config()
    disc = await discovery(c["sso_tenant_id"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    request.session["sso"] = {"state": state, "nonce": nonce, "verifier": verifier, "t": time.time()}
    params = {
        "client_id": c["sso_client_id"], "response_type": "code", "response_mode": "query",
        "redirect_uri": redirect_uri(request), "scope": "openid profile email",
        "state": state, "nonce": nonce, "prompt": "select_account",
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    return disc["authorization_endpoint"] + "?" + urlencode(params)


class SSOError(Exception):
    pass


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        raise SSOError("Microsoft returned an unreadable sign-in token")


async def complete(request, code: str, state: str) -> dict:
    """Exchange the code, verify the ID token and return the claims."""
    pending = request.session.pop("sso", None)
    if not pending or not secrets.compare_digest(pending.get("state", ""), state or ""):
        raise SSOError("Sign-in session expired or didn't match. Please try again.")
    if time.time() - pending["t"] > 600:
        raise SSOError("Sign-in took too long. Please try again.")
    c = config()
    disc = await discovery(c["sso_tenant_id"])
    async with httpx.AsyncClient(timeout=15) as cl:
        r = await cl.post(disc["token_endpoint"], data={
            "client_id": c["sso_client_id"], "client_secret": c["sso_client_secret"],
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(request),
            "code_verifier": pending["verifier"], "scope": "openid profile email",
        })
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "id_token" not in data:
        raise SSOError(data.get("error_description", f"Token exchange failed ({r.status_code})").split("\r\n")[0])
    claims = _claims(data["id_token"])
    tid = claims.get("tid", "")
    expected_iss = disc["issuer"].replace("{tenantid}", tid)
    if claims.get("iss") != expected_iss:
        raise SSOError("Sign-in token came from an unexpected issuer")
    if "{tenantid}" not in disc["issuer"] and tid not in disc["issuer"]:
        raise SSOError("That account belongs to a different Microsoft tenant")
    if claims.get("aud") != c["sso_client_id"]:
        raise SSOError("Sign-in token was issued for a different application")
    if claims.get("exp", 0) < time.time() - 60:
        raise SSOError("Sign-in token has expired")
    if claims.get("nonce") != pending["nonce"]:
        raise SSOError("Sign-in token failed the replay check")
    return claims


def role_for(claims: dict) -> str | None:
    """'admin', 'viewer', or None (not allowed in)."""
    c = config()
    roles = [r.lower() for r in claims.get("roles", []) or []]
    groups = [g.lower() for g in claims.get("groups", []) or []]
    admin_role, admin_group = c["sso_admin_role"].strip().lower(), c["sso_admin_group"].strip().lower()
    if (admin_role and admin_role in roles) or (admin_group and admin_group in groups):
        return "admin"
    if c["sso_viewer_access"] == "tenant":
        return "viewer"
    viewer = c["sso_viewer_role"].strip().lower()
    if viewer and (viewer in roles or viewer in groups):
        return "viewer"
    return None


def upsert_user(claims: dict, role: str) -> int:
    oid = claims.get("oid") or claims.get("sub")
    username = (claims.get("preferred_username") or claims.get("email") or oid).strip()
    name = claims.get("name") or username
    email = claims.get("email") or (username if "@" in username else None)
    now = int(time.time())
    u = db.q1("SELECT id FROM users WHERE sso_sub=?", (oid,))
    if u:
        db.ex("UPDATE users SET username=?, display_name=?, email=?, role=?, last_login=? WHERE id=?",
              (username, name, email, role, now, u["id"]))
        return u["id"]
    clash = db.q1("SELECT id, auth FROM users WHERE lower(username)=lower(?)", (username,))
    if clash:  # a local account with the same name: keep both, suffix the SSO one
        username = f"{username} (SSO)"
    return db.ex("INSERT INTO users(username, password_hash, role, created_at, auth, sso_sub, display_name, "
                 "email, last_login) VALUES (?,?,?,?,?,?,?,?,?)",
                 (username, "", role, now, "sso", oid, name, email, now))
