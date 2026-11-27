"""Dispatch guards tested without Orca, network or inference."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.db import get_conn, init_db
from app.execution import AppError, MissionService


class DispatchPreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        db = self.root / "state.db"
        init_db(db)
        self.conn = get_conn(db)
        self.addCleanup(self.conn.close)
        self.adapter = MagicMock()
        self.adapter.agent_installed.return_value = {"agents": [{"id": "cursor"}]}
        self.adapter.account_list.return_value = {"accounts": [{"agent": "cursor", "planType": "free", "status": "ok"}]}
        self.adapter.status.return_value = {"ok": True}
        self.service = MissionService(self.conn, self.adapter)
        for item in (patch("app.execution.RUNS_DIR", self.root / "runs"), patch("app.execution.LOCK_FILE", self.root / "dispatch.lock")):
            item.start()
            self.addCleanup(item.stop)

    def create(self, coordinator="cursor"):
        return self.service.create_mission({"title": "test", "objective": "goal", "quality_requirements": "check",
                                            "scope": "output", "constraints": "offline", "coordinator": coordinator})

    def assert_no_native_mutations(self):
        for name in ("terminal_create", "run_create", "task_create", "worker_start", "run_use"):
            getattr(self.adapter, name).assert_not_called()

    def test_missing_evidence_blocks_routes(self):
        for agent in ("cursor", "grok", "opencode", "codex", "claude"):
            with self.subTest(agent=agent):
                self.assertFalse(self.service._eligibility(agent, {"installed": True}, [])["eligible"])

    def test_zero_codex_quota_is_known_and_eligible(self):
        row = self.service._eligibility("codex", {"installed": True}, [{"agent": "codex", "status": "ok", "quotaUsed": 0}])
        self.assertEqual(row["quota"], 0)
        self.assertTrue(row["eligible"])

    def test_invalid_codex_quotas_block_dispatch(self):
        for value in (-1, 100, True, float("nan"), "0"):
            with self.subTest(value=value):
                self.assertFalse(self.service._eligibility("codex", {"installed": True}, [{"agent": "codex", "status": "ok", "quotaUsed": value}])["eligible"])

    def test_planning_rechecks_agent_before_native_mutation(self):
        m = self.create("claude")
        with self.assertRaises(AppError) as error:
            self.service.start_plan(m["id"])
        self.assertEqual(error.exception.code, "agent_ineligible")
        self.assert_no_native_mutations()

    def test_planning_includes_planner_in_global_worker_cap(self):
        m = self.create()
        with patch.object(self.service, "count_active_dispatches", return_value=2):
            with self.assertRaises(AppError) as error:
                self.service.start_plan(m["id"])
        self.assertEqual(error.exception.code, "worker_cap")
        self.assert_no_native_mutations()

    def test_model_override_requires_verified_route(self):
        with self.assertRaises(AppError) as error:
            self.service.require_eligible("cursor", "unverified-premium-model")
        self.assertEqual(error.exception.code, "model_unverified")

    def test_unavailable_accounts_revoke_previously_eligible_agent(self):
        self.service.require_eligible("cursor")
        self.adapter.account_list.return_value = {"accounts": []}
        with self.assertRaises(AppError):
            self.service.require_eligible("cursor")

    def test_no_eligible_agents_does_not_disable_plan_validation(self):
        m = self.create()
        self.adapter.account_list.return_value = {"accounts": []}
        with patch("app.execution.load_and_validate_plan_file", side_effect=AppError("test", "stop")) as load:
            with self.assertRaises(AppError):
                self.service._ingest_plan(m["id"])
            self.assertEqual(load.call_args.kwargs["eligible_agents"], set())

    def test_closed_mission_cannot_retry(self):
        m = self.create()
        self.conn.execute("UPDATE missions SET status = 'cancelled', cancel_requested = 1 WHERE id = ?", (m["id"],))
        self.conn.commit()
        with self.assertRaises(AppError):
            self.service.retry_task(m["id"], "missing-task")
        self.assert_no_native_mutations()

    def test_auto_refresh_defaults_off(self):
        row = self.conn.execute("SELECT value FROM settings WHERE key = 'auto_refresh_seconds'").fetchone()
        self.assertEqual(row["value"], "0")


if __name__ == "__main__":
    unittest.main()
