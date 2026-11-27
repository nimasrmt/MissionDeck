"""Application configuration for MissionDeck."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_VERSION = "1.0.0"
HOST = "127.0.0.1"
PORT = 8765
ALLOWED_HOSTS = frozenset({"127.0.0.1:8765", "localhost:8765"})
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:8765", "http://localhost:8765"})

WORKSPACE = Path(__file__).resolve().parent.parent


def _resolve_orca_exe() -> Path:
    """Locate the Orca CLI without pinning a machine-specific install path.

    Resolution order:
    1. ``MISSIONDECK_ORCA_EXE`` environment variable, when set.
    2. ``orca`` on ``PATH``.
    3. On Windows, the existing CLI in the standard per-user Orca install.
    4. The bare name ``orca``, resolved by the OS at call time.

    If the binary is absent the adapter reports Orca as unavailable; the app
    never substitutes a fake runtime.
    """
    override = os.environ.get("MISSIONDECK_ORCA_EXE", "").strip()
    if override:
        return Path(override).expanduser()
    found = shutil.which("orca")
    if found:
        return Path(found)
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            candidate = Path(local_app_data) / "Programs" / "Orca" / "resources" / "bin" / "orca.exe"
            if candidate.is_absolute() and candidate.is_file():
                return candidate
    return Path("orca")


ORCA_EXE = _resolve_orca_exe()
RUNS_DIR = WORKSPACE / ".orca-runs"
DB_PATH = RUNS_DIR / "command-center.db"
PID_FILE = RUNS_DIR / "app.pid"
LOCK_FILE = RUNS_DIR / "mission-start.lock"

SESSION_COOKIE = "cc_session"
CSRF_HEADER = "X-CC-Token"
BODY_LIMIT = 64 * 1024
PLAN_SIZE_LIMIT = 256 * 1024
OUTPUT_CAP = 1 * 1024 * 1024
PREVIEW_CAP = 64 * 1024
WORKER_READ_LIMIT = 20
VALIDATION_TIMEOUT = 120
ADAPTER_TIMEOUT = 60
WORKER_CAP_DEFAULT = 2
WORKER_CAP_MAX = 2
CAPABILITIES_CACHE_SECONDS = 30
COORDINATOR_TERMINAL_TITLE = "MissionDeck — Coordinator"

MAX_PLAN_TASKS = 8
MAX_PLANNER_REPAIRS = 1
MAX_TASK_RETRIES = 2

WORKER_COMPLETION_CONTRACT = (
    "You may and must use native Orca communication tools for completion, required "
    "heartbeat, and blocking questions, even in a read-only/no-tools task. No nested "
    "workers; no unrelated tools. Follow injected native preamble and use your own "
    "active task ID and dispatch ID. Execute `worker_done` through your agent's "
    "shell/terminal tool (or the native Orca completion tool exposed in your preamble) "
    "as an actual command with outcome `succeeded` or `failed`, a concise summary, "
    "evidence and modified paths (if any). Printing, quoting, or describing the "
    "command in your reply is not a report and does not settle the task. Send it "
    "exactly once successfully; a failed transmission is not a receipt: inspect and "
    "retry only if unsettled. Do not end merely with conversational text. If blocked, "
    "ask through native Orca or report failed with partial results. Never file "
    "completion on another worker's behalf. Do not disclose secrets."
)

TERMINAL_MISSION_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "blocked"}
)
NON_TERMINAL_MISSION_STATUSES = frozenset(
    {"draft", "planning", "awaiting_approval", "approved", "running", "review", "unknown"}
)
