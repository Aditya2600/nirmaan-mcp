"""Unit tests for nirmaan.mcp_server helpers and tool defaults."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from nirmaan.mcp_server import (
    _do_create,
    _parse_date,
    _resolve_issue,
    _to_html,
)


class ParseDateTests(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(_parse_date(None))
        self.assertIsNone(_parse_date(""))

    def test_today(self):
        self.assertEqual(_parse_date("today"), date.today().isoformat())
        self.assertEqual(_parse_date("TODAY"), date.today().isoformat())

    def test_tomorrow(self):
        expected = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(_parse_date("tomorrow"), expected)

    def test_relative_days(self):
        self.assertEqual(
            _parse_date("+3d"), (date.today() + timedelta(days=3)).isoformat()
        )
        self.assertEqual(
            _parse_date("+0d"), date.today().isoformat()
        )

    def test_relative_days_invalid_raises(self):
        with self.assertRaises(ValueError):
            _parse_date("+abcd")
        with self.assertRaises(ValueError):
            _parse_date("+d")

    def test_weekday_short(self):
        result = _parse_date("mon")
        parsed = date.fromisoformat(result)
        self.assertEqual(parsed.weekday(), 0)
        # Always a future date — same-day rolls forward 7 days.
        self.assertGreater(parsed, date.today())

    def test_weekday_full_name(self):
        result = _parse_date("friday")
        self.assertEqual(date.fromisoformat(result).weekday(), 4)

    def test_iso_valid(self):
        self.assertEqual(_parse_date("2026-05-15"), "2026-05-15")

    def test_iso_invalid_raises(self):
        with self.assertRaises(ValueError):
            _parse_date("2026-13-40")
        with self.assertRaises(ValueError):
            _parse_date("not-a-date")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            _parse_date("asap")
        with self.assertRaises(ValueError):
            _parse_date("next monday")


class ToHtmlTests(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(_to_html(None))
        self.assertIsNone(_to_html(""))
        self.assertIsNone(_to_html("   \n\n   "))

    def test_plain(self):
        self.assertEqual(_to_html("hello"), "<p>hello</p>")

    def test_paragraphs(self):
        self.assertEqual(_to_html("a\n\nb"), "<p>a</p><p>b</p>")

    def test_single_newline_to_br(self):
        self.assertEqual(_to_html("a\nb"), "<p>a<br/>b</p>")

    def test_escapes_html_special_chars(self):
        result = _to_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)
        self.assertIn("&lt;/script&gt;", result)

    def test_no_html_passthrough(self):
        # Raw HTML must be escaped, not preserved (the old behavior was unsafe).
        result = _to_html("<p>raw paragraph</p>")
        self.assertIn("&lt;p&gt;", result)
        self.assertNotIn("<p>raw paragraph</p>", result)

    def test_ampersand_escaped(self):
        self.assertIn("&amp;", _to_html("a & b"))


def _resolver_client(project_id: str = "proj-1") -> MagicMock:
    """Build a MagicMock PlaneClient configured for _resolve_issue's contract.

    _resolve_issue calls client.search_issues(identifier), filters hits by
    matching sequence_id AND project_id, then calls client.get_issue(id) to
    fetch the full record. Tests need a real (non-Mock) project_id so the
    equality check in the filter actually passes.
    """
    client = MagicMock()
    client.config.project_id = project_id
    return client


class ResolveIssueTests(unittest.TestCase):
    def test_uuid_uses_get_issue(self):
        client = _resolver_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        client.get_issue.return_value = {"id": uuid_str, "sequence_id": 99}
        result = _resolve_issue(client, uuid_str)
        client.get_issue.assert_called_once_with(uuid_str)
        client.search_issues.assert_not_called()
        self.assertEqual(result["sequence_id"], 99)

    def test_prefix_id_resolves_via_sequence(self):
        client = _resolver_client()
        client.search_issues.return_value = [
            {"sequence_id": 1, "id": "a", "project_id": "proj-1"},
            {"sequence_id": 1055, "id": "b", "project_id": "proj-1"},
        ]
        client.get_issue.return_value = {"id": "b", "sequence_id": 1055}
        result = _resolve_issue(client, "BUILDER-1055")
        self.assertEqual(result["id"], "b")
        client.get_issue.assert_called_once_with("b")

    def test_long_prefix_not_misread_as_uuid(self):
        # The old heuristic (>30 chars and >=4 dashes) would mis-classify this.
        client = _resolver_client()
        client.search_issues.return_value = [
            {"sequence_id": 1055, "id": "x", "project_id": "proj-1"},
        ]
        client.get_issue.return_value = {"id": "x", "sequence_id": 1055}
        result = _resolve_issue(client, "VERY-LONG-PROJECT-NAME-WITH-DASHES-1055")
        self.assertEqual(result["id"], "x")
        client.get_issue.assert_called_once_with("x")

    def test_bare_sequence(self):
        client = _resolver_client()
        client.search_issues.return_value = [
            {"sequence_id": 42, "id": "x", "project_id": "proj-1"},
        ]
        client.get_issue.return_value = {"id": "x", "sequence_id": 42}
        self.assertEqual(_resolve_issue(client, "42")["id"], "x")

    def test_other_project_match_ignored(self):
        # Workspace-level search can return hits from other projects with
        # the same sequence number — those must be filtered out.
        client = _resolver_client()
        client.search_issues.return_value = [
            {"sequence_id": 1055, "id": "wrong", "project_id": "other-proj"},
        ]
        with self.assertRaises(ValueError):
            _resolve_issue(client, "BUILDER-1055")
        client.get_issue.assert_not_called()

    def test_unparseable_raises(self):
        client = _resolver_client()
        with self.assertRaises(ValueError):
            _resolve_issue(client, "not-a-thing")

    def test_missing_sequence_raises(self):
        client = _resolver_client()
        client.search_issues.return_value = []
        with self.assertRaises(ValueError):
            _resolve_issue(client, "BUILDER-1055")


def _fake_client():
    """Build a context-managerable mock PlaneClient with sensible defaults."""
    c = MagicMock()
    c.__enter__.return_value = c
    c.__exit__.return_value = False
    c.find_state.side_effect = lambda name: {"id": f"{name.lower()}-id", "name": name}
    c.find_label.return_value = None
    # AgentX is enforced as the only module — make lookup succeed by default.
    c.find_module.side_effect = lambda name: (
        {"id": "agentx-id", "name": "AgentX"} if name.lower() == "agentx" else None
    )
    c.list_states.return_value = []
    c.list_modules.return_value = [{"id": "agentx-id", "name": "AgentX"}]
    c.create_issue.return_value = {"sequence_id": 1, "name": "x"}
    return c


class CreateTicketDefaultsTests(unittest.TestCase):
    def setUp(self):
        # Isolate from any real .env on the test host.
        env_patch = patch.dict(
            "os.environ",
            {
                "PLANE_DEFAULT_LABEL_IDS": "",
                "PLANE_DEFAULT_ESTIMATE": "",
                "PLANE_DEFAULT_MODULE_ID": "",
                "PLANE_DEFAULT_PRIORITY": "",
            },
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def _run(self, **kwargs):
        c = _fake_client()
        with patch("nirmaan.mcp_server._client", return_value=c):
            from nirmaan.mcp_server import create_ticket, done_ticket, start_ticket

            tool = kwargs.pop("_tool", create_ticket)
            # FastMCP may wrap the function; unwrap if needed.
            fn = getattr(tool, "fn", tool)
            result = fn(**kwargs)
        return c, result

    def test_create_ticket_defaults_to_todo(self):
        c, _ = self._run(title="hello")
        c.find_state.assert_called_once_with("Todo")
        # No auto-due when state is Todo (the old hidden behavior is gone).
        kwargs = c.create_issue.call_args.kwargs
        self.assertIsNone(kwargs["target_date"])

    def test_create_ticket_passes_state_through(self):
        c, _ = self._run(title="x", state="In Progress")
        c.find_state.assert_called_once_with("In Progress")

    def test_create_ticket_does_not_auto_set_due(self):
        # Old behavior set due=today when state=Done; that's now done_ticket's job.
        c, _ = self._run(title="x", state="Done")
        kwargs = c.create_issue.call_args.kwargs
        self.assertIsNone(kwargs["target_date"])

    def test_start_ticket_uses_in_progress_and_today(self):
        from nirmaan.mcp_server import start_ticket
        c, _ = self._run(_tool=start_ticket, title="x")
        c.find_state.assert_called_once_with("In Progress")
        kwargs = c.create_issue.call_args.kwargs
        self.assertEqual(kwargs["start_date"], date.today().isoformat())

    def test_done_ticket_uses_done_and_today(self):
        from nirmaan.mcp_server import done_ticket
        c, _ = self._run(_tool=done_ticket, title="x")
        c.find_state.assert_called_once_with("Done")
        kwargs = c.create_issue.call_args.kwargs
        self.assertEqual(kwargs["target_date"], date.today().isoformat())

    def test_done_ticket_due_overridable(self):
        from nirmaan.mcp_server import done_ticket
        c, _ = self._run(_tool=done_ticket, title="x", due="2026-12-01")
        kwargs = c.create_issue.call_args.kwargs
        self.assertEqual(kwargs["target_date"], "2026-12-01")

    def test_unknown_state_raises(self):
        c = _fake_client()
        c.find_state.return_value = None
        c.find_state.side_effect = None
        c.list_states.return_value = [{"name": "Todo"}, {"name": "Done"}]
        with patch("nirmaan.mcp_server._client", return_value=c):
            with self.assertRaises(ValueError):
                _do_create(
                    title="x",
                    description=None,
                    state="Bogus",
                    priority=None,
                    labels=None,
                    estimate=None,
                    due=None,
                    start=None,
                    parent=None,
                )

    def test_create_always_attaches_agentx_module(self):
        c, _ = self._run(title="hello")
        kwargs = c.create_issue.call_args.kwargs
        self.assertEqual(kwargs["module_ids"], ["agentx-id"])

    def test_create_attaches_agentx_even_when_resolvex_exists(self):
        # If both ResolveX and AgentX exist, the bot must still pin to AgentX.
        c = _fake_client()
        c.list_modules.return_value = [
            {"id": "resolvex-id", "name": "ResolveX"},
            {"id": "agentx-id", "name": "AgentX"},
        ]
        with patch("nirmaan.mcp_server._client", return_value=c):
            from nirmaan.mcp_server import create_ticket
            fn = getattr(create_ticket, "fn", create_ticket)
            result = fn(title="hello")
        kwargs = c.create_issue.call_args.kwargs
        self.assertEqual(kwargs["module_ids"], ["agentx-id"])
        self.assertNotIn("resolvex-id", kwargs["module_ids"])
        self.assertEqual(result["module"], "AgentX")

    def test_create_response_includes_module_name(self):
        c, result = self._run(title="hello")
        self.assertEqual(result["module"], "AgentX")

    def test_start_response_includes_module_name(self):
        from nirmaan.mcp_server import start_ticket
        c, result = self._run(_tool=start_ticket, title="x")
        self.assertEqual(result["module"], "AgentX")

    def test_done_response_includes_module_name(self):
        from nirmaan.mcp_server import done_ticket
        c, result = self._run(_tool=done_ticket, title="x")
        self.assertEqual(result["module"], "AgentX")

    def test_create_raises_if_agentx_module_missing(self):
        c = _fake_client()
        c.find_module.side_effect = lambda name: None
        c.list_modules.return_value = [{"id": "other-id", "name": "Other"}]
        with patch("nirmaan.mcp_server._client", return_value=c):
            with self.assertRaises(RuntimeError) as ctx:
                _do_create(
                    title="x",
                    description=None,
                    state="Todo",
                    priority=None,
                    labels=None,
                    estimate=None,
                    due=None,
                    start=None,
                    parent=None,
                )
        # Error message must name the missing module so the operator can act.
        self.assertIn("AgentX", str(ctx.exception))


class UpdateTicketAgentXEnforcementTests(unittest.TestCase):
    """update_ticket must refuse tickets that aren't in the AgentX module."""

    def _run(self, issue_modules):
        from nirmaan.mcp_server import update_ticket

        c = _fake_client()
        # _resolve_issue: UUID path → get_issue
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "modules": issue_modules,
        }
        # Fallback membership check (used when issue payload omits modules)
        c.list_module_issues.return_value = []

        fn = getattr(update_ticket, "fn", update_ticket)
        with patch("nirmaan.mcp_server._client", return_value=c):
            return c, fn(uuid_str, title="renamed")

    def test_refuses_ticket_outside_agentx(self):
        from nirmaan.mcp_server import update_ticket

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "modules": ["some-other-module-id"],
        }
        c.list_module_issues.return_value = []  # not a member

        fn = getattr(update_ticket, "fn", update_ticket)
        with patch("nirmaan.mcp_server._client", return_value=c):
            with self.assertRaises(PermissionError):
                fn(uuid_str, title="renamed")
        c.update_issue.assert_not_called()

    def test_allows_ticket_in_agentx(self):
        c, result = self._run(issue_modules=["agentx-id"])
        c.update_issue.assert_called_once()
        self.assertEqual(result["identifier"], "BUILDER-42")

    def test_falls_back_to_module_issues_lookup(self):
        # Issue payload doesn't list modules; module-issues endpoint confirms membership.
        from nirmaan.mcp_server import update_ticket

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {"id": uuid_str, "sequence_id": 42}
        c.list_module_issues.return_value = [{"id": uuid_str}]

        fn = getattr(update_ticket, "fn", update_ticket)
        with patch("nirmaan.mcp_server._client", return_value=c):
            result = fn(uuid_str, title="renamed")
        c.update_issue.assert_called_once()
        self.assertEqual(result["identifier"], "BUILDER-42")


