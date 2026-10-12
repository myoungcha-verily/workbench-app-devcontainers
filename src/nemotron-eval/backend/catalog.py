"""Workspace catalog: buckets, BigQuery datasets and tables, table schemas.

Built for the failure modes the eval-bench runs showed for Nemotron:
hallucinated columns, `project.Table` paths missing the dataset, and schema
lookups that silently return nothing. Tools answer with real names, and SQL
errors come back with "did you mean" suggestions.
"""
import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CACHE_FILE = DATA_DIR / "catalog.json"
REFRESH_SECONDS = 30 * 60
# Skip value hints on very large tables to keep describe_table cheap.
HINT_MAX_BYTES = 20 * 1024**3
HINT_MAX_COLUMNS = 40

_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+`?([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-$]+){0,2})`?", re.IGNORECASE)


def _bq():
    import sqlrun

    return sqlrun._bq()


class Catalog:
    def __init__(self):
        self.datasets: List[Dict[str, str]] = []  # {"id", "project", "dataset"}
        self.buckets: List[Dict[str, str]] = []   # {"id", "bucket"}
        self.updated_at = 0.0
        self.error = ""
        self._tables: Dict[str, List[str]] = {}   # "project.dataset" -> table names
        self._schemas: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._load_cache()

    # ------------------------------------------------------------ resources

    def _load_cache(self):
        try:
            data = json.loads(CACHE_FILE.read_text())
            self.datasets, self.buckets, self.updated_at = data["datasets"], data["buckets"], data["updated_at"]
        except Exception:
            pass

    def _save_cache(self):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps({"datasets": self.datasets, "buckets": self.buckets, "updated_at": self.updated_at}))
        except OSError:
            pass

    async def refresh(self, mcp, force: bool = False):
        """`wb resource list` takes ~2 minutes in a large workspace, so run it in the background."""
        if not force and self.updated_at and time.time() - self.updated_at < REFRESH_SECONDS:
            return
        async with self._lock:
            if not force and self.updated_at and time.time() - self.updated_at < REFRESH_SECONDS:
                return
            if not await mcp.refresh():
                self.error = mcp.error
                return
            try:
                result = await mcp.call("wb_execute", {"command": "resource list --format=json"})
                text = result["text"]
                resources = json.loads(text[text.index("["):]) if "[" in text else []
            except Exception as e:
                self.error = f"resource list failed: {e}"
                return
            self.datasets = [
                {"id": r.get("id", ""), "project": r["projectId"], "dataset": r["datasetId"]}
                for r in resources if r.get("resourceType") == "BQ_DATASET" and r.get("projectId") and r.get("datasetId")
            ]
            self.buckets = [
                {"id": r.get("id", ""), "bucket": r["bucketName"]}
                for r in resources if r.get("bucketName")
            ]
            self.updated_at = time.time()
            self.error = ""
            self._save_cache()

    async def refresh_loop(self, mcp):
        while True:
            await self.refresh(mcp)
            await asyncio.sleep(REFRESH_SECONDS if self.updated_at else 60)

    # ------------------------------------------------------------ tables

    def _list_tables(self, full_dataset: str) -> List[str]:
        if full_dataset not in self._tables:
            try:
                self._tables[full_dataset] = [t.table_id for t in _bq().list_tables(full_dataset)]
            except Exception:
                self._tables[full_dataset] = []
        return self._tables[full_dataset]

    async def list_tables(self, dataset: str) -> Tuple[str, List[Dict[str, Any]]]:
        full = self.resolve_dataset(dataset)
        if not full:
            raise ValueError(f"Unknown dataset {dataset!r}. Workspace datasets include: "
                             + ", ".join(d["dataset"] for d in self.datasets[:40]))

        def run():
            out = []
            for t in _bq().list_tables(full):
                out.append({"table": t.table_id, "type": t.table_type})
            self._tables[full] = [t["table"] for t in out]
            return out

        return full, await asyncio.to_thread(run)

    def resolve_dataset(self, name: str) -> Optional[str]:
        name = name.strip().strip("`").replace(":", ".")
        parts = name.split(".")
        for d in self.datasets:
            if (len(parts) == 2 and parts[0] == d["project"] and parts[1] == d["dataset"]) or \
               (len(parts) == 1 and name in (d["dataset"], d["id"])):
                return f"{d['project']}.{d['dataset']}"
        return name if len(parts) == 2 else None

    async def table_index(self) -> Dict[str, List[str]]:
        """lower(table name) -> fully qualified ids, across every workspace dataset."""
        def build():
            with ThreadPoolExecutor(max_workers=16) as pool:
                fulls = [f"{d['project']}.{d['dataset']}" for d in self.datasets]
                list(pool.map(self._list_tables, fulls))
            index: Dict[str, List[str]] = {}
            for full in fulls:
                for t in self._tables.get(full, []):
                    index.setdefault(t.lower(), []).append(f"{full}.{t}")
            return index

        return await asyncio.to_thread(build)

    async def resolve_table(self, name: str) -> List[str]:
        """Candidate `project.dataset.table` ids for a possibly incomplete name."""
        name = name.strip().strip("`").replace(":", ".")
        parts = name.split(".")
        if len(parts) == 3:
            return [name]
        if len(parts) == 2:
            full = self.resolve_dataset(parts[0])
            if full and full.count(".") == 1 and parts[0] not in {d["project"] for d in self.datasets}:
                return [f"{full}.{parts[1]}"]
        index = await self.table_index()
        return index.get(parts[-1].lower(), [])

    # ------------------------------------------------------------ schemas

    def _get_table(self, table_id: str):
        if table_id not in self._schemas:
            self._schemas[table_id] = _bq().get_table(table_id)
        return self._schemas[table_id]

    async def describe(self, name: str, hints: bool = True) -> str:
        candidates = await self.resolve_table(name)
        if not candidates:
            raise ValueError(f"No table named {name!r} in the workspace datasets. Use list_tables to see tables.")
        if len(candidates) > 1:
            return "Several tables match; call describe_table with one of:\n" + "\n".join(f"- {c}" for c in candidates[:20])
        table_id = candidates[0]
        table = await asyncio.to_thread(self._get_table, table_id)
        hint_rows = await asyncio.to_thread(self._value_hints, table) if hints else {}
        lines = [
            f"Table `{table_id}`" + (f" — {table.description}" if table.description else ""),
            f"{table.num_rows or 0:,} rows. Always use the full name `{table_id}` in SQL.",
            "",
            "Columns (name TYPE — description; value hints):",
        ]
        for f in table.schema:
            desc = f" — {f.description}" if f.description else ""
            mode = " REPEATED" if f.mode == "REPEATED" else ""
            hint = hint_rows.get(f.name, "")
            lines.append(f"- {f.name} {f.field_type}{mode}{desc}{'; ' + hint if hint else ''}")
        return "\n".join(lines)

    def _value_hints(self, table) -> Dict[str, str]:
        if (table.num_bytes or 0) > HINT_MAX_BYTES:
            return {}
        exprs, kinds = [], {}
        for f in table.schema[:HINT_MAX_COLUMNS]:
            if f.mode == "REPEATED" or f.field_type in ("RECORD", "STRUCT", "BYTES", "GEOGRAPHY", "JSON"):
                continue
            c = f"`{f.name}`"
            n = re.sub(r"\W", "_", f.name)
            exprs.append(f"COUNTIF({c} IS NULL) AS n_{n}")
            if f.field_type in ("STRING", "BOOL", "BOOLEAN"):
                exprs.append(f"APPROX_COUNT_DISTINCT({c}) AS d_{n}")
                exprs.append(f"APPROX_TOP_COUNT(CAST({c} AS STRING), 8) AS t_{n}")
                kinds[f.name] = ("cat", n)
            elif f.field_type in ("INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
                exprs.append(f"MIN({c}) AS lo_{n}, MAX({c}) AS hi_{n}, APPROX_QUANTILES({c}, 2)[SAFE_OFFSET(1)] AS md_{n}, APPROX_COUNT_DISTINCT({c}) AS d_{n}")
                kinds[f.name] = ("num", n)
            elif f.field_type in ("DATE", "DATETIME", "TIMESTAMP", "TIME"):
                exprs.append(f"MIN({c}) AS lo_{n}, MAX({c}) AS hi_{n}")
                kinds[f.name] = ("range", n)
        if not exprs:
            return {}
        from google.cloud import bigquery

        sql = f"SELECT COUNT(*) AS total, {', '.join(exprs)} FROM `{table.project}.{table.dataset_id}.{table.table_id}`"
        try:
            row = list(_bq().query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=HINT_MAX_BYTES)).result())[0]
        except Exception:
            return {}
        total = row["total"] or 0
        out = {}
        for name, (kind, n) in kinds.items():
            parts = []
            nulls = row.get(f"n_{n}") or 0
            if total and nulls:
                parts.append(f"{100 * nulls / total:.0f}% null")
            if kind == "cat":
                distinct = row.get(f"d_{n}") or 0
                top = row.get(f"t_{n}") or []
                if distinct <= 30 and top:
                    parts.append("values: " + ", ".join(f"{t['value']!r} ({t['count']})" for t in top))
                else:
                    parts.append(f"~{distinct:,} distinct, e.g. " + ", ".join(repr(t["value"]) for t in top[:3]))
            elif kind == "num":
                lo, hi, md, distinct = row.get(f"lo_{n}"), row.get(f"hi_{n}"), row.get(f"md_{n}"), row.get(f"d_{n}") or 0
                if lo is not None:
                    parts.append(f"min {lo}, median {md}, max {hi}" + (" (looks like a 0/1 flag)" if distinct == 2 and {lo, hi} <= {0, 1} else ""))
            else:
                lo, hi = row.get(f"lo_{n}"), row.get(f"hi_{n}")
                if lo is not None:
                    parts.append(f"from {lo} to {hi}")
            out[name] = "; ".join(parts)
        return out

    # ------------------------------------------------------------ SQL errors

    async def explain_sql_error(self, sql: str, message: str) -> str:
        """Extra guidance appended to BigQuery errors returned to the model."""
        hints: List[str] = []
        refs = list(dict.fromkeys(_TABLE_REF.findall(sql)))
        try:
            for ref in refs:
                if ref.lower() in ("unnest",) or ref.upper().startswith("INFORMATION_SCHEMA"):
                    continue
                parts = ref.split(".")
                if len(parts) < 3 or "Not found" in message or "Access Denied" in message:
                    candidates = [c for c in await self.resolve_table(ref) if c != ref]
                    if candidates:
                        hints.append(f"`{ref}` is not a full table name. Did you mean: " + ", ".join(f"`{c}`" for c in candidates[:5]) + "?")
                    elif len(parts) < 3:
                        hints.append(f"`{ref}` must be written as `project.dataset.table`.")
            if "Unrecognized name" in message or "not found inside" in message or "Name " in message:
                for ref in refs:
                    if ref.count(".") == 2:
                        try:
                            table = await asyncio.to_thread(self._get_table, ref)
                            hints.append(f"Columns of `{ref}`: " + ", ".join(f.name for f in table.schema))
                        except Exception:
                            pass
        except Exception:
            pass
        if not hints:
            return ""
        return "\nHints:\n" + "\n".join(f"- {h}" for h in hints) + "\nFix the query; do not repeat the same SQL."
