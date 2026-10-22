# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Shared planning and real CLI regressions using approved API fixtures."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import responses

from flightcheck import cli, registry
from flightcheck.da_checks import DAValidation
from flightcheck.da_execution import DAExecutor, plan_da_checks
from flightcheck.runner import CheckResult
from tests.conftest import require_validated_mock
from tests.flightcheck.test_maker_diagnostics import (
    TENANT_ID, _body, _capture, _register_maker_baseline, _register_native,
    setup_workspace as setup_workspace,
)
from tests.mocks import graph as graph_mock
from tests.mocks import azure_arm as arm_mock
from tests.mocks import powerplatform as platform_mock
from tests.mocks import pp_admin as admin_mock
from tests.mocks import dataverse as dataverse_mock


def _run_cli(ws, monkeypatch, http, options, output="report"):
    (ws.root / ".local" / "config.json").write_text(json.dumps(ws.config), encoding="utf-8")
    monkeypatch.setattr("sys.argv", [
        "flightcheck", *options, "--output", output, "--no-open", "--no-telemetry",
    ])
    with pytest.raises(SystemExit) as exit_result:
        cli.main()
    payload = json.loads((ws.root / output / "results.json").read_text(encoding="utf-8"))
    assert (ws.root / output / "report.html").exists()
    assert payload["coverage"]["planned"] == payload["coverage"]["emitted"]
    assert payload["total"] == len(payload["coverage"]["planned"])
    assert sum(
        payload["coverage"][key]
        for key in ("automaticCompleted", "manual", "notApplicable", "limitations")
    ) == payload["total"]
    html = (ws.root / output / "report.html").read_text(encoding="utf-8")
    assert f'{payload["total"]} planned' in html
    assert f'{payload["coverage"]["automaticCompleted"]} automatically completed' in html
    return payload, exit_result.value.code


def _row(payload, key):
    return next(row for row in payload["results"] if row["checkpoint_id"] == key)


def test_register_once_is_discovered_by_full_checkpoint_and_profile(setup_workspace, monkeypatch):
    ws = setup_workspace
    key = "DA-REGISTER-TEST"

    def check(runner):
        count = len(runner._da_native_snapshot["changeset"]["botComponentChanges"])
        return [CheckResult(
            key, "Agent configuration", "High", "Passed", "Selected content observation",
            f"{count} exact selected-agent component(s).", roles=["ESS Maker / Agent Developer"],
        )]

    monkeypatch.setitem(registry.REGISTRY, key, registry.CheckpointSpec(
        key, check, "Agent configuration",
        roles=("ESS Maker / Agent Developer",),
        da=DAValidation(
            purpose="Selected content observation", prerequisites=("DA-CONTENT-001",),
            manual_steps=("Inspect the selected agent in Copilot Studio.",),
            if_incorrect="Repair the selected content and rerun.",
        ),
    ))
    monkeypatch.setitem(registry.PROFILES, "da:test-registration", registry.ProfileSpec(
        "da:test-registration", (key,), "Shared registration regression.",
    ))
    rows = []
    for index, options in enumerate((
        ["--scope", "full"], ["--checkpoint", key],
        ["--profile", "da:test-registration", "--validation-realm", "dev"],
    )):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
            _register_native(http, ws)
            if index == 0:
                _register_maker_baseline(http)
            payload, code = _run_cli(ws, monkeypatch, http, options, f"registration-{index}")
            assert code == 0
            rows.append(_row(payload, key))
            if index:
                assert not any("graph.microsoft.com" in call.request.url for call in http.calls)
    assert rows[0] == rows[1] == rows[2]


def test_full_checkpoint_and_profile_reuse_real_graph_producer(setup_workspace, monkeypatch):
    ws = setup_workspace
    monkeypatch.setitem(registry.PROFILES, "da:test-license", registry.ProfileSpec(
        "da:test-license", ("PRE-001",), "Same supported SKU observation.",
    ))
    rows = []
    for index, options in enumerate((
        ["--scope", "full"], ["--checkpoint", "PRE-001"],
        ["--profile", "da:test-license", "--validation-realm", "dev"],
    )):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
            _register_native(http, ws)
            if index == 0:
                _register_maker_baseline(http)
            else:
                require_validated_mock(graph_mock)
                http.add(**graph_mock.list_subscribed_skus())
            payload, code = _run_cli(ws, monkeypatch, http, options, f"license-{index}")
            assert code == 0
            rows.append(_row(payload, "PRE-001"))
            assert sum("/subscribedSkus" in call.request.url for call in http.calls) == 1
            if index:
                assert not any(
                    value in call.request.url
                    for call in http.calls
                    for value in ("api.bap", "api.flow", "licensing/", "servicePrincipals", "crm.dynamics")
                )
    assert rows[0] == rows[1] == rows[2]
    assert "tenant inventory" in rows[0]["result"]
    assert rows[0]["remediation"] == ""


