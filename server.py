import asyncio
import base64
import json
import os
import re
import secrets
import signal
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"Generated admin password: {ADMIN_PASSWORD}")

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
ENV_FILE_PATH = Path(HERMES_HOME) / ".env"
PAIRING_DIR = Path(HERMES_HOME) / "pairing"
CODE_TTL_SECONDS = 3600
PID1_REAPER_INTERVAL_SECONDS = 5

# Registry of known Hermes env vars exposed in the UI.
# Each entry: (key, label, category, is_password)
ENV_VAR_DEFS = [
    # Model
    ("LLM_MODEL", "Model", "model", False),
    # Providers
    ("OPENROUTER_API_KEY", "OpenRouter API Key", "provider", True),
    ("DEEPSEEK_API_KEY", "DeepSeek API Key", "provider", True),
    ("DASHSCOPE_API_KEY", "DashScope API Key", "provider", True),
    ("GLM_API_KEY", "GLM / Z.AI API Key", "provider", True),
    ("KIMI_API_KEY", "Kimi API Key", "provider", True),
    ("MINIMAX_API_KEY", "MiniMax API Key", "provider", True),
    ("HF_TOKEN", "Hugging Face Token", "provider", True),
    # Tools
    ("PARALLEL_API_KEY", "Parallel API Key", "tool", True),
    ("FIRECRAWL_API_KEY", "Firecrawl API Key", "tool", True),
    ("TAVILY_API_KEY", "Tavily API Key", "tool", True),
    ("FAL_KEY", "FAL API Key", "tool", True),
    ("BROWSERBASE_API_KEY", "Browserbase API Key", "tool", True),
    ("BROWSERBASE_PROJECT_ID", "Browserbase Project ID", "tool", False),
    ("GITHUB_TOKEN", "GitHub Token", "tool", True),
    ("VOICE_TOOLS_OPENAI_KEY", "OpenAI Voice Key", "tool", True),
    ("HONCHO_API_KEY", "Honcho API Key", "tool", True),
    # Messaging — Telegram
    ("TELEGRAM_BOT_TOKEN", "Telegram Bot Token", "messaging", True),
    ("TELEGRAM_ALLOWED_USERS", "Telegram Allowed Users", "messaging", False),
    # Messaging — Discord
    ("DISCORD_BOT_TOKEN", "Discord Bot Token", "messaging", True),
    ("DISCORD_ALLOWED_USERS", "Discord Allowed Users", "messaging", False),
    # Messaging — Slack
    ("SLACK_BOT_TOKEN", "Slack Bot Token", "messaging", True),
    ("SLACK_APP_TOKEN", "Slack App Token", "messaging", True),
    # Messaging — WhatsApp
    ("WHATSAPP_ENABLED", "WhatsApp Enabled", "messaging", False),
    # Messaging — Email
    ("EMAIL_ADDRESS", "Email Address", "messaging", False),
    ("EMAIL_PASSWORD", "Email Password", "messaging", True),
    ("EMAIL_IMAP_HOST", "Email IMAP Host", "messaging", False),
    ("EMAIL_SMTP_HOST", "Email SMTP Host", "messaging", False),
    # Messaging — Mattermost
    ("MATTERMOST_URL", "Mattermost URL", "messaging", False),
    ("MATTERMOST_TOKEN", "Mattermost Token", "messaging", True),
    # Messaging — Matrix
    ("MATRIX_HOMESERVER", "Matrix Homeserver", "messaging", False),
    ("MATRIX_ACCESS_TOKEN", "Matrix Access Token", "messaging", True),
    ("MATRIX_USER_ID", "Matrix User ID", "messaging", False),
    # Messaging — General
    ("GATEWAY_ALLOW_ALL_USERS", "Allow All Users", "messaging", False),
]

PASSWORD_KEYS = {key for key, _, _, is_pw in ENV_VAR_DEFS if is_pw}

PROVIDER_KEYS = [key for key, _, cat, _ in ENV_VAR_DEFS if cat == "provider" and key != "LLM_MODEL"]
CHANNEL_KEYS = {
    "Telegram": "TELEGRAM_BOT_TOKEN",
    "Discord": "DISCORD_BOT_TOKEN",
    "Slack": "SLACK_BOT_TOKEN",
    "WhatsApp": "WHATSAPP_ENABLED",
    "Email": "EMAIL_ADDRESS",
    "Mattermost": "MATTERMOST_TOKEN",
    "Matrix": "MATRIX_ACCESS_TOKEN",
}


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