class TransitionStateAgentXEnforcementTests(unittest.TestCase):
    """transition_state must refuse tickets that aren't in the AgentX module."""

    def test_refuses_ticket_outside_agentx(self):
        from nirmaan.mcp_server import transition_state

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "modules": ["some-other-module-id"],
        }
        c.list_module_issues.return_value = []

        fn = getattr(transition_state, "fn", transition_state)
        with patch("nirmaan.mcp_server._client", return_value=c):
            with self.assertRaises(PermissionError):
                fn(uuid_str, "Done")
        c.transition_state.assert_not_called()

    def test_allows_ticket_in_agentx(self):
        from nirmaan.mcp_server import transition_state

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "modules": ["agentx-id"],
        }

        fn = getattr(transition_state, "fn", transition_state)
        with patch("nirmaan.mcp_server._client", return_value=c):
            result = fn(uuid_str, "Done")
        c.transition_state.assert_called_once_with(uuid_str, "Done")
        self.assertEqual(result["identifier"], "BUILDER-42")


class AddCommentAgentXEnforcementTests(unittest.TestCase):
    """add_comment must refuse tickets that aren't in the AgentX module."""

    def test_refuses_ticket_outside_agentx(self):
        from nirmaan.mcp_server import add_comment

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "modules": ["some-other-module-id"],
        }
        c.list_module_issues.return_value = []

        fn = getattr(add_comment, "fn", add_comment)
        with patch("nirmaan.mcp_server._client", return_value=c):
            with self.assertRaises(PermissionError):
                fn(uuid_str, "hi")
        c.add_comment.assert_not_called()

    def test_allows_ticket_in_agentx(self):
        from nirmaan.mcp_server import add_comment

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "modules": ["agentx-id"],
        }

        fn = getattr(add_comment, "fn", add_comment)
        with patch("nirmaan.mcp_server._client", return_value=c):
            result = fn(uuid_str, "hi")
        c.add_comment.assert_called_once()
        self.assertEqual(result["identifier"], "BUILDER-42")