@pytest.mark.parametrize("status,body,expected", [
    (403, {"error": {"code": "Authorization_RequestDenied"}}, "Manual"),
    (401, {"error": {"code": "InvalidAuthenticationToken"}}, "Error"),
    (503, {"error": {"code": "ServiceUnavailable"}}, "Error"),
    (200, {"unexpected": []}, "Error"),
    (200, {"value": [{}]}, "Error"),
])
def test_permission_fallback_does_not_hide_auth_service_or_shape_errors(
    setup_workspace, monkeypatch, status, body, expected,
):
    ws = setup_workspace
    require_validated_mock(graph_mock)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add("GET", f"{graph_mock.GRAPH_BASE}/subscribedSkus", status=status, json=body)
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", "PRE-001"])
    row = _row(payload, "PRE-001")
    assert row["status"] == expected
    assert code == (expected == "Error")
    if expected == "Manual":
        assert "denied" in row["result"]
        assert row["evidence"]["httpStatus"] == 403
        assert row["roles"] == ["Microsoft 365 Admin"]
        assert "Microsoft 365 admin center" in row["remediation"]
        assert row["evidence"]["manualVerificationSteps"]
        assert row["evidence"]["ifMissingOrIncorrect"]
        assert payload["coverage"]["automaticCompleted"] == 2
    else:
        assert row["evidence"]["executionError"] is True
        assert "retry" in row["remediation"]


def test_failed_identity_accounts_for_every_planned_check_without_remote_reads(setup_workspace, monkeypatch):
    ws = setup_workspace
    ws.config["tenantId"] = "00000000-0000-4000-8000-000000009999"
    with responses.RequestsMock() as http:
        payload, code = _run_cli(ws, monkeypatch, http, ["--scope", "full"])
        assert not http.calls
    assert code == 1
    assert _row(payload, "DA-AGENT-001")["status"] == "Blocked"
    assert _row(payload, "PRE-001")["status"] == "Blocked"
    assert _row(payload, "QA-001")["status"] == "Blocked"
    assert payload["coverage"]["limitations"] > 0
    assert payload["validation_context"]["targetVerified"] is False


@pytest.mark.parametrize("produced", [0, 2])
def test_missing_or_duplicate_producer_rows_are_explicit_errors(setup_workspace, monkeypatch, produced):
    ws = setup_workspace
    key = "DA-OUTPUT-TEST"
    monkeypatch.setitem(registry.REGISTRY, key, registry.CheckpointSpec(
        key=key, category_label="Agent setup",
        roles=("ESS Maker / Agent Developer",),
        category_fn=lambda _: [
            CheckResult(key, "Agent setup", "High", "Passed", "Test", "Observed.", roles=["ESS Maker / Agent Developer"])
            for _ in range(produced)
        ],
        da=DAValidation(
            purpose="Output reconciliation", prerequisites=(),
            manual_steps=("Inspect the selected agent.",),
            if_incorrect="Repair the selected content and rerun.",
        ),
    ))
    with responses.RequestsMock() as http:
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", key])
        assert not http.calls
    row = _row(payload, key)
    assert row["status"] == "Error"
    assert f"returned {produced} matching rows" in row["result"]
    assert code == 1


def test_roles_with_zero_members_do_not_prove_assignment(setup_workspace, monkeypatch):
    ws = setup_workspace
    role = graph_mock.directory_role(
        role_id="00000000-0000-4000-8000-000000001a01",
        role_template_id="62e90394-69f5-4237-9190-012177145e10",
    )
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add(**graph_mock.list_directory_roles(roles=[role]))
        http.add(**graph_mock.list_role_members(role_id=role["id"], members=[]))
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", "PRE-008"])
    row = _row(payload, "PRE-008")
    assert code == 0
    assert row["status"] == "Manual"
    assert "0 active member(s)" in row["result"]
    assert "Do not assign this role to the maker" in row["remediation"]


@pytest.mark.parametrize("state,expected", [("Succeeded", "Passed"), ("Failed", "Failed"), ("Provisioning", "Failed")])
def test_database_type_does_not_override_provisioning_state(state, expected):
    # Pure assertion regression: the legacy precedence bug is independent of HTTP.
    from flightcheck.checks.da_validation import _provisioning

    spec = registry.REGISTRY["ENV-002"]
    runner = SimpleNamespace(
        _maker_environment_snapshot={"properties": {
            "databaseType": "CommonDataService",
            "linkedEnvironmentMetadata": {"resourceProvisioningState": state},
        }},
        maker_workday_extension=True, _da_connection_audit=None,
        da_executor=SimpleNamespace(group=lambda *_: None),
    )
    row = _provisioning(runner, spec)
    assert row.status == expected
    assert f"state: {state}" in row.result
    assert "not used to override" in row.result


def test_full_dispositions_exclude_aliases_guards_and_deferred_alm():
    _, plan, _ = plan_da_checks()
    keys = {spec.key for spec in plan}
    assert "ENV-004" in keys
    assert "ENV-004-WD" not in keys
    assert not keys.intersection({"PUB-001", "PUB-002", "ENV-004-GRS", "WD-WF-001"})
    for spec in registry.REGISTRY.values():
        assert spec.da is not None
        if spec.da.executable:
            assert spec.key in keys
            assert spec.da.manual_steps
        for replacement in spec.da.replacements:
            assert replacement in registry.REGISTRY
    _, alias_plan, aliases = plan_da_checks(checkpoint="ENV-004-WD")
    assert aliases["ENV-004-WD"] == ("ENV-004",)
    assert "ENV-004-WD" not in {spec.key for spec in alias_plan}
    _, provider_plan, provider_aliases = plan_da_checks(checkpoint="DA-INTEGRATIONS-001")
    assert provider_aliases["DA-INTEGRATIONS-001"] == ("WD-INTEGRATION-001", "SN-INTEGRATION-001")
    assert "DA-INTEGRATIONS-001" not in {spec.key for spec in provider_plan}