def write_env_file(path: Path, env_vars: dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True)

    categories = {"model": "Model", "provider": "Providers", "tool": "Tools", "messaging": "Messaging"}
    grouped: dict[str, list[str]] = {cat: [] for cat in categories}
    known_keys = {key for key, _, _, _ in ENV_VAR_DEFS}
    key_to_cat = {key: cat for key, _, cat, _ in ENV_VAR_DEFS}

    for key, value in env_vars.items():
        if not value:
            continue
        cat = key_to_cat.get(key, "other")
        line = f"{key}={value}"
        if cat in grouped:
            grouped[cat].append(line)
        else:
            grouped.setdefault("other", []).append(line)

    lines = []
    for cat, heading in categories.items():
        entries = grouped.get(cat, [])
        if entries:
            lines.append(f"# {heading}")
            lines.extend(sorted(entries))
            lines.append("")

    other = grouped.get("other", [])
    if other:
        lines.append("# Other")
        lines.extend(sorted(other))
        lines.append("")

    path.write_text("\n".join(lines) + "\n" if lines else "")


def mask_secrets(env_vars: dict[str, str]) -> dict[str, str]:
    result = {}
    for key, value in env_vars.items():
        if key in PASSWORD_KEYS and value:
            result[key] = value[:8] + "***" if len(value) > 8 else "***"
        else:
            result[key] = value
    return result


def merge_secrets(new_vars: dict[str, str], existing_vars: dict[str, str]) -> dict[str, str]:
    result = {}
    for key, value in new_vars.items():
        if key in PASSWORD_KEYS and value.endswith("***"):
            result[key] = existing_vars.get(key, "")
        else:
            result[key] = value
    return result


class BasicAuthBackend(AuthenticationBackend):
    async def authenticate(self, conn):
        if "Authorization" not in conn.headers:
            return None

        auth = conn.headers["Authorization"]
        try:
            scheme, credentials = auth.split()
            if scheme.lower() != "basic":
                return None
            decoded = base64.b64decode(credentials).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            raise AuthenticationError("Invalid credentials")

        username, _, password = decoded.partition(":")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            return AuthCredentials(["authenticated"]), SimpleUser(username)

        raise AuthenticationError("Invalid credentials")


def require_auth(request: Request):
    if not request.user.is_authenticated:
        return PlainTextResponse(
            "Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="hermes"'},
        )
    return None


def _read_proc_status(pid: int) -> dict[str, str]:
    status_path = Path("/proc") / str(pid) / "status"
    result: dict[str, str] = {}
    for line in status_path.read_text(errors="replace").splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        result[key] = value.strip()
    return result


def _tracked_child_pids() -> set[int]:
    tracked: set[int] = set()
    try:
        process = gateway.process
    except NameError:
        process = None
    if process and process.returncode is None and process.pid:
        tracked.add(process.pid)
    return tracked


def reap_zombie_children_once() -> int:
    """Reap orphaned zombie children when this process is container PID 1.

    The server intentionally starts the Hermes gateway via asyncio.subprocess,
    so avoid a blunt waitpid(-1) loop that could steal the gateway's exit
    status from asyncio. Instead, scan /proc for zombies directly parented by
    this process and skip tracked child PIDs.
    """
    if os.getpid() != 1:
        return 0

    tracked = _tracked_child_pids()
    reaped = 0
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)
        if pid in tracked:
            continue
        try:
            status = _read_proc_status(pid)
            if int(status.get("PPid", "0") or 0) != 1:
                continue
            if not status.get("State", "").startswith("Z"):
                continue
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
            if waited_pid:
                reaped += 1
        except (ChildProcessError, FileNotFoundError, ProcessLookupError, ValueError):
            continue
        except OSError as exc:
            print(f"[pid1-reaper] failed to reap pid {pid}: {exc}", flush=True)
    return reaped


