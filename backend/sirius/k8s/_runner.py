"""
App-independent subprocess runner for the sirius-k8s MCP server.

Mirrors the streaming pattern in sirius.cortex.__init__._stream but deliberately
has NO dependency on the events bus or app config, so the k8s MCP server can run
fully standalone (stdio) in any client.

Commands are always argument lists — never shell strings — so there is no shell
interpolation. Callers that accept a free-form command string must tokenize it
with shlex first (see mcp_server.helm/kubectl).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

# Only these binaries may be launched. The server exposes a generic helm/kubectl
# runner (per design), but even a "generic" runner should not be able to spawn
# arbitrary executables — just the k8s toolchain.
ALLOWED_BINARIES = {"helm", "kubectl", "aws"}


def run(cmd: list[str], stdin: Optional[str] = None, timeout: int = 600) -> dict:
    """Run an allowed command and capture combined output.

    Returns {ok, exit_code, output, cmd}. Never raises for non-zero exits — the
    caller inspects `ok`. Raises ValueError only for a disallowed/empty command.
    """
    if not cmd:
        raise ValueError("empty command")
    binary = cmd[0]
    if binary not in ALLOWED_BINARIES:
        raise ValueError(
            f"binary {binary!r} not allowed; permitted: {sorted(ALLOWED_BINARIES)}")
    if shutil.which(binary) is None:
        return {"ok": False, "exit_code": 127,
                "output": f"{binary} not found on PATH", "cmd": cmd}

    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": 124,
                "output": f"timed out after {timeout}s", "cmd": cmd}

    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": output.strip(),
        "cmd": cmd,
    }
