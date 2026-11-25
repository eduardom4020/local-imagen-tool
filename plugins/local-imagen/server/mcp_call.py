# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.27", "pillow>=10", "filelock>=3.13", "psutil>=5.9", "send2trash>=1.8"]
# ///
"""Call one tool of the local-imagen MCP server over stdio, for sessions where its tools aren't loaded.

Usage: uv run --script mcp_call.py [--session ID] <tool_name> '<json arguments>'
   or: "<python from ~/.local-imagen/bridge.json>" mcp_call.py [--session ID] <tool_name> '<json arguments>'

Every call starts a fresh server process, so pass the same --session ID (letters, digits, '.', '_', '-') to every
call of one conversation: the project, attached references and pending images belong to that session. Without it,
each call is a new, empty session. Prints the text result; returned images are saved under
<home>/previews/<session>/ and their paths printed.
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


def previews_dir(session: str) -> Path:
    home = os.environ.get("LOCAL_IMAGEN_HOME")
    if not home:
        try:
            home = json.loads((Path.home() / ".local-imagen" / "config.json").read_text(encoding="utf-8")).get("home")
        except (FileNotFoundError, json.JSONDecodeError):
            home = None
    d = Path(home or Path.home() / ".local-imagen").expanduser() / "previews" / (session or "no-session")
    d.mkdir(parents=True, exist_ok=True)
    return d


async def main(session, tool, args):
    env = dict(os.environ)
    if session:
        env["LOCAL_IMAGEN_SESSION"] = session
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")], env=env)
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
            p = previews_dir(session) / f"{time.strftime('%H%M%S')}_{tool}_{i}.jpg"
            p.write_bytes(base64.b64decode(c.data))
            print("preview:", p)


if __name__ == "__main__":
    argv = sys.argv[1:]
    sid = ""
    if len(argv) >= 2 and argv[0] == "--session":
        sid, argv = argv[1], argv[2:]
    if not argv:
        sys.exit(__doc__)
    asyncio.run(main(sid, argv[0], json.loads(argv[1]) if len(argv) > 1 else {}))
