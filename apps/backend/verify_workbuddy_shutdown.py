"""Verify the backend's shutdown hook stops the WorkBuddy app-server.

Boots a gateway through the real provider code, then runs the FastAPI lifespan
context manager's shutdown half and asserts the child process is gone. This is
the exact path `app/main.py` runs when the backend stops; a force-kill would not
exercise it, which is why it is driven directly instead of via taskkill.
"""

from __future__ import annotations

import asyncio
import sys

from app import workbuddy
from app.llm import LLMConfig, complete


async def gateway_pid() -> int | None:
    """PID listening on the app-server's port, or None when not running."""
    endpoint = workbuddy.app_server.status()["gateway_endpoint"]
    if not endpoint:
        return None
    port = int(endpoint.rsplit(":", 1)[1])
    proc = await asyncio.create_subprocess_exec(
        "netstat", "-ano", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    for line in out.decode("utf-8", errors="replace").splitlines():
        if f":{port} " in line and "LISTENING" in line:
            return int(line.split()[-1])
    return None


def alive(pid: int) -> bool:
    import subprocess

    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return str(pid) in result.stdout


async def main() -> int:
    config = LLMConfig(provider="workbuddy", model="deepseek-v4-flash", api_key="")

    print("1. booting the app-server via the provider ...")
    text = await complete("Say OK.", config=config)
    pid = await gateway_pid()
    print(f"   reply={text.strip()[:40]!r} gateway_pid={pid}")
    if pid is None:
        print("FAIL: no gateway process found after a completion")
        return 1

    print("2. running the shutdown half of the FastAPI lifespan ...")
    from app.main import app, lifespan

    ctx = lifespan(app)
    await ctx.__aenter__()
    # __aexit__ is the shutdown half: it must stop the gateway.
    await ctx.__aexit__(None, None, None)

    await asyncio.sleep(1.5)
    still_up = alive(pid)
    print(f"3. gateway pid {pid} alive after shutdown: {still_up}")
    print("\nRESULT:", "FAIL" if still_up else "PASS")
    return 1 if still_up else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
