# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Offline maker diagnostics using the existing report's visual design."""

from collections import Counter
from html import escape
import json
import re

from .runner import RunResult, _REPORT_CSS, _mask_sensitive, _render_header, _slug


_LABELS = {
    "problem": "Problem found", "unverified": "Could not verify",
    "admin": "Needs administrator review", "next": "Note to review",
    "passed": "Passed", "optional": "Optional / not enabled", "manual": "Manual verification",
}
_FILTERS = (
    ("all", "All"), ("passed", "Passed"), ("problem", "Failed"),
    ("warnings", "Warnings"), ("manual", "Manual"),
)
_TITLES = {
    "DA-AGENT-001": "Access to this agent", "DA-CONTENT-001": "Agent content",
    "ESS-SOLN-001": "Employee Self-Service identity", "ENV-004": "Connections used by this agent",
    "WD-DA-PKG-001": "Workday components", "WD-DA-FLOW-001": "Workday automated tasks",
    "WD-DA-AUTH-001": "Permission to run Workday tasks", "WD-DA-TOPIC-001": "Workday conversations",
    "WD-DA-WIRING-001": "Workday conversation setup",
}
_PURPOSES = {
    "DA-AGENT-001": "Checks that your signed-in account can read the exact selected agent in the selected environment.",
    "DA-CONTENT-001": "Checks that the selected agent's cloud-authored components can be read and belong to that agent.",
    "ESS-SOLN-001": "Checks that the selected agent has the Employee Self-Service identity.",
    "ENV-001": "Checks that the environment returned by the service matches the selected environment ID.",
    "ENV-002": "Inspects the linked Dataverse provisioning state without treating database type as proof of successful provisioning.",
    "DA-ENV-LINK-001": "Inspects the environment's declared Dataverse link; a native-only agent does not require Dataverse.",
    "ENV-003": "Reports the selected environment's type.",
    "ENV-004": "Checks that connections required by this agent's active conversations and flows are present, bound and use the expected connectors.",
    "DA-TOPIC-001": "Inspects conversation activation, distinguishing active, inactive and unknown; disabled optional conversations are not defects.",
    "DA-AUTH-001": "Inspects the agent's configured sign-in mode and trigger; it does not test an employee signing in.",
    "DA-INSTRUCTIONS-001": "Checks for readable, nonempty instructions in the cloud-authored agent; it does not assess answer quality.",
    "WD-INTEGRATION-001": "Determines whether this agent's Workday integration is enabled, disabled, configured but unused, absent or undetermined.",
    "SN-INTEGRATION-001": "Determines whether this agent's ServiceNow integration is enabled, disabled, configured but unused, absent or undetermined.",
    "WD-CONN-001": "Checks the exact connection bindings used by this agent's enabled Workday integration, not unrelated connections.",
    "SN-CONN-001": "Checks the exact connection bindings used by this agent's enabled ServiceNow integration, not unrelated connections.",
    "SN-FLOW-001": "Checks the state of ServiceNow flows this agent actually invokes; direct connector actions do not require flows.",
    "WD-DA-PKG-001": "Checks that the Microsoft-provided components required by this agent's enabled Workday integration are installed.",
    "WD-DA-FLOW-001": "Checks that the required Workday automation flows are present once and active.",
    "WD-DA-AUTH-001": "Checks that the selected agent has the required access to its Workday automation flows.",
    "WD-DA-TOPIC-001": "Inspects this agent's Microsoft-provided Workday conversations for duplicate schemas and unknown activation; unused catalog entries are not required.",
    "WD-DA-WIRING-001": "Checks that conversation startup initializes the Workday configuration before loading user context.",
    "DV-CONN-001": "Checks that Workday's runtime Dataverse connection reference is active and bound.",
    "WD-REST-002": "Checks that the selected local user-context conversation redirects to its mapped Workday conversation; local files do not prove cloud freshness.",
    "WD-DA-ATTACH-001": "Verifies in Copilot Studio that the required Workday flows are exposed to this agent and parameter sharing is enabled.",
    "CONFIG-007": "Checks that the selected agent's local instructions are present.",
    "CONFIG-005": "Inspects the starter prompts in the selected agent's local files.",
    "TOPIC-011": "Inventories the selected agent's local conversation files without imposing a minimum count.",
    "CONFIG-014": "Checks local conversation descriptions for placeholder text and parse errors, not an arbitrary word count.",
}
_SUMMARY = (
    ("passed", "Passed", "pass", "green"), ("problem", "Failed", "fail", "red"),
    ("admin", "Needs administrator review", "warn", "amber"),
    ("unverified", "Could not verify", "warn", "amber"),
    ("next", "Notes to review", "warn", "amber"), ("manual", "Manual verification", "manual", "gray"),
    ("optional", "Optional / not enabled", "na", "gray"),
)
_PILLS = {"Passed": "pass", "Failed": "fail", "Blocked": "warn", "Error": "warn",
          "Skipped": "na", "Warning": "warn", "Manual": "manual", "NotConfigured": "na"}


