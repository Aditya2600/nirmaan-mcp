"""Plane MCP server — exposes PlaneClient as tools Claude Code can call."""
from __future__ import annotations

import html
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .plane_client import PlaneClient, PlaneConfig, PlaneError, load_env_files


# Only this module may receive ticket creates/updates from the MCP server.
# Any attempt to mutate a ticket outside it is refused.
ENFORCED_MODULE_NAME = "AgentX"


# ---------- shared helpers (mirrors cli.py for self-containment) ----------

@dataclass
class BotDefaults:
    label_ids: list[str] = field(default_factory=list)
    estimate: Optional[int] = None
    priority: str = "none"

    @classmethod
    def from_env(cls) -> "BotDefaults":
        load_env_files()
        labels_raw = os.getenv("PLANE_DEFAULT_LABEL_IDS", "")
        label_ids = [s.strip() for s in labels_raw.split(",") if s.strip()]
        est = os.getenv("PLANE_DEFAULT_ESTIMATE")
        try:
            estimate = int(est) if est else None
        except ValueError:
            estimate = None
        return cls(
            label_ids=label_ids,
            estimate=estimate,
            priority=(os.getenv("PLANE_DEFAULT_PRIORITY") or "none").lower(),
        )


_WEEKDAYS_SHORT = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAYS_FULL = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_date(s: Optional[str]) -> Optional[str]:
    """Parse 'today' / 'tomorrow' / '+Nd' / weekday / ISO YYYY-MM-DD into ISO date.

    Returns None for empty input. Raises ValueError on unrecognized input so the
    caller surfaces a clear error instead of forwarding garbage to Plane.
    """
    if not s:
        return None
    raw = s
    s = s.lower().strip()
    if s == "today":
        return date.today().isoformat()
    if s == "tomorrow":
        return (date.today() + timedelta(days=1)).isoformat()
    if s.startswith("+") and s.endswith("d") and len(s) > 2:
        try:
            n = int(s[1:-1])
        except ValueError:
            raise ValueError(
                f"Invalid relative date {raw!r}. Expected '+Nd' with integer N."
            )
        return (date.today() + timedelta(days=n)).isoformat()
    weekday = _WEEKDAYS_FULL.get(s) if s in _WEEKDAYS_FULL else _WEEKDAYS_SHORT.get(s)
    if weekday is not None:
        today = date.today()
        # "next" semantics: same-day → 7 days out
        days_ahead = (weekday - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_ahead)).isoformat()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise ValueError(
            f"Invalid date {raw!r}. Expected 'today', 'tomorrow', '+Nd', "
            f"a weekday name, or ISO YYYY-MM-DD."
        )


def _to_html(text: Optional[str]) -> Optional[str]:
    """Convert plain text to safe paragraph HTML for Plane's description_html.

    Always escapes HTML special characters; never passes raw HTML through.
    Splits on blank lines into <p>, converts single newlines to <br/>.
    """
    if not text:
        return None
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return None
    return "".join(
        f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paragraphs
    )


def _resolve_issue(client: PlaneClient, identifier: str) -> dict:
    """Accept a UUID, 'BUILDER-1055', or '1055' and return the issue."""
    try:
        uuid.UUID(identifier)
    except ValueError:
        pass
    else:
        return client.get_issue(identifier)
    seq = identifier.split("-")[-1]
    try:
        seq_num = int(seq)
    except ValueError:
        raise ValueError(f"Cannot parse issue identifier: {identifier}")
    project_id = client.config.project_id
    for hit in client.search_issues(identifier):
        if hit.get("sequence_id") == seq_num and hit.get("project_id") == project_id:
            return client.get_issue(hit["id"])
    raise ValueError(f"No work item with sequence number {seq_num}")


def _client() -> PlaneClient:
    return PlaneClient(PlaneConfig.from_env(require_project=True))


def _agentx_module(client: PlaneClient) -> dict:
    """Look up the enforced module ('AgentX'); raise if it doesn't exist."""
    m = client.find_module(ENFORCED_MODULE_NAME)
    if not m:
        available = [x.get("name") for x in client.list_modules()]
        raise RuntimeError(
            f"Required module {ENFORCED_MODULE_NAME!r} not found in project. "
            f"Available modules: {available}"
        )
    return m


def _issue_module_ids(issue: dict) -> set[str]:
    ids: set[str] = set()
    for key in ("modules", "module_ids"):
        value = issue.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str):
                ids.add(item)
            elif isinstance(item, dict):
                mid = item.get("id") or item.get("module_id")
                if isinstance(mid, str):
                    ids.add(mid)
    return ids


