"""
fix_stats.py — Recalcule tous les stats joueurs depuis l'historique des matchs.

Usage : python3 fix_stats.py
       python3 fix_stats.py --dry-run   (simulation sans modifier la DB)

Le script rejoue chaque match dans l'ordre chronologique et recalcule :
  - wins / losses / streak / best_streak (players + player_queue_elo)
  - ELO (players + player_queue_elo)
  - points (wins × 15 + losses × 5, MVP non recalculé)
  - placement_done

Les points MVP sont conservés tels quels (non recalculés).
"""

import sqlite3
import json
import sys
from collections import defaultdict

DB_PATH = "inhouse.db"
DRY_RUN = "--dry-run" in sys.argv

# ── Constantes (doivent correspondre à bot.py) ──────────────────────────────
K_FACTOR           = 32
K_FACTOR_PLACEMENT = 64
PLACEMENT_MATCHES  = 5
POINTS_WIN         = 15
POINTS_LOSS        = 5

def calc_elo_change(winner_elo, loser_elo, k=K_FACTOR):
    expected = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    gain = round(k * (1 - expected))
    loss = round(k * expected)
    return gain, loss

def is_in_placement(wins, losses):
    return (wins + losses) < PLACEMENT_MATCHES

def get_k_factor(wins, losses):
    return K_FACTOR_PLACEMENT if is_in_placement(wins, losses) else K_FACTOR

# ── Helpers score multiplier (copié depuis bot.py) ──────────────────────────
def score_mult(score_winner, score_loser):
    total = score_winner + score_loser
    if total <= 0:
        return 1.0
    dominance = score_winner / total
    mult = round(0.5 + dominance, 2)
    return max(0.75, min(1.35, mult))

# ── Connexion DB ─────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

# ── Récupérer tous les matchs finished dans l'ordre chronologique ────────────
matches = conn.execute("""
    SELECT * FROM matches
    WHERE status='finished'
    ORDER BY ended_at ASC
""").fetchall()

print(f"{'[DRY-RUN] ' if DRY_RUN else ''}Matchs trouvés : {len(matches)}")
if not matches:
    print("Aucun match terminé en DB. Rien à faire.")
    sys.exit(0)

# ── Récupérer tous les joueurs ───────────────────────────────────────────────
players_db = conn.execute("SELECT * FROM players").fetchall()
all_uids = {p["discord_id"] for p in players_db}

# ── État initial : tout remettre à zéro ─────────────────────────────────────
# Structure : { uid: { wins, losses, elo, streak, best_streak, points, mvp_count } }
state = {}
for p in players_db:
    state[p["discord_id"]] = {
        "wins": 0, "losses": 0, "elo": 1000,
        "streak": 0, "best_streak": 0,
        "points": 0,
        "mvp_count": p["mvp_count"] or 0,  # conservé
    }

# Structure queue : { uid: { queue_id: { wins, losses, elo, streak, best_streak, placement_done } } }
queue_state = defaultdict(lambda: defaultdict(lambda: {
    "wins": 0, "losses": 0, "elo": 1000,
    "streak": 0, "best_streak": 0, "placement_done": 0
}))

# ── Rejouer chaque match ─────────────────────────────────────────────────────
skipped = 0
processed = 0

