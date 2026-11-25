# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27", "pillow>=10"]
# ///
"""Call one tool of the comfyui MCP server over stdio, for sessions where its tools aren't loaded.

Usage: uv run --script mcp_call.py <tool_name> '<json arguments>'
   or: "<python from ~/.comfyui-mcp/bridge.json>" mcp_call.py <tool_name> '<json arguments>'
Prints the text result; returned images are saved under <home>/previews/ and their paths printed.
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent


def previews_dir() -> Path:
    home = os.environ.get("COMFYUI_MCP_HOME")
    if not home:
        try:
            home = json.loads((Path.home() / ".comfyui-mcp" / "config.json").read_text(encoding="utf-8")).get("home")
        except (FileNotFoundError, json.JSONDecodeError):
            home = None
    d = Path(home or Path.home() / ".comfyui-mcp").expanduser() / "previews"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def main(tool, args):
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], env=dict(os.environ))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
    if getattr(res, "is_error", False):
        print("ERROR:", end=" ")
    for i, c in enumerate(res.content):
        if getattr(c, "type", "") == "text":
            print(c.text)
        elif getattr(c, "type", "") == "image":
            p = previews_dir() / f"{time.strftime('%H%M%S')}_{tool}_{i}.jpg"
            p.write_bytes(base64.b64decode(c.data))
            print("preview:", p)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}))
