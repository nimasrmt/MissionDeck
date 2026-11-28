"""Local secrets, exact native routes, explicit inference and failure handling."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import httpx
from fastapi.testclient import TestClient
from app import main
from app.api_models import APIConnections, APIModelError, normalize_url, test_http as probe_http
from app.db import get_conn, init_db, get_setting
from app.execution import MissionService
from app.security import SecurityError, redact, resolve_under_workspace
from tests.test_native_payloads import native_status

HTTPClient = httpx.Client


class APIModelsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.db = self.root / "state.db"
        init_db(self.db); self.conn = get_conn(self.db); self.addCleanup(self.conn.close)
        self.store = APIConnections(self.conn, self.root)
        self.body = {"name": "My model", "protocol": "openai", "base_url": "https://api.provider.example/v1", "model": "team/model-v1", "api_key": "test-private-value-58312", "context_budget": 8192, "output_budget": 1024}

    def add(self):
        return self.store.save(self.body)["connections"][0]

    def enable(self):
        row = self.add()
        with patch("app.api_models.native_version", return_value=1), patch("app.api_models.test_http"), patch.object(self.store, "_probe_native"):
            self.store.probe(row["id"])
        return self.store.public()["connections"][0]

    def test_save_and_reads_do_not_send_requests_or_return_keys(self):
        with patch("app.api_models.httpx.Client") as client, patch("app.api_models.subprocess.run") as cli:
            row = self.add(); self.store.public(); self.store.routes()
            client.assert_not_called(); cli.assert_not_called()
        self.assertFalse(row["verified"]); self.assertIsNone(row["price"]); self.assertIsNone(row["quota"])
        self.assertNotIn(self.body["api_key"], json.dumps(row))
        self.assertNotIn(self.body["api_key"], get_setting(self.conn, "user_api_connections"))
        self.assertNotIn(self.body["api_key"], redact("Provider echoed " + self.body["api_key"]))

    def test_test_enables_exact_model_in_native_config_and_preserves_other_settings(self):
        self.store.config.parent.mkdir()
        self.store.config.write_text(json.dumps({"model": "keep/model", "provider": {"existing": {"name": "Existing"}}}))
        row = self.enable(); data = json.loads(self.store.config.read_text())
        self.assertEqual(data["model"], "keep/model"); self.assertIn("existing", data["provider"])
        entry = data["provider"][row["id"]]
        self.assertEqual(entry["options"]["baseURL"], self.body["base_url"])
        self.assertTrue(entry["options"]["apiKey"].startswith("{file:"))
        self.assertNotIn(self.body["api_key"], json.dumps(data))
        self.assertEqual(entry["models"][self.body["model"]]["limit"]["context"], 8192)
        with patch("app.api_models.check_public_host"), patch.object(self.store, "_probe_native"):
            self.assertEqual(self.store.require(row["route"]), row["route"])
            with self.assertRaises(APIModelError): self.store.require(row["route"] + "-other")

    def test_v2_native_config_uses_the_v2_provider_shape(self):
        row = self.add()
        with patch("app.api_models.native_version", return_value=2), patch("app.api_models.test_http"), patch.object(self.store, "_probe_native"):
            self.store.probe(row["id"])
        entry = json.loads(self.store.config.read_text())["providers"][row["id"]]
        self.assertEqual(entry["package"], "@opencode/ai/providers/openai-compatible")
        self.assertEqual(entry["models"][self.body["model"]]["capabilities"], {"tools": True})

    def test_edit_invalidates_test_and_a_new_endpoint_requires_a_key(self):
        row = self.enable()
        changed = {**self.body, "model": "another", "api_key": ""}
        self.assertFalse(self.store.save(changed, row["id"])["connections"][0]["verified"])
        self.assertEqual(self.store._key(self.store._rows()[0]), self.body["api_key"])
        with self.assertRaises(APIModelError): self.store.save({**changed, "base_url": "https://other.example/v1"}, row["id"])

    def test_changed_native_config_is_not_overwritten_and_disables_dispatch(self):
        row = self.enable(); data = json.loads(self.store.config.read_text())
        data["provider"][row["id"]]["options"]["baseURL"] = "https://other.example/v1"
        self.store.config.write_text(json.dumps(data))
        self.assertFalse(self.store.public()["connections"][0]["verified"])
        with patch("app.api_models.native_version", return_value=1), self.assertRaises(APIModelError): self.store.probe(row["id"])
        self.assertEqual(json.loads(self.store.config.read_text()), data)
        with self.assertRaises(APIModelError): self.store.delete(row["id"])

    def test_changed_key_file_requires_a_new_save_and_test(self):
        row = self.enable(); private = self.store.private / self.store._rows()[0]["key_file"]
        private.write_text("a-different-key-value")
        self.assertFalse(self.store.public()["connections"][0]["verified"])
        with self.assertRaises(APIModelError): self.store.require(row["route"])

    def test_failed_test_never_leaves_route_enabled(self):
        row = self.enable()
        with patch("app.api_models.native_version", return_value=1), patch("app.api_models.test_http", side_effect=APIModelError("api_response", "HTTP 401")):
            with self.assertRaises(APIModelError): self.store.probe(row["id"])
        self.assertFalse(self.store.public()["connections"][0]["verified"])
        self.assertEqual(self.store.public()["connections"][0]["last_error"], "HTTP 401")

    def test_delete_removes_only_its_configuration_and_key_files(self):
        row = self.enable(); before = list(self.store.private.glob("*.key")); self.assertTrue(before)
        self.store.delete(row["id"])
        self.assertEqual(self.store.public()["connections"], [])
        self.assertFalse(list(self.store.private.glob("*.key")))
        self.assertNotIn(row["id"], json.loads(self.store.config.read_text())["provider"])

    def test_rejects_private_urls_credentials_queries_and_substitution(self):
        for url in ["http://api.example/v1", "https://127.0.0.1/v1", "https://169.254.169.254", "https://localhost/v1", "https://key@api.example/v1", "https://api.example/v1?key=x", "https://api.example:8765", "https://api.example/{env:SECRET}"]:
            with self.subTest(url=url), self.assertRaises(APIModelError): normalize_url(url)
        for value in [".orca-runs/private/apis/key.key", ".opencode/opencode.json", ".ORCA-RUNS/PRIVATE/apis/key.key"]:
            with self.subTest(value=value), self.assertRaises(SecurityError): resolve_under_workspace(value, workspace=self.root)

    def test_does_not_write_through_symlinks(self):
        outside = self.root / "elsewhere"; outside.mkdir()
        (self.root / ".orca-runs").mkdir(exist_ok=True)
        (self.root / ".orca-runs/private").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(APIModelError): self.add()

    def test_private_credentials_cannot_be_downloaded_through_a_workspace_alias(self):
        self.add(); private = next(self.store.private.glob("*.key"))
        alias = self.root / "public.txt"; alias.symlink_to(private)
        with self.assertRaises(SecurityError): resolve_under_workspace("public.txt", workspace=self.root)

    def test_native_catalog_must_report_the_exact_requested_model(self):
        row = self.add(); raw = self.store._rows()[0]
        with patch("app.api_models.opencode_executable", return_value="opencode"), patch("app.api_models.subprocess.run", return_value=MagicMock(returncode=0, stdout=b"other/model\n")):
            with self.assertRaises(APIModelError): self.store._probe_native(raw)

    def test_http_test_is_one_bounded_tool_request_and_does_not_echo_errors(self):
        sent = []
        def handler(request):
            sent.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {"name": "missiondeck_connection_check", "arguments": '{"ok":true}'}}]}}]})
        with patch("app.api_models.check_public_host"), patch("app.api_models.httpx.Client", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(handler), **kw)):
            probe_http(self.body, self.body["api_key"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].headers["authorization"], "Bearer " + self.body["api_key"])
        payload = json.loads(sent[0].content); self.assertEqual(payload["max_tokens"], 128)
        self.assertNotIn(self.body["api_key"], json.dumps(payload))
        with patch("app.api_models.check_public_host"), patch("app.api_models.httpx.Client", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(lambda req: httpx.Response(401, text=self.body["api_key"])), **kw)):
            with self.assertRaises(APIModelError) as error: probe_http(self.body, self.body["api_key"])
        self.assertNotIn(self.body["api_key"], str(error.exception))

    def test_anthropic_test_checks_a_real_tool_use_block(self):
        sent = []
        def handler(request):
            sent.append(request)
            return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "missiondeck_connection_check", "input": {"ok": True}}]})
        with patch("app.api_models.check_public_host"), patch("app.api_models.httpx.Client", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(handler), **kw)):
            probe_http({**self.body, "protocol": "anthropic"}, self.body["api_key"])
        self.assertTrue(str(sent[0].url).endswith("/messages"))
        self.assertEqual(sent[0].headers["x-api-key"], self.body["api_key"])

    def test_custom_routes_use_native_workers_without_free_probe_or_fallback(self):
        row = self.enable()
        adapter = MagicMock(); adapter.status.return_value = native_status()
        adapter.agent_installed.return_value = {"agents": [{"id": "opencode"}]}; adapter.account_list.return_value = {}
        service = MissionService(self.conn, adapter)
        with patch("app.execution.WORKSPACE", self.root), patch("app.api_models.check_public_host"), patch.object(APIConnections, "_probe_native"), patch.object(service, "refresh_free_models") as free_probe:
            self.assertEqual(service.require_eligible("opencode", row["route"]), row["route"])
            free_probe.assert_not_called()
            with self.assertRaises(Exception): service.require_eligible("opencode", "mdapi_0000000000000000/no-such-model")
            from app.db import set_setting
            set_setting(self.conn, "user_api_pin", row["route"]); self.conn.commit()
            with self.assertRaises(Exception): service.require_eligible("opencode", "unknown")

    def test_settings_endpoints_are_csrf_protected_write_only_and_no_inference_on_get(self):
        adapter = MagicMock(); adapter.status.return_value = native_status(); adapter.agent_installed.return_value = {"agents": []}; adapter.account_list.return_value = {}
        for p in [patch("app.main.WORKSPACE", self.root), patch("app.execution.WORKSPACE", self.root), patch("app.execution.LOCK_FILE", self.root / "lock")]:
            p.start(); self.addCleanup(p.stop)
        main.set_db_path(self.db); self.addCleanup(main.set_db_path, None)
        main.set_adapter_factory(lambda: adapter); self.addCleanup(main.set_adapter_factory, None)
        with TestClient(main.app, base_url="http://127.0.0.1:8765") as client, patch("app.api_models.test_http") as inference:
            self.assertEqual(client.post("/api/api-models", json=self.body).status_code, 403)
            token = client.get("/api/session").json()["token"]; headers = {"X-CC-Token": token}
            response = client.post("/api/api-models", json=self.body, headers=headers)
            self.assertEqual(response.status_code, 200); self.assertNotIn(self.body["api_key"], response.text)
            self.assertEqual(client.get("/api/api-models").status_code, 200)
            invalid = client.post("/api/api-models", json={**self.body, "api_key": "secret" * 700}, headers=headers)
            self.assertEqual(invalid.status_code, 422); self.assertNotIn("secretsecret", invalid.text)
            inference.assert_not_called()

    def test_manager_dispatch_passes_the_exact_api_model_without_credentials_in_specs(self):
        from app.manager_dialogue import send
        row = self.enable()
        adapter = MagicMock(); adapter.status.return_value = native_status()
        adapter.agent_installed.return_value = {"agents": [{"id": "opencode"}]}; adapter.account_list.return_value = {}
        adapter.terminal_create.return_value = {"handle": "coord_api"}
        adapter.run_create.return_value = {"run_id": "run_api"}
        adapter.task_create.side_effect = [{"task_id": "advice"}, {"task_id": "reply"}]
        adapter.worker_start.return_value = {"dispatch_id": "api_dispatch"}
        adapter.worker_show.return_value = {"state": "running"}
        service = MissionService(self.conn, adapter)
        with patch("app.execution.WORKSPACE", self.root), patch("app.execution.RUNS_DIR", self.root / "runs"), patch("app.execution.LOCK_FILE", self.root / "start.lock"), patch("app.api_models.check_public_host"), patch.object(APIConnections, "_probe_native"), patch.object(service, "refresh_free_models") as free_probe:
            parent = service.create_mission({"title": "API mission", "objective": "Give bounded advice", "quality_requirements": "Checked", "scope": "out", "constraints": "Use my selected API", "coordinator": "opencode"})
            send(service, parent["id"], message="Discuss this goal", advisors=[{"agent": "opencode", "model": row["route"]}], block_id="MGR", request_id="api_request_12345")
            self.assertEqual(adapter.worker_start.call_args.kwargs["model"], row["route"])
            self.assertEqual(adapter.worker_start.call_count, 1)
            free_probe.assert_not_called()
        specs = list((self.root / "runs").glob("**/specs/*.md")); self.assertTrue(specs)
        for spec in specs: self.assertNotIn(self.body["api_key"], spec.read_text())
        self.assertIsNone(service.get_mission(parent["id"])["plan"])


if __name__ == "__main__": unittest.main()
