"""Workflow edits are validated, versioned, and never dispatch native workers."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from app import main
from app.db import get_conn, init_db
from app.execution import AppError, MissionService
from tests.test_native_payloads import native_accounts, native_status


def plan():
    return {"schema_version": 1, "mission_summary": "Build a checked output", "architecture": "One step",
            "selection_rationale": "Included agent", "departments": [{"id": "work", "name": "Work"}],
            "tasks": [{"id": "T1", "title": "Create report", "department": "work", "role": "implementation",
                       "agent": "cursor", "model": "unknown", "deps": [], "write_scope": ["smoke/report.md"],
                       "inputs": [], "outputs": ["smoke/report.md"], "acceptance": ["File exists"],
                       "validation": ["python smoke/check.py"], "review_required": False}],
            "quality_gates": [], "risks": []}


class WorkflowEditorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "test.db"
        init_db(self.db)
        self.conn = get_conn(self.db)
        self.addCleanup(self.conn.close)
        self.adapter = MagicMock()
        self.adapter.status.return_value = native_status()
        self.adapter.agent_installed.return_value = {"agents": [{"id": "cursor"}]}
        self.adapter.account_list.return_value = native_accounts()
        self.service = MissionService(self.conn, self.adapter)
        for item in (patch("app.execution.RUNS_DIR", self.root / "runs"), patch("app.execution.LOCK_FILE", self.root / "lock")):
            item.start(); self.addCleanup(item.stop)
        self.mission = self.service.create_mission({"title": "Editor test", "objective": "Real outputs", "quality_requirements": "Checked",
                                                     "scope": "smoke/", "constraints": "No network", "coordinator": "cursor"})

    def test_save_valid_plan_only_reads_native_capabilities(self):
        saved = self.service.save_plan(self.mission["id"], plan(), 0)
        self.assertEqual(saved["plan_version"], 1)
        self.assertEqual(saved["status"], "awaiting_approval")
        self.assertTrue(saved["can"]["approve"])
        self.assertEqual({c[0] for c in self.adapter.method_calls}, {"status", "agent_installed", "account_list"})

    def test_edit_after_approval_requires_new_approval(self):
        mid = self.mission["id"]
        self.service.save_plan(mid, plan(), 0)
        self.service.approve(mid, 1)
        updated = plan(); updated["tasks"][0]["title"] = "Edited report"
        saved = self.service.save_plan(mid, updated, 2)
        self.assertEqual(saved["plan_version"], 3)
        self.assertIsNone(saved["approved_at"])
        self.assertFalse(saved["can"]["start"])
        with self.assertRaises(AppError) as error: self.service.approve(mid, 1)
        self.assertEqual(error.exception.code, "version_conflict")
        self.assertEqual(self.service.approve(mid, 3)["status"], "approved")

    def test_stale_edit_does_not_overwrite_current_plan(self):
        mid = self.mission["id"]
        self.service.save_plan(mid, plan(), 0)
        with self.assertRaises(AppError) as error: self.service.save_plan(mid, plan(), 0)
        self.assertEqual(error.exception.code, "version_conflict")
        self.assertEqual(self.service.get_mission(mid)["plan_version"], 1)

    def test_cycle_rejected_without_changing_saved_state(self):
        data = plan(); second = copy.deepcopy(data["tasks"][0]); second.update(id="T2", deps=["T1"], outputs=["smoke/other.md"], write_scope=["smoke/other.md"])
        data["tasks"][0]["deps"] = ["T2"]; data["tasks"].append(second)
        with self.assertRaises(AppError) as error: self.service.save_plan(self.mission["id"], data, 0)
        self.assertIn("cycle", str(error.exception.details))
        self.assertEqual(self.service.get_mission(self.mission["id"])["status"], "draft")

    def test_unsafe_output_and_unverified_agents_rejected(self):
        for changes in ({"outputs": ["../escape.txt"]}, {"agent": "claude"}, {"outputs": []}, {"acceptance": []}):
            data = plan(); data["tasks"][0].update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(AppError): self.service.save_plan(self.mission["id"], data, 0)

    def test_unsupported_validation_rejected_before_execution(self):
        data = plan(); data["tasks"][0]["validation"] = ["python -c print(1)"]
        with self.assertRaises(AppError): self.service.save_plan(self.mission["id"], data, 0)
        self.adapter.worker_start.assert_not_called()

    def test_running_or_unknown_mission_plan_is_immutable(self):
        for status in ("planning", "running", "review", "unknown", "cancelled", "succeeded"):
            self.conn.execute("UPDATE missions SET status = ? WHERE id = ?", (status, self.mission["id"]))
            self.conn.commit()
            with self.subTest(status=status):
                with self.assertRaises(AppError): self.service.save_plan(self.mission["id"], plan(), 0)

    def test_http_put_requires_csrf_and_rejects_stale_version(self):
        with patch.object(main, "_db_path", self.db), patch.object(main, "get_adapter", return_value=self.adapter):
            with TestClient(main.app, base_url="http://127.0.0.1:8765") as client:
                url = f"/api/missions/{self.mission['id']}/plan"
                body = {"plan_version": 0, "plan": plan()}
                self.assertEqual(client.put(url, json=body).status_code, 403)
                token = client.get("/api/session").json()["token"]
                self.assertEqual(client.put(url, json=body, headers={"X-CC-Token": token}).status_code, 200)
                self.assertEqual(client.put(url, json=body, headers={"X-CC-Token": token}).status_code, 409)
                self.assertIn("/static/planner.js", client.get("/").text)
                self.assertEqual(client.get("/monitor.html").status_code, 200)
        self.adapter.worker_start.assert_not_called()


if __name__ == "__main__": unittest.main()
