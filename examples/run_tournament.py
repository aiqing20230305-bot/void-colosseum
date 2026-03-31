"""
快速运行一场锦标赛示例：
  python examples/run_tournament.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import requests, json, time

BASE = "http://localhost:8000"

def main():
    print("🏟 VOID COLOSSEUM — Tournament Runner")

    # 注册选手
    players = [
        {"name": "GreedyMaster", "agent_type": "greedy"},
        {"name": "RandomChaos",  "agent_type": "random"},
        {"name": "GreedyBot",    "agent_type": "greedy"},
        {"name": "RandomBot",    "agent_type": "random"},
    ]
    for p in players:
        requests.post(f"{BASE}/api/players", json=p)
        print(f"  ✅ Registered: {p['name']}")

    # 启动锦标赛
    r = requests.post(f"{BASE}/api/tournament", json={
        "players": [p["name"] for p in players],
        "agent_types": {p["name"]: p["agent_type"] for p in players},
        "rounds": 3,
        "async_mode": False,
    })
    result = r.json()
    print(f"\n🏆 Tournament ID: {result['tournament_id']}")
    print(f"   Games played: {result['games_played']}")
    print(f"\n📊 Final Rankings:")
    for i, row in enumerate(result['ranking'], 1):
        medals = ['🥇','🥈','🥉']
        m = medals[i-1] if i <= 3 else f"  {i}."
        print(f"  {m} {row['name']:<14} ELO={row['elo']:.0f}  W{row['wins']}L{row['losses']}D{row['draws']}")

if __name__ == "__main__":
    main()
