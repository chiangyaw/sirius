"""
Native k8s agent skills — event-emitting wrappers around the sirius.k8s runner.

These promote the `sirius-k8s` MCP tools (helm/kubectl/etc.) into first-class,
in-process skills so agents can use them *and* have every step show up in the live
event stream. Reuses the same `ALLOWED_BINARIES`-guarded, shell=False runner as the
MCP server (sirius.k8s._runner.run).

LEAST PRIVILEGE: read-only verbs (kubectl get/describe/logs…, helm list/status…)
run freely. Mutating verbs (helm install/upgrade/uninstall, kubectl apply/delete/
exec/…, apply_manifest, helm_install) are routed through sirius.infra_gate.guard,
which enforces the calling agent's scope + human authorization.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Optional

from sirius import infra_gate
from sirius.events import emit
from sirius.k8s._runner import run
from sirius.skills import registry

# terraform/eks/cortex-agent/daemonset.yaml — where the cortex-mcp MCP writes the
# generated manifest. k8s_skills.py -> parents: [0]=sirius [1]=backend [2]=repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = _REPO_ROOT / "terraform" / "eks" / "cortex-agent" / "daemonset.yaml"

# The k8s "resource" name used for authorization of cluster mutations.
_K8S_RESOURCE = "k8s"

_HELM_MUTATIONS = {"install", "upgrade", "uninstall", "delete", "rollback"}
_KUBECTL_MUTATIONS = {
    "apply", "delete", "create", "replace", "patch", "scale", "edit", "exec",
    "cordon", "drain", "uncordon", "taint", "annotate", "label", "set", "rollout",
    "expose", "run", "autoscale",
}


def _exec(cmd: list[str], *, mutation: bool = False, stdin: Optional[str] = None) -> dict:
    """Run a command via the guarded runner, emitting the command + its result."""
    emit("skill_invoked", f"$ {' '.join(cmd)}",
         severity="warn" if mutation else "info", payload={"cmd": cmd})
    res = run(cmd, stdin=stdin)
    ok = bool(res.get("ok"))
    out = res.get("output", "") or ""
    emit("status", f"{cmd[0]} exited {res.get('exit_code')}",
         severity="success" if ok else "danger",
         payload={"exit_code": res.get("exit_code"), "output": out[-4000:]})
    return res


def k8s_update_kubeconfig(cluster_name: str, region: str, **_: Any) -> dict:
    """Point kubeconfig at an EKS cluster (aws eks update-kubeconfig). Not gated."""
    return _exec(["aws", "eks", "update-kubeconfig",
                  "--name", cluster_name, "--region", region])


def k8s_helm(command: str, **_: Any) -> dict:
    """Run a helm command (leading 'helm' optional). Mutating verbs are gated."""
    args = shlex.split(command)
    if args and args[0] == "helm":
        args = args[1:]
    verb = args[0] if args else ""
    mutation = verb in _HELM_MUTATIONS
    if mutation:
        refusal = infra_gate.guard(_K8S_RESOURCE, f"helm {verb}")
        if refusal:
            return refusal
    return _exec(["helm", *args], mutation=mutation)


def k8s_kubectl(command: str, **_: Any) -> dict:
    """Run a kubectl command (leading 'kubectl' optional). Mutating verbs are gated."""
    args = shlex.split(command)
    if args and args[0] == "kubectl":
        args = args[1:]
    verb = args[0] if args else ""
    mutation = verb in _KUBECTL_MUTATIONS
    if mutation:
        refusal = infra_gate.guard(_K8S_RESOURCE, f"kubectl {verb}")
        if refusal:
            return refusal
    return _exec(["kubectl", *args], mutation=mutation)


def k8s_apply_manifest(path: str = "", manifest: str = "", namespace: str = "",
                       **_: Any) -> dict:
    """kubectl apply a manifest file (defaults to the generated Cortex daemonset)
    or piped YAML. Always gated (mutation)."""
    refusal = infra_gate.guard(_K8S_RESOURCE, "kubectl apply")
    if refusal:
        return refusal
    ns = ["-n", namespace] if namespace else []
    if manifest:
        return _exec(["kubectl", "apply", *ns, "-f", "-"], mutation=True, stdin=manifest)
    target = path or str(_DEFAULT_MANIFEST)
    if not Path(target).exists():
        return {"ok": False, "exit_code": 2,
                "output": f"manifest not found: {target}", "cmd": ["kubectl", "apply"]}
    return _exec(["kubectl", "apply", *ns, "-f", target], mutation=True)


def k8s_helm_install(release: str, chart: str, namespace: str = "default",
                     values_file: str = "", repo_name: str = "", repo_url: str = "",
                     **_: Any) -> dict:
    """(Optionally add a repo) then `helm upgrade --install`. Always gated (mutation)."""
    refusal = infra_gate.guard(_K8S_RESOURCE, "helm upgrade --install")
    if refusal:
        return refusal
    steps: list[dict] = []
    if repo_name and repo_url:
        steps.append(_exec(["helm", "repo", "add", repo_name, repo_url]))
        steps.append(_exec(["helm", "repo", "update"]))
    cmd = ["helm", "upgrade", "--install", release, chart,
           "--namespace", namespace, "--create-namespace"]
    if values_file:
        cmd += ["-f", values_file]
    steps.append(_exec(cmd, mutation=True))
    return {"ok": all(s.get("ok") for s in steps), "steps": steps}


# ── Registration ─────────────────────────────────────────────────────────────
_CMD_SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": "string", "description": "helm/kubectl args"}},
    "required": ["command"],
}
registry.skill("k8s_update_kubeconfig",
               "Point kubeconfig at an EKS cluster (aws eks update-kubeconfig).",
               {"type": "object",
                "properties": {"cluster_name": {"type": "string"},
                               "region": {"type": "string"}},
                "required": ["cluster_name", "region"]})(k8s_update_kubeconfig)
registry.skill("k8s_helm",
               "Run a helm command. Read-only verbs run freely; install/upgrade/"
               "uninstall/rollback require infra authorization.",
               _CMD_SCHEMA)(k8s_helm)
registry.skill("k8s_kubectl",
               "Run a kubectl command. Read-only verbs (get/describe/logs…) run "
               "freely; apply/delete/exec/patch/… require infra authorization.",
               _CMD_SCHEMA)(k8s_kubectl)
registry.skill("k8s_apply_manifest",
               "kubectl apply a manifest file or piped YAML (gated).",
               {"type": "object",
                "properties": {"path": {"type": "string"},
                               "manifest": {"type": "string"},
                               "namespace": {"type": "string"}}})(k8s_apply_manifest)
registry.skill("k8s_helm_install",
               "helm upgrade --install a chart, optionally adding a repo first (gated).",
               {"type": "object",
                "properties": {"release": {"type": "string"},
                               "chart": {"type": "string"},
                               "namespace": {"type": "string"},
                               "values_file": {"type": "string"},
                               "repo_name": {"type": "string"},
                               "repo_url": {"type": "string"}},
                "required": ["release", "chart"]})(k8s_helm_install)