def _kind(row):
    if row.status == "Manual":
        return "manual"
    if row.status == "Failed":
        return "problem"
    if row.status == "Blocked" and row.evidence.get("reviewOwner") == "administrator":
        return "admin"
    if row.status == "NotConfigured" and row.evidence.get("applicability") in {
        "not_configured", "disabled", "configured", "not_applicable",
    }:
        return "optional"
    if row.status in {"Blocked", "Error", "Skipped"} or row.evidence.get("dependencyCoverageComplete") is False:
        return "unverified"
    return "passed" if row.status == "Passed" else "next"


def _customer_text(text):
    return re.sub(r"\bagent owner\b", "Environment Maker user", text, flags=re.IGNORECASE)


def _roles(row):
    return _customer_text(
        ", ".join(row.roles).replace("ESS Maker / Agent Developer", "Environment Maker user")
    ) or "Environment Maker user"


def _prose(text):
    escaped = escape(_mask_sensitive(_customer_text(text)), quote=True)
    return re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', escaped,
    ).replace("\n", "<br>")


def _category(row):
    if _kind(row) in {"manual", "admin"}:
        return "Manual verification"
    if row.checkpoint_id.startswith("WD-") or row.checkpoint_id == "DV-CONN-001":
        return "Workday"
    if row.checkpoint_id.startswith("SN-"):
        return "ServiceNow"
    if row.checkpoint_id == "ENV-004":
        return "Connections"
    return {
        "Native Agent": "Agent", "Agent setup": "Agent", "Agent configuration": "Agent",
        "Workday DA": "Workday", "Workday Extension": "Workday",
        "Local authored files": "Local files",
    }.get(row.category, row.category)


def _next_step(row):
    if row.status == "Passed":
        return "No corrective action is indicated by this check. Rerun it after changing the relevant configuration."
    if _kind(row) == "optional":
        return "No action is required unless you intend to enable this integration or use this optional configuration."
    if row.remediation:
        return row.remediation
    if row.status == "Manual":
        return "Detailed verification steps were not supplied by this check. Ask the listed owner to consult the linked setup documentation before changing settings."
    if row.status == "Failed":
        return "Ask the listed owner to inspect the finding and the linked setup documentation, repair the confirmed defect, and rerun this check."
    if row.status == "Warning":
        return "Review this recommendation with the listed owner. If it applies to your setup, follow the linked guidance and rerun."
    return "Ask the listed owner to resolve the reported access or evidence limitation and rerun. An unverified result is not a confirmed configuration defect."


