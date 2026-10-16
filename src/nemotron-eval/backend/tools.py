"""Tools offered to the model: Workbench MCP tools plus local tools.

Sending all ~100 MCP tool schemas on every turn costs ~14K tokens and
confuses smaller models, so a core set is always loaded and the model can
enable any other tool for the chat with `find_tools`.
"""
import json
import re
from typing import Any, Dict, List, Optional, Set

CORE_MCP_TOOLS = [
    "workspace_get",
    "workspace_list_resources",
    "resource_list_tree",
    "workspace_list_data_collections",
    "resource_check_access",
    # bq_execute is deliberately not core: shells mangle backticked table
    # names in SQL (134+ failed queries in eval-bench). run_sql replaces it.
    "gsutil_execute",
    "gcloud_execute",
    "wb_execute",
    "app_list",
    "workflow_list",
    "workflow_job_list",
    "workflow_job_describe",
    "underlay_list",
    "underlay_list_entities",
    "data_sample_instances",
    "cohort_count_instances",
    "study_list",
]

ARTIFACT_TYPES = ["markdown", "sql", "html", "chart", "code", "csv"]

LOCAL_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "find_tools",
        "description": (
            "Search the full catalog of Workbench tools (workspaces, resources, buckets, datasets, "
            "folders, groups, apps, workflows, cohorts, Data Explorer, exports, Aurora, S3) and enable "
            "the matches for this chat. Use when none of your current tools fits the task."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords, e.g. 'create bucket' or 'cohort export'"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_tables",
        "description": "List the tables in a BigQuery dataset of this workspace (dataset name, or project.dataset).",
        "parameters": {
            "type": "object",
            "properties": {"dataset": {"type": "string", "description": "e.g. BF_20240610_CRF"}},
            "required": ["dataset"],
        },
    },
    {
        "name": "describe_table",
        "description": (
            "Show a table's full name, row count, columns with types and descriptions, and value hints "
            "(category values, numeric ranges, null rates, 0/1 flags). Call this before writing SQL "
            "against a table. Accepts a table name, dataset.table, or project.dataset.table."
        ),
        "parameters": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Run a BigQuery Standard SQL query and return the first rows. This is the only way to run SQL: "
            "never send SQL through bq_execute, wb_execute or gcloud_execute. Use fully qualified "
            "`project.dataset.table` names."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "BigQuery Standard SQL"},
                "max_rows": {"type": "integer", "description": "Rows to return (default 50, max 500)"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run Python 3 code in this chat's working directory and return what it prints. Files persist "
            "between calls. pandas, numpy, scipy, statsmodels and matplotlib are installed. To get BigQuery "
            "data as a DataFrame: `from workbench_data import query; df = query(\"SELECT ...\")`. To read Cloud "
            "Storage: `from workbench_data import list_objects, read_object` (read_object returns bytes)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "Python source; print() what you need to see"}},
            "required": ["code"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a bash command in this chat's working directory and return its output (stdout, stderr, exit code).",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "list_skills",
        "description": "List Workbench skill guides (dashboards, data discovery, custom apps, workflows, scientific domains).",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "read_skill",
        "description": "Read a Workbench skill guide by name, e.g. 'DATA_DISCOVERY' or 'scientific/BIOINFORMATICS'.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "create_artifact",
        "description": (
            "Create an artifact in the user's canvas. Types: markdown (reports, notes), sql (BigQuery SQL "
            "the user can run), html (a self-contained HTML/JS mini-app; it may call "
            "`await workbench.query(sql)` to get BigQuery rows), chart (a Vega-Lite or Plotly JSON spec), "
            "code (any language), csv (tabular data)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ARTIFACT_TYPES},
                "title": {"type": "string"},
                "content": {"type": "string", "description": "Full artifact content"},
                "language": {"type": "string", "description": "For code artifacts, e.g. python"},
            },
            "required": ["type", "title", "content"],
        },
    },
    {
        "name": "update_artifact",
        "description": "Replace the content of an existing artifact, creating a new version.",
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "content": {"type": "string", "description": "Full new content"},
                "title": {"type": "string"},
            },
            "required": ["artifact_id", "content"],
        },
    },
    {
        "name": "read_artifact",
        "description": "Read the latest version of an artifact.",
        "parameters": {
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
        },
    },
]
LOCAL_TOOL_NAMES = {t["name"] for t in LOCAL_TOOLS}

