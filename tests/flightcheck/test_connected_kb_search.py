# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Pure-logic tests for connected knowledge-base search normalization."""

from __future__ import annotations

import pytest

from flightcheck.connected_kb_search import (
    ConnectionClassification,
    ServiceNowIdentifier,
    classify_graph_connection,
    extract_servicenow_identifier,
    normalize_external_item_hits,
)
from tests.mocks import graph


SYS_ID = "0123456789abcdef0123456789abcdef"


def test_normalize_search_hits_preserves_ranked_metadata() -> None:
    payload = graph.external_item_search_response(
        hits=[
            graph.search_hit(
                hit_id="opaque-hit",
                rank=1,
                summary="Leave policy summary",
                resource={
                    "id": "external-item",
                    "properties": {
                        "title": "Parental leave",
                        "sys_id": SYS_ID.upper(),
                        "url": (
                            "https://example.service-now.com/kb"
                            f"?id=kb_article&sys_id={SYS_ID}"
                        ),
                        "category": "Benefits",
                    },
                },
            )
        ]
    )

    hits = normalize_external_item_hits(payload)

    assert len(hits) == 1
    assert hits[0].rank == 1
    assert hits[0].hit_id == "opaque-hit"
    assert hits[0].title == "Parental leave"
    assert hits[0].summary == "Leave policy summary"
    assert hits[0].source_url.endswith(f"sys_id={SYS_ID}")
    assert hits[0].properties["category"] == "Benefits"
    assert hits[0].service_now_identifier == ServiceNowIdentifier(
        kind="sys_id",
        value=SYS_ID,
    )
    assert hits[0].skip_reason is None


@pytest.mark.parametrize(
    "resource",
    [
        {"SYS_ID": SYS_ID.upper()},
        {"properties": {"sysId": SYS_ID.upper()}},
    ],
)
def test_extracts_sys_id_from_recognized_root_or_property_alias(
    resource: dict[str, object],
) -> None:
    assert extract_servicenow_identifier(resource) == ServiceNowIdentifier(
        "sys_id", SYS_ID
    )


def test_extracts_and_normalizes_valid_article_number() -> None:
    resource = {"properties": {"articleNumber": " kb0012345 "}}

    assert extract_servicenow_identifier(resource) == ServiceNowIdentifier(
        "number", "KB0012345"
    )


def test_prefers_explicit_sys_id_over_article_number() -> None:
    resource = {
        "number": "KB0012345",
        "properties": {"sys_id": SYS_ID},
    }

    assert extract_servicenow_identifier(resource) == ServiceNowIdentifier(
        "sys_id", SYS_ID
    )


def test_extracts_sys_id_from_servicenow_source_url() -> None:
    resource = {
        "properties": {
            "sourceUrl": (
                "https://example.service-now.com/now/nav/ui/classic/params/"
                f"target/kb_knowledge.do%3Fsys_id%3D{SYS_ID.upper()}"
            )
        }
    }

    assert extract_servicenow_identifier(resource) == ServiceNowIdentifier(
        "sys_id", SYS_ID
    )


def test_extracts_article_number_from_recognized_servicenow_url() -> None:
    resource = {
        "webUrl": "https://example.servicenow.com/kb_view.do?sysparm_article=kb0012345"
    }

    assert extract_servicenow_identifier(resource) == ServiceNowIdentifier(
        "number", "KB0012345"
    )


@pytest.mark.parametrize(
    "resource",
    [
        {"sys_id": "not-a-sys-id"},
        {"sys_id": SYS_ID[:-1]},
        {"sys_id": f"{SYS_ID}0"},
        {"sys_id": "g" * 32},
        {"properties": {"sysId": 123}},
    ],
)
def test_rejects_malformed_sys_id(resource: dict[str, object]) -> None:
    assert extract_servicenow_identifier(resource) is None


def test_does_not_derive_identifier_from_opaque_hit_id_title_or_summary() -> None:
    payload = graph.external_item_search_response(
        hits=[
            graph.search_hit(
                hit_id=SYS_ID,
                summary="Read KB0012345 for details",
                resource={
                    "properties": {
                        "title": "KB0012345",
                        "url": "https://example.invalid/articles/KB0012345",
                    }
                },
            )
        ]
    )

    hit = normalize_external_item_hits(payload)[0]

    assert hit.service_now_identifier is None
    assert hit.skip_reason == "No verified ServiceNow identifier found."


