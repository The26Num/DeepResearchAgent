"""Short SQLite transactions; all mutations are owned by the orchestrator thread."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from time import monotonic
from uuid import uuid4

from app.persistence.models import Checkpoint, ResearchRun, ResearchRunState, RunStatus, STATE_VERSION
from app.persistence.serialization import IncompatibleCheckpointError, StateIntegrityError, state_from_json


class ResearchRunNotFoundError(LookupError):
    pass


class NoCheckpointError(LookupError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchRunStore:
    def __init__(self, path="workspace/research.db", *, secrets=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets = set(value for value in secrets if value)
        self.secrets.update(value for key, value in os.environ.items()
                            if value and any(word in key.lower() for word in ("api_key", "password", "secret", "authorization")))
        # Only credential values are read for redaction; Settings is never serialized.
        from app.config import get_settings
        settings = get_settings()
        self.secrets.update(value for key in ("model_api_key", "siliconflow_api_key", "tavily_api_key", "verifier_model_api_key")
                            if (value := getattr(settings, key, None)))
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL,
                    current_stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    resume_count INTEGER NOT NULL DEFAULT 0, last_checkpoint_id INTEGER,
                    error_type TEXT, error_message TEXT, state_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES research_runs(run_id), sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL, state_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(run_id, sequence)
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    def redact(self, value):
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            return re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;}]+", r"\1[REDACTED]", value)
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def create_run(self, goal: str) -> ResearchRun:
        if not goal.strip():
            raise ValueError("A research goal is required.")
        run = ResearchRun(run_id=uuid4().hex, goal=self.redact(goal), status=RunStatus.RUNNING,
                          current_stage="RUN_CREATED", created_at=utc_now(), updated_at=utc_now())
        with self.connection() as conn, conn:
            conn.execute("INSERT INTO research_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         tuple(run.model_dump(mode="json").values()))
        return run

    def get_run(self, run_id: str) -> ResearchRun:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ResearchRunNotFoundError(f"Research run not found: {run_id}.")
        return ResearchRun.model_validate(dict(row))

    def list_runs(self, limit=20) -> list[ResearchRun]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM research_runs ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [ResearchRun.model_validate(dict(row)) for row in rows]

    def _update(self, run_id, **values):
        values["updated_at"] = utc_now()
        with self.connection() as conn, conn:
            cursor = conn.execute("UPDATE research_runs SET " + ", ".join(f"{key}=?" for key in values) + " WHERE run_id=?",
                                  (*values.values(), run_id))
            if not cursor.rowcount:
                raise ResearchRunNotFoundError(f"Research run not found: {run_id}.")

    def set_stage(self, run_id, stage):
        self._update(run_id, current_stage=stage)

    def mark_running(self, run_id):
        with self.connection() as conn, conn:
            cursor = conn.execute("UPDATE research_runs SET status='RUNNING', resume_count=resume_count+1, "
                                  "updated_at=?, error_type=NULL, error_message=NULL WHERE run_id=?", (utc_now(), run_id))
            if not cursor.rowcount:
                raise ResearchRunNotFoundError(f"Research run not found: {run_id}.")

    def mark_failed(self, run_id, error):
        self._update(run_id, status="FAILED", error_type=type(error).__name__, error_message=self.redact(str(error)))

    def mark_interrupted(self, run_id):
        self._update(run_id, status="INTERRUPTED", error_type="KeyboardInterrupt", error_message="Research interrupted.")

    def mark_completed(self, run_id):
        checkpoint = self.load_latest_checkpoint(run_id)
        if "RUN_COMPLETED" not in checkpoint.state.completed_stages:
            raise StateIntegrityError("Cannot complete a run without RUN_COMPLETED checkpoint.")
        self._update(run_id, status="COMPLETED", error_type=None, error_message=None)

    def save_checkpoint(self, run_id: str, stage: str, state: ResearchRunState) -> Checkpoint:
        started = monotonic()
        text = json.dumps(self.redact(state.model_dump(mode="json")), ensure_ascii=False, separators=(",", ":"))
        checked = state_from_json(text)
        created = utc_now()
        with self.connection() as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT * FROM research_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise ResearchRunNotFoundError(f"Research run not found: {run_id}.")
            if run["state_version"] != STATE_VERSION:
                raise IncompatibleCheckpointError("Research run state_version is incompatible.")
            if checked.goal != run["goal"]:
                raise StateIntegrityError("Checkpoint goal does not match its ResearchRun.")
            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()[0]
            cursor = conn.execute("INSERT INTO checkpoints(run_id,sequence,stage,state_json,created_at) VALUES (?,?,?,?,?)",
                                  (run_id, sequence, stage, text, created))
            checkpoint_id = cursor.lastrowid
            conn.execute("UPDATE research_runs SET last_checkpoint_id=?,current_stage=?,updated_at=? WHERE run_id=?",
                         (checkpoint_id, stage, created, run_id))
        print(f"[Checkpoint] run_id={run_id} sequence={sequence} stage={stage} state_chars={len(text)} "
              f"elapsed={monotonic() - started:.3f}s", flush=True)
        return Checkpoint(checkpoint_id=checkpoint_id, run_id=run_id, sequence=sequence, stage=stage,
                          created_at=created, state=checked)

    def load_latest_checkpoint(self, run_id: str) -> Checkpoint:
        run = self.get_run(run_id)
        if run.state_version != STATE_VERSION:
            raise IncompatibleCheckpointError(f"Research run state_version={run.state_version} is incompatible.")
        if run.last_checkpoint_id is None:
            raise NoCheckpointError("No checkpoint available for this run.")
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM checkpoints WHERE checkpoint_id=?", (run.last_checkpoint_id,)).fetchone()
        if row is None or row["run_id"] != run_id:
            raise StateIntegrityError("Latest checkpoint is missing or belongs to another run.")
        state = state_from_json(row["state_json"])
        if state.goal != run.goal:
            raise StateIntegrityError("Latest checkpoint goal does not match its run.")
        return Checkpoint(**{key: row[key] for key in ("checkpoint_id", "run_id", "sequence", "stage", "created_at")}, state=state)
