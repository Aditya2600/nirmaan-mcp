"""Unit tests for nirmaan.plane_client using httpx.MockTransport."""
from __future__ import annotations

import json
import unittest
from typing import Any, Callable

import httpx

from nirmaan.plane_client import (
    PlaneClient,
    PlaneConfig,
    PlaneError,
    PlaneModuleAttachError,
)


CONFIG = PlaneConfig(
    base_url="https://plane.test",
    api_key="test-key",
    workspace_slug="ws",
    project_id="proj-1",
)


def _client_with_handler(handler: Callable[[httpx.Request], httpx.Response]) -> PlaneClient:
    """Build a PlaneClient whose underlying HTTP client routes through MockTransport."""
    client = PlaneClient(CONFIG)
    client._client.close()
    client._client = httpx.Client(
        base_url=f"{CONFIG.base_url}/api/v1",
        headers={"X-API-Key": CONFIG.api_key, "Content-Type": "application/json"},
        transport=httpx.MockTransport(handler),
    )
    return client


class UnwrapResultsTests(unittest.TestCase):
    def test_list_passthrough(self):
        self.assertEqual(PlaneClient._unwrap_results([{"a": 1}]), [{"a": 1}])

    def test_empty_list_passthrough(self):
        self.assertEqual(PlaneClient._unwrap_results([]), [])

    def test_paginated_results_key(self):
        self.assertEqual(
            PlaneClient._unwrap_results({"count": 1, "results": [{"a": 1}]}),
            [{"a": 1}],
        )

    def test_paginated_items_key(self):
        self.assertEqual(
            PlaneClient._unwrap_results({"items": [{"b": 2}]}), [{"b": 2}]
        )

    def test_unknown_dict_shape_raises(self):
        with self.assertRaises(PlaneError) as ctx:
            PlaneClient._unwrap_results({"foo": "bar"})
        self.assertIn("foo", str(ctx.exception))

    def test_none_raises(self):
        with self.assertRaises(PlaneError):
            PlaneClient._unwrap_results(None)

    def test_string_raises(self):
        with self.assertRaises(PlaneError):
            PlaneClient._unwrap_results("not a list")


