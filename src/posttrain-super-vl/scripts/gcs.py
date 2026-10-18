#!/usr/bin/env python3
"""Minimal Cloud Storage client (stdlib only; the training image has no gcloud): the VM service account's token from the
metadata server, JSON API for list/download, resumable upload in 64 MB chunks. Only used with the workspace bucket.
usage: gcs.py ls gs://b/prefix | gcs.py get gs://b/obj local | gcs.py getdir gs://b/prefix localdir
       gcs.py put local gs://b/obj | gcs.py putdir localdir gs://b/prefix | gcs.py rm gs://b/obj
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://storage.googleapis.com"
CHUNK = 64 * 1024 * 1024
_tok = {"v": None, "exp": 0}


def token():
    if time.time() > _tok["exp"] - 60:
        req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                                     headers={"Metadata-Flavor": "Google"})
        d = json.load(urllib.request.urlopen(req, timeout=30))
        _tok.update(v=d["access_token"], exp=time.time() + d.get("expires_in", 300))
    return _tok["v"]


def split(uri):
    assert uri.startswith("gs://"), uri
    b, _, o = uri[5:].partition("/")
    return b, o


def req(method, url, data=None, headers=None, raw=False):
    h = {"Authorization": f"Bearer {token()}", **(headers or {})}
    for attempt in range(5):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=600)
            return r if raw else (json.load(r) if r.length != 0 else {})
        except urllib.error.HTTPError as e:
            if e.code in (308,):
                return e
            if e.code < 500 and e.code != 429:
                raise
        except urllib.error.URLError:
            pass
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{method} {url} failed")


def ls(uri):
    b, p = split(uri)
    out, page = [], None
    while True:
        q = {"prefix": p, "fields": "items(name,size,updated),nextPageToken"}
        if page:
            q["pageToken"] = page
        d = req("GET", f"{API}/storage/v1/b/{b}/o?{urllib.parse.urlencode(q)}")
        out += d.get("items", [])
        page = d.get("nextPageToken")
        if not page:
            return out


def get(uri, local):
    b, o = split(uri)
    pathlib.Path(local).parent.mkdir(parents=True, exist_ok=True)
    r = req("GET", f"{API}/storage/v1/b/{b}/o/{urllib.parse.quote(o, safe='')}?alt=media", raw=True)
    with open(local + ".part", "wb") as f:
        while chunk := r.read(CHUNK):
            f.write(chunk)
    os.replace(local + ".part", local)


def getdir(uri, localdir):
    b, p = split(uri.rstrip("/") + "/")
    for it in ls(f"gs://{b}/{p}"):
        if not it["name"].endswith("/"):
            get(f"gs://{b}/{it['name']}", str(pathlib.Path(localdir) / it["name"][len(p):]))


def put(local, uri):
    b, o = split(uri)
    size = os.path.getsize(local)
    r = req("POST", f"{API}/upload/storage/v1/b/{b}/o?uploadType=resumable&name={urllib.parse.quote(o, safe='')}",
            data=b"", headers={"X-Upload-Content-Length": str(size), "Content-Type": "application/json"}, raw=True)
    session = r.headers["Location"]
    with open(local, "rb") as f:
        off = 0
        while True:
            chunk = f.read(CHUNK)
            end = off + len(chunk) - 1
            hdr = {"Content-Length": str(len(chunk)),
                   "Content-Range": f"bytes {off}-{end}/{size}" if chunk else f"bytes */{size}"}
            req("PUT", session, data=chunk, headers=hdr, raw=True)
            off += len(chunk)
            if off >= size:
                break


def putdir(localdir, uri):
    for f in sorted(pathlib.Path(localdir).rglob("*")):
        if f.is_file():
            put(str(f), uri.rstrip("/") + "/" + str(f.relative_to(localdir)))


def rm(uri):
    b, o = split(uri)
    req("DELETE", f"{API}/storage/v1/b/{b}/o/{urllib.parse.quote(o, safe='')}", raw=True)


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    if cmd == "ls":
        for it in ls(args[0]):
            print(it["name"], it.get("size"), it.get("updated"))
    else:
        {"get": get, "getdir": getdir, "put": put, "putdir": putdir, "rm": rm}[cmd](*args)
