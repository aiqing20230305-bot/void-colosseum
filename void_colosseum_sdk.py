"""
VOID COLOSSEUM — Python Agent SDK v0.2
=======================================
Play the arena in 3 lines:

    from void_colosseum import Arena, RandomAgent
    arena = Arena()
    result = arena.fight(RandomAgent("MyBot"), RandomAgent("Rival"))

Or connect your LLM:

    from void_colosseum import Arena, LLMAgent
    arena = Arena()
    result = arena.fight(
        LLMAgent("MyAgent", decide_fn=my_llm_function),
        LLMAgent("Opponent", decide_fn=opponent_fn)
    )

Your decide_fn receives game_state dict, returns move dict:
    {"move": "up", "sprint": False, "use_item": None, "trash_talk": "GG"}
"""

import random
import json
import math
from dataclasses import dataclass, field
from typing import Optional, Callable, List, Dict, Any, Tuple
from enum import Enum
from copy import deepcopy

__version__ = "0.2.0"

# ============================================================
#  CONSTANTS
# ============================================================

W, H = 14, 14
MAX_TURNS = 50
VISION_RANGE = 4

CELL_FLOOR = "floor"
CELL_WALL = "wall"
CELL_GOLD = "gold"
CELL_TRAP = "trap"
CELL_PORTAL = "portal"
CELL_POWERUP = "powerup"
CELL_LAVA = "lava"
CELL_GRASS = "grass"
CELL_ICE = "ice"

ITEMS = ["shield", "reveal", "steal", "sprint", "heal"]
GOLD_VALUES = [5, 10, 10, 15, 15, 20, 25, 30, 50]

DIRECTIONS = {
    "up": (0, -1), "down": (0, 1),
    "left": (-1, 0), "right": (1, 0),
    "stay": (0, 0),
}

TRASH_TALK = [
    "Calculated. 📐", "Too easy. 🥱", "GG 🏆",
    "My neural pathways are tingling. 🧠",
    "Adapt or get deleted. 🗑️",
    "Every move you make, I've already countered. ♟️",
    "The void consumes all. Starting with your score. 🕳️",
    "*yawns in machine learning* 😴",
]


# ============================================================
#  GRID GENERATION
# ============================================================

class SeededRandom:
    """Mulberry32 PRNG for reproducible games."""
    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def next(self) -> float:
        self.state = (self.state + 0x6D2B79F5) & 0xFFFFFFFF
        t = self.state
        t = (t ^ (t >> 15)) & 0xFFFFFFFF
        t = (t * (1 | self.state)) & 0xFFFFFFFF
        t = (t + ((t ^ (t >> 7)) * (61 | t))) & 0xFFFFFFFF
        t = (t ^ (t >> 14)) & 0xFFFFFFFF
        return t / 4294967296

    def randint(self, a: int, b: int) -> int:
        return a + int(self.next() * (b - a + 1))

    def choice(self, lst):
        return lst[int(self.next() * len(lst))]

    def random(self) -> float:
        return self.next()


@dataclass
class Cell:
    type: str = CELL_FLOOR
    value: int = 0        # gold value
    hidden: bool = False  # hidden trap
    item: str = ""        # powerup item name
    pair: Tuple[int, int] = None  # portal destination


