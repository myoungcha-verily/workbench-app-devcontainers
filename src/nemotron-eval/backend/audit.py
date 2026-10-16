"""Governance audit trail for the OpenShell pilot.

One JSON line per decision: SQL broker queries, app-level tool denials, and
OpenShell sandbox events. Kept on the data volume; the Audit view reads it.
"""
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PATH = Path(os.environ.get("DATA_DIR", "/data")) / "audit.jsonl"
_lock = threading.Lock()


def record(session_id: str, layer: str, decision: str, action: str, detail: str, args: Optional[Dict[str, Any]] = None) -> None:
    """layer: app | broker | openshell. decision: allow | deny | info."""
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


def _short(args: Dict[str, Any]) -> Dict[str, Any]:
    return {k: (v[:1000] if isinstance(v, str) else v) for k, v in args.items()}
