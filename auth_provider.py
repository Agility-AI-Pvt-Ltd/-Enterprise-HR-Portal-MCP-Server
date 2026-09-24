"""
Password-based authentication for the remote (HTTP) MCP server.

Claude Desktop / claude.ai custom connectors authenticate remote MCP servers
with OAuth 2.1. This module implements a minimal OAuth authorization server
whose only "login" is a single shared password (SERVER_PASSWORD):

  1. Client adds https://<host>/mcp  → gets 401 + OAuth metadata
  2. Client registers itself (dynamic client registration) and opens /authorize
  3. We redirect the browser to /login, a simple password form
  4. Correct password → auth code → client exchanges it for tokens

Tokens are stateless HMAC-signed blobs keyed on SERVER_PASSWORD, so they
survive server restarts (Render free tier sleeps) and are ALL invalidated
automatically when the password is changed.

Clients that can send headers (Cursor, scripts, MCP Inspector) may skip OAuth
and send the password directly:  Authorization: Bearer <SERVER_PASSWORD>
"""

import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from collections import defaultdict, deque

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

log = logging.getLogger("nddb-hr-portal.auth")

ACCESS_TTL = 24 * 3600  # 1 day
REFRESH_TTL = 30 * 24 * 3600  # 30 days
CODE_TTL = 300  # 5 minutes
LOGIN_TTL = 600  # 10 minutes to type the password
MAX_FAILS, FAIL_WINDOW = 5, 600  # 5 wrong passwords per IP per 10 min


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class PasswordOAuthProvider:
    """OAuthAuthorizationServerProvider backed by one shared password."""

    def __init__(self, password: str, issuer_url: str, client_store=None):
        if not password:
            raise ValueError("SERVER_PASSWORD must not be empty")
        self._password = password
        self._key = hashlib.sha256(b"nddb-hr-mcp-token:" + password.encode()).digest()
        self.issuer_url = issuer_url.rstrip("/")
        self._store = client_store  # optional persistent store (get/save)
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._fails: dict[str, deque] = defaultdict(deque)

    # ------------------------------------------------------------------ utils
    def check_password(self, supplied: str) -> bool:
        return hmac.compare_digest((supplied or "").encode(), self._password.encode())

    def _sign(self, payload: dict) -> str:
        body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64e(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def _verify(self, token: str, kind: str) -> dict | None:
        try:
            body, sig = token.split(".", 1)
            good = _b64e(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, good):
                return None
            data = json.loads(_b64d(body))
        except Exception:
            return None
        if data.get("typ") != kind or data.get("exp", 0) < time.time():
            return None
        return data

    def _issue(self, client_id: str, scopes: list[str], resource: str | None = None) -> OAuthToken:
        now = int(time.time())
        access = self._sign({"typ": "access", "cid": client_id, "scp": scopes, "res": resource,
                             "exp": now + ACCESS_TTL, "jti": secrets.token_hex(8)})
        refresh = self._sign({"typ": "refresh", "cid": client_id, "scp": scopes, "res": resource,
                              "exp": now + REFRESH_TTL, "jti": secrets.token_hex(8)})
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes) if scopes else None)

    def _gc(self):
        now = time.time()
        for k in [k for k, (exp, _, _) in self._pending.items() if exp < now]:
            self._pending.pop(k, None)
        for k in [k for k, c in self._codes.items() if c.expires_at < now]:
            self._codes.pop(k, None)

    # -------------------------------------------------------------- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self._clients.get(client_id)
        if client is None and self._store:
            raw = self._store.get(client_id)
            if raw:
                client = OAuthClientInformationFull.model_validate_json(raw)
                self._clients[client_id] = client
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        if self._store:
            self._store.save(client_info.client_id, client_info.model_dump_json())
        log.info("Registered OAuth client %s (%s)", client_info.client_id, client_info.client_name)

    # ------------------------------------------------------------ authorize
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._gc()
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = (time.time() + LOGIN_TTL, client.client_id, params)
        return f"{self.issuer_url}/login?txn={txn}"

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at >= time.time():
            return code
        return None

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    # --------------------------------------------------------------- tokens
    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        data = self._verify(refresh_token, "refresh")
        if not data or data["cid"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=data["cid"], scopes=data["scp"], expires_at=data["exp"])

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Direct password as bearer token (Cursor, scripts, MCP Inspector)
        if self.check_password(token):
            return AccessToken(token="password", client_id="password-client", scopes=["mcp"],
                               expires_at=int(time.time()) + 3600)
        data = self._verify(token, "access")
        if not data:
            return None
        return AccessToken(token=token, client_id=data["cid"], scopes=data["scp"],
                           expires_at=data["exp"], resource=data.get("res"))

    async def revoke_token(self, token) -> None:
        # Stateless tokens: change SERVER_PASSWORD to revoke everything.
        return None

    # ---------------------------------------------------------- login page
    def _rate_limited(self, ip: str) -> bool:
        q, now = self._fails[ip], time.time()
        while q and q[0] < now - FAIL_WINDOW:
            q.popleft()
        return len(q) >= MAX_FAILS

    async def login_page(self, request: Request) -> Response:
        self._gc()
        if request.method == "GET":
            txn = request.query_params.get("txn", "")
            return self._render(txn, None) if txn in self._pending else self._expired()

        form = await request.form()
        txn = str(form.get("txn", ""))
        pending = self._pending.get(txn)
        if not pending:
            return self._expired()
        ip = request.client.host if request.client else "unknown"
        if self._rate_limited(ip):
            return self._render(txn, "Too many wrong attempts. Try again in 10 minutes.", status=429)
        if not self.check_password(str(form.get("password", ""))):
            self._fails[ip].append(time.time())
            log.warning("Failed MCP login from %s", ip)
            return self._render(txn, "Incorrect password.", status=401)

        self._pending.pop(txn, None)
        _, client_id, params = pending
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code, client_id=client_id, scopes=params.scopes or ["mcp"],
            expires_at=time.time() + CODE_TTL, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, resource=params.resource,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
        )
        log.info("MCP login succeeded for client %s from %s", client_id, ip)
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                status_code=302)

    def _render(self, txn: str, error: str | None, status: int = 200) -> HTMLResponse:
        client_name = "an MCP client"
        if txn in self._pending:
            c = self._clients.get(self._pending[txn][1])
            if c and c.client_name:
                client_name = c.client_name
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        return HTMLResponse(_PAGE.format(body=f"""
            <h1>NDDB HR Portal</h1>
            <p class="sub"><b>{html.escape(client_name)}</b> wants to connect to the HR MCP server.</p>
            {err}
            <form method="post" action="/login">
              <input type="hidden" name="txn" value="{html.escape(txn)}">
              <label for="pw">Server password</label>
              <input id="pw" type="password" name="password" autofocus required autocomplete="current-password">
              <button type="submit">Connect</button>
            </form>"""), status_code=status)

    def _expired(self) -> HTMLResponse:
        return HTMLResponse(_PAGE.format(body="""<h1>Link expired</h1>
            <p class="sub">This login link is invalid or has expired. Start the connection again from your MCP client.</p>"""),
            status_code=400)


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · NDDB HR Portal MCP</title><style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#f4f5f7;font-family:system-ui,-apple-system,sans-serif;color:#1c1e21}}
main{{background:#fff;padding:32px;border-radius:12px;box-shadow:0 2px 16px rgba(0,0,0,.08);width:min(360px,calc(100vw - 32px));box-sizing:border-box}}
h1{{font-size:20px;margin:0 0 6px}} .sub{{color:#555;font-size:14px;margin:0 0 20px}} label{{font-size:13px;font-weight:600}}
input[type=password]{{width:100%;box-sizing:border-box;padding:10px 12px;margin:6px 0 16px;border:1px solid #ccd;border-radius:8px;font-size:15px}}
button{{width:100%;padding:11px;border:0;border-radius:8px;background:#1f5eff;color:#fff;font-size:15px;font-weight:600;cursor:pointer}}
.err{{background:#fdecea;color:#a3201b;padding:8px 12px;border-radius:8px;font-size:14px}}
</style></head><body><main>{body}</main></body></html>"""
