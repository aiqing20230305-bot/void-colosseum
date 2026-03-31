"""
VOID COLOSSEUM — FastAPI Backend Server v0.3
=============================================
Endpoints:
  POST /api/fight              — Run a match, persist result
  POST /api/fight/async        — Start async match, stream via WebSocket
  GET  /api/leaderboard        — Dynamic ELO leaderboard
  GET  /api/replay/{game_id}   — Fetch replay frames for a game
  GET  /api/games              — Recent game history
  POST /api/players            — Register a new named agent
  GET  /api/stats/{player_name}— Per-player stats
  GET  /api/challenge/{code}   — Decode a challenge link
  POST /api/challenge          — Generate a challenge code
  WS   /ws/live/{game_id}      — Stream live match frames

Run:
  uvicorn server:app --reload --port 8000
"""

import asyncio
import base64
import sqlite3
import json
import math
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict, List, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from void_colosseum_sdk import Arena, GreedyAgent, RandomAgent, LLMAgent, BaseAgent

# ============================================================
#  WEBSOCKET MANAGER
# ============================================================

class ConnectionManager:
    def __init__(self):
        # game_id -> set of connected websockets
        self.active: Dict[str, Set[WebSocket]] = {}

    async def connect(self, game_id: str, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(game_id, set()).add(ws)

    def disconnect(self, game_id: str, ws: WebSocket):
        if game_id in self.active:
            self.active[game_id].discard(ws)

    async def broadcast(self, game_id: str, data: dict):
        dead = set()
        for ws in self.active.get(game_id, set()):
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.disconnect(game_id, ws)

ws_manager = ConnectionManager()

# ============================================================
#  DATABASE
# ============================================================

DB_PATH = Path(__file__).parent / "colosseum.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS tournaments (
        id           TEXT PRIMARY KEY,
        status       TEXT DEFAULT 'pending',
        players_json TEXT,
        results_json TEXT,
        created_at   REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS players (
        name        TEXT PRIMARY KEY,
        elo         REAL DEFAULT 1200,
        wins        INTEGER DEFAULT 0,
        losses      INTEGER DEFAULT 0,
        draws       INTEGER DEFAULT 0,
        total_gold  INTEGER DEFAULT 0,
        agent_type  TEXT DEFAULT 'custom',
        legacy_msg  TEXT DEFAULT '',
        created_at  REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS games (
        id           TEXT PRIMARY KEY,
        player_a     TEXT NOT NULL,
        player_b     TEXT NOT NULL,
        winner       TEXT,          -- 'a', 'b', or 'draw'
        winner_name  TEXT,
        score_a      INTEGER DEFAULT 0,
        score_b      INTEGER DEFAULT 0,
        hp_a         INTEGER DEFAULT 0,
        hp_b         INTEGER DEFAULT 0,
        turns        INTEGER DEFAULT 0,
        seed         INTEGER,
        log          TEXT,          -- JSON array of log lines
        replay       TEXT,          -- JSON array of frame snapshots
        played_at    REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS elo_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id     TEXT NOT NULL,
        player_name TEXT NOT NULL,
        elo_before  REAL,
        elo_after   REAL,
        delta       REAL,
        recorded_at REAL DEFAULT (strftime('%s','now'))
    );

    CREATE TABLE IF NOT EXISTS seasons (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        started_at  REAL NOT NULL,
        ended_at    REAL,
        status      TEXT DEFAULT 'active',   -- 'active' | 'closed'
        snapshot    TEXT                     -- JSON: leaderboard at close
    );

    CREATE TABLE IF NOT EXISTS season_stats (
        season_id   INTEGER NOT NULL,
        player_name TEXT NOT NULL,
        elo_peak    REAL DEFAULT 1200,
        elo_final   REAL DEFAULT 1200,
        wins        INTEGER DEFAULT 0,
        losses      INTEGER DEFAULT 0,
        draws       INTEGER DEFAULT 0,
        total_gold  INTEGER DEFAULT 0,
        rank        INTEGER,
        PRIMARY KEY (season_id, player_name)
    );

    CREATE TABLE IF NOT EXISTS maps (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        author      TEXT DEFAULT 'anonymous',
        grid_json   TEXT NOT NULL,   -- serialized 14x14 cell array
        width       INTEGER DEFAULT 14,
        height      INTEGER DEFAULT 14,
        play_count  INTEGER DEFAULT 0,
        created_at  REAL DEFAULT (strftime('%s','now'))
    );
    """)

    # Seed built-in agents
    for name, agent_type in [("GreedyBot", "greedy"), ("RandomBot", "random")]:
        cur.execute(
            "INSERT OR IGNORE INTO players (name, agent_type) VALUES (?, ?)",
            (name, agent_type)
        )

    # Auto-create Season 1 if no active season exists
    active = cur.execute("SELECT id FROM seasons WHERE status='active' LIMIT 1").fetchone()
    if not active:
        cur.execute(
            "INSERT INTO seasons (name, started_at, status) VALUES (?, ?, 'active')",
            ("Season 1", time.time())
        )

    conn.commit()
    conn.close()


# ============================================================
#  ELO CALCULATION
# ============================================================

K = 32  # ELO K-factor


def expected_score(ra: float, rb: float) -> float:
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def update_elo(ra: float, rb: float, result: str):
    """result: 'a' = a wins, 'b' = b wins, 'draw'"""
    ea = expected_score(ra, rb)
    eb = expected_score(rb, ra)
    if result == "a":
        sa, sb = 1.0, 0.0
    elif result == "b":
        sa, sb = 0.0, 1.0
    else:
        sa, sb = 0.5, 0.5
    new_ra = ra + K * (sa - ea)
    new_rb = rb + K * (sb - eb)
    return round(new_ra, 2), round(new_rb, 2)


# ============================================================
#  AGENT FACTORY
# ============================================================

def build_agent(name: str, agent_type: str, decide_url: Optional[str] = None,
                api_key: Optional[str] = None, model: Optional[str] = None) -> BaseAgent:
    if agent_type == "greedy":
        a = GreedyAgent(name)
    elif agent_type == "random":
        a = RandomAgent(name)
    elif agent_type == "claude":
        from void_colosseum_sdk import make_claude_agent
        a = make_claude_agent(name, api_key=api_key,
                              model=model or "claude-sonnet-4-6")
    elif agent_type == "webhook" and decide_url:
        import urllib.request
        url = decide_url
        def webhook_decide(state):
            data = json.dumps(state).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read())
            except Exception:
                return {"move": "stay"}
        a = LLMAgent(name, decide_fn=webhook_decide)
    else:
        a = GreedyAgent(name)  # fallback
    return a


# ============================================================
#  PYDANTIC MODELS
# ============================================================

class FightRequest(BaseModel):
    player_a: str
    player_b: str
    agent_type_a: Optional[str] = "greedy"
    agent_type_b: Optional[str] = "greedy"
    webhook_url_a: Optional[str] = None
    webhook_url_b: Optional[str] = None
    api_key_a: Optional[str] = None   # for agent_type="claude"
    api_key_b: Optional[str] = None
    model_a: Optional[str] = None     # claude model override
    model_b: Optional[str] = None
    seed: Optional[int] = None


class PlayerCreate(BaseModel):
    name: str
    agent_type: Optional[str] = "custom"
    legacy_msg: Optional[str] = ""


class LegacyUpdate(BaseModel):
    legacy_msg: str


class AsyncFightRequest(BaseModel):
    player_a: str
    player_b: str
    agent_type_a: Optional[str] = "greedy"
    agent_type_b: Optional[str] = "greedy"
    webhook_url_a: Optional[str] = None
    webhook_url_b: Optional[str] = None
    api_key_a: Optional[str] = None
    api_key_b: Optional[str] = None
    model_a: Optional[str] = None
    model_b: Optional[str] = None
    seed: Optional[int] = None
    frame_delay: Optional[float] = 0.1   # seconds between broadcast frames


class ChallengeRequest(BaseModel):
    challenger: str
    agent_type: Optional[str] = "greedy"
    webhook_url: Optional[str] = None
    message: Optional[str] = ""


class TournamentRequest(BaseModel):
    players: List[str]                           # agent 名列表，至少2个
    agent_types: Optional[Dict[str, str]] = {}   # name -> 'greedy'/'random'/'webhook'
    webhook_urls: Optional[Dict[str, str]] = {}  # name -> url
    rounds: Optional[int] = 3                    # 每对对战几轮
    async_mode: Optional[bool] = False           # True=后台跑，WS广播进度


# ============================================================
#  LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    print("✅ Database initialized:", DB_PATH)
    yield


# ============================================================
#  APP
# ============================================================

app = FastAPI(
    title="Void Colosseum API",
    version="0.3.0",
    description="Strategic AI arena — backend API",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
#  ROUTES — PLAYERS
# ============================================================

@app.post("/api/players", summary="Register a player")
def create_player(req: PlayerCreate):
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO players (name, agent_type, legacy_msg) VALUES (?, ?, ?)",
            (req.name, req.agent_type, req.legacy_msg)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM players WHERE name=?", (req.name,)).fetchone()
        return dict(row)
    finally:
        conn.close()


@app.get("/api/players/{name}", summary="Get player info")
def get_player(name: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE name=?", (name,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"Player '{name}' not found")
    return dict(row)


@app.patch("/api/players/{name}/legacy", summary="Update legacy message")
def update_legacy(name: str, req: LegacyUpdate):
    conn = get_db()
    conn.execute("UPDATE players SET legacy_msg=? WHERE name=?", (req.legacy_msg, name))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/stats/{player_name}", summary="Player stats")
def player_stats(player_name: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE name=?", (player_name,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"Player '{player_name}' not found")
    player = dict(row)

    history = conn.execute("""
        SELECT elo_before, elo_after, delta, recorded_at
        FROM elo_history WHERE player_name=?
        ORDER BY recorded_at DESC LIMIT 20
    """, (player_name,)).fetchall()

    recent_games = conn.execute("""
        SELECT id, player_a, player_b, winner_name, score_a, score_b, turns, played_at
        FROM games
        WHERE player_a=? OR player_b=?
        ORDER BY played_at DESC LIMIT 10
    """, (player_name, player_name)).fetchall()

    conn.close()
    return {
        **player,
        "elo_history": [dict(h) for h in history],
        "recent_games": [dict(g) for g in recent_games],
    }


# ============================================================
#  ROUTES — FIGHT
# ============================================================

@app.post("/api/fight", summary="Run a match")
def run_fight(req: FightRequest):
    # Ensure both players exist
    conn = get_db()
    for name, atype in [(req.player_a, req.agent_type_a), (req.player_b, req.agent_type_b)]:
        conn.execute(
            "INSERT OR IGNORE INTO players (name, agent_type) VALUES (?, ?)",
            (name, atype)
        )
    conn.commit()

    # Fetch ELO
    ra = conn.execute("SELECT elo FROM players WHERE name=?", (req.player_a,)).fetchone()["elo"]
    rb = conn.execute("SELECT elo FROM players WHERE name=?", (req.player_b,)).fetchone()["elo"]
    conn.close()

    # Build agents and fight
    agent_a = build_agent(req.player_a, req.agent_type_a or "greedy", req.webhook_url_a,
                          api_key=getattr(req, "api_key_a", None),
                          model=getattr(req, "model_a", None))
    agent_b = build_agent(req.player_b, req.agent_type_b or "greedy", req.webhook_url_b,
                          api_key=getattr(req, "api_key_b", None),
                          model=getattr(req, "model_b", None))

    arena = Arena(seed=req.seed)
    result = arena.fight(agent_a, agent_b, verbose=False)

    # Save replay frames (full detail)
    replay_full = []
    for frame in arena.replay:
        frame_grid = []
        for y in range(14):
            for x in range(14):
                cell = arena.grid[y][x]
                frame_grid.append({
                    "x": x, "y": y,
                    "type": cell.type,
                    "value": cell.value,
                    "item": cell.item,
                })
        replay_full.append({**frame, "grid": frame_grid})

    # Update ELO
    winner = result["winner"]
    new_ra, new_rb = update_elo(ra, rb, winner)

    # Persist to DB
    game_id = str(uuid.uuid4())[:8]
    conn = get_db()

    conn.execute("""
        INSERT INTO games
        (id, player_a, player_b, winner, winner_name, score_a, score_b, hp_a, hp_b, turns, seed, log, replay)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        game_id,
        req.player_a, req.player_b,
        winner, result["winner_name"],
        result["player_a"]["score"], result["player_b"]["score"],
        result["player_a"]["hp"], result["player_b"]["hp"],
        result["turns"], result["seed"],
        json.dumps(result["log"]),
        json.dumps(replay_full),
    ))

    # Update player stats
    if winner == "a":
        conn.execute("UPDATE players SET wins=wins+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_ra, result["player_a"]["score"], req.player_a))
        conn.execute("UPDATE players SET losses=losses+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_rb, result["player_b"]["score"], req.player_b))
    elif winner == "b":
        conn.execute("UPDATE players SET losses=losses+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_ra, result["player_a"]["score"], req.player_a))
        conn.execute("UPDATE players SET wins=wins+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_rb, result["player_b"]["score"], req.player_b))
    else:
        conn.execute("UPDATE players SET draws=draws+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_ra, result["player_a"]["score"], req.player_a))
        conn.execute("UPDATE players SET draws=draws+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_rb, result["player_b"]["score"], req.player_b))

    # ELO history
    for pname, before, after in [(req.player_a, ra, new_ra), (req.player_b, rb, new_rb)]:
        conn.execute("""
            INSERT INTO elo_history (game_id, player_name, elo_before, elo_after, delta)
            VALUES (?,?,?,?,?)
        """, (game_id, pname, before, after, round(after - before, 2)))

    conn.commit()
    conn.close()

    return {
        "game_id": game_id,
        "result": result,
        "elo": {
            req.player_a: {"before": ra, "after": new_ra, "delta": round(new_ra - ra, 2)},
            req.player_b: {"before": rb, "after": new_rb, "delta": round(new_rb - rb, 2)},
        },
    }


# ============================================================
#  ROUTES — LEADERBOARD
# ============================================================

@app.get("/api/leaderboard", summary="ELO leaderboard")
def leaderboard(limit: int = 20):
    conn = get_db()
    rows = conn.execute("""
        SELECT name, elo, wins, losses, draws, total_gold, agent_type, legacy_msg
        FROM players
        ORDER BY elo DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
#  ROUTES — GAMES / REPLAY
# ============================================================

@app.get("/api/games", summary="Recent games")
def recent_games(limit: int = 20, player: Optional[str] = None):
    conn = get_db()
    if player:
        rows = conn.execute("""
            SELECT id, player_a, player_b, winner_name, score_a, score_b, turns, played_at
            FROM games WHERE player_a=? OR player_b=?
            ORDER BY played_at DESC LIMIT ?
        """, (player, player, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, player_a, player_b, winner_name, score_a, score_b, turns, played_at
            FROM games ORDER BY played_at DESC LIMIT ?
        """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/replay/{game_id}", summary="Get replay frames")
def get_replay(game_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"Game '{game_id}' not found")
    game = dict(row)
    game["log"] = json.loads(game["log"] or "[]")
    game["replay"] = json.loads(game["replay"] or "[]")
    return game


# ============================================================
#  ROUTES — ASYNC FIGHT + WEBSOCKET
# ============================================================

def _persist_fight(game_id, req_a, req_b, atype_a, atype_b, arena, result):
    """Save fight result to DB and update ELO. Returns elo dict."""
    conn = get_db()
    for name, atype in [(req_a, atype_a), (req_b, atype_b)]:
        conn.execute(
            "INSERT OR IGNORE INTO players (name, agent_type) VALUES (?, ?)",
            (name, atype)
        )
    conn.commit()

    ra = conn.execute("SELECT elo FROM players WHERE name=?", (req_a,)).fetchone()["elo"]
    rb = conn.execute("SELECT elo FROM players WHERE name=?", (req_b,)).fetchone()["elo"]
    winner = result["winner"]
    new_ra, new_rb = update_elo(ra, rb, winner)

    replay_full = []
    for frame in arena.replay:
        frame_grid = []
        for y in range(14):
            for x in range(14):
                cell = arena.grid[y][x]
                frame_grid.append({"x": x, "y": y, "type": cell.type,
                                    "value": cell.value, "item": cell.item})
        replay_full.append({**frame, "grid": frame_grid})

    conn.execute("""
        INSERT INTO games
        (id, player_a, player_b, winner, winner_name, score_a, score_b, hp_a, hp_b, turns, seed, log, replay)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (game_id, req_a, req_b, winner, result["winner_name"],
          result["player_a"]["score"], result["player_b"]["score"],
          result["player_a"]["hp"], result["player_b"]["hp"],
          result["turns"], result["seed"],
          json.dumps(result["log"]), json.dumps(replay_full)))

    if winner == "a":
        conn.execute("UPDATE players SET wins=wins+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_ra, result["player_a"]["score"], req_a))
        conn.execute("UPDATE players SET losses=losses+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_rb, result["player_b"]["score"], req_b))
    elif winner == "b":
        conn.execute("UPDATE players SET losses=losses+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_ra, result["player_a"]["score"], req_a))
        conn.execute("UPDATE players SET wins=wins+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_rb, result["player_b"]["score"], req_b))
    else:
        conn.execute("UPDATE players SET draws=draws+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_ra, result["player_a"]["score"], req_a))
        conn.execute("UPDATE players SET draws=draws+1, elo=?, total_gold=total_gold+? WHERE name=?",
                     (new_rb, result["player_b"]["score"], req_b))

    for pname, before, after in [(req_a, ra, new_ra), (req_b, rb, new_rb)]:
        conn.execute("""
            INSERT INTO elo_history (game_id, player_name, elo_before, elo_after, delta)
            VALUES (?,?,?,?,?)
        """, (game_id, pname, before, after, round(after - before, 2)))

    conn.commit()
    conn.close()
    return {
        req_a: {"before": ra, "after": new_ra, "delta": round(new_ra - ra, 2)},
        req_b: {"before": rb, "after": new_rb, "delta": round(new_rb - rb, 2)},
    }


@app.post("/api/fight/async", summary="Start async match (stream via WebSocket)")
async def start_async_fight(req: AsyncFightRequest):
    """
    Kicks off an async match in the background.
    Connect to /ws/live/{game_id} to receive turn-by-turn frames.
    """
    game_id = str(uuid.uuid4())[:8]

    async def run_match():
        agent_a = build_agent(req.player_a, req.agent_type_a or "greedy", req.webhook_url_a,
                              api_key=getattr(req, "api_key_a", None),
                              model=getattr(req, "model_a", None))
        agent_b = build_agent(req.player_b, req.agent_type_b or "greedy", req.webhook_url_b,
                              api_key=getattr(req, "api_key_b", None),
                              model=getattr(req, "model_b", None))
        arena = Arena(seed=req.seed)
        arena.grid, arena.portals = __import__("void_colosseum_sdk").generate_grid(arena.seed)

        from void_colosseum_sdk import Player, W, H, MAX_TURNS, DIRECTIONS, \
            CELL_LAVA, CELL_GRASS, CELL_GOLD, CELL_TRAP, CELL_PORTAL, \
            CELL_POWERUP, Cell, SeededRandom, GOLD_VALUES
        import random as _random

        arena.players = {
            "a": Player(agent_a.name, x=2, y=2),
            "b": Player(agent_b.name, x=W - 3, y=H - 3),
        }
        arena.turn = 0
        arena.log = []
        arena.replay = []
        agents = {"a": agent_a, "b": agent_b}

        # broadcast start
        await ws_manager.broadcast(game_id, {
            "type": "start",
            "game_id": game_id,
            "player_a": req.player_a,
            "player_b": req.player_b,
            "seed": arena.seed,
        })

        for turn in range(1, arena.max_turns + 1):
            arena.turn = turn
            state_a = arena._make_state("a")
            state_b = arena._make_state("b")

            try:
                dec_a = await asyncio.get_event_loop().run_in_executor(
                    None, agents["a"].decide, state_a)
            except Exception:
                dec_a = {"move": "stay"}

            try:
                dec_b = await asyncio.get_event_loop().run_in_executor(
                    None, agents["b"].decide, state_b)
            except Exception:
                dec_b = {"move": "stay"}

            arena._apply_move("a", dec_a)
            arena._apply_move("b", dec_b)

            pa, pb = arena.players["a"], arena.players["b"]
            for who in ["a", "b"]:
                p = arena.players[who]
                if not p.alive:
                    continue
                cell = arena.grid[p.y][p.x]
                if cell.type == CELL_LAVA:
                    p.hp -= 8
                    if p.hp <= 0:
                        p.alive = False; p.hp = 0
                p.in_grass = cell.type == CELL_GRASS

            if pa.alive and pb.alive and pa.x == pb.x and pa.y == pb.y:
                dmg = _random.randint(10, 20)
                pa.hp -= dmg; pb.hp -= dmg
                if pa.hp <= 0: pa.alive = False; pa.hp = 0
                if pb.hp <= 0: pb.alive = False; pb.hp = 0

            if turn % 7 == 0:
                rng = SeededRandom(arena.seed + turn)
                for _ in range(200):
                    x = rng.randint(1, W - 2); y = rng.randint(1, H - 2)
                    if arena.grid[y][x].type == "floor":
                        arena.grid[y][x] = Cell("gold", value=rng.choice(GOLD_VALUES))
                        break

            snap = arena._snapshot()
            arena.replay.append(snap)

            # broadcast frame
            frame_data = {
                "type": "frame",
                "turn": turn,
                "a": {"x": pa.x, "y": pa.y, "hp": pa.hp, "score": pa.score, "alive": pa.alive},
                "b": {"x": pb.x, "y": pb.y, "hp": pb.hp, "score": pb.score, "alive": pb.alive},
                "log": arena.log[-3:] if arena.log else [],
            }
            await ws_manager.broadcast(game_id, frame_data)
            await asyncio.sleep(req.frame_delay or 0.1)

            if not pa.alive or not pb.alive:
                break

        # determine winner
        if not pa.alive and not pb.alive:
            winner = "draw"
        elif not pa.alive:
            winner = "b"
        elif not pb.alive:
            winner = "a"
        elif pa.score > pb.score:
            winner = "a"
        elif pb.score > pa.score:
            winner = "b"
        else:
            winner = "draw"

        result = {
            "winner": winner,
            "winner_name": "DRAW" if winner == "draw" else arena.players[winner].name,
            "seed": arena.seed,
            "turns": arena.turn,
            "player_a": {"name": pa.name, "score": pa.score, "hp": pa.hp, "alive": pa.alive},
            "player_b": {"name": pb.name, "score": pb.score, "hp": pb.hp, "alive": pb.alive},
            "log": arena.log,
            "replay_frames": len(arena.replay),
        }

        elo = _persist_fight(game_id, req.player_a, req.player_b,
                             req.agent_type_a or "greedy",
                             req.agent_type_b or "greedy",
                             arena, result)

        await ws_manager.broadcast(game_id, {
            "type": "end",
            "game_id": game_id,
            "result": result,
            "elo": elo,
        })

    asyncio.create_task(run_match())
    return {"game_id": game_id, "ws_url": f"/ws/live/{game_id}"}


@app.websocket("/ws/live/{game_id}")
async def websocket_live(websocket: WebSocket, game_id: str):
    """Connect before or during a match to receive real-time frames."""
    await ws_manager.connect(game_id, websocket)
    try:
        while True:
            # keep alive; client can send "ping"
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(game_id, websocket)


# ============================================================
#  ROUTES — CHALLENGE CODES
# ============================================================

@app.post("/api/challenge", summary="Generate a challenge code")
def create_challenge(req: ChallengeRequest):
    """
    Encode a challenge into a base64 token.
    Share the link: /challenge/{code}
    """
    payload = {
        "challenger": req.challenger,
        "agent_type": req.agent_type,
        "webhook_url": req.webhook_url,
        "message": req.message,
        "created_at": int(time.time()),
    }
    code = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return {
        "code": code,
        "link": f"/challenge/{code}",
        "payload": payload,
    }


@app.get("/api/challenge/{code}", summary="Decode a challenge code")
def decode_challenge(code: str):
    try:
        payload = json.loads(base64.urlsafe_b64decode(code.encode()).decode())
        return {"ok": True, "payload": payload}
    except Exception:
        raise HTTPException(400, "Invalid challenge code")


@app.get("/challenge/{code}", include_in_schema=False)
def challenge_page(code: str):
    """Redirect challenge links to the frontend with code in URL fragment."""
    html_path = Path(__file__).parent / "void_colosseum_v02.html"
    if html_path.exists():
        return FileResponse(html_path)
    raise HTTPException(404, "Frontend not found")


# ============================================================
#  ROUTES — TOURNAMENT
# ============================================================

def _run_tournament_sync(tid: str, req: TournamentRequest):
    """
    Execute a full round-robin tournament synchronously.
    Returns (results_dict, ranking_list).
    """
    players = req.players
    agent_types = req.agent_types or {}
    webhook_urls = req.webhook_urls or {}
    rounds = req.rounds or 3

    # Per-player accumulators  {name: {wins, losses, draws, elo}}
    stats: Dict[str, Dict] = {
        p: {"wins": 0, "losses": 0, "draws": 0, "games": []} for p in players
    }

    # Ensure all tournament players exist in DB
    conn = get_db()
    for p in players:
        atype = agent_types.get(p, "greedy")
        conn.execute(
            "INSERT OR IGNORE INTO players (name, agent_type) VALUES (?, ?)",
            (p, atype)
        )
    conn.commit()
    conn.close()

    games_played = 0
    matchups = []

    # Round-robin pairs
    for i in range(len(players)):
        for j in range(i + 1, len(players)):
            pa_name = players[i]
            pb_name = players[j]
            w_a = w_b = w_d = 0
            for _ in range(rounds):
                agent_a = build_agent(pa_name, agent_types.get(pa_name, "greedy"),
                                      webhook_urls.get(pa_name))
                agent_b = build_agent(pb_name, agent_types.get(pb_name, "greedy"),
                                      webhook_urls.get(pb_name))
                arena = Arena()
                result = arena.fight(agent_a, agent_b, verbose=False)

                game_id = str(uuid.uuid4())[:8]
                # reuse _persist_fight helper
                _persist_fight(
                    game_id, pa_name, pb_name,
                    agent_types.get(pa_name, "greedy"),
                    agent_types.get(pb_name, "greedy"),
                    arena, result
                )
                games_played += 1
                winner = result["winner"]
                if winner == "a":
                    stats[pa_name]["wins"] += 1
                    stats[pb_name]["losses"] += 1
                    w_a += 1
                elif winner == "b":
                    stats[pb_name]["wins"] += 1
                    stats[pa_name]["losses"] += 1
                    w_b += 1
                else:
                    stats[pa_name]["draws"] += 1
                    stats[pb_name]["draws"] += 1
                    w_d += 1

                stats[pa_name]["games"].append(game_id)
                stats[pb_name]["games"].append(game_id)

            matchups.append({
                "player_a": pa_name,
                "player_b": pb_name,
                "wins_a": w_a,
                "wins_b": w_b,
                "draws": w_d,
            })

    # Fetch final ELO for ranking
    conn = get_db()
    ranking = []
    for p in players:
        row = conn.execute("SELECT elo FROM players WHERE name=?", (p,)).fetchone()
        elo = row["elo"] if row else 1200.0
        ranking.append({
            "name": p,
            "elo": elo,
            "wins": stats[p]["wins"],
            "losses": stats[p]["losses"],
            "draws": stats[p]["draws"],
        })
    conn.close()

    ranking.sort(key=lambda r: r["elo"], reverse=True)

    return {
        "games_played": games_played,
        "matchups": matchups,
        "ranking": ranking,
    }


async def _run_tournament_async(tid: str, req: TournamentRequest):
    """Background task: run tournament and broadcast progress via WebSocket."""
    channel = f"tournament_{tid}"
    players = req.players
    agent_types = req.agent_types or {}
    webhook_urls = req.webhook_urls or {}
    rounds = req.rounds or 3

    conn = get_db()
    for p in players:
        atype = agent_types.get(p, "greedy")
        conn.execute(
            "INSERT OR IGNORE INTO players (name, agent_type) VALUES (?, ?)",
            (p, atype)
        )
    conn.commit()
    conn.execute(
        "UPDATE tournaments SET status='running' WHERE id=?", (tid,)
    )
    conn.commit()
    conn.close()

    await ws_manager.broadcast(channel, {"type": "tournament_start", "tournament_id": tid,
                                          "players": players})

    stats: Dict[str, Dict] = {
        p: {"wins": 0, "losses": 0, "draws": 0} for p in players
    }
    matchups = []
    games_played = 0

    for i in range(len(players)):
        for j in range(i + 1, len(players)):
            pa_name = players[i]
            pb_name = players[j]
            w_a = w_b = w_d = 0
            for rnd in range(rounds):
                agent_a = build_agent(pa_name, agent_types.get(pa_name, "greedy"),
                                      webhook_urls.get(pa_name))
                agent_b = build_agent(pb_name, agent_types.get(pb_name, "greedy"),
                                      webhook_urls.get(pb_name))
                arena = Arena()
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: arena.fight(agent_a, agent_b, verbose=False)
                )
                game_id = str(uuid.uuid4())[:8]
                _persist_fight(
                    game_id, pa_name, pb_name,
                    agent_types.get(pa_name, "greedy"),
                    agent_types.get(pb_name, "greedy"),
                    arena, result
                )
                games_played += 1
                winner = result["winner"]
                if winner == "a":
                    stats[pa_name]["wins"] += 1
                    stats[pb_name]["losses"] += 1
                    w_a += 1
                elif winner == "b":
                    stats[pb_name]["wins"] += 1
                    stats[pa_name]["losses"] += 1
                    w_b += 1
                else:
                    stats[pa_name]["draws"] += 1
                    stats[pb_name]["draws"] += 1
                    w_d += 1

                await ws_manager.broadcast(channel, {
                    "type": "game_result",
                    "tournament_id": tid,
                    "game_id": game_id,
                    "player_a": pa_name,
                    "player_b": pb_name,
                    "round": rnd + 1,
                    "winner": result["winner_name"],
                    "games_played": games_played,
                })

            matchups.append({
                "player_a": pa_name,
                "player_b": pb_name,
                "wins_a": w_a,
                "wins_b": w_b,
                "draws": w_d,
            })

    # Final ranking
    conn = get_db()
    ranking = []
    for p in players:
        row = conn.execute("SELECT elo FROM players WHERE name=?", (p,)).fetchone()
        elo = row["elo"] if row else 1200.0
        ranking.append({
            "name": p,
            "elo": elo,
            "wins": stats[p]["wins"],
            "losses": stats[p]["losses"],
            "draws": stats[p]["draws"],
        })
    ranking.sort(key=lambda r: r["elo"], reverse=True)

    final_results = {
        "games_played": games_played,
        "matchups": matchups,
        "ranking": ranking,
    }

    conn.execute(
        "UPDATE tournaments SET status='done', results_json=? WHERE id=?",
        (json.dumps(final_results), tid)
    )
    conn.commit()
    conn.close()

    await ws_manager.broadcast(channel, {
        "type": "tournament_end",
        "tournament_id": tid,
        "ranking": ranking,
        "games_played": games_played,
    })


@app.post("/api/tournament", summary="Start a tournament")
async def start_tournament(req: TournamentRequest):
    if len(req.players) < 2:
        raise HTTPException(400, "Need at least 2 players")

    tid = str(uuid.uuid4())[:8]
    conn = get_db()
    conn.execute(
        "INSERT INTO tournaments (id, status, players_json) VALUES (?, ?, ?)",
        (tid, "pending", json.dumps(req.players))
    )
    conn.commit()
    conn.close()

    if req.async_mode:
        asyncio.create_task(_run_tournament_async(tid, req))
        return {
            "tournament_id": tid,
            "status": "running",
            "async_mode": True,
            "ws_url": f"/ws/live/tournament_{tid}",
        }

    # Synchronous mode — run now and return results
    results = _run_tournament_sync(tid, req)

    conn = get_db()
    conn.execute(
        "UPDATE tournaments SET status='done', results_json=? WHERE id=?",
        (json.dumps(results), tid)
    )
    conn.commit()
    conn.close()

    return {
        "tournament_id": tid,
        "status": "done",
        **results,
    }


@app.get("/api/tournament/{tid}", summary="Get tournament status and rankings")
def get_tournament(tid: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM tournaments WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"Tournament '{tid}' not found")
    t = dict(row)
    t["players"] = json.loads(t.pop("players_json") or "[]")
    results_raw = t.pop("results_json", None)
    t["results"] = json.loads(results_raw) if results_raw else None
    return t


# ============================================================
#  SERVE FRONTEND
# ============================================================

# ============================================================
#  ROUTES — SEASON SYSTEM
# ============================================================

def _get_active_season(conn):
    return conn.execute("SELECT * FROM seasons WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()


def _snapshot_season(conn, season_id: int):
    """Write per-player season_stats from current players table."""
    players = conn.execute("SELECT * FROM players").fetchall()
    for i, p in enumerate(sorted(players, key=lambda x: x["elo"], reverse=True), 1):
        conn.execute("""
            INSERT INTO season_stats (season_id, player_name, elo_peak, elo_final,
                                      wins, losses, draws, total_gold, rank)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(season_id, player_name) DO UPDATE SET
                elo_final=excluded.elo_final, elo_peak=MAX(elo_peak, excluded.elo_final),
                wins=excluded.wins, losses=excluded.losses, draws=excluded.draws,
                total_gold=excluded.total_gold, rank=excluded.rank
        """, (season_id, p["name"], p["elo"], p["elo"],
              p["wins"], p["losses"], p["draws"], p["total_gold"], i))


@app.get("/api/seasons", summary="List all seasons")
def list_seasons():
    conn = get_db()
    rows = conn.execute("SELECT id, name, started_at, ended_at, status FROM seasons ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/seasons/current", summary="Current active season")
def current_season():
    conn = get_db()
    season = _get_active_season(conn)
    if not season:
        conn.close()
        raise HTTPException(404, "No active season")
    season = dict(season)

    # Leaderboard for this season (live ELO)
    lb = conn.execute("""
        SELECT name, elo, wins, losses, draws, total_gold, legacy_msg
        FROM players ORDER BY elo DESC LIMIT 50
    """).fetchall()
    conn.close()

    season["leaderboard"] = [dict(r) for r in lb]
    season["games_this_season"] = _count_season_games(season["id"])
    return season


def _count_season_games(season_id: int) -> int:
    conn = get_db()
    s = conn.execute("SELECT started_at FROM seasons WHERE id=?", (season_id,)).fetchone()
    if not s:
        conn.close()
        return 0
    count = conn.execute("SELECT COUNT(*) FROM games WHERE played_at >= ?", (s["started_at"],)).fetchone()[0]
    conn.close()
    return count


@app.get("/api/seasons/{season_id}", summary="Season detail and historical leaderboard")
def get_season(season_id: int):
    conn = get_db()
    season = conn.execute("SELECT * FROM seasons WHERE id=?", (season_id,)).fetchone()
    if not season:
        conn.close()
        raise HTTPException(404, f"Season {season_id} not found")
    season = dict(season)

    if season["status"] == "closed" and season["snapshot"]:
        season["leaderboard"] = json.loads(season["snapshot"])
    else:
        # Live snapshot for active season
        stats = conn.execute("""
            SELECT ss.*, p.legacy_msg FROM season_stats ss
            LEFT JOIN players p ON ss.player_name = p.name
            WHERE ss.season_id = ?
            ORDER BY ss.elo_final DESC
        """, (season_id,)).fetchall()
        season["leaderboard"] = [dict(r) for r in stats]

    conn.close()
    return season


class SeasonCloseRequest(BaseModel):
    new_season_name: Optional[str] = None   # auto-generates if omitted


@app.post("/api/seasons/close", summary="Close active season and start a new one")
def close_season(req: SeasonCloseRequest = SeasonCloseRequest()):
    conn = get_db()
    season = _get_active_season(conn)
    if not season:
        conn.close()
        raise HTTPException(404, "No active season to close")

    sid = season["id"]
    _snapshot_season(conn, sid)

    # Build snapshot JSON for archive
    stats = conn.execute("""
        SELECT player_name, elo_final, wins, losses, draws, total_gold, rank
        FROM season_stats WHERE season_id=? ORDER BY rank
    """, (sid,)).fetchall()
    snapshot = json.dumps([dict(r) for r in stats])

    # Close season
    conn.execute("""
        UPDATE seasons SET status='closed', ended_at=?, snapshot=? WHERE id=?
    """, (time.time(), snapshot, sid))

    # Reset player ELO to soft-reset (regression toward 1200)
    conn.execute("""
        UPDATE players SET
            elo = ROUND(elo * 0.5 + 1200 * 0.5, 2),
            wins = 0, losses = 0, draws = 0, total_gold = 0
    """)

    # Start new season
    num = season["id"] + 1
    new_name = req.new_season_name or f"Season {num}"
    conn.execute(
        "INSERT INTO seasons (name, started_at, status) VALUES (?, ?, 'active')",
        (new_name, time.time())
    )

    conn.commit()
    new_season = _get_active_season(conn)
    conn.close()

    return {
        "closed_season": sid,
        "closed_season_name": season["name"],
        "new_season_id": new_season["id"],
        "new_season_name": new_season["name"],
        "players_elo_reset": True,
    }


@app.get("/api/seasons/{season_id}/player/{player_name}", summary="Player stats for a season")
def season_player_stats(season_id: int, player_name: str):
    conn = get_db()
    row = conn.execute("""
        SELECT * FROM season_stats WHERE season_id=? AND player_name=?
    """, (season_id, player_name)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"No stats for {player_name} in season {season_id}")
    return dict(row)


@app.get("/api/seasons/{season_id}/games", summary="Games played in a season")
def season_games(season_id: int, limit: int = 20):
    conn = get_db()
    s = conn.execute("SELECT started_at, ended_at FROM seasons WHERE id=?", (season_id,)).fetchone()
    if not s:
        conn.close()
        raise HTTPException(404, f"Season {season_id} not found")
    q = "SELECT id, player_a, player_b, winner_name, score_a, score_b, turns, played_at FROM games WHERE played_at >= ?"
    params = [s["started_at"]]
    if s["ended_at"]:
        q += " AND played_at <= ?"
        params.append(s["ended_at"])
    q += f" ORDER BY played_at DESC LIMIT {limit}"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
#  ROUTES — MAP EDITOR
# ============================================================

class MapSaveRequest(BaseModel):
    name: str
    author: Optional[str] = "anonymous"
    grid: List[List[dict]]   # 14×14 array of {type, value?, item?, hidden?}


@app.post("/api/maps", summary="Save a custom map")
def save_map(req: MapSaveRequest):
    map_id = str(uuid.uuid4())[:8]
    conn = get_db()
    conn.execute(
        "INSERT INTO maps (id, name, author, grid_json, width, height) VALUES (?,?,?,?,?,?)",
        (map_id, req.name, req.author,
         json.dumps(req.grid),
         len(req.grid[0]) if req.grid else 14,
         len(req.grid))
    )
    conn.commit()
    conn.close()
    return {"map_id": map_id, "name": req.name, "share_url": f"/map/{map_id}"}


@app.get("/api/maps", summary="List saved maps")
def list_maps(limit: int = 20):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, author, width, height, play_count, created_at FROM maps ORDER BY created_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/maps/{map_id}", summary="Load a saved map")
def get_map(map_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM maps WHERE id=?", (map_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"Map '{map_id}' not found")
    m = dict(row)
    m["grid"] = json.loads(m["grid_json"])
    del m["grid_json"]
    return m


@app.post("/api/fight/map/{map_id}", summary="Fight on a custom map")
def fight_on_map(map_id: str, req: FightRequest):
    """Run a match using a saved custom map instead of procedural generation."""
    conn = get_db()
    row = conn.execute("SELECT * FROM maps WHERE id=?", (map_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"Map '{map_id}' not found")
    grid_data = json.loads(row["grid_json"])
    conn.execute("UPDATE maps SET play_count=play_count+1 WHERE id=?", (map_id,))
    conn.commit()
    conn.close()

    # Reconstruct Cell grid from JSON
    from void_colosseum_sdk import Cell, generate_grid, W, H
    arena = Arena(seed=req.seed or 0)
    # Build grid from map data
    reconstructed = []
    for row_cells in grid_data:
        row_list = []
        for c in row_cells:
            cell = Cell(
                type=c.get("type", "floor"),
                value=c.get("value", 0),
                hidden=c.get("hidden", False),
                item=c.get("item", ""),
                pair=tuple(c["pair"]) if c.get("pair") else None,
            )
            row_list.append(cell)
        reconstructed.append(row_list)
    arena.grid = reconstructed
    arena.portals = []

    from void_colosseum_sdk import Player
    agent_a = build_agent(req.player_a, req.agent_type_a or "greedy", req.webhook_url_a)
    agent_b = build_agent(req.player_b, req.agent_type_b or "greedy", req.webhook_url_b)
    arena.players = {
        "a": Player(agent_a.name, x=2, y=2),
        "b": Player(agent_b.name, x=W - 3, y=H - 3),
    }
    arena.turn = 0; arena.log = []; arena.replay = []

    agents = {"a": agent_a, "b": agent_b}
    result = arena.fight.__func__(arena, agent_a, agent_b, verbose=False) \
        if False else None

    # Run fight directly (grid already set, avoid re-generate)
    from void_colosseum_sdk import MAX_TURNS, CELL_LAVA, CELL_GRASS, SeededRandom, GOLD_VALUES, Cell as SCell
    import random as _rnd
    for turn in range(1, arena.max_turns + 1):
        arena.turn = turn
        sa = arena._make_state("a"); sb = arena._make_state("b")
        try: da = agent_a.decide(sa)
        except: da = {"move": "stay"}
        try: db = agent_b.decide(sb)
        except: db = {"move": "stay"}
        arena._apply_move("a", da); arena._apply_move("b", db)
        pa, pb = arena.players["a"], arena.players["b"]
        for who in ["a","b"]:
            p = arena.players[who]
            if not p.alive: continue
            cell = arena.grid[p.y][p.x]
            if cell.type == CELL_LAVA:
                p.hp -= 8
                if p.hp <= 0: p.alive = False; p.hp = 0
            p.in_grass = cell.type == CELL_GRASS
        if pa.alive and pb.alive and pa.x==pb.x and pa.y==pb.y:
            dmg = _rnd.randint(10,20); pa.hp -= dmg; pb.hp -= dmg
            if pa.hp<=0: pa.alive=False; pa.hp=0
            if pb.hp<=0: pb.alive=False; pb.hp=0
        if turn % 7 == 0:
            rng = SeededRandom(arena.seed + turn)
            for _ in range(200):
                x=rng.randint(1,W-2); y=rng.randint(1,H-2)
                if arena.grid[y][x].type=="floor":
                    arena.grid[y][x]=SCell("gold",value=rng.choice(GOLD_VALUES)); break
        arena.replay.append(arena._snapshot())
        if not pa.alive or not pb.alive: break

    if not pa.alive and not pb.alive: winner="draw"
    elif not pa.alive: winner="b"
    elif not pb.alive: winner="a"
    elif pa.score>pb.score: winner="a"
    elif pb.score>pa.score: winner="b"
    else: winner="draw"

    result = {
        "winner": winner,
        "winner_name": "DRAW" if winner=="draw" else arena.players[winner].name,
        "map_id": map_id, "seed": arena.seed, "turns": arena.turn,
        "player_a": {"name": pa.name, "score": pa.score, "hp": pa.hp, "alive": pa.alive},
        "player_b": {"name": pb.name, "score": pb.score, "hp": pb.hp, "alive": pb.alive},
        "log": arena.log, "replay_frames": len(arena.replay),
    }

    game_id = str(uuid.uuid4())[:8]
    _persist_fight(game_id, req.player_a, req.player_b,
                   req.agent_type_a or "greedy", req.agent_type_b or "greedy",
                   arena, result)
    result["game_id"] = game_id
    return result


@app.get("/map/{map_id}", include_in_schema=False)
def map_page(map_id: str):
    html_path = Path(__file__).parent / "void_colosseum_v02.html"
    if html_path.exists():
        return FileResponse(html_path)
    raise HTTPException(404, "Frontend not found")


@app.get("/", include_in_schema=False)
def serve_frontend():
    html_path = Path(__file__).parent / "void_colosseum_v02.html"
    if html_path.exists():
        return FileResponse(html_path)
    return {"message": "Void Colosseum API v0.3 — open /docs for API reference"}


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.3.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