for m in matches:
    match_id = m["match_id"]
    winner   = m["winner"]
    queue_id = m["queue_id"] if m["queue_id"] else "ascendant"

    try:
        team1_ids = json.loads(m["team1"])
        team2_ids = json.loads(m["team2"])
    except Exception:
        print(f"  ⚠️  Match {match_id} — impossible de parser les équipes, ignoré")
        skipped += 1
        continue

    # team1_ids peut être une liste de dicts {"id": ...} ou de strings
    def extract_ids(team_raw):
        ids = []
        for p in team_raw:
            if isinstance(p, dict):
                ids.append(str(p.get("id", p.get("discord_id", ""))))
            else:
                ids.append(str(p))
        return [i for i in ids if i]

    t1 = extract_ids(team1_ids)
    t2 = extract_ids(team2_ids)

    if not t1 or not t2:
        print(f"  ⚠️  Match {match_id} — équipes vides, ignoré")
        skipped += 1
        continue

    winners_ids = t1 if winner == 1 else t2
    losers_ids  = t2 if winner == 1 else t1

    # S'assurer que tous les joueurs ont un état
    for uid in t1 + t2:
        if uid not in state:
            state[uid] = {
                "wins": 0, "losses": 0, "elo": 1000,
                "streak": 0, "best_streak": 0,
                "points": 0, "mvp_count": 0,
            }

    # Score multiplier
    sw = m["score_winner"] or 13
    sl = m["score_loser"]  or 0
    mult = score_mult(sw, sl)

    # ELO moyen par équipe (queue)
    avg_w = sum(queue_state[uid][queue_id]["elo"] for uid in winners_ids) / len(winners_ids)
    avg_l = sum(queue_state[uid][queue_id]["elo"] for uid in losers_ids)  / len(losers_ids)

    # Calculer les changements
    for uid in winners_ids:
        s  = state[uid]
        qs = queue_state[uid][queue_id]
        k  = get_k_factor(qs["wins"], qs["losses"])
        base_gain, _ = calc_elo_change(int(avg_w), int(avg_l), k)
        gain = round(base_gain * mult)

        s["elo"]         += gain
        s["wins"]        += 1
        s["points"]      += POINTS_WIN
        s["streak"]      += 1
        s["best_streak"]  = max(s["best_streak"], s["streak"])

        qs["elo"]         = s["elo"]
        qs["wins"]       += 1
        qs["streak"]     += 1
        qs["best_streak"] = max(qs["best_streak"], qs["streak"])
        total_after = qs["wins"] + qs["losses"]
        qs["placement_done"] = 1 if total_after >= PLACEMENT_MATCHES else 0

    for uid in losers_ids:
        s  = state[uid]
        qs = queue_state[uid][queue_id]
        k  = get_k_factor(qs["wins"], qs["losses"])
        _, base_loss = calc_elo_change(int(avg_w), int(avg_l), k)
        loss = round(base_loss * mult)

        s["elo"]     = max(100, s["elo"] - loss)
        s["losses"] += 1
        s["points"] += POINTS_LOSS
        s["streak"]  = 0

        qs["elo"]    = s["elo"]
        qs["losses"] += 1
        qs["streak"]  = 0
        total_after = qs["wins"] + qs["losses"]
        qs["placement_done"] = 1 if total_after >= PLACEMENT_MATCHES else 0

    processed += 1

print(f"\nMatchs rejoués : {processed} | Ignorés : {skipped}")

# ── Afficher le diff avant/après ─────────────────────────────────────────────
print("\n── Comparaison avant/après ──────────────────────────────────────────────")
print(f"{'Joueur':<20} {'W avant':>8} {'W après':>8} {'L avant':>8} {'L après':>8} {'ELO avant':>10} {'ELO après':>10}")
print("-" * 80)

for p in players_db:
    uid = p["discord_id"]
    if uid not in state:
        continue
    s = state[uid]
    diff_w = s["wins"]   - (p["wins"]   or 0)
    diff_l = s["losses"] - (p["losses"] or 0)
    diff_e = s["elo"]    - (p["elo"]    or 1000)
    if diff_w != 0 or diff_l != 0:
        marker = " ← DIFF"
    else:
        marker = ""
    print(f"{p['username']:<20} {p['wins'] or 0:>8} {s['wins']:>8} {p['losses'] or 0:>8} {s['losses']:>8} {p['elo'] or 1000:>10} {s['elo']:>10}{marker}")

# ── Appliquer les corrections ────────────────────────────────────────────────
if DRY_RUN:
    print("\n[DRY-RUN] Aucune modification appliquée.")
else:
    print("\nApplication des corrections...")
    for uid, s in state.items():
        conn.execute("""
            UPDATE players SET
                wins=?, losses=?, elo=?, streak=?, best_streak=?, points=?
            WHERE discord_id=?
        """, (s["wins"], s["losses"], s["elo"], s["streak"], s["best_streak"], s["points"], uid))

    for uid, queues in queue_state.items():
        for qid, qs in queues.items():
            conn.execute("""
                UPDATE player_queue_elo SET
                    wins=?, losses=?, elo=?, streak=?, best_streak=?, placement_done=?
                WHERE discord_id=? AND queue_id=?
            """, (qs["wins"], qs["losses"], qs["elo"], qs["streak"], qs["best_streak"], qs["placement_done"], uid, qid))

    conn.commit()
    print(f"✅ Stats recalculées pour {len(state)} joueurs.")

conn.close()
