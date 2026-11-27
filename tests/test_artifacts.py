"""Artifact download scoping and preview redaction."""

from __future__ import annotations

from pathlib import Path

from app.config import WORKSPACE
from app.db import get_conn, new_id, utc_now


def test_artifact_download_scoped(client) -> None:
    c, _runner, _ = client
    mid = c.post(
        "/api/missions",
        json={
            "title": "A",
            "objective": "o",
            "quality_requirements": "q",
            "scope": "s",
            "constraints": "c",
            "coordinator": "cursor",
        },
    ).json()["id"]

    # Create a real file under workspace and register artifact
    rel = f".orca-runs/{mid}/evidence.txt"
    path = WORKSPACE / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("secret sk-abcdefghijklmnopqrstuvwxyz99 and hello", encoding="utf-8")

    from app import main as main_mod

    conn = get_conn(main_mod._db_path)
    art_id = new_id("art")
    conn.execute(
        """INSERT INTO artifacts(id, mission_id, task_id, rel_path, size, sha256, kind, verified, validation_output, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (art_id, mid, None, rel, path.stat().st_size, "abc", "txt", 1, None, utc_now()),
    )
    conn.commit()

    r = c.get(f"/api/missions/{mid}/artifacts/{art_id}")
    assert r.status_code == 200
    assert b"hello" in r.content

    preview = c.get(f"/api/missions/{mid}/artifacts/{art_id}/preview").json()
    assert "sk-abcdefghijklmnopqrstuvwxyz99" not in preview["text"]
    assert preview["redacted"] is True

    # Foreign mission cannot fetch
    other = c.post(
        "/api/missions",
        json={
            "title": "B",
            "objective": "o",
            "quality_requirements": "q",
            "scope": "s",
            "constraints": "c",
            "coordinator": "cursor",
        },
    ).json()["id"]
    r = c.get(f"/api/missions/{other}/artifacts/{art_id}")
    assert r.status_code == 404


def test_artifact_rejects_escape_registration(client) -> None:
    """Even if DB were poisoned with .. path, download must reject."""
    c, _runner, _ = client
    mid = c.post(
        "/api/missions",
        json={
            "title": "A",
            "objective": "o",
            "quality_requirements": "q",
            "scope": "s",
            "constraints": "c",
            "coordinator": "cursor",
        },
    ).json()["id"]
    from app import main as main_mod

    conn = get_conn(main_mod._db_path)
    art_id = new_id("art")
    conn.execute(
        """INSERT INTO artifacts(id, mission_id, task_id, rel_path, size, sha256, kind, verified, validation_output, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (art_id, mid, None, "../Outside.txt", 1, "x", "txt", 0, None, utc_now()),
    )
    conn.commit()
    r = c.get(f"/api/missions/{mid}/artifacts/{art_id}")
    assert r.status_code == 403
