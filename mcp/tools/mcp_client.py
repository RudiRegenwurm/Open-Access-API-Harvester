"""Small real stdio MCP client; no host application account required."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call(root, tool, arguments):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "notanda_mcp.server", "--evidence-dir", str(root.resolve())],
        env=dict(os.environ),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {item.name for item in (await session.list_tools()).tools}
            assert names == {"search_literature", "get_evidence"}, names
            result = await session.call_tool(tool, arguments)
            if result.isError:
                raise RuntimeError("MCP transport/tool error; inspect local setup")
            return result.structuredContent


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--query")
    group.add_argument("--id")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    tool = "get_evidence" if args.id else "search_literature"
    arguments = (
        {"evidence_id": args.id}
        if args.id
        else {"query": args.query, "limit": args.limit}
    )
    output = asyncio.run(call(args.evidence_dir, tool, arguments))
    if args.query and output.get("status") == "ok":
        persisted = json.loads(
            (args.evidence_dir / output["evidence_id"] / "response.json").read_bytes()
        )
        assert output == persisted, "Received result differs from saved response"
    print(json.dumps(output, ensure_ascii=True, indent=2))
    sys.exit(0 if output.get("status") == "ok" else 1)