@pytest.mark.parametrize("value,expected", [
    (None, "Manual"),
    ("", "Manual"),
    ("https://wd.example.test/ccx/api", "Manual"),
    ("https://wd.example.test/ccx/api/", "Manual"),
    ("http://wd.example.test/ccx/api", "Failed"),
    ("https://wd.example.test/wrong/api", "Failed"),
    ("https://wd.example.test/ccx/api/staffing/v1", "Failed"),
    ("https://wd.example.test/ccx/api//", "Failed"),
    ("https:///ccx/api", "Failed"),
    ("https://secret-user:secret-password@wd.example.test/ccx/api", "Failed"),
    ("https://wd.example.test/ccx/api?token=secret-token", "Failed"),
    ("https://wd.example.test/ccx/api#secret-token", "Failed"),
    ("https://wd.example.test:bad/ccx/api", "Failed"),
    ("https://wd.\nexample.test/ccx/api", "Failed"),
    ("https://wd.example.test\\other/ccx/api", "Failed"),
    (" https://wd.example.test/ccx/api", "Failed"),
    ({"url": "https://wd.example.test/ccx/api"}, "Error"),
])
def test_da_rest_checks_saved_structure_without_certifying_deployment(
    setup_workspace, monkeypatch, value, expected,
):
    ws = setup_workspace
    ws.config["restBaseUrl"] = value
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", "WD-REST-001"])
        assert all("workday" not in call.request.url for call in http.calls)
    row = _row(payload, "WD-REST-001")
    assert row["status"] == expected
    assert code == (expected in {"Failed", "Error"})
    assert row["doc_link"].endswith("declarative-agent-workday#phase-4-create-connections")
    rendered = json.dumps(row)
    assert "secret-password" not in rendered
    assert "secret-user" not in rendered
    assert "secret-token" not in rendered
    if expected == "Failed":
        assert "/ccx/api" in row["result"]
        assert "Deployed settings were not read" in row["result"]
        assert "/connect workday" in row["remediation"]
    elif expected == "Manual":
        assert "deployed" in row["result"]
        assert "actual deployed connection" in row["remediation"]
        assert row["evidence"]["manualVerificationSteps"]
    else:
        assert "must be a string" in row["result"]
        assert "retry" in row["remediation"]


def test_reference_data_alias_uses_current_topic_guidance_without_legacy_reads(
    setup_workspace, monkeypatch,
):
    ws = setup_workspace
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", "LOCAL-TC-001"])
        assert not any(
            value in call.request.url
            for call in http.calls for value in ("template", "workday", "crm.dynamics")
        )
    assert code == 0
    assert payload["coverage"]["aliases"]["LOCAL-TC-001"] == ["WD-REF-001"]
    assert "LOCAL-TC-001" not in payload["coverage"]["emitted"]
    row = _row(payload, "WD-REF-001")
    assert row["status"] == "Manual"
    assert "Set Runtime Template Configurations" in row["remediation"]
    assert "Maintain Reference IDs" in row["remediation"]
    assert "k (ReferenceId)" in row["remediation"]
    assert "doubled quotation marks" in row["remediation"]
    assert "unused types" in row["remediation"]
    assert row["doc_link"].endswith("#customize-workday-reference-data")


def test_reconciled_workday_guidance_reuses_full_checkpoint_and_profile_producers(
    setup_workspace, monkeypatch,
):
    from flightcheck.checks import _connection_dependencies
    from tests.flightcheck.checks.test_workday_da_readiness import _component_payload

    ws = setup_workspace
    ws.content["botComponentChanges"] = _component_payload()["botComponentChanges"]
    ws.config["restBaseUrl"] = "https://wd.example.test/ccx/api"
    monkeypatch.setattr(_connection_dependencies, "discover_tenant", lambda _: TENANT_ID)
    keys = (
        "WD-DA-ATTACH-001", "WD-REF-001", "WD-CONN-AUTH-001", "WD-API-CLIENT-001",
        "WD-TENANT-001", "WD-SEC-003", "WD-DA-MAKER-001", "WD-REST-001", "WD-NET-001",
        "WD-CONN-010", "WD-CONN-102",
        "WD-DA-TOPIC-001", "WD-DA-WIRING-001",
    )
    monkeypatch.setitem(registry.PROFILES, "da:test-workday-guidance", registry.ProfileSpec(
        "da:test-workday-guidance", keys, "Same Workday configuration assertions.",
    ))
    selections = (
        ["--scope", "full"],
        ["--profile", "da:test-workday-guidance", "--validation-realm", "dev"],
        ["--checkpoint", "WD-REF-001"],
    )
    findings = []
    for index, options in enumerate(selections):
        with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
            _register_native(http, ws)
            if index == 0:
                _register_maker_baseline(http)
            payload, _ = _run_cli(ws, monkeypatch, http, options, f"workday-guidance-{index}")
            selected = keys if index < 2 else ("WD-REF-001",)
            findings.append({key: _row(payload, key) for key in selected})
            assert not any("wd.example.test" in call.request.url for call in http.calls)
            if index:
                assert not any(
                    value in call.request.url
                    for call in http.calls for value in ("api.flow", "api.powerapps", "graph.microsoft", "crm.dynamics")
                )
    assert findings[0] == findings[1]
    assert findings[0]["WD-REF-001"] == findings[2]["WD-REF-001"]
    assert all(
        row["status"] == ("Passed" if key in {"WD-DA-TOPIC-001", "WD-DA-WIRING-001"} else "Manual")
        for key, row in findings[0].items()
    )
    assert "not a confirmed configuration defect" in findings[0]["WD-DA-ATTACH-001"]["result"]
    assert "Manage" in findings[0]["WD-DA-ATTACH-001"]["remediation"]
    assert "SAML SP ID http://www.workday.com/<tenant>, not api://" in findings[0]["WD-CONN-AUTH-001"]["remediation"]
    assert "no ISU is required" in findings[0]["WD-CONN-AUTH-001"]["remediation"]
    assert "Workday API client ID, not the Entra" in findings[0]["WD-API-CLIENT-001"]["remediation"]
    for area in ("Core Payroll", "Organizations and Roles", "Staffing", "Time Off and Leave"):
        assert area in findings[0]["WD-API-CLIENT-001"]["remediation"]
    assert "Get under Integration Permissions" in findings[0]["WD-SEC-003"]["remediation"]
    assert "Activate All Pending Authentication Policy Changes" in findings[0]["WD-TENANT-001"]["remediation"]
    assert "Publication is not required" in findings[0]["WD-DA-MAKER-001"]["remediation"]
    assert "not evidence of Power Platform runtime egress" in findings[0]["WD-NET-001"]["remediation"]
    for key in ("WD-CONN-010", "WD-CONN-102"):
        steps = findings[0][key]["evidence"]["manualVerificationSteps"]
        assert len(steps) == 3
        assert "ask the Entra and Workday administrators to inspect the same target" in steps[-1]


