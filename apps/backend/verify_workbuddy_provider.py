"""Manual end-to-end check for the native WorkBuddy app-server provider.

Runs the same code path the backend uses (app.workbuddy -> app-server ACP) so a
failure here is a real provider failure, not a test-harness artifact.

    .venv/Scripts/python.exe verify_workbuddy_provider.py
    .venv/Scripts/python.exe verify_workbuddy_provider.py --model hy3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from app import workbuddy
from app.config import settings
from app.llm import LLMConfig, check_llm_health, complete, complete_json


def section(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="deepseek-v4-flash")
    args = parser.parse_args()

    section("1. discovery")
    print(json.dumps(workbuddy.distribution_status(), ensure_ascii=False, indent=2))
    print(f"configured cli_path={settings.workbuddy_cli_path!r} node_path={settings.workbuddy_node_path!r}")

    section("2. readiness (no process should start)")
    before = workbuddy.app_server.status()["gateway_running"]
    ready = await workbuddy.readiness(model=args.model)
    after = workbuddy.app_server.status()["gateway_running"]
    print(json.dumps(ready, ensure_ascii=False, indent=2))
    print(f"gateway_running {before} -> {after} (must not change)")
    if before != after:
        print("FAIL: readiness started a gateway")
        return 1

    section("3. plain text completion (cold start included)")
    started = time.monotonic()
    try:
        text = await complete(
            "In one short sentence, say what a resume-to-job matcher does.",
            system_prompt="You are terse. Answer in one sentence.",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc!r}")
        await workbuddy.app_server.aclose()
        return 1
    print(f"elapsed={time.monotonic() - started:.1f}s")
    print(text[:400])

    section("4. JSON completion (parsed by the backend)")
    try:
        data = await complete_json(
            "Return a JSON object with keys name (string) and skills (array of 3 strings) "
            "for a fictional data analyst named Li Wei.",
            schema_type="keywords",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc!r}")
        await workbuddy.app_server.aclose()
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))

    section("5. health check via llm.check_llm_health (full round trip)")
    config = LLMConfig(provider="workbuddy", model=args.model, api_key="")
    health = await check_llm_health(config, include_details=True, test_prompt="Hi")
    print(json.dumps({k: v for k, v in health.items()}, ensure_ascii=False, indent=2))

    section("6. runtime status after use")
    print(json.dumps(workbuddy.app_server.status(), ensure_ascii=False, indent=2))

    await workbuddy.app_server.aclose()
    ok = bool(health.get("healthy")) and isinstance(data, dict)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