def _assert_in_agentx(client: PlaneClient, issue: dict) -> None:
    """Refuse to mutate tickets that aren't in the AgentX module."""
    agentx = _agentx_module(client)
    if agentx["id"] in _issue_module_ids(issue):
        return
    # Issue payload may not embed modules. Confirm via the module-issues
    # endpoint before refusing, so a sparse serializer doesn't cause false
    # negatives.
    members = {i.get("id") for i in client.list_module_issues(agentx["id"])}
    if issue.get("id") in members:
        return
    raise PermissionError(
        f"BUILDER-{issue.get('sequence_id')} is not in the "
        f"{ENFORCED_MODULE_NAME!r} module; refusing to update."
    )


def _module_names_for_issue(client: PlaneClient, issue: dict) -> list[str]:
    """Resolve module names attached to an issue.

    Reads the issue payload first; if Plane returns a sparse serializer,
    falls back to probing AgentX membership so the enforced module is at
    least surfaced when present.
    """
    modules_by_id = {m["id"]: m.get("name", "?") for m in client.list_modules()}
    ids = _issue_module_ids(issue)
    if ids:
        return sorted({modules_by_id.get(mid, "?") for mid in ids})
    agentx = client.find_module(ENFORCED_MODULE_NAME)
    if agentx:
        members = {i.get("id") for i in client.list_module_issues(agentx["id"])}
        if issue.get("id") in members:
            return [agentx.get("name") or ENFORCED_MODULE_NAME]
    return []


def _do_create(
    *,
    title: str,
    description: Optional[str],
    state: str,
    priority: Optional[str],
    labels: Optional[list[str]],
    estimate: Optional[int],
    due: Optional[str],
    start: Optional[str],
    parent: Optional[str],
) -> dict:
    """Shared issue-creation logic. All tickets are attached to AgentX."""
    defaults = BotDefaults.from_env()
    with _client() as c:
        st = c.find_state(state)
        if not st:
            available = [s["name"] for s in c.list_states()]
            raise ValueError(f"No state '{state}'. Available: {available}")

        label_ids = list(defaults.label_ids)
        for name in (labels or []):
            label = c.find_label(name)
            if label and label["id"] not in label_ids:
                label_ids.append(label["id"])

        agentx = _agentx_module(c)

        issue = c.create_issue(
            name=title,
            description_html=_to_html(description),
            state_id=st["id"],
            priority=priority or defaults.priority,
            label_ids=label_ids or None,
            module_ids=[agentx["id"]],
            estimate_point=estimate if estimate is not None else defaults.estimate,
            start_date=_parse_date(start),
            target_date=_parse_date(due),
            parent=parent,
        )
        return {
            "identifier": f"BUILDER-{issue.get('sequence_id')}",
            "name": issue.get("name"),
            "state": state,
            "module": ENFORCED_MODULE_NAME,
        }


# ---------- MCP server ----------

mcp = FastMCP("nirmaan")

def _ids_from_owner_value(value: Any) -> set[str]:
    """Extract user UUIDs from Plane's inconsistent assignee/creator shapes."""
    ids: set[str] = set()

    if value is None:
        return ids

    if isinstance(value, str):
        ids.add(value)
        return ids

    if isinstance(value, list):
        for item in value:
            ids.update(_ids_from_owner_value(item))
        return ids

    if isinstance(value, dict):
        for key in ("id", "user_id", "member_id"):
            val = value.get(key)
            if isinstance(val, str):
                ids.add(val)

        for key in ("user", "member", "assignee", "created_by", "actor"):
            ids.update(_ids_from_owner_value(value.get(key)))

    return ids


