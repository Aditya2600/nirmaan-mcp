"""
Plane / Nirmaan REST API client.

Targets a self-hosted Plane instance (e.g. https://nirmaan.credresolve.com).
Authentication: X-API-Key header (token generated in Workspace Settings).

Endpoint paths follow Plane's REST API:
  {base}/api/v1/workspaces/{slug}/projects/{project-id}/issues/

If endpoints return 404, verify against your Plane version's API docs at
https://docs.plane.so — endpoint paths can shift between versions.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from dotenv import load_dotenv


# Priority values accepted by Plane
PRIORITIES = {"urgent", "high", "medium", "low", "none"}


def load_env_files() -> None:
    """Load environment variables from both local .env and ~/.nirmaan.env.

    Loads the local .env (or parent) first so it takes precedence, then layers
    ~/.nirmaan.env (or ~/.config/nirmaan/.env) on top without overriding —
    so a user-level config supplies anything the project-level file omits.
    """
    load_dotenv()
    for path in [
        Path.home() / ".nirmaan.env",
        Path.home() / ".config" / "nirmaan" / ".env",
    ]:
        if path.exists():
            load_dotenv(path)
            break


@dataclass
class PlaneConfig:
    """Configuration for the Plane API client.

    Reads from environment variables (typically loaded from .env).
    """
    base_url: str           # e.g. https://nirmaan.credresolve.com
    api_key: str
    workspace_slug: str     # e.g. "cr-product"
    project_id: Optional[str] = None
    module_id: Optional[str] = None
    user_id: Optional[str] = None

    @classmethod
    def from_env(cls, require_project: bool = False) -> "PlaneConfig":
        """Build config from environment / .env file.

        Args:
            require_project: if True, raises if PLANE_PROJECT_ID is unset.
                Use False on the first run when you don't know the ID yet.
        """
        load_env_files()
        base_url = os.getenv("PLANE_BASE_URL", "").rstrip("/")
        api_key = os.getenv("PLANE_API_KEY", "")
        workspace_slug = os.getenv("PLANE_WORKSPACE_SLUG", "")
        project_id = os.getenv("PLANE_PROJECT_ID") or None
        module_id = os.getenv("PLANE_MODULE_ID") or None
        user_id = os.getenv("PLANE_USER_ID") or None

        missing: list[str] = []
        if not base_url:
            missing.append("PLANE_BASE_URL")
        if not api_key or api_key == "plane_api_replace_me":
            missing.append("PLANE_API_KEY")
        if not workspace_slug:
            missing.append("PLANE_WORKSPACE_SLUG")
        if require_project and not project_id:
            missing.append("PLANE_PROJECT_ID")
        if missing:
            raise RuntimeError(
                f"Missing or unset env vars: {', '.join(missing)}. "
                "See .env.example."
            )

        return cls(
            base_url=base_url,
            api_key=api_key,
            workspace_slug=workspace_slug,
            project_id=project_id,
            module_id=module_id,
            user_id=user_id,
        )


class PlaneError(Exception):
    """Raised when the Plane API returns a non-2xx response or a network error."""


class PlaneModuleAttachError(PlaneError):
    """Raised when an issue was created/updated successfully but module attachment failed.

    The created/updated issue is preserved on the exception so callers can recover
    and report the identifier instead of treating the issue as lost.
    """

    def __init__(self, message: str, issue: dict, errors: list[tuple[str, Exception]]):
        super().__init__(message)
        self.issue = issue
        self.errors = errors


class PlaneClient:
    """Thin wrapper around the Plane REST API.

    Use as a context manager so the underlying HTTP connection is closed:

        with PlaneClient(config) as client:
            projects = client.list_projects()
    """

    # Project metadata (states/labels/modules) is cached for this long. Plane
    # admins rarely change these between calls, so a short TTL collapses bursts
    # of find_state/find_label/find_module lookups into a single API hit.
    _METADATA_TTL_SECONDS = 300

    def __init__(self, config: PlaneConfig):
        self.config = config
        self._client = httpx.Client(
            base_url=f"{config.base_url}/api/v1",
            headers={
                "X-API-Key": config.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        # key -> (monotonic-expiry, value)
        self._metadata_cache: dict[str, tuple[float, list[dict]]] = {}

    # ---- internal helpers ----

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            r = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise PlaneError(f"Network error calling {method} {path}: {e}") from e

        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise PlaneError(f"{method} {path} -> HTTP {r.status_code}: {detail}")

        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def _ws(self) -> str:
        return f"/workspaces/{self.config.workspace_slug}"

    def _proj(self) -> str:
        if not self.config.project_id:
            raise RuntimeError(
                "This call requires PLANE_PROJECT_ID. "
                "Set it in .env after running list_projects()."
            )
        return f"{self._ws()}/projects/{self.config.project_id}"

    @staticmethod
    def _unwrap_results(data: Any) -> list[dict]:
        """Plane returns either a list or a paginated dict. Normalize.

        Raises PlaneError on shapes we don't recognize, so a server change or
        misrouted endpoint surfaces immediately instead of silently returning [].
        """
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "items", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            raise PlaneError(
                f"Unexpected list response shape: dict with keys {sorted(data)} "
                "(expected one of 'results', 'items', 'data')."
            )
        raise PlaneError(f"Unexpected list response shape: {type(data).__name__}")

    def _cached_metadata(
        self, key: str, fetch: Callable[[], list[dict]]
    ) -> list[dict]:
        """Return cached metadata if still fresh, otherwise refetch and cache.

        Cache key is namespaced by project_id so a config swap doesn't cross-contaminate.
        """
        full_key = f"{self.config.project_id}:{key}"
        now = time.monotonic()
        entry = self._metadata_cache.get(full_key)
        if entry and entry[0] > now:
            return entry[1]
        value = fetch()
        self._metadata_cache[full_key] = (now + self._METADATA_TTL_SECONDS, value)
        return value

    def invalidate_metadata_cache(self) -> None:
        """Drop the cached states/labels/modules. Call after creating any of those."""
        self._metadata_cache.clear()

    # ---- discovery (no project required) ----

    def list_projects(self) -> list[dict]:
        """List all projects in the workspace."""
        return self._unwrap_results(
            self._request("GET", f"{self._ws()}/projects/")
        )

    def list_workspace_members(self) -> list[dict]:
        """List members of the workspace (for assignee IDs)."""
        return self._unwrap_results(
            self._request("GET", f"{self._ws()}/members/")
        )

    # ---- project metadata (project_id required) ----

    def list_states(self) -> list[dict]:
        """List all workflow states (Todo, In Progress, Done, etc.). Cached for 5min."""
        return self._cached_metadata(
            "states",
            lambda: self._unwrap_results(
                self._request("GET", f"{self._proj()}/states/")
            ),
        )

    def list_cycles(self) -> list[dict]:
        """List all cycles for the project."""
        return self._unwrap_results(
            self._request("GET", f"{self._proj()}/cycles/")
        )

    def list_labels(self) -> list[dict]:
        """List all labels. Cached for 5min."""
        return self._cached_metadata(
            "labels",
            lambda: self._unwrap_results(
                self._request("GET", f"{self._proj()}/labels/")
            ),
        )

    def list_modules(self) -> list[dict]:
        """List all modules (e.g. 'AgentX'). Cached for 5min."""
        return self._cached_metadata(
            "modules",
            lambda: self._unwrap_results(
                self._request("GET", f"{self._proj()}/modules/")
            ),
        )

    def list_module_issues(self, module_id: str) -> list[dict]:
        """List all work items in a module."""
        return self._unwrap_results(
            self._request(
                "GET",
                f"{self._proj()}/modules/{module_id}/module-issues/",
            )
        )

    def add_issues_to_module(self, module_id: str, issue_ids: list[str]) -> Any:
        """Add one or more work items to a module."""
        return self._request(
            "POST",
            f"{self._proj()}/modules/{module_id}/module-issues/",
            json={"issues": list(issue_ids)},
        )

    def add_issue_to_module(self, module_id: str, issue_id: str) -> Any:
        """Attach a single existing issue to a module (e.g. AgentX)."""
        return self.add_issues_to_module(module_id, [issue_id])

    def get_current_cycle(self) -> Optional[dict]:
        """Return the cycle that's active today, or None.

        Plane sometimes returns dates as plain ISO dates and sometimes as ISO
        datetimes with timezone suffixes; slice to the date portion before parsing.
        """
        today = date.today()
        for c in self.list_cycles():
            start = c.get("start_date")
            end = c.get("end_date")
            if not start or not end:
                continue
            try:
                start_d = date.fromisoformat(start[:10])
                end_d = date.fromisoformat(end[:10])
            except ValueError:
                continue
            if start_d <= today <= end_d:
                return c
        return None

    # ---- issues (work items) ----

    def list_issues(self, **filters: Any) -> list[dict]:
        """List work items. Filters become query params (e.g. state=, priority=)."""
        return self._unwrap_results(
            self._request("GET", f"{self._proj()}/issues/", params=filters)
        )

    def get_issue(self, issue_id: str) -> dict:
        """Get a single work item by ID."""
        return self._request("GET", f"{self._proj()}/issues/{issue_id}/")

    def search_issues(self, query: str) -> list[dict]:
        """Workspace-level issue search. Accepts identifiers like 'BUILDER-506'.

        Server-side lookup — avoids paging through every project issue to
        resolve a sequence number. Results are scoped to the workspace and
        contain only summary fields (id, sequence_id, project_id, name).
        """
        data = self._request(
            "GET", f"{self._ws()}/issues/search/", params={"search": query}
        )
        if isinstance(data, dict) and "issues" in data:
            return data["issues"]
        return self._unwrap_results(data)

    def create_issue(
        self,
        name: str,
        description_html: Optional[str] = None,
        state_id: Optional[str] = None,
        priority: Optional[str] = None,
        assignee_ids: Optional[list[str]] = None,
        label_ids: Optional[list[str]] = None,
        estimate_point: Optional[int] = None,
        start_date: Optional[str] = None,
        target_date: Optional[str] = None,
        parent: Optional[str] = None,
        module_ids: Optional[list[str]] = None,
    ) -> dict:
        """Create a new work item.

        Args:
            name: title (required).
            description_html: HTML description.
            state_id: state UUID. Use find_state() to look up by name.
            priority: one of urgent / high / medium / low / none.
            assignee_ids: list of workspace member UUIDs.
            label_ids: list of label UUIDs.
            estimate_point: integer estimate.
            start_date / target_date: ISO date strings, e.g. "2026-05-10".
            parent: parent issue UUID.
            module_ids: list of module UUIDs to attach (e.g. AgentX).
                Attached via separate sub-endpoint after creation.
        """
        if priority and priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}, got {priority!r}")

        payload: dict[str, Any] = {"name": name}
        if description_html is not None:
            payload["description_html"] = description_html
        if state_id:
            payload["state"] = state_id
        if priority:
            payload["priority"] = priority
        if assignee_ids:
            payload["assignees"] = assignee_ids
        if label_ids:
            payload["labels"] = label_ids
        if estimate_point is not None:
            payload["estimate_point"] = estimate_point
        if start_date:
            payload["start_date"] = start_date
        if target_date:
            payload["target_date"] = target_date
        if parent:
            payload["parent"] = parent

        issue = self._request("POST", f"{self._proj()}/issues/", json=payload)

        # Modules are attached via a separate sub-endpoint in Plane. If any
        # attach fails, the issue itself was still created — surface that via
        # PlaneModuleAttachError so callers don't treat it as a lost write.
        if module_ids and issue and issue.get("id"):
            self._attach_modules(issue, module_ids)

        return issue

    def _attach_modules(self, issue: dict, module_ids: list[str]) -> None:
        """Attach an existing issue to one or more modules.

        Attempts every module before raising so the caller sees the full failure set.
        """
        errors: list[tuple[str, Exception]] = []
        for mid in module_ids:
            try:
                self.add_issue_to_module(mid, issue["id"])
            except PlaneError as e:
                errors.append((mid, e))
        if errors:
            ident = issue.get("sequence_id") or issue.get("id")
            failed = ", ".join(mid for mid, _ in errors)
            detail = "; ".join(f"{mid}: {e}" for mid, e in errors)
            raise PlaneModuleAttachError(
                f"Issue {ident} was created but module attach failed for "
                f"[{failed}]: {detail}",
                issue=issue,
                errors=errors,
            )

    # Friendly kwargs accepted by update_issue / create_issue. The Plane API
    # uses the values; we accept the keys for callers that mirror create_issue.
    _UPDATE_FIELD_ALIASES = {
        "assignee_ids": "assignees",
        "label_ids": "labels",
    }

    def update_issue(self, issue_id: str, **fields: Any) -> dict:
        """Update one or more fields on a work item.

        Accepts both Plane field names (state, priority, assignees, labels,
        estimate_point, start_date, target_date, parent, name, description_html)
        and the friendly aliases used by create_issue:

          - assignee_ids -> assignees
          - label_ids    -> labels
          - module_ids   -> attached via the module sub-endpoint after the PATCH

        Raises PlaneModuleAttachError if the PATCH succeeds but a module attach fails.
        """
        if "priority" in fields and fields["priority"] not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")

        module_ids = fields.pop("module_ids", None)

        translated: dict[str, Any] = {}
        for key, value in fields.items():
            translated[self._UPDATE_FIELD_ALIASES.get(key, key)] = value

        issue = (
            self._request(
                "PATCH", f"{self._proj()}/issues/{issue_id}/", json=translated
            )
            if translated
            else self.get_issue(issue_id)
        )

        if module_ids:
            target = issue if isinstance(issue, dict) and issue.get("id") else {"id": issue_id}
            self._attach_modules(target, list(module_ids))

        return issue

    def add_comment(self, issue_id: str, comment_html: str) -> dict:
        """Add a comment to a work item. Body is HTML."""
        return self._request(
            "POST",
            f"{self._proj()}/issues/{issue_id}/comments/",
            json={"comment_html": comment_html},
        )

    # ---- convenience helpers ----

    def find_state(self, name: str) -> Optional[dict]:
        """Find a state by name (case-insensitive). e.g. 'Done', 'In Progress'."""
        target = name.lower()
        for s in self.list_states():
            if s.get("name", "").lower() == target:
                return s
        return None

    def find_label(self, name: str) -> Optional[dict]:
        """Find a label by name (case-insensitive)."""
        target = name.lower()
        for label in self.list_labels():
            if label.get("name", "").lower() == target:
                return label
        return None

    def find_module(self, name: str) -> Optional[dict]:
        """Find a module by name (case-insensitive). e.g. 'AgentX'."""
        target = name.lower()
        for module in self.list_modules():
            if module.get("name", "").lower() == target:
                return module
        return None

    def transition_state(self, issue_id: str, state_name: str) -> dict:
        """Move a work item to a state by name (e.g. 'Done')."""
        state = self.find_state(state_name)
        if not state:
            available = [s.get("name") for s in self.list_states()]
            raise ValueError(
                f"No state named {state_name!r}. Available: {available}"
            )
        return self.update_issue(issue_id, state=state["id"])

    # ---- lifecycle ----

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PlaneClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