# Tools whose purpose is removing something.
_DESTRUCTIVE_NAME = re.compile(r"(delete|remove|revoke)", re.IGNORECASE)
# Deletes hidden inside the generic CLI passthrough tools.
_DESTRUCTIVE_COMMAND = re.compile(
    r"(^|\s)(rm|rb|delete|remove|del)(\s|$)"
    r"|--delete\b|\brsync\b.*\s-d\b"
    r"|\b(drop|truncate)\s+(table|schema|view|database|materialized)\b"
    r"|\bdelete\s+from\b|\bmerge\s+into\b",
    re.IGNORECASE,
)
_COMMAND_TOOLS = {"bq_execute", "gsutil_execute", "gcloud_execute", "wb_execute", "git_execute", "aurora_query"}


def needs_confirmation(name: str, args: Dict[str, Any]) -> Optional[str]:
    """Returns a reason string when the call deletes something."""
    if name in LOCAL_TOOL_NAMES:
        return None
    if _DESTRUCTIVE_NAME.search(name):
        return f"{name} deletes or removes Workbench objects"
    if name in _COMMAND_TOOLS:
        text = " ".join(str(v) for v in args.values())
        if _DESTRUCTIVE_COMMAND.search(text):
            return "this command deletes data"
    return None


# Governed chats (OpenShell pilot) may use only Workbench tools that read.
# Generic CLI passthroughs (gcloud, gsutil, wb, git, bq) can copy data anywhere
# the user's credentials reach, so they are not offered at all.
_GOVERNED_READ_TOOL = re.compile(
    r"(^|_)(list|get|describe|status|tree|check_access|count_instances|sample_instances|query_hints|get_schema|get_entity|version|resolve)($|_)"
)
GOVERNED_DENIED_MCP = _COMMAND_TOOLS | {"resource_credentials", "resource_open_console", "s3_read_file", "s3_copy", "s3_write_file"}


def governed_allows(name: str) -> bool:
    if name in LOCAL_TOOL_NAMES:
        return True
    if name in GOVERNED_DENIED_MCP or _DESTRUCTIVE_NAME.search(name):
        return False
    return bool(_GOVERNED_READ_TOOL.search(name))


def to_ollama(name: str, description: str, schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    params = dict(schema or {})
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return {"type": "function", "function": {"name": name, "description": description or name, "parameters": params}}


def tool_schemas(mcp_tools: Dict[str, Dict[str, Any]], active: Set[str], governed: bool = False) -> List[Dict[str, Any]]:
    schemas = [to_ollama(t["name"], t["description"], t["parameters"]) for t in LOCAL_TOOLS]
    for name in CORE_MCP_TOOLS + sorted(active - set(CORE_MCP_TOOLS)):
        tool = mcp_tools.get(name)
        if tool and (not governed or governed_allows(name)):
            schemas.append(to_ollama(name, tool.get("description", ""), tool.get("inputSchema")))
    return schemas


def search_tools(mcp_tools: Dict[str, Dict[str, Any]], query: str, limit: int = 8) -> List[Dict[str, str]]:
    words = [w for w in re.split(r"[\s_,]+", query.lower()) if w]
    scored = []
    for name, tool in mcp_tools.items():
        haystack = f"{name.replace('_', ' ')} {tool.get('description', '')}".lower()
        score = sum(3 if w in name else 1 for w in words if w in haystack)
        if score:
            scored.append((score, name))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [
        {"name": name, "description": (mcp_tools[name].get("description") or "")[:200]}
        for _, name in scored[:limit]
    ]


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} characters]"


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)
