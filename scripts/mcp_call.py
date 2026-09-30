"""Call one MCP tool over streamable HTTP and print its text result.

uv run python scripts/mcp_call.py --url http://127.0.0.1:7071/mcp TOOL '{"json":"args"}'
Exit code 1 on a tool error (the error text goes to stderr).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import Client
from mcp.types import TextContent


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("tool")
    p.add_argument("args", nargs="?", default="{}")
    a = p.parse_args()
    async with Client(a.url) as client:
        res = await client.call_tool(a.tool, json.loads(a.args))
    text = "\n".join(c.text for c in res.content if isinstance(c, TextContent))
    if res.is_error:
        print(text, file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
