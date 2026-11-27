"""Read-only monitor contract using the native response envelope."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main
from app.orca_adapter import OrcaAdapter, OrcaError
from app.session_monitor import project_sessions


def native_response():
    return {"ok": True, "result": {"agents": [{
        "orcaSessionId": "12345678-1234-1234-1234-123456789abc",
        "kind": "chat", "agent": "kilo", "state": "working",
        "workspace": "~", "repo": "~",
        "title": "private prompt", "lastAssistantMessage": "private response",
        "worktreeId": "private path", "updatedAt": 1795787942334,
    }], "truncated": False, "hostScope": {"omittedHostIds": []}}}


def test_projection_excludes_private_fields():
    row = project_sessions(native_response())["sessions"][0]
    assert row["state"] == "working"
    assert row["session_suffix"] == "56789abc"
    assert row["assistant_message_present"] is True
    assert row["workspace"] == "~"
    assert "private" not in str(row)
    assert set(row) == {"session_suffix", "agent", "kind", "state", "workspace",
                        "repo", "assistant_message_present", "updated_at_ms"}


@pytest.mark.parametrize("bad", [{"ok": False}, {"ok": True, "result": {}},
                                 {"ok": True, "result": {"agents": [None]}}])
def test_invalid_envelope_not_empty_success(bad):
    with pytest.raises(OrcaError):
        project_sessions(bad)


def test_agent_list_only_read_operation():
    runner = MagicMock()
    runner.return_value.returncode = 0
    runner.return_value.stdout = b'{"ok":true,"result":{"agents":[]}}'
    runner.return_value.stderr = b""
    adapter = OrcaAdapter(runner=runner)
    adapter.agent_list()
    assert adapter.all_calls == ["agent_list"]
    assert adapter.mutation_calls == []
    assert runner.call_args.args[0][1:] == ["agent", "list", "--json"]
    assert runner.call_args.kwargs["shell"] is False


def test_monitor_startup_and_read_do_not_dispatch(tmp_path, monkeypatch):
    adapter = MagicMock()
    adapter.agent_list.return_value = native_response()
    monkeypatch.setattr(main, "_db_path", tmp_path / "monitor.db")
    monkeypatch.setattr(main, "_adapter_factory", None)
    monkeypatch.setattr(main, "get_adapter", lambda: adapter)
    with TestClient(main.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/monitor.js").status_code == 200
        response = client.get("/api/agent-sessions")
        assert response.status_code == 200
        assert response.json()["sessions"][0]["state"] == "working"
        assert response.headers["cache-control"] == "no-store"
        adapter.agent_list.side_effect = OrcaError("offline", "private error")
        response = client.get("/api/agent-sessions")
        assert response.status_code == 503
        assert "private" not in response.text
    assert [call[0] for call in adapter.method_calls] == ["agent_list", "agent_list"]
