"""Query BigQuery from run_python: `from workbench_data import query`.

query(sql) returns a pandas DataFrame. In a governed chat the query goes to the
app's SQL broker (SELECT only, allowlisted datasets); otherwise it runs with
the user's own BigQuery credentials.
"""
import json
import os
import urllib.request


def query(sql, max_rows=10000):
    import pandas as pd

    broker = os.environ.get("WORKBENCH_BROKER_URL")
    if not broker:
        from google.cloud import bigquery

        return bigquery.Client().query(sql).result(max_results=max_rows).to_dataframe()
    request = urllib.request.Request(
        broker.rstrip("/") + "/sql",
        data=json.dumps({"sql": sql, "max_rows": max_rows}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("WORKBENCH_BROKER_TOKEN", "")},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"query refused ({e.code}): {e.read().decode(errors='replace')}") from None
    return pd.DataFrame(result["rows"], columns=[c["name"] for c in result["columns"]])
