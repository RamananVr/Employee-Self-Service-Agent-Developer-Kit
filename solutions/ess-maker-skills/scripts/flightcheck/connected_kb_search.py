# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Pure normalization helpers for connected knowledge-base search results."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping
from urllib.parse import unquote, urlsplit

_SYS_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
_ARTICLE_NUMBER_PATTERN = re.compile(r"^KB[0-9]{7}$", re.IGNORECASE)
_SYS_ID_IN_URL_PATTERN = re.compile(r"(?:^|[?&/=_-])([0-9a-fA-F]{32})(?=$|[?&#/=_-])")
_ARTICLE_NUMBER_IN_URL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])KB[0-9]{7}(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_SYS_ID_KEYS = frozenset({"sys_id", "sysid"})
_ARTICLE_NUMBER_KEYS = frozenset(
    {"number", "article_number", "articlenumber", "kb_number", "kbnumber"}
)
_URL_KEYS = frozenset({"url", "source_url", "sourceurl", "web_url", "weburl"})
_SERVICENOW_CONNECTOR_ID = (
    "/providers/Microsoft.PowerApps/apis/shared_service-now".casefold()
)
_MISSING_IDENTIFIER_REASON = "No verified ServiceNow identifier found."


@dataclass(frozen=True)
class ServiceNowIdentifier:
    kind: Literal["sys_id", "number"]
    value: str


@dataclass(frozen=True)
class RankedExternalItem:
    rank: int
    hit_id: str
    title: str
    summary: str
    source_url: str
    properties: dict[str, Any]
    service_now_identifier: ServiceNowIdentifier | None
    skip_reason: str | None


@dataclass(frozen=True)
class ConnectionClassification:
    kind: Literal["servicenow", "other", "ambiguous"]
    evidence: tuple[str, ...]


def normalize_external_item_hits(payload: Any) -> list[RankedExternalItem]:
    """Normalize documented Microsoft Search external-item hit containers."""
    if not isinstance(payload, Mapping):
        return []

    value = payload.get("value")
    if not isinstance(value, list):
        return []

    normalized: list[RankedExternalItem] = []
    seen_identifiers: set[tuple[str, str]] = set()

    for response in value:
        if not isinstance(response, Mapping):
            continue
        containers = response.get("hitsContainers")
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, Mapping):
                continue
            hits = container.get("hits")
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if not isinstance(hit, Mapping):
                    continue
                item = _normalize_hit(hit)
                identifier = item.service_now_identifier
                if identifier is not None:
                    canonical = (identifier.kind, identifier.value)
                    if canonical in seen_identifiers:
                        continue
                    seen_identifiers.add(canonical)
                normalized.append(item)

    return normalized


def extract_servicenow_identifier(resource: Any) -> ServiceNowIdentifier | None:
    """Extract a conservative ServiceNow identifier from recognized fields."""
    if not isinstance(resource, Mapping):
        return None

    properties = resource.get("properties")
    mappings = [
        resource,
        properties if isinstance(properties, Mapping) else {},
    ]

    for mapping in mappings:
        candidate = _first_alias_value(mapping, _SYS_ID_KEYS)
        identifier = _normalize_sys_id(candidate)
        if identifier is not None:
            return identifier

    for mapping in mappings:
        candidate = _first_alias_value(mapping, _ARTICLE_NUMBER_KEYS)
        identifier = _normalize_article_number(candidate)
        if identifier is not None:
            return identifier

    for mapping in mappings:
        for source_url in _alias_values(mapping, _URL_KEYS):
            identifier = _identifier_from_servicenow_url(source_url)
            if identifier is not None:
                return identifier

    return None


