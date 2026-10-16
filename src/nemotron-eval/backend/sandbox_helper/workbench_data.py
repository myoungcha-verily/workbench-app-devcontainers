"""Workbench data from run_python: `from workbench_data import query, list_objects, read_object`.

query(sql) returns a pandas DataFrame. In a governed chat the query goes to the
app's SQL broker (SELECT only, allowlisted datasets); otherwise it runs with
the user's own BigQuery credentials.

list_objects(bucket, prefix) and read_object(bucket, name) read Cloud Storage.
In a governed chat they carry a placeholder token; OpenShell substitutes a
short-lived read-only token for the allowlisted buckets.
"""
import json
import os
import urllib.parse
import urllib.request

GCS_API = "https://storage.googleapis.com/storage/v1/b/"


def query(sql, max_rows=10000):
    import pandas as pd

    broker = os.environ.get("WORKBENCH_BROKER_URL")
    if not broker:
        from google.cloud import bigquery

        return bigquery.Client().query(sql).result(max_results=max_rows).to_dataframe()
    request = urllib.request.Request(
        broker.rstrip("/") + "/sql",
        data=json.dumps({"sql": sql, "max_rows": max_rows}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("WORKBENCH_BROKER_TOKEN", ""),
                 # Attribution only: the broker's audit names the chat. Not an authorization input.
                 "X-Workbench-Sandbox": os.environ.get("WORKBENCH_SANDBOX", "")},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"query refused ({e.code}): {e.read().decode(errors='replace')}") from None
    return pd.DataFrame(result["rows"], columns=[c["name"] for c in result["columns"]])


def _gcs_get(url):
    token = os.environ.get("WORKBENCH_GCS_TOKEN")
    if token is None:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default()
        credentials.refresh(google.auth.transport.requests.Request())
        token = credentials.token
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"storage request refused ({e.code}): {e.read().decode(errors='replace')[:500]}") from None


def _bucket(bucket):
    return bucket.removeprefix("gs://").strip("/")


def list_objects(bucket, prefix=""):
    """Object names in a bucket, optionally under a prefix."""
    names, token = [], ""
    while True:
        query = urllib.parse.urlencode({"prefix": prefix, "fields": "items/name,nextPageToken", **({"pageToken": token} if token else {})})
        page = json.loads(_gcs_get(f"{GCS_API}{_bucket(bucket)}/o?{query}"))
        names += [item["name"] for item in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            return names


def read_object(bucket, name):
    """Object contents as bytes. For a CSV: pd.read_csv(io.BytesIO(read_object(bucket, name)))."""
    return _gcs_get(f"{GCS_API}{_bucket(bucket)}/o/{urllib.parse.quote(name, safe='')}?alt=media")