@pytest.mark.parametrize("state", ["missing", "Inactive"])
@pytest.mark.parametrize("selection", ["full", "checkpoint", "profile"])
def test_required_workday_topic_defects_use_native_snapshot_without_dataverse_dependency(
    setup_workspace, monkeypatch, state, selection,
):
    from flightcheck.checks import _connection_dependencies
    from tests.flightcheck.checks.test_workday_da_readiness import SCHEMA, _component_payload

    ws = setup_workspace
    schema = f"{SCHEMA}.topic.WorkdayManagerCheck"
    content = _component_payload(
        missing=schema if state == "missing" else "",
        inactive=schema if state == "Inactive" else "",
    )
    ws.content["botComponentChanges"] = content["botComponentChanges"]
    monkeypatch.setattr(_connection_dependencies, "discover_tenant", lambda _: TENANT_ID)
    monkeypatch.setitem(registry.PROFILES, "da:test-workday-topics", registry.ProfileSpec(
        "da:test-workday-topics", ("WD-DA-TOPIC-001",), "Required Workday dependencies.",
    ))
    options = {
        "full": ["--scope", "full"],
        "checkpoint": ["--checkpoint", "WD-DA-TOPIC-001"],
        "profile": ["--profile", "da:test-workday-topics", "--validation-realm", "dev"],
    }[selection]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        if selection == "full":
            _register_maker_baseline(http)
        payload, code = _run_cli(ws, monkeypatch, http, options)
        if selection != "full":
            assert not any(
                value in call.request.url
                for call in http.calls for value in ("crm.dynamics", "api.bap", "api.flow", "graph.microsoft")
            )
    row = _row(payload, "WD-DA-TOPIC-001")
    assert code == 1
    assert row["status"] == "Failed", row["result"]
    assert schema in row["result"]
    assert ("missing required" if state == "missing" else "inactive required") in row["result"]
    assert "/connect workday" in row["remediation"]
    assert row["doc_link"].endswith("declarative-agent-workday-reference#system-topics")


@pytest.mark.parametrize("key", ["SAP-001", "DA-SAP-RUNTIME-001"])
def test_excluded_assertions_explain_missing_coverage_without_running_old_probes(
    setup_workspace, monkeypatch, key,
):
    with responses.RequestsMock() as http:
        payload, code = _run_cli(setup_workspace, monkeypatch, http, ["--checkpoint", key])
        assert not http.calls
    row = _row(payload, key)
    assert code == 0
    assert row["status"] == "NotConfigured"
    assert "implementation/scope exclusion" in row["result"]
    assert row["evidence"]["disposition"] == "excluded"


@pytest.mark.parametrize("continuation", [
    "https://other.example.test/v1.0/subscribedSkus",
    "https://graph.microsoft.com/v1.0/users",
    "https://graph.microsoft.com/v1.0/subscribedSkus",
])
def test_strict_graph_pagination_rejects_foreign_wrong_resource_and_cycles(continuation):
    from flightcheck.graph_client import GraphClient

    client = GraphClient(TENANT_ID, strict_reads=True)
    client._token = "read-token"
    with responses.RequestsMock() as http:
        http.get(
            f"{graph_mock.GRAPH_BASE}/subscribedSkus",
            json={"value": [], "@odata.nextLink": continuation},
        )
        with pytest.raises(ValueError, match="continuation"):
            client.get_subscribed_skus()
        assert len(http.calls) == 1


@pytest.mark.parametrize("required,assigned,expected", [
    (True, True, "Passed"), (True, False, "Failed"), (False, False, "Passed"),
])
def test_workday_assignment_reads_only_the_configured_application(
    setup_workspace, monkeypatch, required, assigned, expected,
):
    ws = setup_workspace
    require_validated_mock(graph_mock)
    application = graph_mock.application()
    principal = graph_mock.service_principal(app_role_assignment_required=required)
    ws.config["entraAppId"] = application["appId"]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add(**graph_mock.list_applications(applications=[application]))
        http.add(**graph_mock.list_service_principals(service_principals=[principal]))
        http.add(**graph_mock.list_app_role_assignments(
            sp_id=principal["id"], assignments=[graph_mock.app_role_assignment()] if assigned else [],
        ))
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", "WD-ASSIGN-001"])
        assert not any(value in call.request.url for value in ("api.bap", "api.flow", "api.powerapps") for call in http.calls)
        assert not any("applicationTemplates" in call.request.url for call in http.calls)
    row = _row(payload, "WD-ASSIGN-001")
    assert row["status"] == expected
    assert f"assignment required={required}" in row["result"]
    assert ("assign the approved user/group" in row["remediation"]) is (expected == "Failed")
    assert code == (expected == "Failed")


