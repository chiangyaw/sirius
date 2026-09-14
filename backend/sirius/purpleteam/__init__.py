"""
Purple Team scanning (Feature 2).

Two tiers:
  • LOCAL RECON (always available, non-intrusive): passive HTTP/TLS fingerprinting,
    endpoint/API discovery, and safe exposure checks against a URL or IP. Every
    finding is emitted as a `scan_finding` event and returned as a structured,
    severity-ranked report.
  • INTRUSIVE (GATED): provisions a Kali box on AWS via Terraform, SSHes in, and
    runs active tools (nmap/nikto/nuclei/sqlmap). This is DOUBLE-gated:
      1. config.purpleteam.allow_intrusive must be true, AND
      2. the human must explicitly authorize the exact target via the
         /api/purpleteam/authorize endpoint (typed acknowledgement).
    The LLM cannot self-authorize — it can only run against already-authorized
    targets. Default is refuse.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from sirius.config import load_config
from sirius.events import emit
from sirius.skills import registry

_log = logging.getLogger("sirius.purpleteam")

# Targets a human has explicitly authorized for intrusive testing this session.
_AUTHORIZED_TARGETS: set[str] = set()

COMMON_PATHS = [
    "/robots.txt", "/sitemap.xml", "/.git/config", "/.env", "/.well-known/security.txt",
    "/admin", "/login", "/api", "/api/", "/api/v1", "/api/v2", "/swagger.json",
    "/openapi.json", "/swagger-ui.html", "/api-docs", "/graphql", "/actuator",
    "/actuator/health", "/metrics", "/status", "/health", "/server-status",
    "/.aws/credentials", "/config.json", "/backup.zip", "/phpinfo.php",
]

SECURITY_HEADERS = [
    "content-security-policy", "strict-transport-security", "x-frame-options",
    "x-content-type-options", "referrer-policy", "permissions-policy",
]

_ENDPOINT_RE = re.compile(r"""["'`](/(?:api|v\d|graphql|rest)[a-zA-Z0-9/_\-.]*)["'`]""")
_HREF_RE = re.compile(r"""(?:href|src|action)=["']([^"'#]+)["']""", re.I)


def authorize_target(target: str) -> None:
    _AUTHORIZED_TARGETS.add(_normalize(target))


def is_authorized(target: str) -> bool:
    return _normalize(target) in _AUTHORIZED_TARGETS


def _normalize(target: str) -> str:
    t = target.strip()
    if not t.startswith(("http://", "https://")):
        t = "http://" + t
    p = urlparse(t)
    return (p.hostname or t).lower()


def _finding(title: str, severity: str, **detail: Any) -> dict:
    emit("scan_finding", title, severity=severity, payload=detail)
    return {"title": title, "severity": severity, **detail}


# ── Local recon ───────────────────────────────────────────────────────────────
def recon_target(target: str, **_: Any) -> dict:
    """Non-intrusive recon of a URL or IP. Safe to run against your own assets."""
    base = target.strip()
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    host = urlparse(base).hostname or base
    emit("skill_invoked", f"Purple Team local recon: {host}", payload={"target": base})

    findings: list[dict] = []
    endpoints: set[str] = set()

    client = httpx.Client(follow_redirects=True, timeout=8.0, verify=False,
                          headers={"User-Agent": "Sirius-PurpleTeam/0.1"})
    try:
        # 1. Reachability + fingerprint.
        try:
            r = client.get(base)
        except Exception as e:  # noqa: BLE001
            findings.append(_finding(f"Target unreachable: {e}", "warn", target=base))
            return {"target": base, "reachable": False, "findings": findings}

        server = r.headers.get("server", "unknown")
        powered = r.headers.get("x-powered-by")
        findings.append(_finding(
            f"Reachable — HTTP {r.status_code}, server: {server}", "info",
            status=r.status_code, server=server, x_powered_by=powered,
            final_url=str(r.url)))

        # 2. Missing security headers.
        missing = [h for h in SECURITY_HEADERS if h not in {k.lower() for k in r.headers}]
        if missing:
            findings.append(_finding(
                f"Missing {len(missing)} security header(s): {', '.join(missing)}",
                "warn", missing_headers=missing))

        # 3. Cookie flags.
        for ck in r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else []:
            low = ck.lower()
            flags = [f for f in ("httponly", "secure", "samesite") if f not in low]
            if flags:
                findings.append(_finding(
                    f"Cookie missing flags: {', '.join(flags)}", "warn", cookie=ck[:60]))

        # 4. CORS wildcard.
        acao = r.headers.get("access-control-allow-origin")
        if acao == "*":
            findings.append(_finding("CORS allows any origin (*)", "warn"))

        # 5. TLS check (if https).
        if urlparse(base).scheme == "https":
            findings.append(_tls_finding(host, urlparse(base).port or 443))

        # 6. Harvest endpoints from HTML + inline JS.
        body = r.text or ""
        for m in _HREF_RE.findall(body):
            if m.startswith("/") or m.startswith(base):
                endpoints.add(urljoin(base, m))
        for m in _ENDPOINT_RE.findall(body):
            endpoints.add(urljoin(base, m))

        # 7. Probe common paths (GET, read-only).
        for path in COMMON_PATHS:
            url = urljoin(base, path)
            try:
                pr = client.get(url)
            except Exception:  # noqa: BLE001
                continue
            if pr.status_code < 400:
                endpoints.add(url)
                sev = _path_severity(path, pr)
                if sev:
                    findings.append(_finding(
                        f"Exposed {path} (HTTP {pr.status_code})", sev,
                        url=url, status=pr.status_code,
                        content_type=pr.headers.get("content-type")))
                if path in ("/swagger.json", "/openapi.json", "/api-docs"):
                    findings.extend(_parse_openapi(pr, base, endpoints))
    finally:
        client.close()

    report = {
        "target": base,
        "host": host,
        "reachable": True,
        "findings": sorted(findings, key=lambda f: _SEV_ORDER.get(f["severity"], 9)),
        "endpoints": sorted(endpoints),
        "counts": _counts(findings),
    }
    emit("status", f"Recon complete: {len(findings)} findings, {len(endpoints)} endpoints",
         severity="success", payload=report["counts"])
    return report


_SEV_ORDER = {"danger": 0, "warn": 1, "info": 2, "success": 3}


def _counts(findings: list[dict]) -> dict:
    c: dict[str, int] = {}
    for f in findings:
        c[f["severity"]] = c.get(f["severity"], 0) + 1
    return c


def _path_severity(path: str, resp: httpx.Response) -> str | None:
    high = ("/.git/config", "/.env", "/.aws/credentials", "/config.json", "/backup.zip")
    if path in high and resp.status_code == 200:
        return "danger"
    if path in ("/actuator", "/actuator/health", "/metrics", "/phpinfo.php", "/server-status"):
        return "warn"
    if path in ("/swagger.json", "/openapi.json", "/api-docs", "/graphql"):
        return "warn"
    return "info"


def _parse_openapi(resp: httpx.Response, base: str, endpoints: set[str]) -> list[dict]:
    try:
        spec = resp.json()
    except Exception:  # noqa: BLE001
        return []
    paths = spec.get("paths", {})
    for p in paths:
        endpoints.add(urljoin(base, p))
    return [_finding(
        f"OpenAPI/Swagger spec exposed: {len(paths)} documented API paths", "warn",
        api_paths=list(paths)[:50])]


def _tls_finding(host: str, port: int) -> dict:
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=6) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                version = ssock.version()
        weak = version in ("TLSv1", "TLSv1.1", "SSLv3")
        return _finding(
            f"TLS negotiated: {version}", "warn" if weak else "info",
            tls_version=version, weak=weak)
    except Exception as e:  # noqa: BLE001
        return _finding(f"TLS check failed: {e}", "info")