def test_deduplicates_by_canonical_identifier_preserving_first_ranked_hit() -> None:
    payload = graph.external_item_search_response(
        hits=[
            graph.search_hit(
                hit_id="first",
                rank=1,
                resource={"properties": {"title": "First", "sys_id": SYS_ID}},
            ),
            graph.search_hit(
                hit_id="duplicate",
                rank=2,
                resource={
                    "properties": {
                        "title": "Duplicate",
                        "sysId": SYS_ID.upper(),
                    }
                },
            ),
            graph.search_hit(
                hit_id="distinct",
                rank=3,
                resource={
                    "properties": {
                        "title": "Distinct",
                        "number": "KB0012345",
                    }
                },
            ),
        ]
    )

    hits = normalize_external_item_hits(payload)

    assert [hit.hit_id for hit in hits] == ["first", "distinct"]
    assert [hit.rank for hit in hits] == [1, 3]


def test_missing_identifier_remains_with_stable_skip_reason() -> None:
    payload = graph.external_item_search_response(
        hits=[
            graph.search_hit(
                hit_id="opaque",
                resource={"properties": {"title": "Unmapped article"}},
            )
        ]
    )

    hit = normalize_external_item_hits(payload)[0]

    assert hit.service_now_identifier is None
    assert hit.skip_reason == "No verified ServiceNow identifier found."


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"value": None},
        {"value": {}},
        {"value": [None, {"hitsContainers": None}]},
        {"value": [{"hitsContainers": [{}, {"hits": "not-a-list"}]}]},
    ],
)
def test_malformed_or_missing_response_containers_return_no_hits(
    payload: object,
) -> None:
    assert normalize_external_item_hits(payload) == []


def test_malformed_hit_rows_are_ignored_without_guessing() -> None:
    payload = {
        "value": [
            {
                "hitsContainers": [
                    {
                        "hits": [
                            None,
                            "not-a-hit",
                            {"hitId": "opaque", "rank": "first", "resource": None},
                        ]
                    }
                ]
            }
        ]
    }

    hits = normalize_external_item_hits(payload)

    assert len(hits) == 1
    assert hits[0].rank == 0
    assert hits[0].hit_id == "opaque"
    assert hits[0].service_now_identifier is None


def test_classifies_verified_servicenow_connector_metadata() -> None:
    classification = classify_graph_connection(
        graph.external_connection(
            connector_id="serviceNowKnowledge",
            name="HR knowledge",
            description="Employee articles",
        )
    )

    assert classification == ConnectionClassification(
        kind="servicenow",
        evidence=("connectorId=serviceNowKnowledge",),
    )


def test_classifies_clear_explicit_non_servicenow_connector_metadata() -> None:
    classification = classify_graph_connection(
        graph.external_connection(
            connector_id="salesforce",
            name="CRM knowledge",
            description="Customer articles",
        )
    )

    assert classification == ConnectionClassification(
        kind="other",
        evidence=("connectorId=salesforce",),
    )


@pytest.mark.parametrize(
    ("name", "description", "expected_evidence"),
    [
        ("ServiceNow HR knowledge", "Employee articles", ("name mentions ServiceNow",)),
        (
            "HR knowledge",
            "Synced from Service Now",
            ("description mentions ServiceNow",),
        ),
    ],
)
def test_name_or_description_hints_alone_are_ambiguous(
    name: str,
    description: str,
    expected_evidence: tuple[str, ...],
) -> None:
    classification = classify_graph_connection(
        graph.external_connection(
            connector_id=None,
            name=name,
            description=description,
        )
    )

    assert classification == ConnectionClassification(
        kind="ambiguous",
        evidence=expected_evidence,
    )


def test_connection_with_no_classification_evidence_is_ambiguous() -> None:
    assert classify_graph_connection({}) == ConnectionClassification(
        kind="ambiguous",
        evidence=(),
    )
