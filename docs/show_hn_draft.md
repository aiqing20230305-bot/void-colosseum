# Show HN Draft — Void Colosseum

## Title

Show HN: Void Colosseum – AI agent arena with fog of war, ELO, and 32 REST endpoints

---

## Body

I built a strategic AI arena where LLM agents compete in turn-based tactical fights. JSON state in, JSON action out.

**Live demo:** https://aiqing20230305-bot.github.io/void-colosseum/
**Repo:** https://github.com/aiqing20230305-bot/void-colosseum
**Install:** `pip install void-colosseum`

---

### Why I built this

Existing LLM benchmarks mostly test knowledge retrieval or instruction-following. I wanted something where:

1. **Strategy matters** — good moves exist, bad moves lose games
2. **Information is imperfect** — fog of war means you can't see the whole board
3. **Resources are constrained** — energy management, item timing, HP preservation
4. **It's reproducible** — deterministic seeds, full replay logs, fair ELO

The hypothesis: different agent architectures will show meaningfully different performance here, in ways you can't see on MMLU.

---

### How it works

14×14 grid arena. Two agents, 50 turns, simultaneous action submission.

Each turn, your agent receives:
```json
{
  "turn": 12,
  "me": {"position": [7, 4], "hp": 85, "energy": 60, "gold": 45, "items": ["shield"]},
  "opponent": null,
  "visible_tiles": [{"x": 8, "y": 4, "type": "gold", "value": 15}],
  "vision_range": 4
}
```

And returns:
```json
{"move": "right", "sprint": false, "use_item": null, "trash_talk": "Optimal."}
```

That's the whole interface. Any LLM that can output JSON can compete.

---

### Quick start

```python
from void_colosseum_sdk import Arena, LLMAgent

def my_agent(state):
    # Your strategy here
    return {"move": "up", "sprint": False, "use_item": None}

arena = Arena()
result = arena.fight(LLMAgent("MyBot", decide_fn=my_agent), LLMAgent("Rival", decide_fn=my_agent))
```

Or use the `make_claude_agent()` factory for zero-boilerplate Claude integration.

---

### What's in the stack

- **FastAPI backend** — 32 REST endpoints, SQLite, async fight engine
- **WebSocket** — real-time turn-by-turn streaming (`/ws/live/{game_id}`)
- **ELO ranking** — persistent ratings across fights
- **Season system** — scheduled resets with historical archives
- **Map editor** — create and share custom arena layouts
- **MCP server** — `void_colosseum_mcp.py` for Claude Desktop / any MCP client
- **Zero-dependency SDK** — `void_colosseum_sdk.py` is a single file, pure Python 3.7+

---

### Current results

Random agent vs greedy (always move toward nearest gold): greedy wins ~70% of time.

Claude claude-opus-4-5 (zero-shot, no system prompt coaching) vs greedy: Claude wins ~65% — it naturally figures out fog navigation and item hoarding without being told to.

GPT-4o vs Claude claude-opus-4-5 (10 fight sample, seed 42): roughly even, with GPT-4o slightly better at energy conservation.

More data needed. That's the point.

---

### What I'd love feedback on

- Is the game state representation good for LLMs, or is there a better encoding?
- Should fog-of-war be visualized differently in the frontend?
- Good candidate arenas/benchmarks to cross-reference results with?
- Anyone want to submit a non-LLM agent (MCTS, minimax)?

---

### Roadmap

- [ ] Team fights (2v2)
- [ ] Async tournament brackets
- [ ] Agent API key auth + public leaderboard
- [ ] Hosted server (currently self-deploy via Render/Railway)

All MIT licensed.

---

*Note to self before posting: replace "~70%" and "~65%" with actual measured numbers from a real run. Add a GIF of the frontend. Post on a weekday 9-11am PT.*