def _render_finding(row, index):
    kind = _kind(row)
    filter_group = {"admin": "manual", "unverified": "warnings", "next": "warnings"}.get(kind, kind)
    manual = _category(row) == "Manual verification"
    purpose = _PURPOSES.get(row.checkpoint_id, row.description)
    owner = _roles(row)
    steps = row.evidence.get("manualVerificationSteps")
    next_html = (
        "<ol>" + "".join(f"<li>{_prose(step)}</li>" for step in steps) + "</ol>"
        if row.status == "Manual" and isinstance(steps, list) and steps and all(isinstance(step, str) for step in steps)
        else f"<p>{_prose(_next_step(row))}</p>"
    )
    missing = row.evidence.get("ifMissingOrIncorrect")
    return (
        f'<details class="finding" id="finding-{index}" data-kind="{kind}" '
        f'data-filter-group="{filter_group}" '
        f'data-recorded-status="{escape(row.status)}"{" open" if manual else ""}>'
        '<summary><span class="finding-label">'
        f'<code class="check-id">{escape(row.checkpoint_id)}</code>'
        f'<strong>{escape(_TITLES.get(row.checkpoint_id, row.description))}</strong></span>'
        f'<span class="pill {_PILLS.get(row.status, "na")}">{escape(row.status.replace("NotConfigured", "Not configured"))}</span>'
        f'<span class="finding-purpose"><b>What this checks:</b> {_prose(purpose)}</span></summary>'
        '<div class="finding-body"><div><h3>Result</h3>'
        f'<p>{_prose(row.result)}</p><h3>Who should check or fix this</h3><p>{escape(owner)}</p></div>'
        f'<div><h3>{"Verification steps" if manual else "Next step"}</h3>{next_html}'
        + (f'<h3>If missing or incorrect</h3><p>{_prose(missing)}</p>' if isinstance(missing, str) and missing else "")
        + '</div></div>'
        '<details class="support-details"><summary>Technical details</summary>'
        f'<p>Recorded status: {escape(row.status)}.</p>'
        + (f'<pre>{escape(_mask_sensitive(json.dumps(row.evidence, indent=2)))}</pre>' if row.evidence else "")
        + (f'<p><a href="{escape(row.doc_link, quote=True)}" target="_blank" rel="noopener noreferrer">Documentation</a></p>'
           if row.doc_link.startswith(("https://", "http://")) else "")
        + '</details></details>'
    )