@pytest.mark.parametrize("missing", ["none", "scope_authorization", "graph_permission"])
def test_workday_scope_requires_authorization_for_the_actual_exposed_scope(
    setup_workspace, monkeypatch, missing,
):
    ws = setup_workspace
    app = graph_mock.application(graph_permissions=missing != "graph_permission")
    if missing == "scope_authorization":
        app["api"]["preAuthorizedApplications"][0]["delegatedPermissionIds"] = []
    ws.config["entraAppId"] = app["appId"]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add(**graph_mock.list_applications(applications=[app]))
        http.add(**graph_mock.list_service_principals())
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", "WD-ENTRA-SCOPE-001"])
    row = _row(payload, "WD-ENTRA-SCOPE-001")
    assert row["status"] == ("Passed" if missing == "none" else "Failed")
    assert "Enabled user_impersonation" in row["result"]
    assert code == (missing != "none")


def test_wrong_workday_application_response_never_becomes_a_sibling_fallback(setup_workspace, monkeypatch):
    ws = setup_workspace
    ws.config["entraAppId"] = graph_mock.MOCK_WORKDAY_APP_ID
    sibling = graph_mock.service_principal(app_id="00000000-0000-4000-8000-00000000abcd")
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add(**graph_mock.list_applications())
        http.add(**graph_mock.list_service_principals(service_principals=[sibling]))
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", "WD-ASSIGN-001"])
        assert not any("appRoleAssignedTo" in call.request.url for call in http.calls)
    assert code == 1
    assert _row(payload, "WD-ASSIGN-001")["status"] == "Error"
    assert "exact application" in _row(payload, "WD-ASSIGN-001")["result"]


@pytest.mark.parametrize("value", ["Active", "Inactive", "unknown", "absent"])
def test_full_optional_servicenow_accounts_for_every_child_without_legacy_flows(
    setup_workspace, monkeypatch, value,
):
    ws = setup_workspace
    payload = _body(_capture("env004_dependencies.yaml")[1])
    topic = deepcopy(next(item for item in payload["botComponentChanges"] if "shared_service-now" in json.dumps(item)))
    topic["component"]["status"] = value
    if value != "absent":
        ws.content["botComponentChanges"].append(topic)
        ws.content["connectionReferenceChanges"] = payload["connectionReferenceChanges"]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        _register_maker_baseline(http)
        result, code = _run_cli(ws, monkeypatch, http, ["--scope", "full"])
        assert not any("/flows/" in call.request.url for call in http.calls)
    assert not any(row["checkpoint_id"].startswith("SN-CFG-") for row in result["results"])
    if value == "Active":
        assert _row(result, "SN-CONN-001")["status"] == "Failed"
        assert _row(result, "SN-RUN-001")["evidence"]["applicability"] == "not_applicable"
        assert _row(result, "SN-FLOW-001")["evidence"]["applicability"] == "not_applicable"
        assert code == 1
    elif value in {"Inactive", "absent"}:
        assert _row(result, "SN-CONN-001")["evidence"]["applicability"] == "not_applicable"
        assert code == 0
    else:
        assert _row(result, "SN-CONN-001")["status"] == "Manual"
        assert _row(result, "SN-CONN-001")["evidence"]["applicability"] == "unknown"


@pytest.mark.parametrize("allocated,http_status,expected", [
    (100, 200, "Passed"), (0, 200, "Manual"), (100, 403, "Manual"), (100, 503, "Error"),
])
def test_capacity_uses_actual_allocation_without_turning_billing_alternatives_into_defects(
    setup_workspace, monkeypatch, allocated, http_status, expected,
):
    require_validated_mock(platform_mock)
    ws = setup_workspace
    entitlement = platform_mock.environment_entitlement(allocated=allocated)
    entitlement["environmentId"] = ws.config["environmentId"]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add(**platform_mock.get_environment_entitlements(
            environment_id=ws.config["environmentId"], entitlements=[entitlement], status=http_status,
        ))
        payload, _ = _run_cli(ws, monkeypatch, http, ["--checkpoint", "ENV-CAPACITY-001"])
    row = _row(payload, "ENV-CAPACITY-001")
    assert row["status"] == expected
    if http_status == 200:
        assert f"{allocated}" in row["result"]
        assert "audience or usage sufficiency" in row["result"]
    assert bool(row["remediation"]) is (expected != "Passed")


@pytest.mark.parametrize("state,budget_status,expected", [
    ("Enabled", 200, "Passed"), ("Disabled", 403, "Failed"),
    ("Enabled", 403, "Manual"), ("Enabled", 401, "Error"),
])
def test_billing_scopes_links_and_does_not_hide_unhealthy_subscription_behind_budget_denial(
    setup_workspace, monkeypatch, state, budget_status, expected,
):
    require_validated_mock(platform_mock)
    require_validated_mock(arm_mock)
    ws = setup_workspace
    policy = platform_mock.billing_policy()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add(**platform_mock.list_billing_policies(policies=[policy]))
        http.add(**platform_mock.list_policy_environments(environments=[
            platform_mock.billing_policy_environment(environment_id=ws.config["environmentId"]),
        ]))
        http.add(**arm_mock.get_subscription(state=state))
        if state != "Disabled":
            http.add(**arm_mock.list_budgets(budgets=[arm_mock.budget()], status=budget_status))
        payload, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", "PRE-005"])
        if state == "Disabled":
            assert not any("/budgets" in call.request.url for call in http.calls)
    row = _row(payload, "PRE-005")
    assert row["status"] == expected
    assert code == (expected in {"Failed", "Error"})
    if expected == "Passed":
        assert "Enabled subscriptions" in row["result"]
        assert row["remediation"] == ""
    elif expected == "Failed":
        assert "unhealthy subscription" in row["result"]
        assert row["remediation"]
    elif expected == "Manual":
        assert row["evidence"]["subscriptions"][0]["state"] == "Enabled"
        assert "denied" in row["result"]
        assert row["evidence"]["manualVerificationSteps"]