def classify_graph_connection(connection: Any) -> ConnectionClassification:
    """Classify a Graph connection using explicit metadata, never loose text."""
    if not isinstance(connection, Mapping):
        return ConnectionClassification("ambiguous", ())

    connector_id = connection.get("connectorId")
    if isinstance(connector_id, str) and connector_id.strip():
        normalized_connector_id = connector_id.strip()
        folded_connector_id = normalized_connector_id.casefold()
        evidence = (f"connectorId={normalized_connector_id}",)
        if folded_connector_id == _SERVICENOW_CONNECTOR_ID:
            return ConnectionClassification("servicenow", evidence)
        if "servicenow" not in folded_connector_id:
            return ConnectionClassification("other", evidence)
        return ConnectionClassification("ambiguous", evidence)

    evidence: list[str] = []
    for field, label in (
        ("name", "name mentions ServiceNow"),
        ("description", "description mentions ServiceNow"),
    ):
        value = connection.get(field)
        if isinstance(value, str) and _mentions_servicenow(value):
            evidence.append(label)

    return ConnectionClassification("ambiguous", tuple(evidence))


def _normalize_hit(hit: Mapping[str, Any]) -> RankedExternalItem:
    resource = hit.get("resource")
    resource_mapping = resource if isinstance(resource, Mapping) else {}
    properties_value = resource_mapping.get("properties")
    properties = dict(properties_value) if isinstance(properties_value, Mapping) else {}
    identifier = extract_servicenow_identifier(resource_mapping)

    rank_value = hit.get("rank")
    rank = (
        rank_value
        if isinstance(rank_value, int) and not isinstance(rank_value, bool)
        else 0
    )

    return RankedExternalItem(
        rank=rank,
        hit_id=_string_value(hit.get("hitId")),
        title=_resource_text(resource_mapping, properties, {"title"}),
        summary=_string_value(hit.get("summary")),
        source_url=_resource_text(resource_mapping, properties, _URL_KEYS),
        properties=properties,
        service_now_identifier=identifier,
        skip_reason=None if identifier is not None else _MISSING_IDENTIFIER_REASON,
    )


def _resource_text(
    resource: Mapping[str, Any],
    properties: Mapping[str, Any],
    aliases: set[str] | frozenset[str],
) -> str:
    for mapping in (resource, properties):
        value = _first_alias_value(mapping, aliases)
        if isinstance(value, str):
            return value.strip()
    return ""


def _first_alias_value(
    mapping: Mapping[str, Any],
    aliases: set[str] | frozenset[str],
) -> Any:
    return next(_alias_values(mapping, aliases), None)


def _alias_values(
    mapping: Mapping[str, Any],
    aliases: set[str] | frozenset[str],
):
    for key, value in mapping.items():
        if isinstance(key, str) and key.casefold() in aliases:
            yield value


def _normalize_sys_id(value: Any) -> ServiceNowIdentifier | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _SYS_ID_PATTERN.fullmatch(candidate):
        return None
    return ServiceNowIdentifier("sys_id", candidate.lower())


def _normalize_article_number(value: Any) -> ServiceNowIdentifier | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _ARTICLE_NUMBER_PATTERN.fullmatch(candidate):
        return None
    return ServiceNowIdentifier("number", candidate.upper())


def _identifier_from_servicenow_url(value: Any) -> ServiceNowIdentifier | None:
    if not isinstance(value, str):
        return None
    source_url = value.strip()
    if not source_url:
        return None

    try:
        hostname = urlsplit(source_url).hostname
    except ValueError:
        return None
    if not hostname or not _is_servicenow_hostname(hostname):
        return None

    decoded_url = unquote(unquote(source_url))
    sys_id_match = _SYS_ID_IN_URL_PATTERN.search(decoded_url)
    if sys_id_match:
        return ServiceNowIdentifier("sys_id", sys_id_match.group(1).lower())

    article_match = _ARTICLE_NUMBER_IN_URL_PATTERN.search(decoded_url)
    if article_match:
        return ServiceNowIdentifier("number", article_match.group(0).upper())
    return None


def _is_servicenow_hostname(hostname: str) -> bool:
    folded = hostname.casefold().rstrip(".")
    return (
        folded.endswith(".service-now.com")
        or folded.endswith(".servicenow.com")
        or folded in {"service-now.com", "servicenow.com"}
    )


def _mentions_servicenow(value: str) -> bool:
    return bool(re.search(r"\bservice[\s-]*now\b", value, re.IGNORECASE))


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