def _issue_assignee_ids(issue: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in (
        "assignees",
        "assignee_ids",
        "assignees_ids",
        "assignee_details",
        "assigned_to",
    ):
        ids.update(_ids_from_owner_value(issue.get(key)))
    return ids


def _issue_created_by_ids(issue: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in (
        "created_by",
        "created_by_id",
        "creator",
        "created_by_detail",
    ):
        ids.update(_ids_from_owner_value(issue.get(key)))
    return ids


def _issue_state_group(issue: dict[str, Any], states_by_id: dict[str, dict]) -> str:
    state = issue.get("state")

    if isinstance(state, dict):
        return str(state.get("group") or state.get("name") or "").lower()

    if isinstance(state, str):
        st = states_by_id.get(state) or {}
        return str(st.get("group") or st.get("name") or "").lower()

    return ""

@mcp.tool()
def list_my_tickets(
    state: Optional[str] = None,
    limit: int = 20,
    include_done: bool = False,
    scope: str = "assigned",
) -> list[dict]:
    """List your work items.

    scope:
    - assigned: only tickets assigned to PLANE_USER_ID
    - created: only tickets created by PLANE_USER_ID
    - both: assigned OR created

    Server-side Plane filters are treated only as optimization.
    Final ownership check is always done locally.
    """
    if scope not in {"assigned", "created", "both"}:
        raise ValueError("scope must be one of: assigned, created, both")

    config = PlaneConfig.from_env(require_project=True)
    user_id = config.user_id
    if not user_id:
        raise ValueError("PLANE_USER_ID not set in .env or ~/.nirmaan.env")

    with PlaneClient(config) as c:
        filters: dict[str, Any] = {}

        if state:
            st = c.find_state(state)
            if not st:
                available = [s["name"] for s in c.list_states()]
                raise ValueError(f"No state '{state}'. Available: {available}")
            filters["state"] = st["id"]

        states_by_id = {s["id"]: s for s in c.list_states()}

        # Fetch candidates. Do not trust API assignee/created_by filters blindly.
        # If Plane ignores filters, local filtering below still protects us.
        candidates = c.list_issues(
            **filters,
            expand="assignees,state,created_by",
            per_page=100,
            order_by="-updated_at",
        )

        filtered: list[dict[str, Any]] = []

        for issue in candidates:
            state_group = _issue_state_group(issue, states_by_id)
            if not include_done and state_group in {"completed", "cancelled", "canceled", "done"}:
                continue

            assigned = user_id in _issue_assignee_ids(issue)
            created = user_id in _issue_created_by_ids(issue)

            if scope == "assigned" and not assigned:
                continue
            if scope == "created" and not created:
                continue
            if scope == "both" and not (assigned or created):
                continue

            filtered.append(issue)

        filtered.sort(
            key=lambda i: i.get("updated_at") or i.get("created_at") or "",
            reverse=True,
        )

        filtered = filtered[:limit]

        return [
            {
                "identifier": f"BUILDER-{i.get('sequence_id')}",
                "name": i.get("name"),
                "state": (
                    i["state"].get("name")
                    if isinstance(i.get("state"), dict)
                    else states_by_id.get(i.get("state"), {}).get("name", "?")
                ),
                "priority": i.get("priority"),
                "target_date": i.get("target_date"),
            }
            for i in filtered
        ]
    
@mcp.tool()
def list_workspace_members() -> list[dict]:
    """List all workspace members — useful for finding your own UUID.

    Returns each member's id, email, and display name (when available).
    Plane wraps member info inconsistently across versions, so the raw
    dict is included as 'raw' for fallback.
    """
    with _client() as c:
        members = c.list_workspace_members()
        out = []
        for m in members:
            inner = m.get("member") if isinstance(m.get("member"), dict) else m
            out.append({
                "id": inner.get("id"),
                "email": inner.get("email"),
                "display_name": inner.get("display_name") or inner.get("first_name"),
                "raw": m,
            })
        return out


@mcp.tool()
def get_ticket(identifier: str) -> dict:
    """Fetch full details of a work item by identifier (e.g. 'BUILDER-784')."""
    with _client() as c:
        issue = _resolve_issue(c, identifier)
        states_by_id = {s["id"]: s["name"] for s in c.list_states()}
        labels_by_id = {lbl["id"]: lbl["name"] for lbl in c.list_labels()}
        modules = _module_names_for_issue(c, issue)
        return {
            "identifier": f"BUILDER-{issue.get('sequence_id')}",
            "name": issue.get("name"),
            "description_html": issue.get("description_html"),
            "state": states_by_id.get(issue.get("state"), "?"),
            "priority": issue.get("priority"),
            "labels": [
                labels_by_id.get(lbl, "?") for lbl in (issue.get("labels") or [])
            ],
            "modules": modules,
            "estimate_point": issue.get("estimate_point"),
            "start_date": issue.get("start_date"),
            "target_date": issue.get("target_date"),
        }


@mcp.tool()
def create_ticket(
    title: str,
    description: Optional[str] = None,
    state: str = "Todo",
    priority: Optional[str] = None,
    labels: Optional[list[str]] = None,
    estimate: Optional[int] = None,
    due: Optional[str] = None,
    start: Optional[str] = None,
    parent: Optional[str] = None,
) -> dict:
    """Create a new work item. Always attached to the AgentX module.

    Defaults to 'Todo' state. For starting work or logging completed work,
    prefer `start_ticket` / `done_ticket`, which apply the appropriate
    state and dates.

    Args:
        title: Required title.
        description: Plain text; HTML special chars are escaped.
        state: State name. Default 'Todo'.
        priority: urgent / high / medium / low / none.
        labels: List of label names (added to defaults).
        estimate: Integer estimate point.
        due: 'today' / 'fri' / '+3d' / ISO YYYY-MM-DD.
        start: Same format as due.
        parent: Parent issue UUID.
    """
    return _do_create(
        title=title,
        description=description,
        state=state,
        priority=priority,
        labels=labels,
        estimate=estimate,
        due=due,
        start=start,
        parent=parent,
    )


@mcp.tool()
def start_ticket(
    title: str,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[list[str]] = None,
    estimate: Optional[int] = None,
    due: Optional[str] = None,
    parent: Optional[str] = None,
) -> dict:
    """Create a work item in 'In Progress' with start=today. Attached to AgentX."""
    return _do_create(
        title=title,
        description=description,
        state="In Progress",
        priority=priority,
        labels=labels,
        estimate=estimate,
        due=due,
        start="today",
        parent=parent,
    )


@mcp.tool()
def done_ticket(
    title: str,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[list[str]] = None,
    estimate: Optional[int] = None,
    due: Optional[str] = None,
    parent: Optional[str] = None,
) -> dict:
    """Create a work item in 'Done' with due=today (overridable). Attached to AgentX."""
    return _do_create(
        title=title,
        description=description,
        state="Done",
        priority=priority,
        labels=labels,
        estimate=estimate,
        due=due or "today",
        start=None,
        parent=parent,
    )


@mcp.tool()
def update_ticket(
    identifier: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    estimate: Optional[int] = None,
    due: Optional[str] = None,
    start: Optional[str] = None,
) -> dict:
    """Update fields on an existing work item.

    Refuses to update tickets outside the AgentX module.
    """
    with _client() as c:
        issue = _resolve_issue(c, identifier)
        _assert_in_agentx(c, issue)
        fields: dict[str, Any] = {}
        if title is not None:
            fields["name"] = title
        if description is not None:
            fields["description_html"] = _to_html(description)
        if priority is not None:
            fields["priority"] = priority
        if estimate is not None:
            fields["estimate_point"] = estimate
        if due is not None:
            fields["target_date"] = _parse_date(due)
        if start is not None:
            fields["start_date"] = _parse_date(start)
        if not fields:
            raise ValueError("No fields to update.")
        c.update_issue(issue["id"], **fields)
        return {
            "identifier": f"BUILDER-{issue.get('sequence_id')}",
            "updated_fields": list(fields.keys()),
        }


@mcp.tool()
def transition_state(identifier: str, state: str) -> dict:
    """Move a work item to a new state.

    Refuses to transition tickets outside the AgentX module.
    """
    with _client() as c:
        issue = _resolve_issue(c, identifier)
        _assert_in_agentx(c, issue)
        c.transition_state(issue["id"], state)
        return {"identifier": f"BUILDER-{issue.get('sequence_id')}", "state": state}


@mcp.tool()
def add_comment(identifier: str, text: str) -> dict:
    """Add a comment to a work item.

    Refuses to comment on tickets outside the AgentX module.
    """
    with _client() as c:
        issue = _resolve_issue(c, identifier)
        _assert_in_agentx(c, issue)
        c.add_comment(issue["id"], _to_html(text) or "")
        return {"identifier": f"BUILDER-{issue.get('sequence_id')}", "comment_added": True}


@mcp.tool()
def list_states() -> list[str]:
    """List all available states (Todo, In Progress, Done, etc.)."""
    with _client() as c:
        return [s["name"] for s in c.list_states()]


@mcp.tool()
def list_labels() -> list[str]:
    """List all available labels."""
    with _client() as c:
        return [lbl["name"] for lbl in c.list_labels()]


@mcp.tool()
def list_modules() -> list[str]:
    """List modules in the project (debugging-only).

    This tool exists to inspect what modules exist (e.g. confirm AgentX
    is present, see legacy modules like ResolveX) — NOT to choose where
    a new ticket should land. Ticket creation is always pinned to the
    AgentX module by `_do_create`; there is no module parameter on
    create_ticket / start_ticket / done_ticket and no way to override it.
    """
    with _client() as c:
        return [m["name"] for m in c.list_modules()]


@mcp.tool()
def get_current_cycle() -> Optional[dict]:
    """Get the currently active cycle (sprint), if any."""
    with _client() as c:
        cycle = c.get_current_cycle()
        if not cycle:
            return None
        return {
            "name": cycle.get("name"),
            "start_date": cycle.get("start_date"),
            "end_date": cycle.get("end_date"),
        }


def main():
    """Entry point — run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