# ── Intrusive (gated) ─────────────────────────────────────────────────────────
def intrusive_scan(target: str, **_: Any) -> dict:
    """Active scan from a Kali box. Refuses unless double-gated authorization is met."""
    cfg = load_config()
    if not cfg.purpleteam.allow_intrusive:
        msg = ("Intrusive scanning is disabled. Set purpleteam.allow_intrusive: true "
               "in config.yaml AND authorize the target via /api/purpleteam/authorize.")
        emit("blocked", "Intrusive scan refused (feature disabled)", severity="danger",
             payload={"target": target})
        return {"ok": False, "refused": True, "reason": msg}
    if not is_authorized(target):
        msg = (f"Target {target!r} is not authorized for intrusive testing. A human must "
               "confirm ownership/authorization via /api/purpleteam/authorize first.")
        emit("blocked", "Intrusive scan refused (target not authorized)", severity="danger",
             payload={"target": target})
        return {"ok": False, "refused": True, "reason": msg}

    emit("skill_invoked", f"Authorized intrusive scan: {target}", severity="warn",
         payload={"target": target})
    from sirius.purpleteam.kali import run_intrusive
    return run_intrusive(target)


registry.skill(
    "purpleteam_recon",
    "Run non-intrusive purple-team recon (fingerprint, endpoint/API discovery, "
    "exposure checks) against a URL or IP you own.",
    {"type": "object",
     "properties": {"target": {"type": "string", "description": "URL or IP to recon"}},
     "required": ["target"]},
)(recon_target)

registry.skill(
    "purpleteam_intrusive_scan",
    "Run an ACTIVE/intrusive scan from a provisioned Kali box. Only works on targets "
    "a human has explicitly authorized; refuses otherwise.",
    {"type": "object",
     "properties": {"target": {"type": "string", "description": "authorized URL or IP"}},
     "required": ["target"]},
)(intrusive_scan)