class CreateIssuePayloadTests(unittest.TestCase):
    def test_payload_assembly_translates_friendly_kwargs(self):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": "issue-1", "sequence_id": 42})

        with _client_with_handler(handler) as c:
            issue = c.create_issue(
                name="Do thing",
                description_html="<p>desc</p>",
                state_id="state-1",
                priority="high",
                assignee_ids=["u-1"],
                label_ids=["l-1", "l-2"],
                estimate_point=3,
                start_date="2026-05-10",
                target_date="2026-05-12",
                parent="parent-1",
            )

        self.assertEqual(issue["sequence_id"], 42)
        self.assertEqual(captured["path"], "/api/v1/workspaces/ws/projects/proj-1/issues/")
        self.assertEqual(
            captured["body"],
            {
                "name": "Do thing",
                "description_html": "<p>desc</p>",
                "state": "state-1",
                "priority": "high",
                "assignees": ["u-1"],
                "labels": ["l-1", "l-2"],
                "estimate_point": 3,
                "start_date": "2026-05-10",
                "target_date": "2026-05-12",
                "parent": "parent-1",
            },
        )

    def test_minimal_payload_omits_unset_fields(self):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"id": "issue-1"})

        with _client_with_handler(handler) as c:
            c.create_issue(name="just a title")

        self.assertEqual(captured["body"], {"name": "just a title"})

    def test_invalid_priority_raises_before_request(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            self.fail("should not hit the network")

        with _client_with_handler(handler) as c:
            with self.assertRaises(ValueError):
                c.create_issue(name="x", priority="bogus")


class ModuleAttachPartialFailureTests(unittest.TestCase):
    def test_attach_failure_raises_but_preserves_issue(self):
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            calls.append(f"{request.method} {path}")
            if path.endswith("/issues/") and request.method == "POST":
                return httpx.Response(
                    201, json={"id": "issue-1", "sequence_id": 7}
                )
            if "/modules/mod-good/module-issues/" in path:
                return httpx.Response(200, json={"ok": True})
            if "/modules/mod-bad/module-issues/" in path:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(404)

        with _client_with_handler(handler) as c:
            with self.assertRaises(PlaneModuleAttachError) as ctx:
                c.create_issue(
                    name="x", module_ids=["mod-good", "mod-bad"]
                )

        err = ctx.exception
        self.assertEqual(err.issue["id"], "issue-1")
        self.assertEqual(err.issue["sequence_id"], 7)
        # Both attach attempts ran (we don't bail on first failure)
        self.assertEqual([m for m, _ in err.errors], ["mod-bad"])
        self.assertIn("7", str(err))
        self.assertIn("mod-bad", str(err))
        # Sanity: PlaneModuleAttachError is catchable as PlaneError
        self.assertIsInstance(err, PlaneError)

    def test_all_attaches_succeed_returns_issue(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/issues/") and request.method == "POST":
                return httpx.Response(201, json={"id": "issue-1", "sequence_id": 8})
            return httpx.Response(200, json={"ok": True})

        with _client_with_handler(handler) as c:
            issue = c.create_issue(name="x", module_ids=["mod-1"])

        self.assertEqual(issue["sequence_id"], 8)


class UpdateIssueTests(unittest.TestCase):
    def test_friendly_kwargs_translated(self):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            captured["method"] = request.method
            return httpx.Response(200, json={"id": "issue-1"})

        with _client_with_handler(handler) as c:
            c.update_issue(
                "issue-1",
                assignee_ids=["u-1"],
                label_ids=["l-1"],
                priority="low",
            )

        self.assertEqual(captured["method"], "PATCH")
        self.assertEqual(
            captured["body"],
            {"assignees": ["u-1"], "labels": ["l-1"], "priority": "low"},
        )

    def test_module_ids_attached_after_patch(self):
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": "issue-1"})
            # module attach
            return httpx.Response(200, json={"ok": True})

        with _client_with_handler(handler) as c:
            c.update_issue("issue-1", priority="high", module_ids=["mod-1"])

        # The PATCH body must NOT contain module_ids (it's not a Plane field
        # on the issue PATCH endpoint).
        self.assertEqual(calls[0][0], "PATCH")
        self.assertEqual(calls[1][0], "POST")
        self.assertIn("/modules/mod-1/module-issues/", calls[1][1])

    def test_module_only_update_skips_patch(self):
        """If the only kwarg is module_ids, we shouldn't send an empty PATCH."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json={"id": "issue-1"})
            return httpx.Response(200, json={"ok": True})

        with _client_with_handler(handler) as c:
            c.update_issue("issue-1", module_ids=["mod-1"])

        self.assertNotIn("PATCH", calls)
        self.assertIn("POST", calls)

    def test_invalid_priority_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            self.fail("should not hit the network")

        with _client_with_handler(handler) as c:
            with self.assertRaises(ValueError):
                c.update_issue("issue-1", priority="bogus")


class MetadataCacheTests(unittest.TestCase):
    def _states_handler(self, calls: list[str]):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json={"results": [{"id": "s-1", "name": "Done"}]})
        return handler

    def test_repeated_list_states_hits_api_once(self):
        calls: list[str] = []
        with _client_with_handler(self._states_handler(calls)) as c:
            c.list_states()
            c.list_states()
            c.list_states()
        self.assertEqual(len(calls), 1)

    def test_find_helpers_share_cache(self):
        calls: list[str] = []
        with _client_with_handler(self._states_handler(calls)) as c:
            self.assertIsNotNone(c.find_state("done"))
            self.assertIsNotNone(c.find_state("DONE"))
            self.assertIsNone(c.find_state("nope"))
        self.assertEqual(len(calls), 1)

    def test_invalidate_forces_refetch(self):
        calls: list[str] = []
        with _client_with_handler(self._states_handler(calls)) as c:
            c.list_states()
            c.invalidate_metadata_cache()
            c.list_states()
        self.assertEqual(len(calls), 2)

    def test_ttl_expiry_refetches(self):
        calls: list[str] = []
        with _client_with_handler(self._states_handler(calls)) as c:
            c.list_states()
            # Expire the cached entry by rewinding its expiry into the past.
            for key, (_, value) in list(c._metadata_cache.items()):
                c._metadata_cache[key] = (0.0, value)
            c.list_states()
        self.assertEqual(len(calls), 2)

    def test_cycles_not_cached(self):
        """Cycles are time-sensitive (get_current_cycle) and intentionally uncached."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(200, json={"results": []})

        with _client_with_handler(handler) as c:
            c.list_cycles()
            c.list_cycles()
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
