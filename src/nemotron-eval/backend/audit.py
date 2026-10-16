"""Governance audit trail for the OpenShell pilot.

One JSON line per decision: SQL broker queries, app-level tool denials, and
OpenShell sandbox events. Kept on the data volume; the Audit view reads it.
"""
import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PATH = Path(os.environ.get("DATA_DIR", "/data")) / "audit.jsonl"
# Optional gs://bucket/prefix: the trail is copied there so it outlives the app.
EXPORT_URI = os.environ.get("AUDIT_EXPORT_URI", "").rstrip("/")
EXPORT_INTERVAL_S = 300
export_status = {"uri": EXPORT_URI, "last_export": None, "error": ""}
_lock = threading.Lock()


def record(session_id: str, layer: str, decision: str, action: str, detail: str, args: Optional[Dict[str, Any]] = None) -> None:
    """layer: app | broker | openshell. decision: allow | deny | fail (stopped without a policy decision) | info."""
    event = {"ts": time.time(), "session_id": session_id, "layer": layer, "decision": decision,
             "action": action, "detail": detail[:2000], "args": _short(args or {})}
    print(f"[audit] {layer} {decision} {action} {session_id} {detail[:200]}", flush=True)
    try:
        with _lock:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            with PATH.open("a") as f:
                f.write(json.dumps(event, default=str) + "\n")
    except OSError as e:
        print(f"[audit] could not write {PATH}: {e}", flush=True)


def recent(limit: int = 500, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    if not PATH.exists():
        return []
    with _lock:
        lines = PATH.read_text().splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    if session_id:
        events = [e for e in events if e["session_id"] == session_id]
    return events[-limit:][::-1]


async def export_loop(app_name: str) -> None:
    """Copy the trail to EXPORT_URI/<app>.jsonl whenever it has changed."""
    if not EXPORT_URI.startswith("gs://"):
        return
    bucket, _, prefix = EXPORT_URI[5:].partition("/")
    exported = None
    while True:
        await asyncio.sleep(EXPORT_INTERVAL_S)
        try:
            stat = PATH.stat() if PATH.exists() else None
            if stat and stat.st_mtime != exported:
                await asyncio.to_thread(_upload, bucket, f"{prefix}/{app_name}.jsonl".lstrip("/"))
                exported = stat.st_mtime
                export_status.update(last_export=time.time(), error="")
        except Exception as e:
            export_status["error"] = f"{type(e).__name__}: {e}"[:300]
            print(f"[audit] export to {EXPORT_URI} failed: {e}", flush=True)


def _upload(bucket: str, name: str) -> None:
    from google.cloud import storage

    with _lock:
        data = PATH.read_bytes()
    storage.Client().bucket(bucket).blob(name).upload_from_string(data, content_type="application/x-ndjson")


def _short(args: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (v[:1000] if isinstance(v, str) else v) for k, v in args.items()}
