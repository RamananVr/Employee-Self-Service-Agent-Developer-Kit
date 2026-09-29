# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unit tests for the ``resolve_kb_connection`` CLI shim's JSON contract.

Runs ``main()`` in-process with the working directory switched to a
``tmp_path`` (so the ``.local/config.json`` relative-path convention is
exercised) and ``discover_tenant``/``PVAClient`` monkeypatched — no network
access.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import resolve_kb_connection

FAKE_BOT_ID = "00000000-0000-0000-0000-000000003333"


class _FakePVA:
    """Duck-typed stand-in for ``PVAClient`` constructed as ``PVAClient(tenant_id, env_url)``."""

    instances: list["_FakePVA"] = []

    def __init__(self, tenant_id: str, env_url: str) -> None:
        self.tenant_id = tenant_id
        self.env_url = env_url
        self.is_configured = True
        self.authenticate_error: Exception | None = None
        self.knowledge_sources: list[dict[str, Any]] = []
        _FakePVA.instances.append(self)

    def authenticate(self) -> None:
        if self.authenticate_error is not None:
            raise self.authenticate_error

    def get_knowledge_sources(self, bot_id: str) -> list[dict[str, Any]]:
        return list(self.knowledge_sources)


def _write_config(tmp_path: Path, *, bot_id: str | None = FAKE_BOT_ID) -> None:
    local_dir = tmp_path / ".local"
    local_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "dataverseEndpoint": "https://contoso.crm.dynamics.com",
        "agent": {"botId": bot_id} if bot_id is not None else {},
    }
    (local_dir / "config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )


def _gc_knowledge_source() -> dict[str, Any]:
    """A KnowledgeSourceComponent bound to a Graph Connector source."""
    return {
        "$kind": "KnowledgeSourceComponent",
        "displayName": "Mock GC KB",
        "id": "00000000-0000-0000-0000-000000007777",
        "state": "mc",
        "status": "Active",
        "configuration": {
            "$kind": "KnowledgeSourceConfiguration",
            "source": {
                "$kind": "GraphConnectorSearchSource",
                "connectionId": {
                    "$kind": "EnvironmentVariableReference",
                    "schemaName": "msdyn_x.envVar.gc",
                },
                "connectionName": "ServiceNowKB48",
                "contentSourceDisplayName": "Mock GC KB",
                "publisherName": "Microsoft",
            },
        },
    }


@pytest.fixture(autouse=True)
def _reset_fake_pva_instances():
    _FakePVA.instances = []
    yield
    _FakePVA.instances = []


def _patch_pva(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve_kb_connection, "PVAClient", _FakePVA)
    monkeypatch.setattr(
        resolve_kb_connection, "discover_tenant", lambda env_url: "tenant-123"
    )


def test_missing_config_reports_json_error_and_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _patch_pva(monkeypatch)

    exit_code = resolve_kb_connection.main([])

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["connections"] == []
    assert "/setup" in payload["error"]


def test_zero_bound_sources_reports_none_bound_and_exits_zero(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _patch_pva(monkeypatch)

    exit_code = resolve_kb_connection.main([])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "none_bound", "connections": []}
    assert _FakePVA.instances[0].tenant_id == "tenant-123"
    assert _FakePVA.instances[0].env_url == "https://contoso.crm.dynamics.com"


def test_one_bound_source_reports_ok_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _patch_pva(monkeypatch)
    real_pva_init = _FakePVA.__init__

    def _init_with_source(self, tenant_id, env_url):
        real_pva_init(self, tenant_id, env_url)
        self.knowledge_sources = [_gc_knowledge_source()]

    monkeypatch.setattr(_FakePVA, "__init__", _init_with_source)

    exit_code = resolve_kb_connection.main([])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["connections"] == [
        {
            "connection_name": "ServiceNowKB48",
            "state": "mc",
            "status": "Active",
        }
    ]


def test_authenticate_failure_reports_json_error_and_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path)
    _patch_pva(monkeypatch)
    real_pva_init = _FakePVA.__init__

    def _init_with_auth_error(self, tenant_id, env_url):
        real_pva_init(self, tenant_id, env_url)
        self.authenticate_error = RuntimeError("network unreachable")

    monkeypatch.setattr(_FakePVA, "__init__", _init_with_auth_error)

    exit_code = resolve_kb_connection.main([])

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["connections"] == []
    assert "network unreachable" in payload["error"]
