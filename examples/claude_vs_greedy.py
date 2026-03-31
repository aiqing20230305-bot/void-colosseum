"""
Claude vs GreedyAgent — Ranked Match Example
=============================================
Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/claude_vs_greedy.py

Or pass key directly:
    python examples/claude_vs_greedy.py --key sk-ant-...
"""

import sys
import os
import argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from void_colosseum_sdk import Arena, GreedyAgent, RandomAgent, make_claude_agent, Tournament


def main():
    parser = argparse.ArgumentParser(description="Claude vs Greedy — Void Colosseum")
    parser.add_argument("--key", default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Claude model ID")
    parser.add_argument("--games", type=int, default=3, help="Number of games to play")
    parser.add_argument("--seed", type=int, default=None, help="Fixed seed for reproducibility")
    parser.add_argument("--tournament", action="store_true", help="Run full tournament")
    args = parser.parse_args()

    api_key = args.key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("⚠  No API key found. Set ANTHROPIC_API_KEY or pass --key")
        print("   Running Claude-Greedy simulation instead (no API calls)...")
        api_key = None

    print(f"🏟  VOID COLOSSEUM — Claude vs World")
    print(f"   Model : {args.model}")
    print(f"   Games : {args.games}")
    print()

    if args.tournament:
        _run_tournament(api_key, args.model)
        return

    # Head-to-head
    wins = {"Claude": 0, "Greedy": 0, "Draw": 0}
    total_gold = {"Claude": 0, "Greedy": 0}

    for i in range(1, args.games + 1):
        seed = args.seed or None
        arena = Arena(seed=seed)

        if api_key:
            claude = make_claude_agent(f"Claude-{args.model.split('-')[1]}", api_key=api_key, model=args.model)
        else:
            # Fallback: Claude simulated as GreedyAgent for demo
            claude = GreedyAgent("Claude-sim")

        greedy = GreedyAgent("GreedyBaseline")
        result = arena.fight(claude, greedy, verbose=False)

        pa = result["player_a"]
        pb = result["player_b"]
        wname = result["winner_name"]

        if result["winner"] == "a":
            wins["Claude"] += 1
        elif result["winner"] == "b":
            wins["Greedy"] += 1
        else:
            wins["Draw"] += 1

        total_gold["Claude"] += pa["score"]
        total_gold["Greedy"] += pb["score"]

        icon = "🏆" if result["winner"] == "a" else ("💀" if result["winner"] == "b" else "🤝")
        print(f"  Game {i:02d} | {icon} {wname:<18} "
              f"Claude={pa['score']:3d}g/{pa['hp']:3d}hp  "
              f"Greedy={pb['score']:3d}g/{pb['hp']:3d}hp  "
              f"T={result['turns']}  seed={result['seed']}")

    print()
    print("=" * 60)
    total = args.games
    print(f"  Claude        : {wins['Claude']}W / {wins['Greedy']}L / {wins['Draw']}D  "
          f"({wins['Claude']/total*100:.0f}% win rate)")
    print(f"  Avg gold/game : Claude={total_gold['Claude']//total}  Greedy={total_gold['Greedy']//total}")

    if wins["Claude"] > wins["Greedy"]:
        print("\n  🏆 Claude wins the series!")
    elif wins["Greedy"] > wins["Claude"]:
        print("\n  🤖 GreedyBaseline holds its ground.")
    else:
        print("\n  🤝 Dead heat.")


def _run_tournament(api_key, model):
    print("⚔  Tournament mode: Claude vs Greedy vs Random × 2")
    print()

    if api_key:
        claude = make_claude_agent(f"Claude-Sonnet", api_key=api_key, model=model)
    else:
        claude = GreedyAgent("Claude-sim")

    agents = [
        claude,
        GreedyAgent("Greedy-A"),
        GreedyAgent("Greedy-B"),
        RandomAgent("Chaos"),
    ]

    t = Tournament(agents, rounds=3)
    results = t.round_robin(verbose=False)

    medals = ["🥇", "🥈", "🥉", "  4."]
    print("📊 Final Rankings:")
    for i, (name, stats) in enumerate(results["ranking"]):
        m = medals[i] if i < len(medals) else f"  {i+1}."
        wr = stats["wins"] / max(1, stats["wins"] + stats["losses"]) * 100
        print(f"  {m} {name:<16} W{stats['wins']}L{stats['losses']}D{stats['draws']}  "
              f"({wr:.0f}% WR)  {stats['score']}g total")


if __name__ == "__main__":
    main()
