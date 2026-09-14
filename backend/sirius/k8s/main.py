"""Entry point for the sirius-k8s MCP server:  python -m sirius.k8s.main"""

from __future__ import annotations

from sirius.k8s.mcp_server import build_server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