@pytest.mark.parametrize("provider", ["workday", "servicenow"])
@pytest.mark.parametrize("sample,expected", [
    ("success", "Passed"), ("failure", "Failed"), ("stale", "Manual"),
    ("child", "Manual"), ("future", "Error"), ("naive", "Error"),
])
def test_passive_history_checks_only_exact_provider_flows_and_validated_recent_samples(provider, sample, expected):
    from flightcheck.pp_admin_client import PPAdminClient

    require_validated_mock(admin_mock)
    flow_id = "00000000-0000-4000-8000-000000007101"
    key = "WD-RUN-001" if provider == "workday" else "SN-RUN-001"
    connector = "shared_workdaysoap" if provider == "workday" else "shared_service-now"
    principal_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    if sample == "stale":
        principal_time -= timedelta(hours=2)
    if sample == "future":
        principal_time += timedelta(hours=2)
    timestamp = principal_time.isoformat()
    if sample == "naive":
        timestamp = principal_time.replace(tzinfo=None).isoformat()
    response_name = "Respond_to_Copilot_with_Success" if provider == "workday" else "Respond_to_Copilot"
    if sample == "child":
        response_name = ""
    run = admin_mock.flow_run(
        env_id=admin_mock.MOCK_ENV_ID, flow_id=flow_id, start_time=timestamp,
        status="Failed" if sample == "failure" else "Succeeded", response_name=response_name,
    )
    client = PPAdminClient(TENANT_ID)
    client.set_read_tokens(flow_token="fixture-read-token")
    runner = SimpleNamespace(
        agent_slug="hr", env_id=admin_mock.MOCK_ENV_ID, tenant_id=TENANT_ID,
        pp_admin=client, scope="checkpoint", da_explicit_selection=True,
        _da_connection_audit=SimpleNamespace(references=[
            SimpleNamespace(connector_id=connector, flow_ids=[flow_id]),
            SimpleNamespace(connector_id="shared_unrelated", flow_ids=["unrelated-flow"]),
        ]),
    )
    executor = DAExecutor(runner, SimpleNamespace(), (key,), (registry.REGISTRY[key],), {})
    executor.groups["connections"] = []  # The exact dependency audit above is the producer's pure input.
    runner.da_executor = executor
    with responses.RequestsMock() as http:
        http.add(**admin_mock.list_flow_runs(flow_id=flow_id, runs=[run]))
        row = executor.run(runner)[0]
        assert len(http.calls) == 1
        assert flow_id in http.calls[0].request.url
    assert row.status == expected
    if expected in {"Passed", "Failed"}:
        assert "scoreable recent exact-flow" in row.result
        assert "not a current intended-employee smoke test" in row.result
    elif expected == "Manual":
        assert "No scoreable" in row.result
        assert row.evidence["windowMinutes"] == 60
    else:
        assert "timestamp" in row.result
    assert bool(row.remediation) is (expected != "Passed")


@pytest.mark.parametrize("surface", ["powerplatform", "arm"])
@pytest.mark.parametrize("bad", ["shape", "empty", "foreign", "wrong_resource", "conflicting", "cycle"])
def test_strict_billing_collection_never_hides_invalid_or_unsafe_continuation(surface, bad):
    from flightcheck.azure_arm_client import AzureArmClient
    from flightcheck.powerplatform_client import PowerPlatformClient

    if surface == "powerplatform":
        require_validated_mock(platform_mock)
        client = PowerPlatformClient(TENANT_ID, strict_reads=True)
        fixture = platform_mock.list_billing_policies()
        read = client.list_billing_policies
        continuation_key = "@odata.nextLink"
    else:
        require_validated_mock(arm_mock)
        client = AzureArmClient(TENANT_ID, strict_reads=True)
        fixture = arm_mock.list_budgets()
        def read():
            return client.list_budgets(arm_mock.MOCK_SUBSCRIPTION_ID)
        continuation_key = "nextLink"
    client._token = "fixture-read-token"
    body = fixture["json"]
    if bad == "shape":
        body = {"unexpected": []}
    else:
        body[continuation_key] = {
            "empty": "", "foreign": "https://example.invalid/collection",
            "wrong_resource": fixture["url"] + "/other", "conflicting": fixture["url"] + "?page=2",
            "cycle": fixture["url"],
        }[bad]
        if bad == "conflicting":
            body["nextLink" if surface == "powerplatform" else "@odata.nextLink"] = fixture["url"] + "?page=3"
    fixture["json"] = body
    with responses.RequestsMock() as http:
        http.add(**fixture)
        with pytest.raises(ValueError, match="collection|continuation"):
            read()
        assert len(http.calls) <= 2


