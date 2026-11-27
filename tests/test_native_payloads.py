"""Regression tests based on the sanitized native Windows CLI structure."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.db import get_conn, init_db
from app.execution import AppError, MissionService, extract_handle, extract_id
from app.native_payloads import project_status
from app.orca_adapter import OrcaAdapter, OrcaError


def native_status():
    return {"id": "request-status", "ok": True, "result": {
        "app": {}, "runtime": {"state": "ready", "connectionState": "connected", "appVersion": "1.4.278"}}}


def native_accounts():
    return {"id": "request-accounts", "ok": True, "result": {
        "codex": {"accounts": []}, "claude": {"accounts": []}, "opencode": {"accounts": []},
        "rateLimits": {
            "cursor": {"provider": "cursor", "weekly": None, "status": "ok", "planType": "free", "error": None},
            "codex": {"provider": "codex", "weekly": None, "status": "ok", "planType": "free", "error": None},
            "grok": {"provider": "grok", "weekly": None, "status": "error", "error": "private error text"},
            "claude": {"provider": "claude", "weekly": None, "status": "unavailable"},
        }}}


class NativePayloadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        db = Path(directory.name) / "test.db"
        init_db(db)
        self.conn = get_conn(db)
        self.addCleanup(self.conn.close)
        self.adapter = MagicMock()
        self.adapter.status.return_value = native_status()
        self.adapter.agent_installed.return_value = {"id": "request-installed", "ok": True, "result": {
            "agents": [{"id": name, "name": name} for name in ("cursor", "codex", "grok", "claude", "opencode")]}}
        self.adapter.account_list.return_value = native_accounts()
        self.service = MissionService(self.conn, self.adapter)

    def agents(self):
        return {row["id"]: row for row in self.service.capabilities()["agents"]}

    def test_native_rate_limits_enable_only_cursor_with_reported_evidence(self):
        agents = self.agents()
        self.assertTrue(agents["cursor"]["eligible"])
        self.assertIn("planType=free", agents["cursor"]["evidence"])
        self.assertIn("source=result.rateLimits", agents["cursor"]["evidence"])
        self.assertIsNone(agents["cursor"]["quota"])
        for name in ("codex", "grok", "claude", "opencode"):
            self.assertFalse(agents[name]["eligible"], name)
        self.assertNotIn("private error text", json.dumps(agents))

    def test_cursor_paid_plan_remains_blocked(self):
        self.adapter.account_list.return_value["result"]["rateLimits"]["cursor"]["planType"] = "pro"
        self.assertFalse(self.agents()["cursor"]["eligible"])

    def test_native_errors_override_legacy_eligible_rows(self):
        data = self.adapter.account_list.return_value
        data["accounts"] = [{"agent": "cursor", "planType": "free", "status": "ok"}]
        data["result"]["rateLimits"]["cursor"]["status"] = "error"
        self.assertFalse(self.agents()["cursor"]["eligible"])

    def test_inconsistent_native_error_blocks_dispatch(self):
        self.adapter.account_list.return_value["result"]["rateLimits"]["cursor"]["error"] = "error despite status ok"
        self.assertFalse(self.agents()["cursor"]["eligible"])

    def test_provider_identity_mismatch_is_not_borrowed(self):
        self.adapter.account_list.return_value["result"]["rateLimits"]["cursor"]["provider"] = "codex"
        self.assertFalse(self.agents()["cursor"]["eligible"])

    def test_failed_native_response_does_not_grant_eligibility(self):
        self.adapter.account_list.return_value["ok"] = False
        self.assertFalse(self.agents()["cursor"]["eligible"])

    def test_unknown_codex_quota_is_not_inferred_from_free_plan(self):
        self.assertIsNone(self.agents()["codex"]["quota"])
        self.assertFalse(self.agents()["codex"]["eligible"])

    def test_flat_legacy_account_payload_remains_supported(self):
        self.adapter.account_list.return_value = {"accounts": [{"agent": "cursor", "status": "ok", "planType": "free"}]}
        self.assertTrue(self.agents()["cursor"]["eligible"])

    def test_native_status_reports_version_and_projects_only_runtime_fields(self):
        data = native_status()
        data["result"]["runtime"]["privateMetadata"] = "private account identity"
        result = project_status(data)
        self.assertTrue(result["available"])
        self.assertEqual(result["version"], "1.4.278")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("request-status", json.dumps(result))

    def test_disconnected_native_runtime_blocks_dispatch(self):
        self.adapter.status.return_value["result"]["runtime"]["connectionState"] = "disconnected"
        self.assertFalse(project_status(self.adapter.status.return_value)["available"])
        with self.assertRaises(AppError) as error:
            self.service.require_eligible("cursor")
        self.assertEqual(error.exception.code, "orca_unavailable")
        self.adapter.worker_start.assert_not_called()

    def test_failed_status_envelope_is_not_available(self):
        data = native_status()
        data["ok"] = False
        self.assertFalse(project_status(data)["available"])

    def test_health_endpoint_uses_native_runtime_version(self):
        from app import main
        with patch.object(main, "get_adapter", return_value=self.adapter):
            body = main.api_health()
        self.assertEqual(body["orca"]["version"], "1.4.278")
        self.assertTrue(body["orca"]["available"])

    def test_resource_identifiers_do_not_use_request_correlation_id(self):
        for key in ("run_id", "task_id", "dispatch_id"):
            payload = {"id": "request-correlation", "ok": True, "result": {key: "native-resource"}}
            self.assertEqual(extract_id(payload, key, "id"), "native-resource")
        payload = {"id": "request-correlation", "ok": True, "result": {"handle": "native-terminal"}}
        self.assertEqual(extract_handle(payload), "native-terminal")

    def test_missing_result_id_never_falls_back_to_request_id(self):
        for result in ({}, None, [], {"ok": True}):
            payload = {"id": "request-correlation", "ok": True, "result": result}
            self.assertIsNone(extract_id(payload, "run_id", "id"))
            self.assertIsNone(extract_handle(payload))

    def test_nested_terminal_handle_and_flat_legacy_id(self):
        self.assertEqual(extract_handle({"id": "request", "ok": True, "result": {"terminal": {"handle": "term"}}}), "term")
        self.assertEqual(extract_id({"id": "native-run"}, "run_id", "id"), "native-run")
        self.assertEqual(extract_handle({"handle": "native-terminal"}), "native-terminal")

    def test_adapter_rejects_ok_false_even_with_zero_exit_status(self):
        process = MagicMock(returncode=0, stdout=b'{"id":"request","ok":false,"error":"private"}', stderr=b"")
        adapter = OrcaAdapter(runner=MagicMock(return_value=process))
        with self.assertRaises(OrcaError) as error:
            adapter.status()
        self.assertEqual(error.exception.code, "native_error")
        self.assertNotIn("private", str(error.exception))

    def test_native_planning_uses_resource_ids_end_to_end(self):
        self.adapter.terminal_create.return_value = {"id": "request-terminal", "ok": True, "result": {"terminal": {"handle": "terminal-native"}}}
        self.adapter.run_create.return_value = {"id": "request-run", "ok": True, "result": {"run_id": "run-native"}}
        self.adapter.task_create.return_value = {"id": "request-task", "ok": True, "result": {"task_id": "task-native"}}
        self.adapter.worker_start.return_value = {"id": "request-worker", "ok": True, "result": {"dispatch_id": "dispatch-native"}}
        with patch("app.execution.RUNS_DIR", self.root / "runs"), patch("app.execution.LOCK_FILE", self.root / "dispatch.lock"):
            mission = self.service.create_mission({"title": "Schema test", "objective": "Verify native identifiers",
                "quality_requirements": "Evidence", "scope": "output", "constraints": "Offline", "coordinator": "cursor"})
            result = self.service.start_plan(mission["id"])
        self.assertEqual(result["run_id"], "run-native")
        self.assertEqual(result["planner"]["dispatch_id"], "dispatch-native")
        self.assertEqual(self.adapter.task_create.call_args.kwargs["from_handle"], "terminal-native")
        self.assertEqual(self.adapter.worker_start.call_args.kwargs["run_id"], "run-native")
        self.assertEqual(self.adapter.worker_start.call_args.kwargs["task_id"], "task-native")


if __name__ == "__main__":
    unittest.main()
