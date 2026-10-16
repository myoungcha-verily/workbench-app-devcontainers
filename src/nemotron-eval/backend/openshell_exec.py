"""Governed run_shell / run_python: one OpenShell sandbox per chat.

The sandbox has no network of its own. Its supervisor allows only what
openshell/policy.yaml lists (the app's SQL broker) and logs every decision.
Real credentials never enter it: the broker token is a provider credential,
so the sandbox sees a placeholder that the supervisor replaces on requests to
the broker endpoint only.

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
from executors import COMMAND_TIMEOUT_S, format_result

CLI = os.environ.get("OPENSHELL_CLI", "/usr/local/bin/openshell")
GATEWAY = os.environ.get("OPENSHELL_GATEWAY_ENDPOINT", "")
CONFIG_DIR = Path(os.environ.get("APP_ROOT", "/app")) / "openshell"
POLICY = CONFIG_DIR / "policy.yaml"
PROFILE = CONFIG_DIR / "broker-profile.yaml"
IMAGE = os.environ.get("SANDBOX_IMAGE", "nemotron-agent-sandbox:0.1")
PROVIDER = "workbench-broker"
BROKER_URL = "http://host.openshell.internal:8080/broker"

# Shorthand OCSF lines, e.g.
#   NET:OPEN [MED] DENIED /usr/bin/curl(64) -> httpbin.org:443 [policy:- engine:opa] [reason:no matching policy]
#   HTTP:POST [MED] DENIED POST http://host:8080/path [policy:x engine:opa]
_DECISION = re.compile(r"\b(NET|HTTP):(\w+) \[\w+\] (ALLOWED|DENIED) (.*)")
_REASON = re.compile(r"\[reason:([^\]]*)\]")


def sandbox_name(session_id: str) -> str:
    # OpenShell names are at most 19 characters.
    return "chat-" + hashlib.sha1(session_id.encode()).hexdigest()[:12]


class OpenShellExecutor:
    governed = True

    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.ready: Dict[str, bool] = {}
        self.lock = asyncio.Lock()
        self.status = {"gateway": "not checked", "provider": "not created", "error": ""}

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
        credential = f"WORKBENCH_BROKER_TOKEN={self.token}"
        r = await self.cli("provider", "create", "--name", PROVIDER, "--type", "workbench-broker", "--credential", credential)
        if r["exit_code"] != 0 and "exist" in (r["stderr"] + r["stdout"]).lower():
            # Gateway state outlives app restarts: rotate the token instead.
            r = await self.cli("provider", "update", PROVIDER, "--credential", credential, "--wait")
        if r["exit_code"] != 0:
            self.status.update(provider="create failed", error=(r["stderr"] or r["stdout"]).strip()[-500:])
            return
        self.status.update(provider="ready", error="")

    async def ensure(self, session_id: str) -> Optional[str]:
        """Creates the chat's sandbox on first use. Returns an error message or None."""
        name = sandbox_name(session_id)
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
                    "--provider", PROVIDER, "--no-auto-providers", "--no-tty", "--detach",
                    "--env", f"WORKBENCH_BROKER_URL={BROKER_URL}",
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
        denials = await self.denials(session_id, since)
        result = {"text": text, "exit_code": r["exit_code"]}
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

    async def delete(self, session_id: str) -> None:
        name = sandbox_name(session_id)
        self.ready.pop(name, None)
        await self.cli("sandbox", "delete", name, timeout=60)
