"""Launch the TraceLens MCP server over stdio from any working directory.

MCPAdapter(Path(this_file)) runs `python this_file` as a subprocess; the backend package uses
relative imports, so we put the repo root on sys.path first and then run the real server.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.mcp_server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run(transport="stdio")