class GetTicketModulesTests(unittest.TestCase):
    """get_ticket must surface the modules a ticket belongs to."""

    def test_returns_module_names_from_issue_payload(self):
        from nirmaan.mcp_server import get_ticket

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "name": "x",
            "state": "todo-id",
            "modules": ["agentx-id"],
        }
        c.list_states.return_value = [{"id": "todo-id", "name": "Todo"}]
        c.list_labels.return_value = []
        c.list_modules.return_value = [
            {"id": "agentx-id", "name": "AgentX"},
            {"id": "resolvex-id", "name": "ResolveX"},
        ]

        fn = getattr(get_ticket, "fn", get_ticket)
        with patch("nirmaan.mcp_server._client", return_value=c):
            result = fn(uuid_str)
        self.assertEqual(result["modules"], ["AgentX"])

    def test_falls_back_to_module_issues_when_payload_sparse(self):
        # Issue payload omits module info; AgentX membership must still surface.
        from nirmaan.mcp_server import get_ticket

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "name": "x",
            "state": "todo-id",
        }
        c.list_states.return_value = [{"id": "todo-id", "name": "Todo"}]
        c.list_labels.return_value = []
        c.list_modules.return_value = [{"id": "agentx-id", "name": "AgentX"}]
        c.list_module_issues.return_value = [{"id": uuid_str}]

        fn = getattr(get_ticket, "fn", get_ticket)
        with patch("nirmaan.mcp_server._client", return_value=c):
            result = fn(uuid_str)
        self.assertEqual(result["modules"], ["AgentX"])

    def test_returns_empty_list_when_not_in_any_module(self):
        from nirmaan.mcp_server import get_ticket

        c = _fake_client()
        uuid_str = "12345678-1234-1234-1234-123456789012"
        c.get_issue.return_value = {
            "id": uuid_str,
            "sequence_id": 42,
            "name": "x",
            "state": "todo-id",
        }
        c.list_states.return_value = [{"id": "todo-id", "name": "Todo"}]
        c.list_labels.return_value = []
        c.list_modules.return_value = [{"id": "agentx-id", "name": "AgentX"}]
        c.list_module_issues.return_value = []  # not a member of AgentX either

        fn = getattr(get_ticket, "fn", get_ticket)
        with patch("nirmaan.mcp_server._client", return_value=c):
            result = fn(uuid_str)
        self.assertEqual(result["modules"], [])


