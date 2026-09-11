"""WorkBuddy app-server provider - run the LLM with no API key.

Resume-Matcher normally reaches a model through LiteLLM, which needs a
third-party API key. This module adds a provider that needs none: it talks to
the **WorkBuddy / CodeBuddy app-server** that ships inside the local WorkBuddy
install (``codebuddy --serve``) and spends the signed-in account's model quota.

Transport is ACP (Agent Client Protocol): JSON-RPC 2.0 requests POSTed to the
gateway's ``/api/v1/acp`` endpoint and answered as an SSE stream. A gateway
process is pinned to a single model, so switching models restarts it. The
process is started lazily on first use and reaped after an idle period.

Everything here is self-contained: no external bridge process, no API key, no
``api_base`` for the user to configure. Selecting the ``workbuddy`` provider is
the whole setup.

Architecture reference (read-only, NOT modified):
    D:/CyberBoss/src/adapters/runtime/codebuddy/
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Gateway routes (app-server HTTP surface)
# --------------------------------------------------------------------------- #

ROUTE_HEALTH = "/api/v1/health"
ROUTE_CONNECT = "/api/v1/acp/connect"
ROUTE_ACP = "/api/v1/acp"

# Models advertised by `codebuddy --model`. Used for the Settings dropdown and
# for capability lookups; NOT used to reject a model the user typed by hand,
# because a newer CLI may support models this build has never heard of.
MODELS: tuple[str, ...] = (
    "auto",
    "hy4-preview",
    "hy4-preview-x",
    "hy3",
    "hy3-x",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "kimi-k3-1",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)

# "auto" lets the gateway pick whatever the account can currently use, which is
# the safest default when nothing has been configured yet.
DEFAULT_MODEL = "auto"

# WorkBuddy models are modern, long-context chat models; the CLI does its own
# token budgeting, so this module never sends max_tokens over ACP. The value is
# only used to stop callers from clamping to LiteLLM's unknown-model fallback.
WORKBUDDY_MAX_OUTPUT_TOKENS = 32768


class WorkBuddyError(RuntimeError):
    """Failure with an actionable code the Settings UI can render.

    ``code`` mirrors the ones CyberBoss surfaces (CODEBUDDY_BINARY_NOT_FOUND,
    CODEBUDDY_LOGIN_REQUIRED, ...) so a user hitting a problem gets the same
    vocabulary in both apps.
    """

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


# --------------------------------------------------------------------------- #
# CLI discovery
# --------------------------------------------------------------------------- #

_PROBE_TIMEOUT_SECONDS = 10
_REGISTRY_UNINSTALL_KEYS = (
    r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKLM\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)

# Discovery shells out (reg.exe, --version, --help) so it is resolved once per
# process and cached.
_DISTRIBUTION: dict[str, str] | None = None
_DISTRIBUTION_ERROR: WorkBuddyError | None = None


def _cli_looks_valid(cli_path: str) -> bool:
    return bool(cli_path) and Path(cli_path).is_file()


def _node_looks_valid(node_path: str) -> bool:
    return bool(node_path) and Path(node_path).is_file()


def _windows_creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run_probe(command: str, args: list[str]) -> str:
    """Run a discovery probe and return combined stdout+stderr."""
    completed = subprocess.run(
        [command, *args],
        shell=False,
        capture_output=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
        creationflags=_windows_creation_flags(),
    )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    return f"{stdout}\n{stderr}"


def _managed_node_candidates() -> list[str]:
    """Node builds shipped with WorkBuddy, newest first."""
    candidates: list[str] = []
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    versions_root = Path(home) / ".workbuddy" / "binaries" / "node" / "versions"
    try:
        entries = sorted(
            (entry.name for entry in versions_root.iterdir() if entry.is_dir()),
            key=lambda name: [int(part) if part.isdigit() else 0 for part in re.split(r"[.\-]", name)],
            reverse=True,
        )
    except OSError:
        entries = []
    candidates.extend(str(versions_root / name / "node.exe") for name in entries)
    return candidates


def _cli_paths_for(install_location: str) -> list[str]:
    """CLI entrypoints inside a WorkBuddy install directory, best guess first."""
    base = Path(install_location)
    return [
        str(base / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"),
        str(base / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy.js"),
        str(base / "resources" / "codebuddy" / "cli.js"),
    ]


def _node_paths_for(install_location: str) -> list[str]:
    """Bundled Node runtimes inside a WorkBuddy install directory."""
    base = Path(install_location)
    return [
        str(base / "resources" / "app.asar.unpacked" / "cli" / "node.exe"),
        str(base / "resources" / "codebuddy" / "node.exe"),
    ]


def _default_install_location() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return ""
    return str(Path(local_app_data) / "Programs" / "WorkBuddy")


def _registry_install_locations() -> list[str]:
    """Install dirs recorded by the WorkBuddy uninstaller.

    Costs one ``reg.exe`` subprocess per hive, so callers try the default
    install location first and only fall back to this.
    """
    locations: list[str] = []
    for key in _REGISTRY_UNINSTALL_KEYS:
        try:
            output = _run_probe("reg.exe", ["query", key, "/s"])
        except Exception:  # noqa: BLE001 - a missing/denied hive only removes a fallback
            continue
        for block in re.split(r"\r?\n(?=HKEY_)", output, flags=re.IGNORECASE):
            if not re.search(
                r"^\s*DisplayName\s+REG_\w+\s+WorkBuddy\s*$", block, re.IGNORECASE | re.MULTILINE
            ):
                continue
            match = re.search(
                r"^\s*InstallLocation\s+REG_\w+\s+(.+?)\s*$", block, re.IGNORECASE | re.MULTILINE
            )
            if match:
                locations.append(match.group(1).strip())
    return locations


def _install_locations() -> list[str]:
    """WorkBuddy install dirs, standard location first.

    The default ``%LOCALAPPDATA%\\Programs\\WorkBuddy`` path covers the
    overwhelming majority of installs, so the registry scan only runs when it
    comes up empty — an install-time cost, not a per-launch one.
    """
    locations: list[str] = []
    default_location = _default_install_location()
    if default_location:
        locations.append(default_location)
    if not any(_cli_looks_valid(path) for loc in locations for path in _cli_paths_for(loc)):
        locations.extend(_registry_install_locations())
    return _dedupe_paths(locations)


def _dedupe_paths(candidates: list[str]) -> list[str]:
    """Case- and separator-insensitive de-duplication that keeps input order."""
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        key = candidate.replace("/", "\\").lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _cli_candidates() -> list[str]:
    candidates: list[str] = []
    if settings.workbuddy_cli_path:
        candidates.append(settings.workbuddy_cli_path)

    for location in _install_locations():
        candidates.extend(_cli_paths_for(location))

    found = shutil.which("codebuddy")
    if found:
        candidates.append(found)

    return _dedupe_paths(candidates)


def _node_candidates() -> list[str]:
    candidates: list[str] = []
    if settings.workbuddy_node_path:
        candidates.append(settings.workbuddy_node_path)
    candidates.extend(_managed_node_candidates())

    for location in _install_locations():
        candidates.extend(_node_paths_for(location))

    found = shutil.which("node") or shutil.which("node.exe")
    if found:
        candidates.append(found)

    return _dedupe_paths(candidates)


def resolve_distribution(force: bool = False) -> dict[str, str]:
    """Locate a usable WorkBuddy CLI + Node runtime.

    A candidate pair is only accepted once ``--version`` reports a version and
    ``--help`` advertises ``--serve`` - the same contract CyberBoss's locator
    enforces, so we never spawn a binary that cannot host the app-server.
    """
    global _DISTRIBUTION, _DISTRIBUTION_ERROR

    if _DISTRIBUTION is not None and not force:
        return _DISTRIBUTION
    if _DISTRIBUTION_ERROR is not None and not force:
        raise _DISTRIBUTION_ERROR

    nodes = [path for path in _node_candidates() if _node_looks_valid(path)]
    if not nodes:
        error = WorkBuddyError(
            "CODEBUDDY_RUNTIME_NOT_FOUND",
            "No Node.js runtime was found to launch the WorkBuddy app-server.",
            "Install WorkBuddy, or set WORKBUDDY_NODE_PATH to a Node 18+ executable.",
        )
        _DISTRIBUTION_ERROR = error
        raise error

    clis = [path for path in _cli_candidates() if _cli_looks_valid(path)]
    if not clis:
        error = WorkBuddyError(
            "CODEBUDDY_BINARY_NOT_FOUND",
            "The WorkBuddy CLI was not found on this machine.",
            "Install WorkBuddy and sign in, or set WORKBUDDY_CLI_PATH to the bundled "
            "codebuddy entrypoint.",
        )
        _DISTRIBUTION_ERROR = error
        raise error

    last_problem = ""
    for cli in clis:
        for node in nodes:
            try:
                version_output = _run_probe(node, [cli, "--version"])
            except Exception as exc:  # noqa: BLE001
                last_problem = f"version probe failed: {exc!r}"
                continue
            version_match = re.search(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b", version_output)
            if not version_match:
                last_problem = "version unreadable"
                continue
            try:
                help_output = _run_probe(node, [cli, "--help"])
            except Exception as exc:  # noqa: BLE001
                last_problem = f"help probe failed: {exc!r}"
                continue
            if not re.search(r"(?:^|\s)--serve(?:\s|$|[=,])", help_output, re.MULTILINE):
                last_problem = "candidate does not advertise --serve"
                continue

            _DISTRIBUTION = {
                "cli": cli,
                "node": node,
                "version": version_match.group(1),
            }
            logger.info(
                "WorkBuddy app-server runtime located (CLI %s, node %s, version %s)",
                cli,
                node,
                _DISTRIBUTION["version"],
            )
            return _DISTRIBUTION

    error = WorkBuddyError(
        "CODEBUDDY_API_INCOMPATIBLE",
        "The installed WorkBuddy CLI cannot host the app-server.",
        last_problem or "Update WorkBuddy and try again.",
    )
    _DISTRIBUTION_ERROR = error
    raise error


def distribution_status() -> dict[str, Any]:
    """Non-throwing view of discovery, for the Settings status panel."""
    try:
        found = resolve_distribution()
    except WorkBuddyError as exc:
        return {"available": False, "error_code": exc.code, "message": exc.message, "hint": exc.hint}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error_code": "discovery_failed", "message": str(exc), "hint": ""}
    return {
        "available": True,
        "cli_path": found["cli"],
        "node_path": found["node"],
        "cli_version": found["version"],
    }


# --------------------------------------------------------------------------- #
# SSE parsing
# --------------------------------------------------------------------------- #


class _SSEParser:
    """Incremental Server-Sent Events parser for ACP responses."""

    def __init__(self, on_message: Callable[[dict], None]) -> None:
        self._buffer = ""
        self._on_message = on_message

    def push(self, text: str) -> None:
        self._buffer += text
        while True:
            marker = self._buffer.find("\n\n")
            if marker == -1:
                break
            block, self._buffer = self._buffer[:marker], self._buffer[marker + 2 :]
            self._handle(block.replace("\r\n", "\n"))

    def finish(self) -> None:
        if self._buffer.strip():
            block, self._buffer = self._buffer, ""
            self._handle(block.replace("\r\n", "\n"))

    def _handle(self, block: str) -> None:
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                self._on_message(json.loads(payload))
            except json.JSONDecodeError:
                continue


def _message_text(message: dict) -> str:
    """Pull assistant text out of an ACP ``session/update`` notification."""
    if message.get("method") != "session/update":
        return ""
    update = (message.get("params") or {}).get("update") or {}
    if update.get("sessionUpdate") != "agent_message_chunk":
        return ""
    content = update.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    if isinstance(update.get("text"), str):
        return update["text"]
    return ""


# --------------------------------------------------------------------------- #
# Prompt translation
# --------------------------------------------------------------------------- #


def build_prompt(messages: Iterable[dict], response_format: Any = None) -> str:
    """Flatten an OpenAI-style messages array into a single ACP prompt.

    The app-server takes one prompt string, so roles become explicit tags. The
    JSON-mode instruction is appended last so it outranks anything earlier in
    the transcript (models weight trailing instructions most heavily).
    """
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").lower()
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        content = "" if content is None else str(content)
        if not content.strip():
            continue
        if role == "system":
            lines.append(f"<system>\n{content}\n</system>")
        elif role == "assistant":
            lines.append(f"<assistant>\n{content}\n</assistant>")
        else:
            lines.append(f"<user>\n{content}\n</user>")

    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        lines.append(
            "<system>\nRespond with a single raw JSON object only. No markdown code "
            "fences, no commentary, no text before or after the JSON.\n</system>"
        )
    return "\n\n".join(lines)


def normalize_model(model: str | None) -> str:
    """Strip any LiteLLM provider prefix and fall back to the default model."""
    name = (model or "").strip()
    for prefix in ("openai/", "openai_compatible/", "workbuddy/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name or DEFAULT_MODEL


def is_workbuddy_model(model_name: str) -> bool:
    """True when a bare LiteLLM model name belongs to this provider."""
    return normalize_model(model_name) in MODELS


# --------------------------------------------------------------------------- #
# App-server process + ACP connection
# --------------------------------------------------------------------------- #


class WorkBuddyAppServer:
    """Owns one ``codebuddy --serve`` gateway and multiplexes ACP calls onto it.

    The gateway is bound to a single model at start-up, so the manager keeps at
    most one process alive and restarts it when the requested model changes.
    Calls are serialized: one ACP connection is a single conversation channel
    and interleaving prompts on it would mix their streams.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._endpoint = ""
        self._connection_id = ""
        self._model = ""
        self._client: httpx.AsyncClient | None = None
        self._password = ""

        self._prompt_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()

        self._last_used = time.monotonic()
        self._boot_id = uuid.uuid4().hex[:8]
        self._idle_task: asyncio.Task | None = None
        self._sessions = 0
        self._prompts = 0
        self._restarts = 0
        self._last_error = ""

    # -- lifecycle ---------------------------------------------------------- #

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # trust_env=False: a corporate/system proxy must not intercept
            # loopback traffic to our own gateway.
            self._client = httpx.AsyncClient(timeout=None, trust_env=False)
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_reaper())
        return self._client

    async def aclose(self) -> None:
        """Stop the gateway and release the HTTP client (app shutdown)."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        await self._stop_gateway()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def reset(self) -> None:
        """Drop the current gateway so the next call starts a fresh one."""
        self._connection_id = ""
        await self._stop_gateway()

    async def _idle_reaper(self) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                if self._proc is None:
                    continue
                idle_for = time.monotonic() - self._last_used
                if idle_for > settings.workbuddy_idle_seconds:
                    logger.info("WorkBuddy app-server idle for %.0fs - stopping", idle_for)
                    async with self._start_lock:
                        await self._stop_gateway()
        except asyncio.CancelledError:
            return

    def _write_overlays(self) -> tuple[str, str, str]:
        """Write the throwaway settings/mcp overlay files the CLI is launched with.

        The overlay keeps the user's real WorkBuddy config untouched: the CLI
        is pointed at a temp directory holding only an empty MCP config and a
        one-shot gateway password.
        """
        overlay = Path(tempfile.gettempdir()) / "resumematcher-workbuddy" / uuid.uuid4().hex
        overlay.mkdir(parents=True, exist_ok=True)
        settings_path = overlay / "settings.json"
        mcp_path = overlay / "mcp.json"
        settings_path.write_text(
            json.dumps({"gateway": {"auth": "password", "password": self._password}}),
            encoding="utf-8",
        )
        mcp_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        return str(settings_path), str(mcp_path), str(overlay)

    def _free_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def _start_gateway(self, model: str) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return

        distribution = await asyncio.to_thread(resolve_distribution)
        self._password = secrets.token_urlsafe(32)
        settings_path, mcp_path, overlay_dir = self._write_overlays()
        workdir = Path(tempfile.gettempdir()) / "resumematcher-workbuddy-workspace"
        workdir.mkdir(parents=True, exist_ok=True)

        port = self._free_port()
        endpoint = f"http://127.0.0.1:{port}"

        args = [
            distribution["node"],
            distribution["cli"],
            "--serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--settings",
            settings_path,
            "--strict-mcp-config",
            "--mcp-config",
            mcp_path,
            "--no-session-persistence",
            # Treat the gateway as a pure text-in/text-out model endpoint: no
            # tools means no file reads, edits, or shell commands from a resume.
            "--tools",
            "",
            "--permission-mode",
            "default",
            "--model",
            model,
        ]

        env = dict(os.environ)
        env["CODEBUDDY_GATEWAY_AUTH"] = "password"
        # GatewayAuth.setup() reads the password from this env var; the
        # settings.json field is only a fallback and is ignored with --settings.
        env["CODEBUDDY_GATEWAY_PASSWORD"] = self._password

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(workdir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=getattr(asyncio.subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        logger.info(
            "WorkBuddy app-server starting on %s (model=%s, pid=%s)",
            endpoint,
            model,
            self._proc.pid,
        )

        try:
            await self._wait_health(endpoint)
        except Exception:
            await self._stop_gateway()
            raise

        self._endpoint = endpoint
        self._model = model
        self._connection_id = ""
        self._last_error = ""
        logger.info("WorkBuddy app-server healthy (model=%s)", model)

    async def _wait_health(self, endpoint: str) -> None:
        client = await self._ensure_client()
        deadline = time.monotonic() + settings.workbuddy_startup_timeout_seconds
        last = "no attempt"
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                output = ""
                if self._proc.stdout is not None:
                    with contextlib.suppress(Exception):
                        output = (await self._proc.stdout.read()).decode("utf-8", errors="replace")
                raise WorkBuddyError(
                    "CODEBUDDY_SESSION_FAILED",
                    f"WorkBuddy app-server exited during start-up (rc={self._proc.returncode}).",
                    output.strip()[-500:] or "Check that WorkBuddy is installed and signed in.",
                )
            try:
                response = await client.get(
                    endpoint + ROUTE_HEALTH, headers=self._headers(), timeout=5.0
                )
                if response.status_code == 200:
                    return
                last = f"HTTP {response.status_code}: {response.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                last = repr(exc)
            await asyncio.sleep(0.4)
        raise WorkBuddyError(
            "CODEBUDDY_SESSION_FAILED",
            f"WorkBuddy app-server did not become healthy within "
            f"{settings.workbuddy_startup_timeout_seconds}s.",
            last,
        )

    async def _stop_gateway(self) -> None:
        self._connection_id = ""
        self._model = ""
        self._endpoint = ""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except (asyncio.TimeoutError, TimeoutError):
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        logger.info("WorkBuddy app-server stopped")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._password}",
            "X-CodeBuddy-Request": "1",
        }
        if extra:
            headers.update(extra)
        return headers

    # -- ACP ---------------------------------------------------------------- #

    async def _ensure_ready(self, model: str) -> None:
        """Guarantee a healthy gateway pinned to ``model`` plus a live ACP link."""
        async with self._start_lock:
            if self._proc is None or self._proc.returncode is not None or self._model != model:
                if self._proc is not None:
                    # Model switch (or a dead process): the gateway is bound to
                    # one model for its lifetime, so it has to be replaced.
                    await self._stop_gateway()
                await self._start_gateway(model)
            if not self._connection_id:
                await self._connect()

    async def _connect(self) -> None:
        client = await self._ensure_client()
        try:
            response = await client.post(
                self._endpoint + ROUTE_CONNECT, headers=self._headers(), timeout=30.0
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkBuddyError(
                "CODEBUDDY_SESSION_FAILED",
                "Could not reach the WorkBuddy app-server.",
                repr(exc),
            ) from exc

        if response.status_code in (401, 403):
            raise WorkBuddyError(
                "CODEBUDDY_LOGIN_REQUIRED",
                "The WorkBuddy app-server rejected the gateway credentials.",
                "Open WorkBuddy and make sure you are signed in, then test again.",
            )
        if response.status_code >= 400:
            raise WorkBuddyError(
                "CODEBUDDY_SESSION_FAILED",
                f"ACP connect failed with HTTP {response.status_code}.",
                response.text[:300],
            )

        payload = response.json()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        connection_id = data.get("connectionId") or payload.get("connectionId")
        if not connection_id:
            raise WorkBuddyError(
                "CODEBUDDY_SESSION_FAILED",
                "ACP connect returned no connectionId.",
                json.dumps(payload)[:300],
            )

        # Must be set before the first _rpc call: every ACP request carries the
        # connection id as a header, and an empty value is rejected with 400.
        self._connection_id = connection_id

        await self._rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "ResumeMatcher", "version": "1.0.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
            timeout=60.0,
        )
        logger.info("ACP connected (%s...)", connection_id[:8])

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
        on_text: Callable[[str], None] | None = None,
    ) -> tuple[dict, list[dict]]:
        """Send one JSON-RPC request over ACP and collect the SSE reply."""
        client = await self._ensure_client()
        request_id = str(uuid.uuid4())
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        messages: list[dict] = []

        def collect(message: dict) -> None:
            messages.append(message)
            text = _message_text(message)
            if text and on_text is not None:
                on_text(text)

        parser = _SSEParser(collect)

        try:
            async with client.stream(
                "POST",
                self._endpoint + ROUTE_ACP,
                headers=self._headers(
                    {
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "acp-connection-id": self._connection_id,
                    }
                ),
                json=body,
                timeout=timeout,
            ) as response:
                if response.status_code in (401, 403):
                    raise WorkBuddyError(
                        "CODEBUDDY_LOGIN_REQUIRED",
                        "The WorkBuddy app-server rejected the ACP credentials.",
                        "Sign in to WorkBuddy and test the connection again.",
                    )
                if response.status_code >= 400:
                    raw = await response.aread()
                    raise WorkBuddyError(
                        "CODEBUDDY_TURN_FAILED",
                        f"ACP {method} failed with HTTP {response.status_code}.",
                        raw[:400].decode("utf-8", errors="replace"),
                    )
                async for chunk in response.aiter_text():
                    parser.push(chunk)
        except WorkBuddyError:
            raise
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            raise WorkBuddyError(
                "CODEBUDDY_TURN_FAILED",
                f"ACP {method} transport failure.",
                repr(exc),
            ) from exc
        parser.finish()

        reply = next((m for m in messages if str(m.get("id", "")) == request_id), None)
        if reply is None:
            raise WorkBuddyError(
                "CODEBUDDY_TURN_FAILED",
                f"ACP {method} produced no correlated response.",
                f"{len(messages)} messages received",
            )
        if reply.get("error"):
            raise WorkBuddyError(
                "CODEBUDDY_TURN_FAILED",
                f"ACP {method} returned an error.",
                json.dumps(reply["error"])[:400],
            )
        return reply.get("result") or {}, messages

    # -- public ------------------------------------------------------------- #

    async def prompt(
        self,
        messages: list[dict],
        model: str | None = None,
        response_format: Any = None,
        timeout: float | None = None,
    ) -> str:
        """Run one isolated ACP prompt and return the assistant text.

        Each call gets its own ACP session, so no conversation state leaks
        between resume runs even though the gateway process is reused.
        """
        resolved_model = normalize_model(model)
        prompt_text = build_prompt(messages, response_format)
        budget = timeout or settings.workbuddy_prompt_timeout_seconds

        async with self._prompt_lock:
            self._last_used = time.monotonic()
            last_error: Exception | None = None

            for attempt in (1, 2):
                try:
                    await self._ensure_ready(resolved_model)
                    session, _ = await self._rpc(
                        "session/new",
                        {"cwd": self._workdir(), "mcpServers": []},
                        timeout=90.0,
                    )
                    session_id = session.get("sessionId")
                    if not session_id:
                        raise WorkBuddyError(
                            "CODEBUDDY_SESSION_FAILED",
                            "session/new returned no sessionId.",
                            json.dumps(session)[:300],
                        )
                    self._sessions += 1

                    chunks: list[str] = []
                    result, _ = await self._rpc(
                        "session/prompt",
                        {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt_text}]},
                        timeout=budget,
                        on_text=chunks.append,
                    )
                    self._prompts += 1
                    self._last_used = time.monotonic()

                    stop_reason = (result or {}).get("stopReason")
                    if stop_reason and stop_reason not in ("end_turn", "cancelled"):
                        logger.warning("WorkBuddy app-server stopReason=%s", stop_reason)

                    text = "".join(chunks).strip()
                    if not text:
                        raise WorkBuddyError(
                            "CODEBUDDY_TURN_FAILED",
                            "The WorkBuddy model returned no text.",
                            f"stopReason={stop_reason}",
                        )
                    return text
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    logger.warning("WorkBuddy ACP attempt %d failed: %r", attempt, exc)
                    self._connection_id = ""
                    if attempt == 1:
                        # A stale gateway is the usual cause; drop it and retry.
                        self._restarts += 1
                        await self.reset()
                        continue

            if isinstance(last_error, WorkBuddyError):
                raise last_error
            raise WorkBuddyError(
                "CODEBUDDY_TURN_FAILED",
                "The WorkBuddy app-server did not complete the request.",
                repr(last_error),
            ) from last_error

    def _workdir(self) -> str:
        workdir = Path(tempfile.gettempdir()) / "resumematcher-workbuddy-workspace"
        workdir.mkdir(parents=True, exist_ok=True)
        return str(workdir)

    def status(self) -> dict[str, Any]:
        return {
            "provider": "workbuddy",
            "boot_id": self._boot_id,
            "gateway_running": self._proc is not None and self._proc.returncode is None,
            "gateway_endpoint": self._endpoint,
            "acp_connected": bool(self._connection_id),
            "model": self._model,
            "idle_seconds": round(time.monotonic() - self._last_used, 1),
            "sessions": self._sessions,
            "prompts": self._prompts,
            "restarts": self._restarts,
            "last_error": self._last_error,
        }


# Process-wide singleton: one gateway serves the whole backend.
app_server = WorkBuddyAppServer()


# --------------------------------------------------------------------------- #
# Convenience API used by app.llm
# --------------------------------------------------------------------------- #


def available_models() -> list[str]:
    """Model ids the Settings dropdown offers."""
    return list(MODELS)


def max_output_tokens() -> int:
    return WORKBUDDY_MAX_OUTPUT_TOKENS


async def complete_chat(
    messages: list[dict],
    model: str | None = None,
    response_format: Any = None,
    timeout: float | None = None,
) -> str:
    """One-shot completion through the WorkBuddy app-server."""
    return await app_server.prompt(
        messages, model=model, response_format=response_format, timeout=timeout
    )


async def health(
    model: str | None = None,
    timeout: float = 60.0,
    prompt: str | None = None,
) -> dict[str, Any]:
    """Probe the app-server end to end with a trivial prompt.

    A successful round-trip proves the whole chain - CLI present, account
    signed in, model usable - which is what the Settings "Test connection"
    button needs to report.
    """
    distribution = distribution_status()
    if not distribution.get("available"):
        return {
            "healthy": False,
            "provider": "workbuddy",
            "model": normalize_model(model),
            "error_code": distribution.get("error_code", "CODEBUDDY_BINARY_NOT_FOUND"),
            "message": distribution.get("message", ""),
            "hint": distribution.get("hint", ""),
        }

    resolved_model = normalize_model(model)
    started = time.monotonic()
    try:
        text = await app_server.prompt(
            [{"role": "user", "content": prompt or "Reply with the single word: ok"}],
            model=resolved_model,
            timeout=timeout,
        )
    except WorkBuddyError as exc:
        return {
            "healthy": False,
            "provider": "workbuddy",
            "model": resolved_model,
            "error_code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "healthy": False,
            "provider": "workbuddy",
            "model": resolved_model,
            "error_code": "CODEBUDDY_TURN_FAILED",
            "message": "The WorkBuddy app-server request failed.",
            "hint": repr(exc),
        }

    return {
        "healthy": True,
        "provider": "workbuddy",
        "model": resolved_model,
        "response_model": f"workbuddy/{resolved_model}",
        "model_output": text,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "cli_version": distribution.get("cli_version", ""),
    }


def reset_discovery_cache() -> None:
    """Forget the cached CLI discovery (used after a WorkBuddy install/upgrade)."""
    global _DISTRIBUTION, _DISTRIBUTION_ERROR
    _DISTRIBUTION = None
    _DISTRIBUTION_ERROR = None


async def readiness(model: str | None = None) -> dict[str, Any]:
    """Cheap readiness probe that never starts a gateway.

    ``GET /status`` is polled by the UI, and a full ``health()`` probe would
    spawn the app-server process as a side effect of a read — turning a page
    load into a multi-second (or multi-minute, on a cold CLI) operation.

    So this reports what the discovery layer already knows: whether a usable
    CLI exists and whether a gateway happens to be warm. A warm gateway is
    *not* re-verified here either; the Settings "Test connection" button owns
    the authoritative end-to-end round-trip via ``health()``.
    """
    distribution = distribution_status()
    if not distribution.get("available"):
        return {
            "healthy": False,
            "provider": "workbuddy",
            "model": normalize_model(model),
            "error_code": distribution.get("error_code", "CODEBUDDY_BINARY_NOT_FOUND"),
            "message": distribution.get("message", ""),
            "hint": distribution.get("hint", ""),
        }

    status = app_server.status()
    result: dict[str, Any] = {
        "healthy": True,
        "provider": "workbuddy",
        "model": normalize_model(model),
        "cli_version": distribution.get("cli_version", ""),
        "gateway_running": status["gateway_running"],
        "gateway_model": status["model"],
    }
    if not status["gateway_running"]:
        # Nothing is running yet, so nothing has proven the account can
        # actually serve this model. Surface that rather than claiming a
        # verified state the backend has not observed.
        result["warning_code"] = "workbuddy_unverified"
        result["warning"] = (
            "WorkBuddy is installed but the app-server is not running yet. "
            "Use Test connection to verify the account and model."
        )
    return result
