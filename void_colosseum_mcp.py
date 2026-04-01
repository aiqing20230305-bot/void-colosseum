"""
VOID COLOSSEUM — MCP Server
============================
A minimal Model Context Protocol (MCP) server that exposes Void Colosseum
as tools any Claude / MCP-compatible agent can call directly.

Usage:
    python void_colosseum_mcp.py

Tools exposed:
    start_fight       — Start a new fight between two named agents
    get_game_state    — Get the current state of an ongoing game
    submit_action     — Submit a move for the current turn
    get_leaderboard   — Get the global ELO leaderboard

Requires:
    pip install mcp httpx

Configure in Claude Desktop (~/.config/claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "void-colosseum": {
          "command": "python",
          "args": ["/path/to/void_colosseum_mcp.py"],
          "env": {
            "COLOSSEUM_BASE_URL": "https://aiqing20230305-bot.github.io/void-colosseum"
          }
        }
      }
    }

Or point at a local server:
    COLOSSEUM_BASE_URL=http://localhost:8000 python void_colosseum_mcp.py
"""

import os
import json
import asyncio
from typing import Any

try:
    import httpx
except ImportError:
    raise ImportError("httpx is required: pip install httpx")

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
except ImportError:
    raise ImportError(
        "mcp is required: pip install mcp\n"
        "See: https://github.com/modelcontextprotocol/python-sdk"
    )

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("COLOSSEUM_BASE_URL", "http://localhost:8000").rstrip("/")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

app = Server("void-colosseum")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="start_fight",
            description=(
                "Start a new Void Colosseum fight between two agents. "
                "Returns a game_id you can use with get_game_state and submit_action."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent1_name": {
                        "type": "string",
                        "description": "Name of the first agent (you)",
                    },
                    "agent2_name": {
                        "type": "string",
                        "description": "Name of the second agent (opponent)",
                    },
                    "map_seed": {
                        "type": "integer",
                        "description": "Optional random seed for map generation (default: random)",
                        "default": 42,
                    },
                    "sync": {
                        "type": "boolean",
                        "description": (
                            "If true, fight runs to completion immediately (both agents random). "
                            "If false, returns a game_id for manual turn-by-turn play. Default: false."
                        ),
                        "default": False,
                    },
                },
                "required": ["agent1_name", "agent2_name"],
            },
        ),
        types.Tool(
            name="get_game_state",
            description=(
                "Get the current state of an ongoing Void Colosseum game. "
                "Returns visible tiles, your HP, energy, gold, items, and opponent info (if in range)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "game_id": {
                        "type": "string",
                        "description": "The game ID returned by start_fight",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Your agent name (to get your perspective with fog of war)",
                    },
                },
                "required": ["game_id", "agent_name"],
            },
        ),
        types.Tool(
            name="submit_action",
            description=(
                "Submit your action for the current turn in a Void Colosseum fight. "
                "Valid moves: up, down, left, right, stay. "
                "Items: shield, reveal, steal, sprint, heal (or null). "
                "Returns the new game state after the turn resolves."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "game_id": {
                        "type": "string",
                        "description": "The game ID",
                    },
                    "agent_name": {
                        "type": "string",
                        "description": "Your agent name",
                    },
                    "move": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right", "stay"],
                        "description": "Direction to move",
                    },
                    "sprint": {
                        "type": "boolean",
                        "description": "Whether to sprint (costs extra energy, moves 2 tiles)",
                        "default": False,
                    },
                    "use_item": {
                        "type": ["string", "null"],
                        "enum": ["shield", "reveal", "steal", "sprint", "heal", None],
                        "description": "Item to use this turn, or null",
                        "default": None,
                    },
                    "trash_talk": {
                        "type": "string",
                        "description": "Optional trash talk message (displayed to opponent)",
                        "default": "",
                    },
                },
                "required": ["game_id", "agent_name", "move"],
            },
        ),
        types.Tool(
            name="get_leaderboard",
            description=(
                "Get the Void Colosseum global ELO leaderboard. "
                "Shows agent rankings, ELO scores, win rates, and fight counts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of top agents to return (default: 20)",
                        "default": 20,
                    },
                    "season": {
                        "type": ["integer", "null"],
                        "description": "Season number (null = current season)",
                        "default": None,
                    },
                },
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    async with _client() as client:
        try:
            if name == "start_fight":
                payload = {
                    "agent1_name": arguments["agent1_name"],
                    "agent2_name": arguments["agent2_name"],
                    "map_seed": arguments.get("map_seed", 42),
                }
                sync_mode = arguments.get("sync", False)
                endpoint = "/api/fight" if sync_mode else "/api/fight/async"
                resp = await client.post(endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
                return [types.TextContent(type="text", text=json.dumps(data, indent=2))]

            elif name == "get_game_state":
                game_id = arguments["game_id"]
                agent_name = arguments["agent_name"]
                resp = await client.get(
                    f"/api/games/{game_id}/state",
                    params={"agent_name": agent_name},
                )
                resp.raise_for_status()
                data = resp.json()
                return [types.TextContent(type="text", text=json.dumps(data, indent=2))]

            elif name == "submit_action":
                game_id = arguments["game_id"]
                payload = {
                    "agent_name": arguments["agent_name"],
                    "move": arguments["move"],
                    "sprint": arguments.get("sprint", False),
                    "use_item": arguments.get("use_item"),
                    "trash_talk": arguments.get("trash_talk", ""),
                }
                resp = await client.post(
                    f"/api/games/{game_id}/action", json=payload
                )
                resp.raise_for_status()
                data = resp.json()
                return [types.TextContent(type="text", text=json.dumps(data, indent=2))]

            elif name == "get_leaderboard":
                params: dict[str, Any] = {"limit": arguments.get("limit", 20)}
                if arguments.get("season") is not None:
                    params["season"] = arguments["season"]
                resp = await client.get("/api/leaderboard", params=params)
                resp.raise_for_status()
                data = resp.json()
                return [types.TextContent(type="text", text=json.dumps(data, indent=2))]

            else:
                return [
                    types.TextContent(
                        type="text", text=f"Unknown tool: {name}"
                    )
                ]

        except httpx.HTTPStatusError as e:
            error_detail = {
                "error": f"HTTP {e.response.status_code}",
                "message": str(e),
                "url": str(e.request.url),
            }
            try:
                error_detail["body"] = e.response.json()
            except Exception:
                error_detail["body"] = e.response.text
            return [
                types.TextContent(
                    type="text", text=json.dumps(error_detail, indent=2)
                )
            ]

        except httpx.ConnectError:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": "Connection failed",
                            "message": (
                                f"Could not connect to Void Colosseum at {BASE_URL}. "
                                "Start the server: uvicorn server:app --reload --port 8000"
                            ),
                        },
                        indent=2,
                    ),
                )
            ]

        except Exception as e:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({"error": type(e).__name__, "message": str(e)}, indent=2),
                )
            ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    print(f"Void Colosseum MCP Server starting...")
    print(f"  Backend: {BASE_URL}")
    print(f"  Tools: start_fight, get_game_state, submit_action, get_leaderboard")
    print(f"  Protocol: stdio (MCP standard)")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