class CreateToolSignatureTests(unittest.TestCase):
    """The create/start/done tools must not expose a module override knob."""

    def _signature_params(self, tool):
        import inspect

        fn = getattr(tool, "fn", tool)
        return set(inspect.signature(fn).parameters)

    def test_create_ticket_has_no_module_param(self):
        from nirmaan.mcp_server import create_ticket
        params = self._signature_params(create_ticket)
        self.assertNotIn("module", params)
        self.assertNotIn("module_id", params)
        self.assertNotIn("module_ids", params)
        self.assertNotIn("skip_default_module", params)

    def test_start_ticket_has_no_module_param(self):
        from nirmaan.mcp_server import start_ticket
        params = self._signature_params(start_ticket)
        self.assertNotIn("module", params)
        self.assertNotIn("module_id", params)
        self.assertNotIn("module_ids", params)
        self.assertNotIn("skip_default_module", params)

    def test_done_ticket_has_no_module_param(self):
        from nirmaan.mcp_server import done_ticket
        params = self._signature_params(done_ticket)
        self.assertNotIn("module", params)
        self.assertNotIn("module_id", params)
        self.assertNotIn("module_ids", params)
        self.assertNotIn("skip_default_module", params)


class ListMyTicketsEnvLoadingTests(unittest.TestCase):
    """Regression: PLANE_USER_ID set only in ~/.nirmaan.env must be picked up.

    Bug: list_my_tickets used to read PLANE_USER_ID via os.getenv before any
    dotenv loading happened, and the ~/.nirmaan.env fallback only fired when
    no local .env existed. So a value living only in ~/.nirmaan.env was
    invisible to the tool, even though every other config var loaded fine.
    """

    def test_user_id_loaded_from_home_nirmaan_env(self):
        from nirmaan.mcp_server import list_my_tickets
        from nirmaan import plane_client

        with tempfile.TemporaryDirectory() as fake_home:
            home = Path(fake_home)
            (home / ".nirmaan.env").write_text(
                "PLANE_BASE_URL=https://plane.test\n"
                "PLANE_API_KEY=test-key\n"
                "PLANE_WORKSPACE_SLUG=ws\n"
                "PLANE_PROJECT_ID=proj-1\n"
                "PLANE_USER_ID=user-uuid-123\n"
            )

            required = [
                "PLANE_BASE_URL", "PLANE_API_KEY", "PLANE_WORKSPACE_SLUG",
                "PLANE_PROJECT_ID", "PLANE_USER_ID",
            ]

            real_load_dotenv = plane_client.load_dotenv

            # Suppress the local-.env discovery step (load_dotenv() with no
            # args walks up from the source file and would find the repo's
            # real .env). Pass-through for explicit paths so ~/.nirmaan.env
            # still loads.
            def fake_load_dotenv(*args, **kwargs):
                if not args and "dotenv_path" not in kwargs:
                    return False
                return real_load_dotenv(*args, **kwargs)

            with patch.dict(os.environ, {}, clear=False), \
                 patch.object(Path, "home", return_value=home), \
                 patch.object(plane_client, "load_dotenv", side_effect=fake_load_dotenv):
                for k in required:
                    os.environ.pop(k, None)

                captured = {}
                fake = _fake_client()
                fake.list_issues.return_value = []
                fake.list_states.return_value = []

                def make_client(config):
                    captured["config"] = config
                    return fake

                fn = getattr(list_my_tickets, "fn", list_my_tickets)
                with patch("nirmaan.mcp_server.PlaneClient", side_effect=make_client):
                    result = fn()

                self.assertEqual(captured["config"].user_id, "user-uuid-123")
                self.assertEqual(result, [])
                # Implementation no longer trusts Plane's assignee/created_by
                # filters — it fetches candidates with expand and filters
                # locally. Just confirm a fetch happened.
                self.assertTrue(fake.list_issues.called)