def render_maker_report(result: RunResult) -> str:
    from .runner import maker_diagnostic_outcome

    counts = Counter(_kind(row) for row in result.results)
    outcome = maker_diagnostic_outcome(result)
    title = {
        "ISSUES_FOUND": "Setup issues found",
        "SOME_CHECKS_UNVERIFIED": "Some checks could not run",
        "CHECKED_WITH_NOTES": "Setup checks completed with notes",
        "SETUP_CHECKS_PASSED": "Setup checks passed",
    }[outcome]
    if outcome == "CHECKED_WITH_NOTES" and counts["manual"] and not counts["next"]:
        title = "Automated checks passed; manual verification remains" if counts["passed"] else "Manual verification remains"
    color = "red" if counts["problem"] else "amber" if any(counts[k] for k in ("unverified", "admin", "next", "manual")) else "green"
    ledger = '<div class="syn-stats" aria-label="Status legend and totals">' + "".join(
        f'<span class="stat {style}"><i class="dot {dot}" aria-hidden="true"></i>'
        f'<b>{counts[kind]}</b> {label}</span>'
        for kind, label, style, dot in _SUMMARY
    ) + '</div>'
    stats = (
        '<table class="status-summary"><caption>Recorded results</caption><thead><tr>'
        + "".join(
            f'<th scope="col">{label}</th>'
            for kind, label, _style, _dot in _SUMMARY
        )
        + '<th scope="col">Total</th></tr></thead><tbody><tr>'
        + "".join(f'<td data-label="{escape(label)}">{counts[kind]}</td>' for kind, label, _style, _dot in _SUMMARY)
        + f'<td data-label="Total">{len(result.results)}</td></tr></tbody></table>'
    )
    categories = {}
    actions, findings = [], {}
    for index, row in enumerate(result.results):
        kind = _kind(row)
        category = _category(row)
        categories.setdefault(category, []).append(row)
        owner = _roles(row)
        row_title = _TITLES.get(row.checkpoint_id, row.description)
        row_id = f"finding-{index}"
        finding = row.result
        brief = finding if len(finding) <= 260 else finding[:260].rsplit(" ", 1)[0] + "..."
        if kind not in {"passed", "optional", "manual", "admin"}:
            actions.append(
                f'<tr><th scope="row" data-label="Area"><a href="#{row_id}">'
                f'<code>{escape(row.checkpoint_id)}</code> {escape(row_title)}</a>'
                f'<span class="advisory">{escape(row.status)} - {_LABELS[kind]}</span>'
                f'<p class="finding-summary">{_prose(brief)}</p></th>'
                f'<td data-label="Next step">{_prose(_next_step(row))}</td>'
                f'<td data-label="Who can help">{escape(owner)}</td></tr>'
            )
        findings.setdefault(category, []).append(_render_finding(row, index))
    if "Manual verification" in categories:
        categories["Manual verification"] = categories.pop("Manual verification")
    tiles, sections = [], []
    for stage, (category, rows) in enumerate(categories.items(), 1):
        kinds = {_kind(row) for row in rows}
        category_color = "red" if "problem" in kinds else "amber" if kinds & {"unverified", "admin", "next"} else "gray" if kinds <= {"optional", "manual"} else "green"
        category_label = (
            "Problem found" if "problem" in kinds else "Could not fully verify" if kinds & {"unverified", "admin"}
            else "Manual verification" if "manual" in kinds else "Notes to review" if "next" in kinds else "Optional / not enabled" if kinds == {"optional"} else "Checks passed"
        )
        anchor = "manual-verification" if category == "Manual verification" else "cat-" + _slug(category)
        tiles.append(
            f'<a class="syn-tile {category_color}" href="#{anchor}">'
            f'<div class="syn-no">Stage {stage}</div>'
            f'<div class="syn-name">{escape(category)}</div>'
            f'<div class="syn-role">{category_label}</div></a>'
        )
        if category == "Manual verification":
            continue
        sections.append(
            f'<details class="sec category" id="{anchor}" open><summary>{escape(category)}'
            f'<span class="advisory">{len(rows)} recorded check(s)</span></summary>'
            f'<div class="sec-body">{"".join(findings[category])}</div></details>'
        )
    filters = "".join(
        f'<button type="button" data-filter="{kind}" aria-pressed="{"true" if kind == "all" else "false"}">'
        f'{label}</button>'
        for kind, label in _FILTERS
    )
    reading_guide = (
        '<section class="reading-guide" aria-label="How to read this report"><h2>How to read this report</h2><ul>'
        '<li><b>Green / Passed:</b> the stated configuration was checked. No repair is indicated for that check.</li>'
        '<li><b>Red / Failed:</b> a specific configuration defect was found. Follow its next steps and rerun.</li>'
        '<li><b>Warnings:</b> recommendations and checks that could not be verified. Read the recorded status and follow the next step.</li>'
        '<li><b>Manual:</b> the person listed must inspect the setting. Follow the steps in Manual verification.</li>'
        '<li><b>Optional integrations:</b> disabled or unconfigured items appear under All and do not require action.</li>'
        '<li><b>Check details:</b> expand a check for its result, responsible person and next steps.</li></ul>'
        + ('<p>This run checked local files only; it did not inspect deployed settings.</p>' if result.scope == "local" else "")
        + '</section>'
    )
    verdict_class = {"red": "verdict-not-ready", "amber": "verdict-warnings", "green": "verdict-ready"}[color]
    header = _render_header(
        result, verdict_class, "", title,
        "Configuration inspection only, not release approval or proof that employee tasks work.",
        reading_guide=reading_guide,
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><title>ESS maker setup checks</title>'
        f'<style>{_REPORT_CSS}{_CSS}</style></head><body><main class="wrap">'
        + header
        + '<section class="actions-panel"><div class="ap-head"><h2>Action items</h2>'
        '<span class="ap-sub">What to do next for the configuration inspected.</span></div>'
        + ('<table class="action-table"><thead><tr><th>Area</th><th>Next step</th><th>Who can help</th></tr></thead>'
           f'<tbody>{"".join(actions)}</tbody></table>' if actions else '<p>No setup issues were found in the checks performed.</p>')
        + (f'<p><a href="#manual-verification">{len(categories.get("Manual verification", []))} item(s) need a person to verify settings.</a> '
           'See Manual verification for the responsible person and detailed steps.</p>' if categories.get("Manual verification") else "")
        + '</section><section class="synopsis"><div class="syn-head"><h2>Readiness at a glance</h2>'
        '<span class="hint">Select any category to inspect its recorded findings.</span></div>'
        f'{ledger}<details class="count-details"><summary>Status totals table</summary>{stats}</details>'
        f'<div class="syn-grid">{"".join(tiles)}</div></section>'
        '<section class="checks-card"><h2>Check details</h2><div class="filters maker-filters" hidden>'
        f'{filters}<label>Search <input id="search" type="search"></label>'
        '<button type="button" id="expand-all">Expand all</button>'
        '<button type="button" id="collapse-all">Collapse all</button></div>'
        f'{"".join(sections)}<p id="no-match" hidden>No matching checks.</p></section>'
        '<section class="checks-card category manual-verification" id="manual-verification"><h2>Manual verification</h2>'
        '<p>These settings need to be checked by the person listed below.</p>'
        + "".join(findings.get("Manual verification", []))
        + ('<p>No manual verification items were identified for this run.</p>' if not categories.get("Manual verification") else "")
        + '</section>'
        '<footer><a href="results.json">Download original check results</a></footer>'
        f'</main><script>{_SCRIPT}</script></body></html>'
    )


