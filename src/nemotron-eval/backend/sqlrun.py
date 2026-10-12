"""BigQuery access for run_sql, the canvas Run button, and HTML mini-apps.

Uses the VM's service account (the user's Workbench pet service account),
which has the same data access as `bq` in the workspace.
"""
import asyncio
import re
import time
from typing import Any, Dict, Optional

READ_ONLY_TYPES = {"SELECT"}
DESTRUCTIVE_TYPES = {"DELETE", "MERGE", "TRUNCATE_TABLE"}

_client = None


def _bq():
    global _client
    if _client is None:
        from google.cloud import bigquery

        _client = bigquery.Client()
    return _client


def _is_destructive(statement_type: str) -> bool:
    return statement_type in DESTRUCTIVE_TYPES or statement_type.startswith("DROP")


def _dry_run(sql: str) -> Dict[str, Any]:
    from google.cloud import bigquery

    job = _bq().query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    return {"statement_type": job.statement_type or "UNKNOWN", "bytes_estimate": job.total_bytes_processed or 0}


def _run(sql: str, max_rows: int) -> Dict[str, Any]:
    started = time.time()
    job = _bq().query(sql)
    result = job.result(max_results=max_rows)
    columns = [{"name": f.name, "type": f.field_type} for f in (result.schema or [])]
    rows = [[_jsonable(v) for v in row.values()] for row in result]
    return {
        "columns": columns,
        "rows": rows,
        "total_rows": result.total_rows if result.total_rows is not None else len(rows),
        "bytes_processed": job.total_bytes_processed or 0,
        "statement_type": job.statement_type or "UNKNOWN",
        "job_id": job.job_id,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return str(value)


async def check(sql: str) -> Dict[str, Any]:
    info = await asyncio.to_thread(_dry_run, sql)
    info["destructive"] = _is_destructive(info["statement_type"])
    info["read_only"] = info["statement_type"] in READ_ONLY_TYPES
    return info


async def run(sql: str, max_rows: int = 500, read_only: bool = False, confirmed: bool = False) -> Dict[str, Any]:
    """Raises PermissionError when the statement needs confirmation or is not allowed."""
    info = await check(sql)
    if read_only and not info["read_only"]:
        raise PermissionError(f"Only SELECT queries are allowed here (got {info['statement_type']})")
    if info["destructive"] and not confirmed:
        raise PermissionError(f"needs_confirmation:{info['statement_type']}")
    return await asyncio.to_thread(_run, sql, max(1, min(int(max_rows), 5000)))


def as_text(result: Dict[str, Any], limit_chars: int = 12000) -> str:
    """Compact CSV-like rendering for the model."""
    header = ",".join(c["name"] for c in result["columns"])
    lines = [header] + [",".join("" if v is None else str(v) for v in row) for row in result["rows"]]
    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = text[:limit_chars] + "\n… [truncated]"
    summary = (
        f"\n\n({len(result['rows'])} of {result['total_rows']} rows shown, "
        f"{result['bytes_processed'] / 1e6:.1f} MB processed, statement {result['statement_type']})"
    )
    return text + summary


def friendly_error(e: Exception) -> str:
    """BigQuery errors without the request URL, location and job id noise."""
    message = getattr(e, "message", None) or str(e) or type(e).__name__
    message = re.sub(r"^\d{3} (POST|GET) https://\S+: ", "", message)
    message = re.sub(r"^(POST|GET) https://\S+: ", "", message)
    message = re.sub(r"\n+Location: .*", "", message)
    message = re.sub(r"\n+Job ID: .*", "", message)
    return message.strip()[:2000]