def generate_grid(seed: int) -> Tuple[List[List[Cell]], List]:
    rng = SeededRandom(seed)
    grid = [[Cell() for _ in range(W)] for _ in range(H)]

    # Walls: border + random
    for y in range(H):
        for x in range(W):
            if x == 0 or y == 0 or x == W - 1 or y == H - 1:
                grid[y][x] = Cell(CELL_WALL)
            elif rng.random() < 0.18:
                grid[y][x] = Cell(CELL_WALL)

    # Cellular automata smoothing
    for _ in range(2):
        new_grid = deepcopy(grid)
        for y in range(1, H - 1):
            for x in range(1, W - 1):
                walls = sum(
                    1 for dy in range(-1, 2) for dx in range(-1, 2)
                    if grid[y + dy][x + dx].type == CELL_WALL
                )
                if walls >= 5:
                    new_grid[y][x] = Cell(CELL_WALL)
                elif walls <= 2:
                    new_grid[y][x] = Cell(CELL_FLOOR)
        grid = new_grid

    # Clear spawn zones
    for dy in range(3):
        for dx in range(3):
            grid[1 + dy][1 + dx] = Cell(CELL_FLOOR)
            grid[H - 2 - dy][W - 2 - dx] = Cell(CELL_FLOOR)

    def find_empty():
        for _ in range(200):
            x = rng.randint(1, W - 2)
            y = rng.randint(1, H - 2)
            if grid[y][x].type == CELL_FLOOR:
                return x, y
        return None

    # Place elements
    for _ in range(18):
        pos = find_empty()
        if pos:
            grid[pos[1]][pos[0]] = Cell(CELL_GOLD, value=rng.choice(GOLD_VALUES))

    for _ in range(5):
        pos = find_empty()
        if pos:
            grid[pos[1]][pos[0]] = Cell(CELL_TRAP, hidden=rng.random() < 0.5)

    for _ in range(6):
        pos = find_empty()
        if pos:
            grid[pos[1]][pos[0]] = Cell(CELL_LAVA)

    for _ in range(8):
        pos = find_empty()
        if pos:
            grid[pos[1]][pos[0]] = Cell(CELL_GRASS)

    for _ in range(4):
        pos = find_empty()
        if pos:
            grid[pos[1]][pos[0]] = Cell(CELL_ICE)

    for _ in range(5):
        pos = find_empty()
        if pos:
            grid[pos[1]][pos[0]] = Cell(CELL_POWERUP, item=rng.choice(ITEMS))

    # Portals
    portals = []
    for _ in range(2):
        a, b = find_empty(), find_empty()
        if a and b:
            grid[a[1]][a[0]] = Cell(CELL_PORTAL, pair=b)
            grid[b[1]][b[0]] = Cell(CELL_PORTAL, pair=a)
            portals.append((a, b))

    return grid, portals


# ============================================================
#  PLAYER STATE
# ============================================================

@dataclass
class Player:
    name: str
    x: int = 0
    y: int = 0
    hp: int = 100
    max_hp: int = 100
    energy: int = 10
    max_energy: int = 10
    score: int = 0
    items: List[str] = field(default_factory=list)
    alive: bool = True
    in_grass: bool = False
    shielded: int = 0  # turns remaining


# ============================================================
#  AGENT BASE CLASSES
# ============================================================

