import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from models import Artifact, ChatSession, EvalResult, EvalRun

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


class Store:
    """DuckDB on the data volume, so chats and eval runs survive app restarts.

    Only touched from the event loop thread, so one shared connection is safe.
    """

    def __init__(self, db_path: Optional[Path] = None):
        db_path = db_path or DATA_DIR / "eval.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(db_path))
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id VARCHAR PRIMARY KEY,
                name VARCHAR,
                created_at TIMESTAMP,
                tasks JSON,
                model_ids JSON,
                status VARCHAR,
                completed INTEGER,
                total INTEGER
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS results (
                run_id VARCHAR,
                task_id VARCHAR,
                model_id VARCHAR,
                output TEXT,
                latency_ms INTEGER,
                tokens INTEGER,
                success BOOLEAN,
                error TEXT,
                scores JSON,
                PRIMARY KEY (run_id, task_id, model_id)
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id VARCHAR PRIMARY KEY,
                updated_at TIMESTAMP,
                body JSON
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS artifacts (
                id VARCHAR,
                version INTEGER,
                session_id VARCHAR,
                created_at TIMESTAMP,
                body JSON,
                PRIMARY KEY (id, version)
            )
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS prompts (
                id VARCHAR PRIMARY KEY,
                title VARCHAR,
                prompt TEXT,
                created_at TIMESTAMP
            )
        """)

    # --- chat sessions ---

    def save_session(self, session: ChatSession):
        self.con.execute(
            "INSERT OR REPLACE INTO sessions (id, updated_at, body) VALUES (?, ?, ?)",
            [session.id, session.updated_at, session.model_dump_json()],
        )

    def get_session(self, session_id: str) -> Optional[ChatSession]:
        row = self.con.execute("SELECT body FROM sessions WHERE id = ?", [session_id]).fetchone()
        return ChatSession.model_validate_json(row[0]) if row else None

    def list_sessions(self) -> List[ChatSession]:
        rows = self.con.execute("SELECT body FROM sessions ORDER BY updated_at DESC").fetchall()
        return [ChatSession.model_validate_json(r[0]) for r in rows]

    def delete_session(self, session_id: str):
        self.con.execute("DELETE FROM sessions WHERE id = ?", [session_id])
        self.con.execute("DELETE FROM artifacts WHERE session_id = ?", [session_id])

    # --- canvas artifacts (every update is a new version) ---

    def save_artifact(self, artifact: Artifact) -> Artifact:
        self.con.execute(
            "INSERT OR REPLACE INTO artifacts (id, version, session_id, created_at, body) VALUES (?, ?, ?, ?, ?)",
            [artifact.id, artifact.version, artifact.session_id, artifact.created_at, artifact.model_dump_json()],
        )
        return artifact

    def create_artifact(self, session_id: str, type: str, title: str, content: str, language=None) -> Artifact:
        return self.save_artifact(
            Artifact(id="a" + uuid.uuid4().hex[:8], session_id=session_id, type=type, title=title,
                     content=content, language=language)
        )

    def update_artifact(self, artifact_id: str, content: str, title=None) -> Artifact:
        latest = self.get_artifact(artifact_id)
        if not latest:
            raise KeyError(f"No artifact {artifact_id}")
        return self.save_artifact(
            latest.model_copy(update={
                "version": latest.version + 1,
                "content": content,
                "title": title or latest.title,
                "created_at": datetime.now(),
            })
        )

    def get_artifact(self, artifact_id: str, version: Optional[int] = None) -> Optional[Artifact]:
        if version is None:
            row = self.con.execute(
                "SELECT body FROM artifacts WHERE id = ? ORDER BY version DESC LIMIT 1", [artifact_id]
            ).fetchone()
        else:
            row = self.con.execute(
                "SELECT body FROM artifacts WHERE id = ? AND version = ?", [artifact_id, version]
            ).fetchone()
        return Artifact.model_validate_json(row[0]) if row else None

    def artifact_versions(self, artifact_id: str) -> List[Artifact]:
        rows = self.con.execute(
            "SELECT body FROM artifacts WHERE id = ? ORDER BY version", [artifact_id]
        ).fetchall()
        return [Artifact.model_validate_json(r[0]) for r in rows]

    def list_artifacts(self, session_id: str) -> List[Artifact]:
        """Latest version of each artifact in a chat, oldest first."""
        rows = self.con.execute(
            """
            SELECT body FROM artifacts a
            WHERE session_id = ? AND version = (SELECT max(version) FROM artifacts b WHERE b.id = a.id)
            ORDER BY (SELECT min(created_at) FROM artifacts c WHERE c.id = a.id)
            """,
            [session_id],
        ).fetchall()
        return [Artifact.model_validate_json(r[0]) for r in rows]

    # --- saved prompts ---

    def list_prompts(self) -> List[Dict[str, Any]]:
        rows = self.con.execute("SELECT id, title, prompt, created_at FROM prompts ORDER BY created_at DESC").fetchall()
        return [{"id": r[0], "title": r[1], "prompt": r[2], "created_at": r[3]} for r in rows]

    def save_prompt(self, title: str, prompt: str) -> Dict[str, Any]:
        item = {"id": "p" + uuid.uuid4().hex[:8], "title": title, "prompt": prompt, "created_at": datetime.now()}
        self.con.execute("INSERT INTO prompts VALUES (?, ?, ?, ?)", [item["id"], title, prompt, item["created_at"]])
        return item

    def delete_prompt(self, prompt_id: str):
        self.con.execute("DELETE FROM prompts WHERE id = ?", [prompt_id])

    # --- eval runs ---

    def save_run(self, run: EvalRun):
        self.con.execute(
            """
            INSERT OR REPLACE INTO runs (id, name, created_at, tasks, model_ids, status, completed, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run.id,
                run.name,
                run.created_at,
                json.dumps([t.model_dump() for t in run.tasks]),
                json.dumps(run.model_ids),
                run.status,
                run.completed,
                run.total,
            ],
        )

    def save_result(self, result: EvalResult):
        self.con.execute(
            """
            INSERT OR REPLACE INTO results (run_id, task_id, model_id, output, latency_ms, tokens, success, error, scores)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                result.run_id,
                result.task_id,
                result.model_id,
                result.output,
                result.latency_ms,
                result.tokens,
                result.success,
                result.error,
                json.dumps(result.scores),
            ],
        )

    def load_results(self, run_id: str) -> Dict[str, Any]:
        rows = self.con.execute(
            "SELECT task_id, model_id, output, latency_ms, tokens, success, error, scores FROM results WHERE run_id = ?",
            [run_id],
        ).fetchall()
        results: Dict[str, Any] = {}
        for task_id, model_id, output, latency_ms, tokens, success, error, scores in rows:
            results.setdefault(task_id, {})[model_id] = {
                "output": output,
                "latency_ms": latency_ms,
                "tokens": tokens,
                "success": success,
                "error": error,
                "scores": json.loads(scores),
            }
        return results

    def list_runs(self) -> List[Dict[str, Any]]:
        rows = self.con.execute(
            "SELECT id, name, created_at, model_ids, status, completed, total FROM runs ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "created_at": r[2],
                "model_ids": json.loads(r[3]),
                "status": r[4],
                "completed": r[5],
                "total": r[6],
            }
            for r in rows
        ]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        row = self.con.execute(
            "SELECT id, name, created_at, tasks, model_ids, status, completed, total FROM runs WHERE id = ?",
            [run_id],
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "created_at": row[2],
            "tasks": json.loads(row[3]),
            "model_ids": json.loads(row[4]),
            "status": row[5],
            "completed": row[6],
            "total": row[7],
            "results": self.load_results(run_id),
        }