@pytest.mark.parametrize("selection", ["checkpoint", "profile"])
@pytest.mark.parametrize("installed", [True, False])
def test_workday_package_inspection_is_available_before_topics_are_activated(
    setup_workspace, monkeypatch, selection, installed,
):
    from flightcheck.checks import _connection_dependencies

    ws = setup_workspace
    require_validated_mock(dataverse_mock)
    monkeypatch.setattr(_connection_dependencies, "discover_tenant", lambda _: TENANT_ID)
    environment = _body(next(
        item for item in _capture("flightcheck_pp_admin.yaml")
        if "api.bap.microsoft.com" in item["request"]["uri"] and "/environments/" in item["request"]["uri"]
    ))
    env_url = environment["properties"]["linkedEnvironmentMetadata"]["instanceUrl"]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add(**dataverse_mock.query(
            base_url=env_url, entity_set="solutions",
            records=[{
                "solutionid": "00000000-0000-4000-8000-000000008801",
                "uniquename": "msdyn_EssWorkdayRuntime", "friendlyname": "Workday runtime",
                "ismanaged": True, "version": "2.1.0.0",
            }] if installed else [],
        ))
        options = ["--checkpoint", "WD-DA-PKG-001"] if selection == "checkpoint" else [
            "--profile", "workday-da:package-ready", "--validation-realm", "dev",
        ]
        payload, code = _run_cli(ws, monkeypatch, http, options)
        assert not any(value in call.request.url for call in http.calls for value in ("api.flow", "graph.microsoft", "connectivity/"))
    row = _row(payload, "WD-DA-PKG-001")
    assert row["status"] == ("Passed" if installed else "Failed")
    assert ("msdyn_EssWorkdayRuntime" if installed else "not installed") in row["result"]
    assert bool(row["remediation"]) is not installed
    assert code == (not installed)


@pytest.mark.parametrize("provider", ["WD", "SN"])
def test_selective_integration_uses_real_mixed_dependency_capture_without_other_provider_binding_reads(
    setup_workspace, monkeypatch, provider,
):
    from flightcheck.checks import _connection_dependencies
    from urllib.parse import urlsplit

    ws = setup_workspace
    interactions = _capture("env004_dependencies.yaml")
    content = _body(interactions[1])
    service_connection = "00000000-0000-4000-8000-000000008803"
    if provider == "SN":
        for change in content["botComponentChanges"]:
            if "shared_service-now" in json.dumps(change):
                change["component"]["status"] = "Active"
        content["connectionReferenceChanges"][0]["connectionReference"]["connectionId"] = service_connection
    ws.content["botComponentChanges"].extend(content["botComponentChanges"])
    ws.content["connectionReferenceChanges"] = content["connectionReferenceChanges"]
    captured_env = next(
        item["request"]["uri"].split("/environments/")[1].split("?")[0]
        for item in interactions
        if "api.bap.microsoft.com" in item["request"]["uri"]
    )
    monkeypatch.setattr(_connection_dependencies, "discover_tenant", lambda _: TENANT_ID)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        for item in interactions:
            uri = item["request"]["uri"]
            if "minimalBots" in uri or "api.bap.microsoft" in uri:
                continue
            uri = urlsplit(uri.replace(captured_env, ws.config["environmentId"]))._replace(query="").geturl()
            body = _body(item)
            if provider == "SN" and uri.endswith("/connections"):
                original = next(row for row in body["value"] if "shared_workdaysoap" in row["id"])
                reference = deepcopy(original)
                reference["id"] = original["id"].replace("shared_workdaysoap", "shared_service-now").rsplit("/", 1)[0] + "/" + service_connection
                reference["name"] = service_connection
                reference["properties"]["apiId"] = original["properties"]["apiId"].replace("shared_workdaysoap", "shared_service-now")
                body["value"].append(reference)
            http.add(item["request"]["method"], uri, json=body)
        result, code = _run_cli(ws, monkeypatch, http, ["--checkpoint", f"{provider}-CONN-001"])
        assert not any("graph.microsoft.com" in call.request.url for call in http.calls)
        if provider == "SN":
            assert not any("/connectionreferences" in call.request.url for call in http.calls)
    row = _row(result, f"{provider}-CONN-001")
    assert row["status"] == "Passed", row["result"]
    assert "Exact bindings were verified" in row["result"]
    expected_connector = "shared_workdaysoap" if provider == "WD" else "shared_service-now"
    assert all(ref["connector_id"].endswith(expected_connector) for ref in row["evidence"]["references"])
    assert not row["remediation"]
    assert code == 0
    assert not any(row["checkpoint_id"].startswith("SN-" if provider == "WD" else "WD-") for row in result["results"])


@pytest.mark.parametrize("grant_target,expected", [
    ("graph", "Passed"), ("other_resource", "Failed"), ("other_client", "Failed"), ("user", "Failed"),
])
def test_workday_consent_is_bound_to_exact_client_graph_resource_and_tenant_wide_grant(
    setup_workspace, monkeypatch, grant_target, expected,
):
    ws = setup_workspace
    ws.config["entraAppId"] = graph_mock.MOCK_WORKDAY_APP_ID
    graph_sp_id = "00000000-0000-4000-8000-000000008802"
    graph_sp = graph_mock.service_principal(
        sp_id=graph_sp_id, app_id="00000003-0000-0000-c000-000000000000",
    )
    grant = graph_mock.oauth2_permission_grant(
        resource_id=graph_sp_id if grant_target != "other_resource" else "other-resource",
        client_id=graph_mock.MOCK_WORKDAY_SP_ID if grant_target != "other_client" else "other-client",
        consent_type="Principal" if grant_target == "user" else "AllPrincipals",
    )
    with responses.RequestsMock(assert_all_requests_are_fired=False) as http:
        _register_native(http, ws)
        http.add(**graph_mock.list_applications())
        http.add(**graph_mock.list_service_principals())
        http.add(**graph_mock.list_service_principals(service_principals=[graph_sp]))
        http.add(**graph_mock.list_oauth2_permission_grants(grants=[grant]))
        payload, _ = _run_cli(ws, monkeypatch, http, ["--checkpoint", "WD-ENTRA-CONSENT-001"])
    row = _row(payload, "WD-ENTRA-CONSENT-001")
    assert row["status"] == expected, row["result"]
    assert "Grants to other resources were not counted" in row["result"]
    assert bool(row["remediation"]) is (expected == "Failed")


