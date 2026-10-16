"""Where run_shell and run_python execute.

Ungoverned chats run commands directly in the app container as the app user,
the way most coding agents work today: the user's credentials, the full
network and every workspace bucket are within reach.

Governed chats (the OpenShell pilot) run them in an OpenShell sandbox per chat.
See openshell_exec.py.
"""
import asyncio
import os
import shutil
from pathlib import Path
from typing import Dict

APP_USER = os.environ.get("APP_USER", "nemotron")
WORK_ROOT = Path(os.environ.get("AGENT_WORK_ROOT", "/tmp/agent-work"))
COMMAND_TIMEOUT_S = int(os.environ.get("AGENT_COMMAND_TIMEOUT_S", "120"))
OUTPUT_LIMIT = 20000
HELPER = Path(__file__).parent / "sandbox_helper" / "workbench_data.py"


def clip(text: str) -> str:
    if len(text) <= OUTPUT_LIMIT:
        return text
    return text[: OUTPUT_LIMIT // 2] + "\n… [output truncated] …\n" + text[-OUTPUT_LIMIT // 2:]


def format_result(stdout: str, stderr: str, exit_code: int, timed_out: bool = False) -> str:
    parts = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append("[stderr]\n" + stderr.rstrip())
    if timed_out:
        parts.append(f"[timed out after {COMMAND_TIMEOUT_S}s]")
    parts.append(f"[exit code {exit_code}]")
    return clip("\n".join(parts))


class LocalExecutor:
    """Runs in the app container as the app user, with that user's credentials."""

    governed = False

    def workdir(self, session_id: str) -> Path:
        path = WORK_ROOT / session_id
        if not path.exists():
            path.mkdir(parents=True)
            shutil.copy(HELPER, path / "workbench_data.py")
            shutil.chown(path, APP_USER)
            shutil.chown(path / "workbench_data.py", APP_USER)
        return path

    async def run(self, session_id: str, argv: list, stdin: str = "") -> Dict:
        cwd = self.workdir(session_id)
        command = ["sudo", "-H", "-u", APP_USER, "--", "env", f"PYTHONPATH={cwd}", *argv]
        proc = await asyncio.create_subprocess_exec(
            *command, cwd=cwd, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), COMMAND_TIMEOUT_S)
            return {"stdout": out.decode(errors="replace"), "stderr": err.decode(errors="replace"),
                    "exit_code": proc.returncode, "timed_out": False}
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"stdout": "", "stderr": "", "exit_code": -9, "timed_out": True}

    async def shell(self, session_id: str, command: str) -> Dict:
        r = await self.run(session_id, ["bash", "-lc", command])
        return {"text": format_result(r["stdout"], r["stderr"], r["exit_code"], r["timed_out"]), "exit_code": r["exit_code"]}

    async def python(self, session_id: str, code: str) -> Dict:
        r = await self.run(session_id, ["/opt/agent-python/bin/python", "-"], stdin=code)
        return {"text": format_result(r["stdout"], r["stderr"], r["exit_code"], r["timed_out"]), "exit_code": r["exit_code"]}
