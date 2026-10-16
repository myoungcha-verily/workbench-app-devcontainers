"""Governed run_shell / run_python: one OpenShell sandbox per chat.

The sandbox has no network of its own. Its supervisor allows only what
openshell/policy.yaml lists (the app's SQL broker) and logs every decision.
Real credentials never enter it: the broker token and the downscoped storage
token (token_minter.py) are provider credentials, so the sandbox sees
placeholders that the supervisor replaces on requests to the allowed endpoints
only.

When the policy blocks a connection, OpenShell drafts a rule that would allow
it. proposals() lists the drafts for a chat; decide() approves one into the
live sandbox policy or rejects it. The user makes that call in the chat.

Uses the `openshell` CLI: the Python SDK needs protobuf 6, which the app's
Google Cloud libraries don't support yet.
"""
import asyncio
import hashlib
import os
import re
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional

import audit
import sqlrun
import token_minter
from executors import COMMAND_TIMEOUT_S, format_result

CLI = os.environ.get("OPENSHELL_CLI", "/usr/local/bin/openshell")
GATEWAY = os.environ.get("OPENSHELL_GATEWAY_ENDPOINT", "")
CONFIG_DIR = Path(os.environ.get("APP_ROOT", "/app")) / "openshell"
BASE_POLICY = CONFIG_DIR / "policy.yaml"
PROFILE = CONFIG_DIR / "broker-profile.yaml"
# Policy and storage profile rendered with the GOVERNED_BUCKETS allowlist.
RUNTIME_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "openshell"
POLICY = RUNTIME_DIR / "policy.yaml"
GCS_PROFILE = RUNTIME_DIR / "storage-profile.yaml"
IMAGE = os.environ.get("SANDBOX_IMAGE", "nemotron-agent-sandbox:0.2")
PROVIDER = "workbench-broker"
GCS_PROVIDER = "workbench-storage"
# Refresh the storage token this long before it expires.
GCS_REFRESH_MARGIN_S = 600
BROKER_URL = "http://host.openshell.internal:8080/broker"

# Shorthand OCSF lines, e.g.
#   NET:OPEN [MED] DENIED /usr/bin/curl(64) -> httpbin.org:443 [policy:- engine:opa] [reason:no matching policy]
#   HTTP:POST [MED] DENIED POST http://host:8080/path [policy:x engine:opa]
_DECISION = re.compile(r"\b(NET|HTTP):(\w+) \[\w+\] (ALLOWED|DENIED) (.*)")
_REASON = re.compile(r"\[reason:([^\]]*)\]")
_TIMESTAMP = re.compile(r"^\[(\d+(?:\.\d+)?)\]")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# A command that fails because the sandbox lacks a program or module is stopped
# too, but by absence rather than policy, so the supervisor logs nothing.
_MISSING = re.compile(r"(?:^|\n)(?:bash: (?:line \d+: )?)?(\S+): command not found|No module named '([^']+)'")


def sandbox_name(session_id: str) -> str:
    # OpenShell names are at most 19 characters.
    return "chat-" + hashlib.sha1(session_id.encode()).hexdigest()[:12]