class BaseAgent:
    """Override decide() to create your agent."""

    def __init__(self, name: str = "Agent"):
        self.name = name

    def decide(self, game_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Receive game state, return your move.

        game_state keys:
            turn, max_turns,
            you: {pos, hp, max_hp, energy, max_energy, score, items},
            opponent: {pos (or "hidden"), score},
            visible_cells: [{x, y, type, value?, item?, hidden?}],
            grid_size: {w, h},
            msg: str (opponent's trash talk)

        Return:
            {move: "up"|"down"|"left"|"right"|"stay",
             sprint: bool,
             use_item: str|None,
             trash_talk: str}
        """
        raise NotImplementedError


class RandomAgent(BaseAgent):
    """Moves randomly. Good baseline."""

    def decide(self, state):
        moves = ["up", "down", "left", "right", "stay"]
        return {
            "move": random.choice(moves),
            "sprint": random.random() < 0.1,
            "use_item": None,
            "trash_talk": random.choice(TRASH_TALK) if random.random() < 0.1 else "",
        }


class GreedyAgent(BaseAgent):
    """Always moves toward nearest visible gold."""

    def decide(self, state):
        my_pos = state["you"]["pos"]
        best_target = None
        best_score = -1

        for cell in state["visible_cells"]:
            if cell["type"] == CELL_GOLD:
                dist = abs(cell["x"] - my_pos[0]) + abs(cell["y"] - my_pos[1])
                score = cell.get("value", 10) / (dist + 1)
                if score > best_score:
                    best_score = score
                    best_target = (cell["x"], cell["y"])

        if best_target:
            dx = best_target[0] - my_pos[0]
            dy = best_target[1] - my_pos[1]
            if abs(dx) > abs(dy):
                move = "right" if dx > 0 else "left"
            else:
                move = "down" if dy > 0 else "up"
        else:
            move = random.choice(["up", "down", "left", "right"])

        return {"move": move, "sprint": False, "use_item": None, "trash_talk": ""}


class LLMAgent(BaseAgent):
    """Wraps any function (e.g., LLM API call) as an agent."""

    def __init__(self, name: str, decide_fn: Callable):
        super().__init__(name)
        self.decide_fn = decide_fn

    def decide(self, state):
        result = self.decide_fn(state)
        # Normalize response
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = {"move": "stay"}
        return {
            "move": result.get("move", "stay"),
            "sprint": result.get("sprint", False),
            "use_item": result.get("use_item"),
            "trash_talk": result.get("trash_talk", ""),
        }


# ============================================================
#  ARENA — GAME ENGINE
# ============================================================

class Arena:
    """
    The Void Colosseum game engine.

    Usage:
        arena = Arena(seed=42)
        result = arena.fight(agent_a, agent_b, verbose=True)
    """

    def __init__(self, seed: int = None, max_turns: int = MAX_TURNS):
        self.seed = seed or random.randint(0, 2**31)
        self.max_turns = max_turns
        self.grid = None
        self.portals = []
        self.players = {}
        self.turn = 0
        self.log = []
        self.replay = []

    def fight(self, agent_a: BaseAgent, agent_b: BaseAgent,
              verbose: bool = False) -> Dict[str, Any]:
        """Run a full game. Returns result dict."""

        self.grid, self.portals = generate_grid(self.seed)
        self.players = {
            "a": Player(agent_a.name, x=2, y=2),
            "b": Player(agent_b.name, x=W - 3, y=H - 3),
        }
        self.turn = 0
        self.log = []
        self.replay = []
        agents = {"a": agent_a, "b": agent_b}

        if verbose:
            print(f"\n⚔ VOID COLOSSEUM — {agent_a.name} vs {agent_b.name}")
            print(f"  Seed: {self.seed} | Turns: {self.max_turns}")
            print(f"  Grid: {W}×{H} | Vision: {VISION_RANGE}")
            print("=" * 50)

        for turn in range(1, self.max_turns + 1):
            self.turn = turn

            # Generate game states for each player
            state_a = self._make_state("a")
            state_b = self._make_state("b")

            # Get decisions
            try:
                dec_a = agents["a"].decide(state_a)
            except Exception as e:
                dec_a = {"move": "stay"}
                self._log(f"⚠ {agent_a.name} error: {e}")

            try:
                dec_b = agents["b"].decide(state_b)
            except Exception as e:
                dec_b = {"move": "stay"}
                self._log(f"⚠ {agent_b.name} error: {e}")

            # Apply moves
            self._apply_move("a", dec_a)
            self._apply_move("b", dec_b)

            # Terrain effects
            for who in ["a", "b"]:
                p = self.players[who]
                if not p.alive:
                    continue
                cell = self.grid[p.y][p.x]
                if cell.type == CELL_LAVA:
                    p.hp -= 8
                    self._log(f"🔥 {p.name} burns on lava! -8 HP")
                    if p.hp <= 0:
                        p.alive = False
                        p.hp = 0
                p.in_grass = cell.type == CELL_GRASS

            # Collision combat
            pa, pb = self.players["a"], self.players["b"]
            if pa.alive and pb.alive and pa.x == pb.x and pa.y == pb.y:
                dmg = random.randint(10, 20)
                pa.hp -= dmg
                pb.hp -= dmg
                self._log(f"💥 Collision! Both take {dmg} damage!")
                if pa.hp <= 0:
                    pa.alive = False; pa.hp = 0
                if pb.hp <= 0:
                    pb.alive = False; pb.hp = 0

            # Reveal adjacent hidden traps
            for who in ["a", "b"]:
                p = self.players[who]
                if not p.alive:
                    continue
                for dy in range(-1, 2):
                    for dx in range(-1, 2):
                        nx, ny = p.x + dx, p.y + dy
                        if 0 <= nx < W and 0 <= ny < H:
                            c = self.grid[ny][nx]
                            if c.type == CELL_TRAP and c.hidden:
                                c.hidden = False
                                self._log(f"⚠ {p.name} spotted a hidden trap!")

            # Spawn gold periodically
            if turn % 7 == 0:
                rng = SeededRandom(self.seed + turn)
                for _ in range(200):
                    x = rng.randint(1, W - 2)
                    y = rng.randint(1, H - 2)
                    if self.grid[y][x].type == CELL_FLOOR:
                        self.grid[y][x] = Cell(CELL_GOLD, value=rng.choice([15, 20, 25, 30, 50]))
                        self._log("✨ New gold spawned!")
                        break

            # Save replay frame
            self.replay.append(self._snapshot())

            # Trash talk
            for who, dec in [("a", dec_a), ("b", dec_b)]:
                tt = dec.get("trash_talk", "")
                if tt:
                    self._log(f"💬 {self.players[who].name}: \"{tt}\"")

            if verbose and turn % 10 == 0:
                print(f"  T{turn}: {pa.name}={pa.score}g/{pa.hp}hp "
                      f"vs {pb.name}={pb.score}g/{pb.hp}hp")

            # Check elimination
            if not pa.alive or not pb.alive:
                break

        # Determine winner
        pa, pb = self.players["a"], self.players["b"]
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
            "winner_name": "DRAW" if winner == "draw" else self.players[winner].name,
            "seed": self.seed,
            "turns": self.turn,
            "player_a": {
                "name": pa.name, "score": pa.score,
                "hp": pa.hp, "alive": pa.alive,
            },
            "player_b": {
                "name": pb.name, "score": pb.score,
                "hp": pb.hp, "alive": pb.alive,
            },
            "log": self.log,
            "replay_frames": len(self.replay),
        }

        if verbose:
            print("=" * 50)
            if winner == "draw":
                print("  🤝 DRAW!")
            else:
                w = self.players[winner]
                print(f"  🏆 {w.name} WINS! Score: {w.score} | HP: {w.hp}")
            print()

        return result

    def _make_state(self, who: str) -> Dict[str, Any]:
        """Generate the game state visible to a specific player."""
        me = self.players[who]
        opp = self.players["a" if who == "b" else "b"]

        # Visible cells (within vision range)
        visible = []
        for y in range(H):
            for x in range(W):
                dist = abs(x - me.x) + abs(y - me.y)
                if dist <= VISION_RANGE:
                    cell = self.grid[y][x]
                    info = {"x": x, "y": y, "type": cell.type}
                    if cell.type == CELL_GOLD:
                        info["value"] = cell.value
                    if cell.type == CELL_POWERUP:
                        info["item"] = cell.item
                    if cell.type == CELL_TRAP:
                        info["hidden"] = cell.hidden
                    visible.append(info)

        # Opponent visibility
        opp_dist = abs(opp.x - me.x) + abs(opp.y - me.y)
        if opp.alive and opp_dist <= VISION_RANGE and not opp.in_grass:
            opp_info = {"pos": [opp.x, opp.y], "score": opp.score}
        else:
            opp_info = {"pos": "hidden", "score": opp.score}

        return {
            "turn": self.turn,
            "max_turns": self.max_turns,
            "you": {
                "pos": [me.x, me.y],
                "hp": me.hp, "max_hp": me.max_hp,
                "energy": me.energy, "max_energy": me.max_energy,
                "score": me.score,
                "items": list(me.items),
            },
            "opponent": opp_info,
            "visible_cells": visible,
            "grid_size": {"w": W, "h": H},
            "msg": "",
        }

    def _apply_move(self, who: str, decision: Dict):
        p = self.players[who]
        if not p.alive:
            return

        # Use item
        item = decision.get("use_item")
        if item and item in p.items and p.energy >= 2:
            p.energy -= 2
            p.items.remove(item)
            opp = self.players["a" if who == "b" else "b"]

            if item == "reveal":
                for y in range(H):
                    for x in range(W):
                        if abs(x - p.x) + abs(y - p.y) <= VISION_RANGE:
                            c = self.grid[y][x]
                            if c.type == CELL_TRAP and c.hidden:
                                c.hidden = False
                self._log(f"👁 {p.name} used REVEAL!")
            elif item == "heal":
                p.hp = min(p.max_hp, p.hp + 30)
                self._log(f"💚 {p.name} healed +30 HP!")
            elif item == "shield":
                p.shielded = 3
                self._log(f"🛡 {p.name} activated SHIELD!")
            elif item == "steal":
                stolen = min(opp.score, 15)
                opp.score -= stolen
                p.score += stolen
                self._log(f"💸 {p.name} STOLE {stolen} gold!")
            elif item == "sprint":
                p.energy += 2
                self._log(f"💨 {p.name} used SPRINT BOOST!")

        if p.shielded > 0:
            p.shielded -= 1

        move = decision.get("move", "stay")
        dx, dy = DIRECTIONS.get(move, (0, 0))

        if move == "stay":
            p.energy = min(p.max_energy, p.energy + 2)
            return

        sprint = decision.get("sprint", False)
        cost = 3 if sprint else 1
        if p.energy < cost:
            p.energy = min(p.max_energy, p.energy + 2)
            self._log(f"😴 {p.name} rests (no energy)")
            return

        p.energy -= cost
        steps = 2 if sprint else 1

        for _ in range(steps):
            nx, ny = p.x + dx, p.y + dy
            if not (0 <= nx < W and 0 <= ny < H) or self.grid[ny][nx].type == CELL_WALL:
                break

            p.x, p.y = nx, ny
            cell = self.grid[ny][nx]

            # Ice slide
            if cell.type == CELL_ICE:
                sx, sy = nx + dx, ny + dy
                while 0 <= sx < W and 0 <= sy < H and self.grid[sy][sx].type != CELL_WALL:
                    p.x, p.y = sx, sy
                    if self.grid[sy][sx].type != CELL_ICE:
                        break
                    sx, sy = sx + dx, sy + dy
                self._log(f"❄ {p.name} slides on ice!")

            # Gold
            if cell.type == CELL_GOLD:
                p.score += cell.value
                self._log(f"💰 {p.name} +{cell.value} gold!")
                self.grid[ny][nx] = Cell(CELL_FLOOR)

            # Trap
            if cell.type == CELL_TRAP:
                if p.shielded:
                    self._log(f"🛡 {p.name}'s shield blocks trap!")
                else:
                    p.score = max(0, p.score - 15)
                    p.hp -= 10
                    self._log(f"💀 {p.name} hit trap! -15 gold, -10 HP")
                    if p.hp <= 0:
                        p.alive = False; p.hp = 0
                self.grid[ny][nx] = Cell(CELL_FLOOR)

            # Portal
            if cell.type == CELL_PORTAL and cell.pair:
                p.x, p.y = cell.pair
                self._log(f"🌀 {p.name} warped through portal!")

            # Powerup
            if cell.type == CELL_POWERUP and len(p.items) < 4:
                p.items.append(cell.item)
                self._log(f"⚡ {p.name} grabbed {cell.item.upper()}!")
                self.grid[ny][nx] = Cell(CELL_FLOOR)

    def _log(self, msg: str):
        self.log.append(f"[T{self.turn}] {msg}")

    def _snapshot(self) -> Dict:
        return {
            "turn": self.turn,
            "a": {"x": self.players["a"].x, "y": self.players["a"].y,
                  "hp": self.players["a"].hp, "score": self.players["a"].score},
            "b": {"x": self.players["b"].x, "y": self.players["b"].y,
                  "hp": self.players["b"].hp, "score": self.players["b"].score},
        }


# ============================================================
#  TOURNAMENT
# ============================================================

class Tournament:
    """Run round-robin or elimination tournaments."""

    def __init__(self, agents: List[BaseAgent], rounds: int = 3):
        self.agents = agents
        self.rounds = rounds
        self.results = {a.name: {"wins": 0, "losses": 0, "draws": 0, "score": 0}
                        for a in agents}

    def round_robin(self, verbose: bool = False) -> Dict:
        """Every agent fights every other agent."""
        for i, a in enumerate(self.agents):
            for j, b in enumerate(self.agents):
                if i >= j:
                    continue
                for r in range(self.rounds):
                    arena = Arena()
                    result = arena.fight(a, b, verbose=verbose)
                    self._record(result)

        # Sort by wins
        ranking = sorted(
            self.results.items(),
            key=lambda x: (x[1]["wins"], x[1]["score"]),
            reverse=True
        )

        if verbose:
            print("\n🏆 TOURNAMENT RESULTS")
            print("=" * 50)
            for i, (name, stats) in enumerate(ranking):
                print(f"  {i+1}. {name}: {stats['wins']}W "
                      f"{stats['losses']}L {stats['draws']}D "
                      f"(Total: {stats['score']} gold)")

        return {"ranking": ranking, "details": self.results}

    def _record(self, result: Dict):
        a_name = result["player_a"]["name"]
        b_name = result["player_b"]["name"]
        a_score = result["player_a"]["score"]
        b_score = result["player_b"]["score"]

        self.results[a_name]["score"] += a_score
        self.results[b_name]["score"] += b_score

        if result["winner"] == "a":
            self.results[a_name]["wins"] += 1
            self.results[b_name]["losses"] += 1
        elif result["winner"] == "b":
            self.results[b_name]["wins"] += 1
            self.results[a_name]["losses"] += 1
        else:
            self.results[a_name]["draws"] += 1
            self.results[b_name]["draws"] += 1


# ============================================================
#  CONVENIENCE: Claude Agent
# ============================================================

CLAUDE_SYSTEM_PROMPT = """You are playing Void Colosseum — a strategic grid-based arena game.
Each turn you receive the game state as JSON and must respond with ONLY a JSON move.

RULES:
- Move: up/down/left/right/stay. Move costs 1 energy. Stay recovers +2 energy.
- Sprint (sprint:true): move 2 cells, costs 3 energy total.
- Items cost 2 energy each:
    shield  → blocks all damage for 3 turns
    reveal  → unmask hidden traps within vision
    steal   → take up to 15 gold from opponent
    heal    → recover +30 HP (max 100)
    sprint  → instantly gain +2 energy

TERRAIN EFFECTS:
  💰 gold    → collect for score points (5–50)
  💀 trap    → -15 gold, -10 HP (some hidden!)
  🔥 lava    → -8 HP every turn you stand on it
  🌿 grass   → you become invisible to opponent
  ❄  ice     → slide until hitting a wall or non-ice cell
  🌀 portal  → teleport to paired portal location

STRATEGY TIPS:
- Prioritize high-value gold clusters early; energy is scarce.
- Sprint toward gold only if you have ≥4 energy remaining.
- Stay/rest when energy ≤2 to avoid being stranded.
- Use 'reveal' before moving into unexplored zones.
- Grass provides stealth — hide there when opponent is close.
- If opponent pos is "hidden", they may be in grass or far away.
- With ≤5 turns left, chase gold aggressively if you're losing.

CRITICAL: Respond with ONLY valid JSON, nothing else:
{"move":"up","sprint":false,"use_item":null,"trash_talk":""}

Valid moves: "up" "down" "left" "right" "stay"
use_item must be null or one of your current items."""


def make_claude_agent(name: str = "Claude-Agent", api_key: str = None,
                      model: str = "claude-sonnet-4-6"):
    """
    Create an agent powered by Claude API.
    Requires: pip install anthropic

    Usage:
        agent = make_claude_agent("MyClaude", api_key="sk-ant-...")
        arena = Arena()
        result = arena.fight(agent, GreedyAgent("Baseline"))

    Args:
        name:     Display name on the leaderboard
        api_key:  Anthropic API key (or set ANTHROPIC_API_KEY env var)
        model:    Claude model ID (default: claude-sonnet-4-6)
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError("pip install anthropic")

    import os
    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def decide(state):
        # Build a compact state summary to save tokens
        you = state["you"]
        opp = state["opponent"]
        vis = state["visible_cells"]
        gold_cells = [c for c in vis if c["type"] == "gold"]
        danger_cells = [c for c in vis if c["type"] in ("trap", "lava")]

        compact = {
            "turn": f"{state['turn']}/{state['max_turns']}",
            "pos": you["pos"],
            "hp": f"{you['hp']}/{you['max_hp']}",
            "energy": f"{you['energy']}/{you['max_energy']}",
            "score": you["score"],
            "items": you["items"],
            "opp_pos": opp["pos"],
            "opp_score": opp["score"],
            "gold_nearby": [{"pos": [c["x"],c["y"]], "val": c.get("value",10)} for c in gold_cells],
            "danger_nearby": [{"pos": [c["x"],c["y"]], "type": c["type"]} for c in danger_cells],
            "grid": f"{state['grid_size']['w']}x{state['grid_size']['h']}",
        }

        try:
            resp = client.messages.create(
                model=model,
                max_tokens=120,
                system=CLAUDE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(compact)}],
            )
            text = resp.content[0].text.strip()
            if "```" in text:
                text = text.split("```")[1].replace("json", "").strip()
            return json.loads(text)
        except Exception as e:
            return {"move": "stay", "sprint": False, "use_item": None, "trash_talk": f"err:{e}"}

    return LLMAgent(name, decide_fn=decide)


# ============================================================
#  CLI
# ============================================================

def _cli_main():
    """Entry point for `void-colosseum` CLI command."""
    import sys

    print("🏟 VOID COLOSSEUM — Agent Arena v0.2")
    print()

    if "--tournament" in sys.argv:
        agents = [
            GreedyAgent("Greedy-1"),
            GreedyAgent("Greedy-2"),
            RandomAgent("Chaos"),
            RandomAgent("Random"),
        ]
        t = Tournament(agents, rounds=5)
        t.round_robin(verbose=True)
    elif "--server" in sys.argv:
        try:
            import uvicorn
            print("Starting Void Colosseum server on http://0.0.0.0:8000 ...")
            uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
        except ImportError:
            print("Install server deps: pip install void-colosseum[server]")
    else:
        arena = Arena()
        result = arena.fight(
            GreedyAgent("Alpha"),
            RandomAgent("Beta"),
            verbose=True,
        )
        print(f"Replay: {result['replay_frames']} frames saved")
        print()
        print("Usage:")
        print("  void-colosseum               # quick fight")
        print("  void-colosseum --tournament  # round-robin tournament")
        print("  void-colosseum --server      # start API server")


if __name__ == "__main__":
    _cli_main()
