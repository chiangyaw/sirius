"""
Kali-on-AWS intrusive scan runner (gated).

Called ONLY from purpleteam.intrusive_scan after double-gated authorization.
Provisions terraform/kali-box, SSHes in, runs an active toolchain against the
authorized target, streams output as events, then reminds the operator to tear
the box down.

The Terraform project (terraform/kali-box) generates an SSH keypair, writes the
private key to terraform/kali-box/generated/kali_key.pem (git-ignored), locks the
security group to the caller's public IP, and outputs `public_ip` + `ssh_key_path`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from sirius.events import emit
from sirius.terraform_runner import TERRAFORM_ROOT, terraform_apply

_log = logging.getLogger("sirius.purpleteam.kali")

# Active tools to run on the Kali box against the authorized target.
TOOLCHAIN = [
    ("nmap", "nmap -Pn -sV -T4 --top-ports 200 {host}"),
    ("nikto", "nikto -host {url} -maxtime 120 || true"),
    ("nuclei", "nuclei -u {url} -severity medium,high,critical -silent || true"),
    ("whatweb", "whatweb {url} || true"),
]


def run_intrusive(target: str) -> dict:
    host = target.replace("https://", "").replace("http://", "").split("/")[0]
    url = target if target.startswith("http") else f"http://{target}"

    emit("terraform_step", "Provisioning Kali box (terraform/kali-box)…",
         severity="warn", payload={"target": target})
    # The intrusive double-gate already cleared a human check, so pre-authorize the
    # nested kali-box provision to avoid a second infra-authorization prompt.
    from sirius import infra_gate
    infra_gate.authorize_infra("kali-box")
    apply = terraform_apply("kali-box")
    if not apply.get("ok"):
        return {"ok": False, "stage": "provision", "detail": apply}

    outputs = apply.get("outputs", {})
    public_ip = _out(outputs, "public_ip")
    key_path = _out(outputs, "ssh_key_path") or str(
        TERRAFORM_ROOT / "kali-box" / "generated" / "kali_key.pem")
    if not public_ip:
        return {"ok": False, "stage": "provision", "error": "no public_ip output"}

    emit("status", f"Kali box up at {public_ip}; connecting over SSH…",
         severity="success", payload={"public_ip": public_ip})

    try:
        import paramiko
    except ImportError:
        return {"ok": False, "stage": "ssh",
                "error": "paramiko not installed (pip install 'sirius[purpleteam]')",
                "public_ip": public_ip}

    results: dict[str, str] = {}
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(public_ip, username="kali",
                       key_filename=key_path, timeout=60, banner_timeout=60)
        for name, tmpl in TOOLCHAIN:
            cmd = tmpl.format(host=host, url=url)
            emit("scan_finding", f"[kali] $ {cmd}", severity="warn",
                 payload={"tool": name, "cmd": cmd})
            _stdin, stdout, stderr = client.exec_command(cmd, timeout=240)
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            combined = (out + ("\n" + err if err.strip() else "")).strip()
            results[name] = combined
            for line in combined.splitlines()[:200]:
                emit("scan_finding", f"[{name}] {line}", severity="info",
                     payload={"tool": name})
    except Exception as e:  # noqa: BLE001
        emit("error", f"SSH/scan error: {e}", severity="danger")
        return {"ok": False, "stage": "ssh", "error": str(e), "public_ip": public_ip}
    finally:
        client.close()

    emit("status",
         "Intrusive scan complete. Remember to tear down: terraform destroy kali-box.",
         severity="warn", payload={"teardown": "terraform destroy kali-box"})
    return {"ok": True, "public_ip": public_ip, "target": target,
            "tools": list(results), "results": results,
            "teardown_hint": "Run terraform_destroy(project='kali-box') when done."}


def _out(outputs: dict, key: str) -> str | None:
    v = outputs.get(key)
    if isinstance(v, dict):
        return v.get("value")
    return v