class OpenShellExecutor:
    governed = True

    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.ready: Dict[str, bool] = {}
        self.sessions: Dict[str, str] = {}  # sandbox name -> chat session id, for broker attribution
        self.lock = asyncio.Lock()
        self.status = {"gateway": "not checked", "provider": "not created", "error": "",
                       "storage": "disabled" if not token_minter.BUCKETS else "not created",
                       "buckets": token_minter.BUCKETS, "storage_token_expires": None}

    @property
    def available(self) -> bool:
        return bool(GATEWAY) and Path(CLI).exists()

    async def cli(self, *args: str, stdin: str = "", timeout: float = 120) -> Dict:
        env = {**os.environ, "OPENSHELL_GATEWAY_ENDPOINT": GATEWAY, "NO_COLOR": "1"}
        # No stdin pipe unless there is input: writing to a pipe the CLI never
        # reads fails once it exits.
        proc = await asyncio.create_subprocess_exec(
            CLI, *args, env=env, stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin else None), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"stdout": "", "stderr": f"openshell {args[0]} timed out", "exit_code": -9, "timed_out": True}
        return {"stdout": out.decode(errors="replace"), "stderr": err.decode(errors="replace"),
                "exit_code": proc.returncode, "timed_out": False}

    async def setup(self) -> None:
        try:
            await self._setup()
        except Exception as e:
            self.status.update(error=f"setup failed: {type(e).__name__}: {e}")
            print(f"[openshell] setup failed: {e!r}", flush=True)

    async def _setup(self) -> None:
        """Import the broker profile and (re)create the provider with this run's token."""
        if not self.available:
            self.status.update(gateway="unavailable", error="OpenShell CLI or gateway endpoint missing")
            return
        for attempt in range(60):
            r = await self.cli("status", timeout=20)
            if r["exit_code"] == 0:
                self.status["gateway"] = "connected"
                break
            self.status.update(gateway="waiting", error=(r["stderr"] or r["stdout"]).strip()[-300:])
            await asyncio.sleep(5)
        else:
            return
        r = await self.cli("profile", "import", "-f", str(PROFILE))
        if r["exit_code"] != 0 and "exist" not in (r["stderr"] + r["stdout"]).lower():
            self.status.update(provider="profile import failed", error=(r["stderr"] or r["stdout"]).strip()[-500:])
            return
        self.render_policy()
        r = await self.put_provider(PROVIDER, "workbench-broker", f"WORKBENCH_BROKER_TOKEN={self.token}")
        if r["exit_code"] != 0:
            self.status.update(provider="create failed", error=(r["stderr"] or r["stdout"]).strip()[-500:])
            return
        if token_minter.BUCKETS:
            r = await self.cli("profile", "import", "-f", str(GCS_PROFILE))
            if r["exit_code"] != 0 and "exist" in (r["stderr"] + r["stdout"]).lower():
                # The bucket allowlist may have changed since the profile was imported.
                r = await self.cli("profile", "update", "-f", str(GCS_PROFILE), "workbench-storage")
            if r["exit_code"] != 0:
                self.status.update(provider="storage profile import failed", error=(r["stderr"] or r["stdout"]).strip()[-500:])
                return
            if not await self.refresh_storage_token():
                return
        self.status.update(provider="ready", error="")

    async def put_provider(self, name: str, kind: str, credential: str) -> Dict:
        r = await self.cli("provider", "create", "--name", name, "--type", kind, "--credential", credential)
        if r["exit_code"] != 0 and "exist" in (r["stderr"] + r["stdout"]).lower():
            # Gateway state outlives app restarts: rotate the credential instead.
            r = await self.cli("provider", "update", name, "--credential", credential, "--wait")
        return r

    def render_policy(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        policy = BASE_POLICY.read_text()
        if token_minter.BUCKETS:
            endpoint = (
                "      - host: storage.googleapis.com\n        port: 443\n        protocol: rest\n"
                "        enforcement: enforce\n        # GCS object names are %2F-encoded in API paths.\n"
                "        allow_encoded_slash: true\n        rules:\n" + token_minter.endpoint_rules() + "\n"
            )
            policy += ("\n  # Read-only object access on the allowlisted buckets, with a downscoped token.\n"
                       "  workbench_storage:\n    name: workbench_storage\n    endpoints:\n" + endpoint
                       + "    binaries:\n      - path: /usr/local/bin/python3.11\n")
            GCS_PROFILE.write_text(
                "# Generated by openshell_exec.py: downscoped storage token for governed sandboxes.\n"
                "id: workbench-storage\ndisplay_name: Workbench storage (read-only, downscoped)\n"
                "description: Short-lived token limited to object reads on the allowlisted buckets\ncategory: other\n"
                "credentials:\n  - name: token\n    env_vars: [WORKBENCH_GCS_TOKEN]\n    required: true\n"
                "    auth_style: bearer\n    header_name: authorization\n"
                "endpoints:\n" + endpoint.replace("\n      ", "\n  ").replace("      - host", "  - host", 1)
                + "binaries:\n  - /usr/local/bin/python3.11\n")
        POLICY.write_text(policy)

    async def refresh_storage_token(self) -> bool:
        try:
            token, expires = await asyncio.to_thread(token_minter.mint)
        except Exception as e:
            self.status.update(storage="mint failed", error=f"storage token: {type(e).__name__}: {e}"[:500])
            return False
        r = await self.put_provider(GCS_PROVIDER, "workbench-storage", f"WORKBENCH_GCS_TOKEN={token}")
        if r["exit_code"] != 0:
            self.status.update(storage="provider failed", error=(r["stderr"] or r["stdout"]).strip()[-500:])
            return False
        self.status.update(storage="ready", storage_token_expires=expires)
        audit.record("-", "openshell", "info", "storage_token",
                     f"minted a read-only token for {', '.join(token_minter.BUCKETS)}, expires in {int(expires - time.time())}s", {})
        return True

    async def storage_refresh_loop(self) -> None:
        while token_minter.BUCKETS:
            expires = self.status.get("storage_token_expires") or 0
            await asyncio.sleep(max(30, expires - time.time() - GCS_REFRESH_MARGIN_S))
            if self.status["provider"] == "ready":
                await self.refresh_storage_token()

    async def ensure(self, session_id: str) -> Optional[str]:
        """Creates the chat's sandbox on first use. Returns an error message or None."""
        name = sandbox_name(session_id)
        self.sessions[name] = session_id
        if self.ready.get(name):
            return None
        async with self.lock:
            if self.ready.get(name):
                return None
            if self.status["provider"] != "ready":
                await self.setup()
                if self.status["provider"] != "ready":
                    return f"The OpenShell sandbox is not available: {self.status['error'] or self.status['gateway']}"
            got = await self.cli("sandbox", "get", name, timeout=30)
            if got["exit_code"] != 0:
                started = time.time()
                r = await self.cli(
                    "sandbox", "create", "--name", name, "--from", IMAGE, "--policy", str(POLICY),
                    "--provider", PROVIDER, *(["--provider", GCS_PROVIDER] if token_minter.BUCKETS else []),
                    "--no-auto-providers", "--no-tty", "--detach",
                    "--env", f"WORKBENCH_BROKER_URL={BROKER_URL}", "--env", f"WORKBENCH_SANDBOX={name}",
                    "--label", f"chat={name}", timeout=300,
                )
                if r["exit_code"] != 0:
                    return "Could not create the sandbox: " + (r["stderr"] or r["stdout"]).strip()[-800:]
                print(f"[openshell] created {name} in {time.time() - started:.1f}s", flush=True)
                audit.record(session_id, "openshell", "info", "sandbox", f"created {name}", {})
            self.ready[name] = True
        return None

    async def run(self, session_id: str, argv: List[str], stdin: str = "") -> Dict:
        error = await self.ensure(session_id)
        if error:
            return {"text": error, "exit_code": 1}
        name = sandbox_name(session_id)
        since = time.time()
        r = await self.cli("sandbox", "exec", name, "--workdir", "/sandbox", "--timeout", str(COMMAND_TIMEOUT_S),
                           "--no-tty", "--", *argv, stdin=stdin, timeout=COMMAND_TIMEOUT_S + 30)
        if r["exit_code"] != 0 and "not found" in r["stderr"].lower():
            self.ready.pop(name, None)
        timed_out = r["timed_out"] or r["exit_code"] == 124
        text = format_result(r["stdout"], r["stderr"], r["exit_code"], timed_out)
        command = argv[-1] if argv[:2] == ["bash", "-lc"] else stdin
        audit.record(session_id, "openshell", "allow", "exec", f"{' '.join(argv[:2])} exited {r['exit_code']}",
                     {"command": command})
        denials = await self.denials(session_id, since)
        result = {"text": text, "exit_code": r["exit_code"]}
        missing = sorted({a or b for a, b in _MISSING.findall(r["stdout"] + "\n" + r["stderr"])})
        if missing:
            audit.record(session_id, "openshell", "fail", "missing_tool",
                         f"not available in the sandbox: {', '.join(missing)}", {"command": command})
            result["missing"] = missing
        if denials:
            lines = "\n".join(f"- {d['summary']}" for d in denials)
            result["text"] += f"\n\n[OpenShell policy blocked {len(denials)} connection(s):\n{lines}]"
            result["blocked"] = {"layer": "openshell", "events": denials}
        return result

    async def shell(self, session_id: str, command: str) -> Dict:
        return await self.run(session_id, ["bash", "-lc", command])

    async def python(self, session_id: str, code: str) -> Dict:
        return await self.run(session_id, ["python3", "-"], stdin=code)

    async def denials(self, session_id: str, since: float) -> List[Dict]:
        """DENIED decisions logged by the sandbox's supervisor since `since`."""
        await asyncio.sleep(1.0)  # decisions reach the gateway's log shortly after the exec returns
        seconds = max(5, int(time.time() - since) + 5)
        r = await self.cli("logs", sandbox_name(session_id), "--since", f"{seconds}s", "--source", "sandbox",
                           "-n", "500", timeout=30)
        events = []
        for line in r["stdout"].splitlines():
            m = _DECISION.search(line)
            if not m or m.group(3) != "DENIED":
                continue
            # Lines start with an epoch timestamp: "[1792118560.650] [sandbox] ..."
            ts = _TIMESTAMP.match(line)
            if ts and float(ts.group(1)) < since - 0.5:
                continue
            detail = m.group(4)
            reason = _REASON.search(detail)
            target = re.sub(r"\s*\[.*$", "", detail).strip()
            event = {"kind": f"{m.group(1)}:{m.group(2)}", "target": target,
                     "reason": reason.group(1) if reason else "", "raw": line.strip()[-400:]}
            event["summary"] = f"{event['kind']} {target}" + (f" ({event['reason']})" if event["reason"] else "")
            # A DNS refusal is followed by the connection denial for the same host.
            if event["kind"] == "NET:REFUSE":
                continue
            if all(e["target"] != target for e in events):
                events.append(event)
                audit.record(session_id, "openshell", "deny", event["kind"], event["summary"], {"raw": event["raw"]})
        return events

    async def proposals(self, session_id: str, status: str = "pending") -> List[Dict]:
        """Rules OpenShell drafted from this chat's blocked connections (pending, approved or rejected)."""
        name = sandbox_name(session_id)
        if not self.ready.get(name):
            return []
        r = await self.cli("rule", "get", name, "--status", status, timeout=30)
        return parse_rules(r["stdout"], status) if r["exit_code"] == 0 else []

    async def describe(self, session_id: str) -> Dict:
        """What the chat's sandbox is and may do, for the Sandbox panel."""
        name = sandbox_name(session_id)
        allowed = [{"what": "SQL broker", "detail": "POST /broker/sql on the app: SELECT only, row and byte caps",
                    "scope": "datasets: " + ", ".join(sqlrun.GOVERNED_DATASETS), "program": "python3.11"}]
        if token_minter.BUCKETS:
            allowed.append({"what": "Cloud Storage, read-only", "detail": "GET objects with a downscoped token",
                            "scope": "buckets: " + ", ".join(token_minter.BUCKETS), "program": "python3.11"})
        approved = await self.proposals(session_id, "approved") if self.ready.get(name) else []
        return {
            "available": self.available and self.status["provider"] == "ready",
            "status": self.status,
            "sandbox": {"name": name, "created": bool(self.ready.get(name)), "image": IMAGE},
            "isolation": [
                "Runs as uid 1500, not root; system directories are read-only (Landlock, required)",
                "No network of its own: every connection goes through the OpenShell proxy, which checks it against the policy",
                "No real credentials: the broker and storage tokens are placeholders the proxy fills in for allowed requests only",
            ],
            "allowed": allowed,
            "approved": approved,
        }

    async def decide(self, session_id: str, chunk_id: str, approve: bool, reason: str = "") -> Dict:
        name = sandbox_name(session_id)
        pending = {p["chunk"]: p for p in await self.proposals(session_id)}
        if chunk_id not in pending:
            return {"ok": False, "message": "This proposal is no longer pending."}
        args = ["rule", "approve" if approve else "reject", name, "--chunk-id", chunk_id]
        if not approve and reason:
            args += ["--reason", reason]
        r = await self.cli(*args, timeout=60)
        out = _ANSI.sub("", (r["stdout"] + r["stderr"]).strip())
        ok = r["exit_code"] == 0
        p = pending[chunk_id]
        audit.record(session_id, "openshell", ("approve" if approve else "reject") if ok else "fail", "policy_rule",
                     f"{'approved' if approve else 'rejected'} by the user: {p.get('endpoints', '')} for {p.get('binary', '')}"
                     + ("" if ok else f" (failed: {out[-200:]})"), {"chunk": chunk_id, "rule": p.get("rule", "")})
        return {"ok": ok, "message": out[-500:]}

    def session_for(self, name: str) -> str:
        return self.sessions.get(name, name or "sandbox")

    async def delete(self, session_id: str) -> None:
        name = sandbox_name(session_id)
        self.ready.pop(name, None)
        await self.cli("sandbox", "delete", name, timeout=60)


def parse_rules(text: str, status: str = "pending") -> List[Dict]:
    """Parses `openshell rule get` output (plain text; the CLI has no JSON output)."""
    chunks, current = [], None
    for line in _ANSI.sub("", text).splitlines():
        key, _, value = line.strip().partition(":")
        if key == "Chunk":
            current = {"chunk": value.strip()}
            chunks.append(current)
        elif current is not None and key in ("Status", "Rule", "Binary", "Confidence", "Rationale", "Security",
                                             "Prover", "Application", "Endpoints", "Binaries", "Hits"):
            current[key.lower()] = value.strip()
    return [c for c in chunks if c.get("status", status) == status]