_CSS = """
.wrap>section,.wrap>header{margin-bottom:16px}.checks-card{padding:20px}
.checks-card h2{font-size:20px;font-weight:600;margin:0 0 12px}
.report .meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:3px 24px;overflow-wrap:anywhere}
.reading-guide{border-top:1px solid var(--line);margin-top:16px;padding-top:12px;font-size:13px}
.reading-guide h2{font-size:16px;line-height:22px;margin:0 0 8px}
.reading-guide ul{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 28px;margin:0;padding-left:18px}
.reading-guide p{margin:8px 0 0}.count-details{margin-bottom:16px;font-size:13px}.count-details>summary{cursor:pointer}
.status-summary{width:100%;border-collapse:collapse;table-layout:fixed;margin-bottom:20px}
.status-summary caption{text-align:left;font-size:13px;color:var(--muted);margin-bottom:8px}
.status-summary th,.status-summary td{text-align:left;padding:8px;border-bottom:1px solid var(--line);font-size:13px;overflow-wrap:anywhere}
.status-summary th{font-weight:400;color:var(--muted)}.status-summary td{font-size:20px;font-weight:600}
.status-summary td[data-label="Passed"]{color:var(--pass)}.status-summary td[data-label="Failed"]{color:var(--fail)}
.status-summary td[data-label="Needs administrator review"],.status-summary td[data-label="Could not verify"],.status-summary td[data-label="Notes to review"]{color:var(--pill-warn)}
.status-summary td[data-label="Optional / not enabled"]{color:var(--na)}
.action-table{width:100%;border-collapse:collapse;table-layout:fixed}
.action-table th,.action-table td{padding:12px;vertical-align:top;border-bottom:1px solid var(--line);text-align:left;overflow-wrap:anywhere}
.action-table th:first-child{width:28%}.action-table th:last-child{width:23%}
.advisory{display:block;font-size:12px;color:var(--muted)}
.sec-body{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;align-items:start}
.finding{min-width:0;padding:12px;border:1px solid var(--line);border-radius:4px}
.finding>summary{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:6px 10px;list-style:none}
.finding>summary::-webkit-details-marker{display:none}
.finding-label{display:flex;flex-wrap:wrap;gap:3px 8px;min-width:0}
.check-id{font-size:12px;color:var(--muted)}.finding-label strong{font-size:14px}
.finding-purpose{grid-column:1 / -1;font-size:13px;color:var(--muted)}
.finding>summary::after{content:"Expand details";grid-column:1 / -1;color:var(--accent);font-size:12px}
.finding[open]>summary::after{content:"Collapse details"}
.finding-body h3{font-size:13px;margin:14px 0 4px}.finding-body p{font-size:13px;margin:4px 0}
.finding-body ol{font-size:13px;padding-left:20px}.finding-body li+li{margin-top:8px}
.support-details{border-top:1px solid var(--line);padding-top:10px;margin-top:12px;font-size:12px}
.manual-verification .finding+.finding{margin-top:12px}
.manual-verification .finding-body{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}
.finding-summary{font-size:13px;font-weight:400;margin:6px 0 0}
.finding summary{cursor:pointer}.finding p{overflow-wrap:anywhere}.finding pre{white-space:pre-wrap;overflow-wrap:anywhere}
.finding .pill{grid-column:2;grid-row:1}.finding-purpose,.finding-label{overflow-wrap:anywhere}
.maker-filters{display:flex;gap:8px;flex-wrap:wrap;padding:12px 0}
.maker-filters button[aria-pressed=true]{font-weight:600;color:var(--accent);border-bottom-color:var(--accent)}
.maker-filters input{max-width:100%}[hidden]{display:none!important}
@media(max-width:600px){.action-table,.action-table tbody,.action-table tr,.action-table td,.action-table tbody th{display:block;width:100%}
.report .meta,.reading-guide ul,.sec-body,.manual-verification .finding-body{grid-template-columns:minmax(0,1fr)}
.status-summary,.status-summary tbody{display:block}.status-summary thead{display:none}
.status-summary tr{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}
.status-summary td{display:block}.status-summary td::before{content:attr(data-label);display:block;font-size:12px;font-weight:400;color:var(--muted)}
.action-table thead{display:none}.action-table td[data-label]::before{content:attr(data-label);display:block;font-weight:600}
.action-table th:first-child,.action-table th:last-child{width:100%}.checks-card{padding:12px}}
"""

