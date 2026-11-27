"""Ensure diagnostic reports cannot copy arbitrary account text or map keys."""
import json
import unittest
from unittest.mock import MagicMock

from app.orca_adapter import OrcaError
from diagnose_orca import collect, project


class DiagnosticTests(unittest.TestCase):
    def test_keeps_nested_account_schema_and_usage(self):
        data = {"ok": True, "result": {"groups": [{"providerId": "cursor", "accounts": [
            {"accountId": "private-id", "usage": {"planType": "free", "status": "ok", "usedPercent": 0}, "authType": "oauth"}
        ]}]}}
        out = project(data)
        account = out["result"]["groups"][0]["accounts"][0]
        self.assertEqual(account["usage"]["planType"], "free")
        self.assertEqual(account["usage"]["usedPercent"], 0)
        self.assertEqual(account["accountId"], "<text omitted>")

    def test_omits_secrets_identities_arbitrary_errors_and_dynamic_keys(self):
        private = ["private@example.test", "arbitrary-access-secret", "private-account-name", "C:/private/user/path"]
        data = {"apiKey": private[1], "email": private[0], "name": private[2], "error": private[3],
                "metadata": {private[0]: "value", private[1]: "value"}, "details": {"password": private[1]}}
        encoded = json.dumps(project(data))
        for secret in private:
            self.assertNotIn(secret, encoded)
        self.assertNotIn("apiKey", encoded)
        self.assertNotIn("password", encoded)

    def test_non_finite_usage_is_json_safe(self):
        out = project({"quotaUsed": float("nan"), "usedPercent": float("inf")})
        json.dumps(out, allow_nan=False)
        self.assertEqual(out["quotaUsed"], "<number>")

    def test_version_is_retained_without_raw_status_text(self):
        out = project({"result": {"app": {"version": "1.4.278", "name": "private account title"}}})
        self.assertEqual(out["result"]["app"]["version"], "1.4.278")
        self.assertEqual(out["result"]["app"]["name"], "<text omitted>")

    def test_array_and_depth_are_bounded(self):
        out = project({"accounts": [{"agent": "cursor"}] * 100})
        self.assertEqual(len(out["accounts"]), 13)
        self.assertEqual(out["accounts"][-1]["_truncated_items"], 88)
        nested = {}
        for _ in range(30):
            nested = {"result": nested}
        self.assertIn("<truncated>", json.dumps(project(nested)))

    def test_collect_only_uses_three_read_operations_and_masks_error_details(self):
        adapter = MagicMock()
        adapter.status.return_value = {"ok": True}
        adapter.agent_installed.return_value = {"agents": [{"id": "cursor"}]}
        adapter.account_list.side_effect = OrcaError("cli_error", "private@example.test", {"secret": "private-token"})
        out = collect(adapter)
        self.assertEqual([call[0] for call in adapter.mock_calls], ["status", "agent_installed", "account_list"])
        self.assertEqual(out["accounts"], {"error": "cli_error"})
        self.assertNotIn("private", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
