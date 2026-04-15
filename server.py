import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"Generated admin password: {ADMIN_PASSWORD}")

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
ENV_FILE_PATH = Path(HERMES_HOME) / ".env"
PAIRING_DIR = Path(HERMES_HOME) / "pairing"
CODE_TTL_SECONDS = 3600

WEB_PORT = 9119
WEB_BASE_URL = f"http://127.0.0.1:{WEB_PORT}"

# Session cookie signing — stable across restarts so long as ADMIN_PASSWORD is set
_COOKIE_NAME = "hermes_session"
_SIGNER = TimestampSigner(
    hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest(),
    salt="hermes-railway",
)
_SESSION_MAX_AGE = 86400 * 7  # 7 days

PROVIDER_KEYS = [
    "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
    "GLM_API_KEY", "KIMI_API_KEY", "MINIMAX_API_KEY", "HF_TOKEN",
]
CHANNEL_KEYS = {
    "Telegram": "TELEGRAM_BOT_TOKEN",
    "Discord": "DISCORD_BOT_TOKEN",
    "Slack": "SLACK_BOT_TOKEN",
    "WhatsApp": "WHATSAPP_ENABLED",
    "Email": "EMAIL_ADDRESS",
    "Mattermost": "MATTERMOST_TOKEN",
    "Matrix": "MATRIX_ACCESS_TOKEN",
}

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# .env helpers (still needed for gateway auto-start and pairing)
# ---------------------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _check_basic(username: str, password: str) -> bool:
    return (
        hmac.compare_digest(username, ADMIN_USERNAME)
        and hmac.compare_digest(password, ADMIN_PASSWORD)
    )


def _make_session_cookie() -> str:
    return _SIGNER.sign(b"ok").decode()


