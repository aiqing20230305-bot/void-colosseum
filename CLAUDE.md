# Void Colosseum — Claude Code Instructions

## Project overview

FastAPI backend (`server.py`) + Python SDK (`void_colosseum_sdk.py`) + browser frontend (`docs/index.html`).

- Run server: `uvicorn server:app --reload --port 8000`
- API docs: http://localhost:8000/docs
- Run SDK directly: `python void_colosseum_sdk.py`
- Run example: `python examples/claude_vs_greedy.py`

## Project structure

```
server.py               # FastAPI backend — 32 endpoints, SQLite, ELO, seasons
void_colosseum_sdk.py   # Python SDK + Game Engine (zero deps)
docs/index.html         # Browser frontend (i18n EN/中文)
examples/               # Example scripts
pyproject.toml          # pip package config (void-colosseum 0.3.0)
requirements.txt        # fastapi, uvicorn, pydantic, websockets
render.yaml             # Render.com deploy
railway.json            # Railway deploy
vercel.json             # Vercel deploy
```

## Key files

- `server.py:406` — `/api/fight` sync fight route
- `server.py:629` — `/api/fight/async` + WebSocket at `/ws/live/{game_id}`
- `server.py:1056` — tournament routes
- `server.py:1139` — season system routes
- `server.py:1299` — map editor routes
- `void_colosseum_sdk.py:make_claude_agent()` — Claude LLM agent factory

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
