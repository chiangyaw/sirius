"""
sirius.k8s — standalone helm/kubectl runner exposed as the sirius-k8s MCP server.

Product-agnostic cluster deploy helpers used to install workloads (e.g. the
Cortex XDR agent) onto a cluster. Kept independent of the app's events bus and
config so the MCP server (sirius.k8s.mcp_server) can run standalone over stdio.
"""

from sirius.k8s._runner import ALLOWED_BINARIES, run

__all__ = ["run", "ALLOWED_BINARIES"]
