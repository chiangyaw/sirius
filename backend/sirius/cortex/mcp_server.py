"""
Cortex XDR MCP server.

Exposes the Cortex XDR API + Sirius helpers as Model Context Protocol tools so any
MCP client (Claude Desktop, Claude Code, Sirius itself) can drive the tenant.
Runs over stdio.

Run:  python -m sirius.cortex.mcp_server
Register in an MCP client config, e.g.:
  {"mcpServers": {"cortex-mcp": {"command": "python",
    "args": ["-m", "sirius.cortex.mcp_server"]}}}

Honors the same env (CORTEX_API_KEY / CORTEX_API_KEY_ID / CORTEX_FQDN) and config
(cortex.fqdn, cortex.mock_mode, cortex.advanced_auth) as the in-app skills.
"""

from __future__ import annotations

from sirius.config import load_config
from sirius.cortex.client import CortexClient, is_k8s_related
from sirius.cortex import (
    cortex_generate_k8s_deployment,
    cortex_deploy_k8s_konnector,
    cortex_k8s_attack_report,
    cortex_download_distribution,
)


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:  # pragma: no cover
        raise SystemExit("mcp not installed. Run: pip install 'sirius[mcp]'") from e

    mcp = FastMCP("cortex-mcp")
    client = CortexClient(load_config().cortex)

    @mcp.tool()
    def test_connection() -> dict:
        """Probe Cortex XDR connectivity and auth mode."""
        return client.test_connection()

    @mcp.tool()
    def get_incidents(limit: int = 50) -> dict:
        """List recent Cortex XDR incidents."""
        return client.get_incidents(limit)

    @mcp.tool()
    def get_k8s_incidents(limit: int = 50) -> dict:
        """List only Kubernetes-related Cortex XDR incidents."""
        incs = client.get_incidents(limit).get("incidents", [])
        return {"incidents": [i for i in incs if is_k8s_related(i)]}

    @mcp.tool()
    def get_incident_extra_data(incident_id: str) -> dict:
        """Get the full alert/artifact detail for one incident."""
        return client.get_incident_extra_data(incident_id)

    @mcp.tool()
    def get_alerts(limit: int = 50) -> dict:
        """List recent Cortex XDR alerts."""
        return client.get_alerts(limit)

    @mcp.tool()
    def get_endpoints(limit: int = 100) -> dict:
        """List Cortex XDR endpoints/agents."""
        return client.get_endpoints(limit)

    @mcp.tool()
    def k8s_attack_report(limit: int = 50, max_incidents: int = 5) -> str:
        """Generate a markdown attack report from Kubernetes-related incidents."""
        return cortex_k8s_attack_report(limit=limit, max_incidents=max_incidents)

    @mcp.tool()
    def generate_k8s_deployment(distribution_id: str = "", namespace: str = "cortex",
                                cluster_name: str = "sirius-demo", image: str = "") -> dict:
        """Generate Cortex XDR k8s agent deployment (Helm values + DaemonSet manifest)."""
        return cortex_generate_k8s_deployment(distribution_id=distribution_id,
                                              namespace=namespace,
                                              cluster_name=cluster_name, image=image)

    @mcp.tool()
    def deploy_k8s_konnector(cluster_name: str, region: str = "", namespace: str = "",
                             profile_dir: str = "", auth_json: str = "",
                             values_file: str = "", release: str = "konnector") -> dict:
        """Install the Cortex agent / k8s connector on an EKS cluster from the
        downloaded k8s security-profile pair. This is the ONLY supported way to
        install the Cortex k8s connector — use it whenever asked to deploy the
        Cortex/XDR agent to a Kubernetes cluster.

        It needs NO Cortex distribution API: it runs `helm registry login` with the
        profile's auth.json (GCP _json_key) then `helm upgrade --install` of the OCI
        konnector-launcher chart using the profile's values.yaml. Repeatable and
        idempotent. If the Cortex distribution API is erroring/unavailable, use THIS.

        Requires helm/kubectl/aws on PATH and AWS creds in the environment. The
        profile pair (auth.json + values.yaml) is read from profile_dir or the
        configured cortex.konnector_profile_dir; override the individual files with
        auth_json / values_file if needed.
        """
        return cortex_deploy_k8s_konnector(
            cluster_name=cluster_name, region=region, namespace=namespace,
            profile_dir=profile_dir, auth_json=auth_json, values_file=values_file,
            release=release)

    @mcp.tool()
    def get_distribution_versions() -> dict:
        """List available Cortex agent versions per platform."""
        return client.get_distribution_versions()

    @mcp.tool()
    def download_distribution(distribution_id: str = "", package_type: str = "x64",
                              dest_dir: str = "", name: str = "",
                              platform: str = "windows", agent_version: str = "") -> dict:
        """Create (optional) → poll → download a Cortex agent installer (e.g. Windows
        MSI) in one call. Pass distribution_id to download an existing package, or
        name+platform+agent_version to create a standalone package then download it.
        The create call is async so this polls until the build finishes."""
        return cortex_download_distribution(
            distribution_id=distribution_id, package_type=package_type, dest_dir=dest_dir,
            name=name, platform=platform, agent_version=agent_version)

    @mcp.tool()
    def isolate_endpoint(endpoint_id: str) -> dict:
        """Isolate the endpoint running an affected container (containment response)."""
        return client.isolate_endpoint(endpoint_id)

    @mcp.tool()
    def unisolate_endpoint(endpoint_id: str) -> dict:
        """Remove isolation from an endpoint."""
        return client.unisolate_endpoint(endpoint_id)

    @mcp.tool()
    def scan_endpoint(endpoint_id: str) -> dict:
        """Trigger a malware scan on an endpoint."""
        return client.scan_endpoint(endpoint_id)

    @mcp.tool()
    def run_xql_query(query: str, timeframe_minutes: int = 60) -> dict:
        """Run a Cortex XQL query and return the results."""
        return client.run_xql_query(query, timeframe_minutes)

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
