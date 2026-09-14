"""Entry point for the sirius-cloud MCP server:  python -m sirius.cloud.main"""

from __future__ import annotations

from sirius.cloud.mcp_server import build_server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