async def pid1_reaper_loop():
    if os.getpid() != 1:
        return
    print("[pid1-reaper] enabled", flush=True)
    while True:
        reaped = reap_zombie_children_once()
        if reaped:
            print(f"[pid1-reaper] reaped {reaped} zombie child process(es)", flush=True)
        await asyncio.sleep(PID1_REAPER_INTERVAL_SECONDS)


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


gateway = GatewayManager()
config_lock = asyncio.Lock()


async def homepage(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    return templates.TemplateResponse(request, "index.html")


async def health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gateway.state})


async def api_config_get(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    async with config_lock:
        env_vars = read_env_file(ENV_FILE_PATH)
    defs = [
        {"key": key, "label": label, "category": cat, "password": is_pw}
        for key, label, cat, is_pw in ENV_VAR_DEFS
    ]
    return JSONResponse({"vars": mask_secrets(env_vars), "defs": defs})


async def api_config_put(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    try:
        restart = body.pop("_restartGateway", False)
        new_vars = body.get("vars", {})

        async with config_lock:
            existing = read_env_file(ENV_FILE_PATH)
            merged = merge_secrets(new_vars, existing)
            # Preserve any existing vars not in the UI
            for key, value in existing.items():
                if key not in merged:
                    merged[key] = value
            write_env_file(ENV_FILE_PATH, merged)

        if restart:
            asyncio.create_task(gateway.restart())

        return JSONResponse({"ok": True, "restarting": restart})
    except Exception as e:
        print(f"Config save error: {type(e).__name__}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err

    env_vars = read_env_file(ENV_FILE_PATH)

    providers = {}
    for key in PROVIDER_KEYS:
        label = key.replace("_API_KEY", "").replace("_TOKEN", "").replace("HF_", "HuggingFace ").replace("_", " ").title()
        providers[label] = {"configured": bool(env_vars.get(key))}

    channels = {}
    for name, key in CHANNEL_KEYS.items():
        val = env_vars.get(key, "")
        channels[name] = {"configured": bool(val) and val.lower() not in ("false", "0", "no")}

    return JSONResponse({
        "gateway": gateway.get_status(),
        "providers": providers,
        "channels": channels,
    })


async def api_logs(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    return JSONResponse({"lines": list(gateway.logs)})


async def api_gateway_start(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.start())
    return JSONResponse({"ok": True})


async def api_gateway_stop(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.stop())
    return JSONResponse({"ok": True})


async def api_gateway_restart(request: Request):
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
    asyncio.create_task(gateway.restart())
    return JSONResponse({"ok": True})


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
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
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
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
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
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
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
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
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
    auth_err = require_auth(request)
    if auth_err:
        return auth_err
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


async def auto_start_gateway():
    env_vars = read_env_file(ENV_FILE_PATH)
    has_provider = any(env_vars.get(key) for key in PROVIDER_KEYS)
    if has_provider:
        asyncio.create_task(gateway.start())


routes = [
    Route("/", homepage),
    Route("/health", health),
    Route("/api/config", api_config_get, methods=["GET"]),
    Route("/api/config", api_config_put, methods=["PUT"]),
    Route("/api/status", api_status),
    Route("/api/logs", api_logs),
    Route("/api/gateway/start", api_gateway_start, methods=["POST"]),
    Route("/api/gateway/stop", api_gateway_stop, methods=["POST"]),
    Route("/api/gateway/restart", api_gateway_restart, methods=["POST"]),
    Route("/api/pairing/pending", api_pairing_pending),
    Route("/api/pairing/approve", api_pairing_approve, methods=["POST"]),
    Route("/api/pairing/deny", api_pairing_deny, methods=["POST"]),
    Route("/api/pairing/approved", api_pairing_approved),
    Route("/api/pairing/revoke", api_pairing_revoke, methods=["POST"]),
]

@asynccontextmanager
async def lifespan(app):
    reaper_task = asyncio.create_task(pid1_reaper_loop())
    await auto_start_gateway()
    try:
        yield
    finally:
        reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await reaper_task
        await gateway.stop()
        reap_zombie_children_once()


app = Starlette(
    routes=routes,
    middleware=[Middleware(AuthenticationMiddleware, backend=BasicAuthBackend())],
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
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    loop.run_until_complete(server.serve())