class ListMyTicketsFilteringTests(unittest.TestCase):
    """The Plane API sometimes returns project-wide tickets even when filters
    are passed. list_my_tickets must filter locally by PLANE_USER_ID."""

    USER_ID = "user-uuid-123"
    OTHER_ID = "someone-else-uuid"

    def _candidates(self):
        # Three tickets returned by the API. Only BUILDER-1046 is assigned
        # to USER_ID; the others belong to someone else or have no assignees.
        return [
            {
                "sequence_id": 1044,
                "name": "not mine",
                "state": {"name": "Todo", "group": "unstarted"},
                "assignees": [{"id": self.OTHER_ID}],
                "created_by": self.OTHER_ID,
                "updated_at": "2026-05-08T00:00:00Z",
            },
            {
                "sequence_id": 1046,
                "name": "mine",
                "state": {"name": "In Progress", "group": "started"},
                "assignees": [{"id": self.USER_ID}],
                "created_by": self.OTHER_ID,
                "updated_at": "2026-05-09T00:00:00Z",
            },
            {
                "sequence_id": 1050,
                "name": "unassigned",
                "state": {"name": "Todo", "group": "unstarted"},
                "assignees": [],
                "created_by": self.OTHER_ID,
                "updated_at": "2026-05-10T00:00:00Z",
            },
            {
                "sequence_id": 900,
                "name": "mine but done",
                "state": {"name": "Done", "group": "completed"},
                "assignees": [{"id": self.USER_ID}],
                "created_by": self.USER_ID,
                "updated_at": "2026-05-01T00:00:00Z",
            },
        ]

    def _run(self, **kwargs):
        from nirmaan.mcp_server import list_my_tickets

        fake = _fake_client()
        fake.list_issues.return_value = self._candidates()
        fake.list_states.return_value = []

        config = MagicMock()
        config.user_id = self.USER_ID
        config.project_id = "proj-1"

        fn = getattr(list_my_tickets, "fn", list_my_tickets)
        with patch("nirmaan.mcp_server.PlaneConfig.from_env", return_value=config), \
             patch("nirmaan.mcp_server.PlaneClient", return_value=fake), \
             patch("nirmaan.mcp_server._client", return_value=fake):
            return fn(**kwargs)

    def test_default_scope_returns_only_assigned_active_tickets(self):
        # Default scope=assigned, include_done=False — only BUILDER-1046.
        result = self._run()
        self.assertEqual([r["identifier"] for r in result], ["BUILDER-1046"])

    def test_excludes_done_by_default(self):
        # BUILDER-900 is assigned to USER_ID but is Done, so it must be hidden.
        result = self._run()
        self.assertNotIn("BUILDER-900", [r["identifier"] for r in result])

    def test_include_done_surfaces_completed_assigned(self):
        result = self._run(include_done=True)
        ids = [r["identifier"] for r in result]
        self.assertIn("BUILDER-1046", ids)
        self.assertIn("BUILDER-900", ids)
        self.assertNotIn("BUILDER-1044", ids)
        self.assertNotIn("BUILDER-1050", ids)

    def test_scope_created_filters_by_creator(self):
        # USER_ID created only BUILDER-900 (which is Done — needs include_done).
        result = self._run(scope="created", include_done=True)
        self.assertEqual([r["identifier"] for r in result], ["BUILDER-900"])

    def test_scope_both_returns_assigned_or_created(self):
        result = self._run(scope="both", include_done=True)
        ids = [r["identifier"] for r in result]
        self.assertIn("BUILDER-1046", ids)
        self.assertIn("BUILDER-900", ids)
        self.assertNotIn("BUILDER-1044", ids)
        self.assertNotIn("BUILDER-1050", ids)

    def test_results_sorted_by_updated_at_desc(self):
        result = self._run(scope="both", include_done=True)
        # 1046 (2026-05-09) should come before 900 (2026-05-01).
        ids = [r["identifier"] for r in result]
        self.assertLess(ids.index("BUILDER-1046"), ids.index("BUILDER-900"))

    def test_invalid_scope_raises(self):
        with self.assertRaises(ValueError):
            self._run(scope="bogus")

    def test_missing_user_id_raises(self):
        from nirmaan.mcp_server import list_my_tickets

        config = MagicMock()
        config.user_id = None
        config.project_id = "proj-1"

        fn = getattr(list_my_tickets, "fn", list_my_tickets)
        with patch("nirmaan.mcp_server.PlaneConfig.from_env", return_value=config):
            with self.assertRaises(ValueError):
                fn()


if __name__ == "__main__":
    unittest.main()