_SCRIPT = """
const filters = Array.from(document.querySelectorAll('[data-filter]'));
const findings = Array.from(document.querySelectorAll('.finding'));
let kind = 'all';
document.querySelector('.maker-filters').hidden = false;
function applyFilter() {
  const search = document.getElementById('search').value.trim().toLowerCase();
  findings.forEach(row => row.hidden = !(kind === 'all' || row.dataset.filterGroup === kind) ||
    !row.textContent.toLowerCase().includes(search));
  document.querySelectorAll('.category').forEach(category => {
    category.hidden = !Array.from(category.querySelectorAll('.finding')).some(row => !row.hidden);
    if (!category.hidden && (kind !== 'all' || search)) category.open = true;
  });
  filters.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.filter === kind)));
  document.getElementById('no-match').hidden = findings.some(row => !row.hidden);
}
filters.forEach(button => button.addEventListener('click', () => {
  kind = button.dataset.filter; applyFilter();
}));
document.getElementById('search').addEventListener('input', applyFilter);
document.getElementById('expand-all').addEventListener('click', () => {
  document.querySelectorAll('.category,.finding').forEach(row => { if (!row.hidden) row.open = true; });
});
document.getElementById('collapse-all').addEventListener('click', () => {
  document.querySelectorAll('.category,.finding').forEach(row => row.open = false);
});
function openTarget(hash) {
  const target = document.getElementById(hash.slice(1));
  if (!target) return;
  kind = 'all'; document.getElementById('search').value = ''; applyFilter();
  let current = target;
  while (current) {
    if (current.tagName === 'DETAILS') current.open = true;
    current = current.parentElement;
  }
  if (target.classList.contains('manual-verification')) {
    target.querySelectorAll('.finding').forEach(row => row.open = true);
  }
  target.scrollIntoView({block:'start'});
}
document.querySelectorAll('a[href^="#"]').forEach(link => link.addEventListener('click',
  () => openTarget(link.getAttribute('href'))));
window.addEventListener('hashchange', () => openTarget(location.hash));
if (location.hash) openTarget(location.hash);
"""
