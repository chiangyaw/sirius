"""Minimal Sirius CLI — start/stop the web server."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

_PID = Path.home() / ".sirius" / "web.pid"


def _serve(host: str, port: int, reload: bool) -> None:
    cmd = [sys.executable, "-m", "uvicorn", "sirius.ui.web:app",
           "--host", host, "--port", str(port)]
    if reload:
        cmd.append("--reload")
    print(f"Starting Sirius at http://{host}:{port}  (Ctrl-C or `sirius stop` to stop)")
    proc = subprocess.Popen(cmd, env=os.environ.copy())
    _PID.parent.mkdir(parents=True, exist_ok=True)
    _PID.write_text(str(proc.pid))
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _PID.unlink(missing_ok=True)


def _serve_bg(host: str, port: int) -> None:
    _PID.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "uvicorn", "sirius.ui.web:app",
           "--host", host, "--port", str(port)]
    proc = subprocess.Popen(cmd, env=os.environ.copy(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _PID.write_text(str(proc.pid))
    print(f"Sirius running in background (pid {proc.pid}) at http://{host}:{port}")


def _pids_on_port(port: int) -> list[int]:
    """Best-effort lookup of pids listening on a TCP port via lsof."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, check=False,
        ).stdout
    except FileNotFoundError:
        return []
    return [int(x) for x in out.split()]


def _kill(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


def _stop(port: int) -> None:
    # 1) Prefer the pid recorded by `serve` / `serve-bg`.
    if _PID.exists():
        pid = int(_PID.read_text().strip())
        _PID.unlink(missing_ok=True)
        if _kill(pid):
            print(f"Stopped Sirius (pid {pid}).")
            return
        print(f"Pid {pid} from pid file not running; checking port {port}...")

    # 2) Fall back to whatever is listening on the port.
    pids = _pids_on_port(port)
    if not pids:
        print(f"No Sirius process found (no pid file, nothing on port {port}).")
        return
    for pid in pids:
        if _kill(pid):
            print(f"Stopped Sirius on port {port} (pid {pid}).")


def main() -> None:
    p = argparse.ArgumentParser(prog="sirius", description="Sirius platform CLI")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="Run the web server (foreground)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=5173)
    s.add_argument("--reload", action="store_true")

    b = sub.add_parser("serve-bg", help="Run the web server in the background")
    b.add_argument("--host", default="127.0.0.1")
    b.add_argument("--port", type=int, default=5173)

    st = sub.add_parser("stop", help="Stop the web server (foreground or background)")
    st.add_argument("--port", type=int, default=5173,
                    help="Port to fall back to if no pid file is found")

    args = p.parse_args()
    if args.cmd == "serve":
        _serve(args.host, args.port, args.reload)
    elif args.cmd == "serve-bg":
        _serve_bg(args.host, args.port)
    elif args.cmd == "stop":
        _stop(args.port)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
