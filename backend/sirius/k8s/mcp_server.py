"""
sirius-k8s MCP server.

A standalone, product-agnostic Model Context Protocol server that runs `helm`
and `kubectl` so an MCP client can install workloads on a cluster — e.g. `helm
install` the Cortex XDR agent chart, or `kubectl apply -f` the manifest generated
by the cortex-mcp MCP's `generate_k8s_deployment` tool.

Runs over stdio:  python -m sirius.k8s.main

Register in an MCP client, e.g.:
  {"mcpServers": {"sirius-k8s": {"command": "backend/.venv/bin/python",
    "args": ["-m", "sirius.k8s.main"]}}}

SECURITY: this is a GENERIC runner — `helm(command)` / `kubectl(command)` accept
free-form arguments and can do anything those tools can (deploy, delete, exec).
It is not gated behind a config flag. Mitigations: commands are tokenized with
shlex and executed as argument lists (never `shell=True`), and only the
helm/kubectl/aws binaries may be launched (see _runner.ALLOWED_BINARIES). Only
expose this server to trusted clients.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from sirius.k8s._runner import run

# terraform/eks/cortex-agent/ — where the cortex-mcp MCP writes generated manifests.
# k8s/mcp_server.py -> parents: [0]=k8s [1]=sirius [2]=backend; .parent = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2].parent
_DEFAULT_MANIFEST = _REPO_ROOT / "terraform" / "eks" / "cortex-agent" / "daemonset.yaml"


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit("mcp not installed. Run: pip install 'sirius[mcp]'") from e

    mcp = FastMCP("sirius-k8s")

    @mcp.tool()
    def update_kubeconfig(cluster_name: str, region: str) -> dict:
        """Point kubeconfig at an EKS cluster (aws eks update-kubeconfig)."""
        return run(["aws", "eks", "update-kubeconfig",
                    "--name", cluster_name, "--region", region])

    @mcp.tool()
    def helm(command: str) -> dict:
        """Run an arbitrary `helm` command, e.g. 'upgrade --install cortex-agent
        cortex/cortex-agent -n cortex --create-namespace -f values.yaml'.
        The leading 'helm' is optional and stripped if present."""
        args = shlex.split(command)
        if args and args[0] == "helm":
            args = args[1:]
        return run(["helm", *args])

    @mcp.tool()
    def kubectl(command: str) -> dict:
        """Run an arbitrary `kubectl` command, e.g. 'get ds -n cortex'.
        The leading 'kubectl' is optional and stripped if present."""
        args = shlex.split(command)
        if args and args[0] == "kubectl":
            args = args[1:]
        return run(["kubectl", *args])

    @mcp.tool()
    def apply_manifest(path: str = "", manifest: str = "", namespace: str = "") -> dict:
        """kubectl apply a manifest. Provide `path` to apply a file (defaults to
        the generated Cortex agent daemonset.yaml), or `manifest` to pipe YAML
        content via stdin. `namespace` is optional (-n)."""
        ns = ["-n", namespace] if namespace else []
        if manifest:
            return run(["kubectl", "apply", *ns, "-f", "-"], stdin=manifest)
        target = path or str(_DEFAULT_MANIFEST)
        if not Path(target).exists():
            return {"ok": False, "exit_code": 2,
                    "output": f"manifest not found: {target}", "cmd": ["kubectl", "apply"]}
        return run(["kubectl", "apply", *ns, "-f", target])

    @mcp.tool()
    def helm_install(release: str, chart: str, namespace: str = "default",
                     values_file: str = "", repo_name: str = "", repo_url: str = "") -> dict:
        """Convenience: (optionally add a repo) then `helm upgrade --install`.
        Returns the combined output of each step."""
        steps: list[dict] = []
        if repo_name and repo_url:
            steps.append(run(["helm", "repo", "add", repo_name, repo_url]))
            steps.append(run(["helm", "repo", "update"]))
        cmd = ["helm", "upgrade", "--install", release, chart,
               "--namespace", namespace, "--create-namespace"]
        if values_file:
            cmd += ["-f", values_file]
        steps.append(run(cmd))
        return {"ok": all(s["ok"] for s in steps), "steps": steps}

    return mcp
