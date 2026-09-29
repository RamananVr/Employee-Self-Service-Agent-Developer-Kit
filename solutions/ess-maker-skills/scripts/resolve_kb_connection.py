# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""CLI shim: resolve which knowledge source(s) are bound to the local agent.

Prints a JSON ``ResolutionResult`` (see ``flightcheck.kb_connection_resolver``)
to stdout and exits 0 on success (``"ok"``/``"none_bound"``) or 1 on
``"error"``. Never calls ``auth.load_config()`` (which ``sys.exit``s on a
missing/mismatched config) so every failure mode is surfaced as JSON instead
of an uncaught process exit.
"""

from __future__ import annotations

import json
import os
from typing import Any

from auth import discover_tenant
from flightcheck.kb_connection_resolver import resolve_bound_connections
from flightcheck.pva_client import PVAClient

LOCAL_STATE_DIR = ".local"


def _error_result(message: str) -> dict[str, Any]:
    return {"status": "error", "connections": [], "error": message}


def main(argv: list[str] | None = None) -> int:
    """Run the KB connection resolution CLI. Returns the process exit code."""
    config_path = os.path.join(LOCAL_STATE_DIR, "config.json")
    if not os.path.exists(config_path):
        print(json.dumps(_error_result(
            f"{config_path} not found. Run /setup first."
        )))
        return 1

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps(_error_result(
            f"Could not read {config_path}: {exc}"
        )))
        return 1

    if not isinstance(config, dict):
        print(json.dumps(_error_result(
            f"{config_path} does not contain a JSON object."
        )))
        return 1

    env_url = config.get("dataverseEndpoint", "")
    bot_id = config.get("agent", {}).get("botId")

    try:
        tenant_id = discover_tenant(env_url)
    except Exception as exc:  # noqa: BLE001 — surfaced as JSON, not raised
        print(json.dumps(_error_result(
            f"Could not discover tenant for {env_url!r}: {exc}"
        )))
        return 1

    pva = PVAClient(tenant_id, env_url)
    try:
        pva.authenticate()
    except Exception as exc:  # noqa: BLE001 — surfaced as JSON, not raised
        print(json.dumps(_error_result(
            f"Copilot Studio (Island Gateway) authentication failed: {exc}"
        )))
        return 1

    result = resolve_bound_connections(pva, bot_id)
    print(json.dumps(result.to_dict()))
    return 0 if result.status != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