def _validate_session_cookie(value: str) -> bool:
    try:
        _SIGNER.unsign(value, max_age=_SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _is_authenticated(request: Request) -> bool:
    # 1. Valid session cookie
    cookie = request.cookies.get(_COOKIE_NAME)
    if cookie and _validate_session_cookie(cookie):
        return True

    # 2. HTTP Basic auth
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("ascii")
            username, _, password = decoded.partition(":")
            if _check_basic(username, password):
                return True
        except Exception:
            pass

    return False


def _auth_response() -> Response:
    return PlainTextResponse(
        "Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="hermes"'},
    )


# ---------------------------------------------------------------------------
# Gateway manager
# ---------------------------------------------------------------------------

class GatewayManager:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.start_time: float | None = None
        self.restart_count = 0
        self._read_tasks: list[asyncio.Task] = []

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        self.state = "starting"
        try:
            env = os.environ.copy()
            env["HERMES_HOME"] = HERMES_HOME
            env_vars = read_env_file(ENV_FILE_PATH)
            env.update(env_vars)

            self.process = await asyncio.create_subprocess_exec(
                "hermes", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self.state = "running"
            self.start_time = time.time()
            task = asyncio.create_task(self._read_output())
            self._read_tasks.append(task)
        except Exception as e:
            self.state = "error"
            self.logs.append(f"Failed to start gateway: {e}")

    async def stop(self):
        if not self.process or self.process.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        self.state = "stopped"
        self.start_time = None

    async def restart(self):
        await self.stop()
        self.restart_count += 1
        await self.start()

    async def _read_output(self):
        try:
            while self.process and self.process.stdout:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                cleaned = ANSI_ESCAPE.sub("", decoded)
                self.logs.append(cleaned)
        except asyncio.CancelledError:
            return
        if self.process and self.process.returncode is not None and self.state == "running":
            self.state = "error"
            self.logs.append(f"Gateway exited with code {self.process.returncode}")

    def get_status(self) -> dict:
        pid = None
        if self.process and self.process.returncode is None:
            pid = self.process.pid
        uptime = None
        if self.start_time and self.state == "running":
            uptime = int(time.time() - self.start_time)
        return {
            "state": self.state,
            "pid": pid,
            "uptime": uptime,
            "restart_count": self.restart_count,
        }


# ---------------------------------------------------------------------------
# Web UI manager (hermes web)
# ---------------------------------------------------------------------------

class WebManager:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task | None = None

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        try:
            env = os.environ.copy()
            env["HERMES_HOME"] = HERMES_HOME

            self.process = await asyncio.create_subprocess_exec(
                "hermes", "dashboard",
                "--port", str(WEB_PORT),
                "--no-open",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self._read_task = asyncio.create_task(self._drain_output())
        except Exception as e:
            print(f"Failed to start hermes web: {e}")

    async def stop(self):
        if not self.process or self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()

    async def _drain_output(self):
        try:
            while self.process and self.process.stdout:
                line = await self.process.stdout.readline()
                if not line:
                    break
                print(f"[hermes web] {line.decode('utf-8', errors='replace').rstrip()}", flush=True)
        except asyncio.CancelledError:
            return
        if self.process and self.process.returncode is not None:
            print(f"[hermes web] exited with code {self.process.returncode}", flush=True)

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        """Poll WEB_PORT until hermes web is accepting TCP connections."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        print(f"[hermes web] waiting for port {WEB_PORT}…", flush=True)
        while loop.time() < deadline:
            if self.process and self.process.returncode is not None:
                print("[hermes web] process exited before becoming ready", flush=True)
                return False
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", WEB_PORT), timeout=1.0
                )
                writer.close()
                await writer.wait_closed()
                print(f"[hermes web] ready on port {WEB_PORT}", flush=True)
                return True
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(1)
        print(f"[hermes web] timed out waiting for port {WEB_PORT}", flush=True)
        return False


gateway = GatewayManager()
web_manager = WebManager()
_proxy_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# Reverse proxy helpers
# ---------------------------------------------------------------------------

# Headers that must not be forwarded hop-by-hop
_HOP_BY_HOP = frozenset([
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
])


async def _proxy_request(request: Request) -> Response:
    """Forward request to the upstream hermes web FastAPI server."""
    assert _proxy_client is not None

    url = f"{WEB_BASE_URL}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # Forward headers, stripping hop-by-hop and host
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "host"
    }

    body = await request.body()

    try:
        upstream = await _proxy_client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
            follow_redirects=False,
        )
    except httpx.ConnectError:
        return PlainTextResponse("Hermes web UI is starting, please wait…", status_code=503)
    except Exception as e:
        return PlainTextResponse(f"Proxy error: {e}", status_code=502)

    # Strip hop-by-hop from upstream response
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gateway.state})


async def proxy_handler(request: Request):
    """Auth gate + reverse proxy for everything going to hermes web."""
    if not _is_authenticated(request):
        return _auth_response()

    response = await _proxy_request(request)

    # Set session cookie on successful page loads so the browser doesn't need
    # to re-send Basic auth credentials on every subsequent request
    if response.status_code < 400:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("basic ") and not request.cookies.get(_COOKIE_NAME):
            response.set_cookie(
                _COOKIE_NAME,
                _make_session_cookie(),
                max_age=_SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
            )

    return response


# Gateway control (not in upstream hermes web)

async def api_gateway_start(request: Request):
    if not _is_authenticated(request):
        return _auth_response()
    asyncio.create_task(gateway.start())
    return JSONResponse({"ok": True})


async def api_gateway_stop(request: Request):
    if not _is_authenticated(request):
        return _auth_response()
    asyncio.create_task(gateway.stop())
    return JSONResponse({"ok": True})


async def api_gateway_restart(request: Request):
    if not _is_authenticated(request):
        return _auth_response()
    asyncio.create_task(gateway.restart())
    return JSONResponse({"ok": True})


# Pairing (not in upstream hermes web)

def _load_pairing_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_pairing_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _pairing_platforms(suffix: str) -> list[str]:
    if not PAIRING_DIR.exists():
        return []
    return [
        f.stem.rsplit(f"-{suffix}", 1)[0]
        for f in PAIRING_DIR.glob(f"*-{suffix}.json")
    ]


async def api_pairing_pending(request: Request):
    if not _is_authenticated(request):
        return _auth_response()
    now = time.time()
    results = []
    for platform in _pairing_platforms("pending"):
        pending = _load_pairing_json(PAIRING_DIR / f"{platform}-pending.json")
        for code, info in pending.items():
            age = now - info.get("created_at", now)
            if age > CODE_TTL_SECONDS:
                continue
            results.append({
                "platform": platform,
                "code": code,
                "user_id": info.get("user_id", ""),
                "user_name": info.get("user_name", ""),
                "age_minutes": int(age / 60),
            })
    return JSONResponse({"pending": results})


async def api_pairing_approve(request: Request):
    if not _is_authenticated(request):
        return _auth_response()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    platform = body.get("platform", "")
    code = body.get("code", "").upper().strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)

    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _load_pairing_json(pending_path)
    if code not in pending:
        return JSONResponse({"error": "Code not found or expired"}, status_code=404)

    entry = pending.pop(code)
    _save_pairing_json(pending_path, pending)

    approved_path = PAIRING_DIR / f"{platform}-approved.json"
    approved = _load_pairing_json(approved_path)
    approved[entry["user_id"]] = {
        "user_name": entry.get("user_name", ""),
        "approved_at": time.time(),
    }
    _save_pairing_json(approved_path, approved)

    return JSONResponse({"ok": True, "user_id": entry["user_id"], "user_name": entry.get("user_name", "")})


async def api_pairing_deny(request: Request):
    if not _is_authenticated(request):
        return _auth_response()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    platform = body.get("platform", "")
    code = body.get("code", "").upper().strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)

    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _load_pairing_json(pending_path)
    if code in pending:
        del pending[code]
        _save_pairing_json(pending_path, pending)

    return JSONResponse({"ok": True})


async def api_pairing_approved(request: Request):
    if not _is_authenticated(request):
        return _auth_response()
    results = []
    for platform in _pairing_platforms("approved"):
        approved = _load_pairing_json(PAIRING_DIR / f"{platform}-approved.json")
        for user_id, info in approved.items():
            results.append({
                "platform": platform,
                "user_id": user_id,
                "user_name": info.get("user_name", ""),
                "approved_at": info.get("approved_at", 0),
            })
    return JSONResponse({"approved": results})


async def api_pairing_revoke(request: Request):
    if not _is_authenticated(request):
        return _auth_response()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    platform = body.get("platform", "")
    user_id = body.get("user_id", "")
    if not platform or not user_id:
        return JSONResponse({"error": "platform and user_id required"}, status_code=400)

    approved_path = PAIRING_DIR / f"{platform}-approved.json"
    approved = _load_pairing_json(approved_path)
    if user_id in approved:
        del approved[user_id]
        _save_pairing_json(approved_path, approved)

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Auto-start
# ---------------------------------------------------------------------------

async def auto_start_gateway():
    env_vars = read_env_file(ENV_FILE_PATH)
    has_provider = any(env_vars.get(key) for key in PROVIDER_KEYS)
    if has_provider:
        asyncio.create_task(gateway.start())


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

routes = [
    Route("/health", health),
    # Gateway control
    Route("/api/gateway/start", api_gateway_start, methods=["POST"]),
    Route("/api/gateway/stop", api_gateway_stop, methods=["POST"]),
    Route("/api/gateway/restart", api_gateway_restart, methods=["POST"]),
    # Pairing
    Route("/api/pairing/pending", api_pairing_pending),
    Route("/api/pairing/approve", api_pairing_approve, methods=["POST"]),
    Route("/api/pairing/deny", api_pairing_deny, methods=["POST"]),
    Route("/api/pairing/approved", api_pairing_approved),
    Route("/api/pairing/revoke", api_pairing_revoke, methods=["POST"]),
    # Everything else → hermes web (React SPA + upstream API)
    Route("/{path:path}", proxy_handler, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
    Route("/", proxy_handler),
]


@asynccontextmanager
async def lifespan(app):
    global _proxy_client
    _proxy_client = httpx.AsyncClient(timeout=30.0)

    await web_manager.start()
    await web_manager.wait_ready(timeout=30.0)
    await auto_start_gateway()

    yield

    await gateway.stop()
    await web_manager.stop()
    await _proxy_client.aclose()


app = Starlette(
    routes=routes,
    middleware=[],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def handle_signal():
        loop.create_task(gateway.stop())
        loop.create_task(web_manager.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    loop.run_until_complete(server.serve())
