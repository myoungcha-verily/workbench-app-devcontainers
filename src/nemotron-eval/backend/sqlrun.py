"""BigQuery access for run_sql, the canvas Run button, and HTML mini-apps.

Uses the VM's service account (the user's Workbench pet service account),
which has the same data access as `bq` in the workspace.
"""
import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional

READ_ONLY_TYPES = {"SELECT"}
DESTRUCTIVE_TYPES = {"DELETE", "MERGE", "TRUNCATE_TABLE"}

# Governed mode (OpenShell pilot): what agent queries may touch. Datasets are
# `dataset` (in this project) or `project.dataset`.
GOVERNED_DATASETS = [d.strip() for d in os.environ.get("GOVERNED_DATASETS", "redteam_bhs").split(",") if d.strip()]
GOVERNED_MAX_BYTES = int(os.environ.get("GOVERNED_MAX_BYTES", str(2 * 10**9)))
GOVERNED_MAX_ROWS = int(os.environ.get("GOVERNED_MAX_ROWS", "500"))

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
    return {
        "statement_type": job.statement_type or "UNKNOWN",
        "bytes_estimate": job.total_bytes_processed or 0,
        "tables": [f"{t.project}.{t.dataset_id}.{t.table_id}" for t in (job.referenced_tables or [])],
    }


def _run(sql: str, max_rows: int, max_bytes: Optional[int] = None) -> Dict[str, Any]:
    from google.cloud import bigquery

    started = time.time()
    config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes) if max_bytes else None
    job = _bq().query(sql, job_config=config)
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


def _dataset_allowed(table: str, allowed: List[str]) -> bool:
    project, dataset, _ = table.split(".", 2)
    return any(a == f"{project}.{dataset}" or (a == dataset and project == _bq().project) for a in allowed)


# BigQuery reports no referenced tables for INFORMATION_SCHEMA views, so they
# are checked by name. Region-level views (`region-us`.INFORMATION_SCHEMA.JOBS)
# would show other queries in the project and are never allowed.
_INFO_SCHEMA = re.compile(r"([\w.-]+)`?\s*\.\s*`?INFORMATION_SCHEMA\b", re.IGNORECASE)


def _info_schema_outside(sql: str, allowed: List[str]) -> List[str]:
    outside = []
    for match in _INFO_SCHEMA.finditer(sql.replace("`", "")):
        parts = match.group(1).split(".")
        dataset = parts[-1]
        project = parts[-2] if len(parts) > 1 else _bq().project
        if dataset.lower().startswith("region-") or not _dataset_allowed(f"{project}.{dataset}.x", allowed):
            outside.append(match.group(0))
    return outside


class PolicyDenied(PermissionError):
    """A governed-mode rule refused the query. The message says which rule."""


async def run_governed(sql: str, max_rows: int = 50) -> Dict[str, Any]:
    """The SQL broker for governed agents: SELECT only, allowlisted datasets,
    byte and row caps. BigQuery tokens can't be narrowed to a dataset, so the
    broker is what enforces the boundary."""
    info = await asyncio.to_thread(_dry_run, sql)
    if info["statement_type"] != "SELECT":
        raise PolicyDenied(f"only SELECT queries are allowed (got {info['statement_type']})")
    outside = [t for t in info["tables"] if not _dataset_allowed(t, GOVERNED_DATASETS)]
    outside += [t for t in _info_schema_outside(sql, GOVERNED_DATASETS) if not any(o.startswith(t) for o in outside)]
    if outside:
        raise PolicyDenied(f"tables outside the allowed datasets ({', '.join(GOVERNED_DATASETS)}): {', '.join(outside)}")
    if info["bytes_estimate"] > GOVERNED_MAX_BYTES:
        raise PolicyDenied(f"query would scan {info['bytes_estimate'] / 1e9:.1f} GB; the limit is {GOVERNED_MAX_BYTES / 1e9:.1f} GB")
    return await asyncio.to_thread(_run, sql, max(1, min(int(max_rows), GOVERNED_MAX_ROWS)), GOVERNED_MAX_BYTES)


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