@pytest.mark.parametrize("status,kind", [(403, "permission_denied"), (503, "error")])
def test_inactive_flow_discovery_retains_permission_vs_service_error(status, kind):
    from flightcheck.checks._connection_dependencies import ConnectionAudit
    from flightcheck.checks.maker_integrations import run_optional_integration_checks
    from flightcheck.pp_admin_client import PPAdminClient

    content = _body(_capture("env004_dependencies.yaml")[1])
    topic = next(change for change in content["botComponentChanges"] if "InvokeFlowAction" in json.dumps(change))
    topic["component"]["status"] = "Inactive"
    content["botComponentChanges"] = [topic]
    content["bot"] = {"schemaName": "gptagent_copilotforemployeeselfservicehr"}
    client = PPAdminClient(TENANT_ID)
    client.set_read_tokens(flow_token="fixture-read-token")
    runner = SimpleNamespace(
        _da_native_snapshot={"changeset": content}, _da_connection_audit=ConnectionAudit(),
        pp_admin=client, env_id=admin_mock.MOCK_ENV_ID,
    )
    from flightcheck.checks._connection_dependencies import _nodes
    flow_id = next(node["flowId"] for node in _nodes(topic) if node.get("$kind") == "InvokeFlowAction")
    require_validated_mock(admin_mock)
    uri = next(
        item["request"]["uri"] for item in _capture("env004_dependencies.yaml")
        if f"/flows/{flow_id}?" in item["request"]["uri"]
    )
    captured_env = uri.split("/environments/")[1].split("/")[0]
    uri = uri.replace(captured_env, runner.env_id).split("?")[0]
    with responses.RequestsMock() as http:
        http.add("GET", uri, status=status, json={})
        rows = run_optional_integration_checks(runner)
    assert all(row.status == "Blocked" for row in rows)
    assert runner._da_connection_audit.read_limitations[0]["kind"] == kind
    assert flow_id in runner._da_connection_audit.read_limitations[0]["context"]


@pytest.mark.parametrize("mode,expected", [
    ("allow", "Passed"), ("blocked", "Failed"), ("blocked_unknown", "Failed"),
    ("denied", "Manual"), ("malformed", "Error"),
])
def test_dlp_wrapper_evaluates_exact_connectors_without_hiding_policy_read_limitations(mode, expected):
    from flightcheck.pp_admin_client import PPAdminClient

    require_validated_mock(admin_mock)
    connector = "/providers/Microsoft.PowerApps/apis/shared_workdaysoap"
    policy = admin_mock.dlp_policy_modern(
        business=[connector] if mode == "allow" else [],
        blocked=[connector] if mode == "blocked" else [],
    )
    if mode == "blocked_unknown":
        policy = admin_mock.dlp_policy(blocked=[connector])
    client = PPAdminClient(TENANT_ID)
    client.set_read_tokens(powerapps_token="fixture-read-token")
    runner = SimpleNamespace(
        agent_slug="hr", env_id=admin_mock.MOCK_ENV_ID, pp_admin=client,
        da_observations={}, _da_connection_audit=SimpleNamespace(references=[
            SimpleNamespace(connector_id=connector),
        ]),
    )
    if mode == "blocked_unknown":
        runner._da_connection_audit.references.append(SimpleNamespace(connector_id="shared_service-now"))
    key = "INFRA-006"
    executor = DAExecutor(runner, SimpleNamespace(), (key,), (registry.REGISTRY[key],), {})
    runner.da_executor = executor
    uri = next(
        item["request"]["uri"] for item in _capture("flightcheck_pp_admin.yaml")
        if "/apiPolicies" in item["request"]["uri"]
    )
    with responses.RequestsMock() as http:
        http.add(
            "GET", uri, status=403 if mode == "denied" else 200,
            json={} if mode == "denied" else {"value": [{"properties": None}] if mode == "malformed" else [policy]},
        )
        row = executor.run(runner)[0]
    assert row.status == expected, row.result
    assert bool(row.remediation) is (expected != "Passed")
    if mode in {"allow", "blocked", "blocked_unknown"}:
        assert "Selected connector classification" in row.result
        assert bool(row.evidence["blocked"]) is (mode != "allow")
    if mode == "denied":
        assert "denied" in row.result
        assert "Data policies" in row.remediation


@pytest.mark.parametrize("dependency", ["missing", "cycle"])
def test_da_registry_rejects_invalid_prerequisite_graph_before_execution(monkeypatch, dependency):
    from dataclasses import replace

    original = registry.REGISTRY["PRE-001"]
    bad = "NOT-REGISTERED" if dependency == "missing" else original.key
    monkeypatch.setitem(registry.REGISTRY, original.key, replace(
        original, da=replace(original.da, prerequisites=(bad,)),
    ))
    with pytest.raises(ValueError, match="Missing DA|Cyclic DA"):
        plan_da_checks(checkpoint=original.key)
