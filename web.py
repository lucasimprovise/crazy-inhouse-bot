"""
Crazy Inhouse — Serveur web public
Tourne en parallèle du bot Discord via un thread
"""
import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, jsonify, abort
from threading import Thread

app = Flask(__name__)

DB_PATH = os.getenv("DB_PATH", "inhouse.db")

QUEUES = {
    "radiant": {"name": "Radiant / Immo3", "emoji": "👑"},
    "ascendant": {"name": "Ascendant / Plat", "emoji": "💎"},
    "gc": {"name": "Game Changers", "emoji": "🌸"},
}

RANKS = [
    (2200, "Radiant",    "#ff4655", "🔴"),
    (2000, "Immortel 3", "#ff6b6b", "🟠"),
    (1800, "Immortel 2", "#ff8c42", "🟠"),
    (1600, "Immortel 1", "#ffa552", "🟡"),
    (1400, "Ascendant 3","#57c7a3", "🟢"),
    (1250, "Ascendant 2","#4fb8a0", "🟢"),
    (1100, "Ascendant 1","#48a99c", "🟢"),
    (1000, "Diamant 3",  "#4fc3f7", "🔵"),
    (900,  "Diamant 2",  "#29b6f6", "🔵"),
    (800,  "Diamant 1",  "#0288d1", "🔵"),
    (700,  "Platine 3",  "#26c6da", "🩵"),
    (600,  "Platine 2",  "#00bcd4", "🩵"),
    (500,  "Platine 1",  "#00acc1", "🩵"),
    (400,  "Or 3",       "#ffd700", "🟡"),
    (300,  "Or 2",       "#ffc107", "🟡"),
    (200,  "Or 1",       "#ffb300", "🟡"),
    (100,  "Argent 3",   "#9e9e9e", "⚪"),
    (0,    "Argent 1",   "#757575", "⚪"),
]

def get_rank(elo):
    for threshold, name, color, icon in RANKS:
        if elo >= threshold:
            return name, color, icon
    return "Argent 1", "#757575", "⚪"

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn

# ─── HTML Template ───────────────────────────────────────────────────────────

def html_page(title, content, active_page=""):
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — Crazy Inhouse</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {{
  --red: #ff4655;
  --red-dim: #cc2233;
  --dark: #0f0f0f;
  --darker: #080808;
  --card: #161618;
  --border: #2a2a2e;
  --text: #e8e8f0;
  --muted: #666680;
  --gold: #ffd700;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  background: var(--darker);
  color: var(--text);
  font-family: 'Rajdhani', sans-serif;
  font-size: 16px;
  min-height: 100vh;
  overflow-x: hidden;
}}

/* Background grid */
body::before {{
  content: '';
  position: fixed;
  inset: 0;
  background-image:
    linear-gradient(rgba(255,70,85,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,70,85,0.03) 1px, transparent 1px);
  background-size: 40px 40px;
  pointer-events: none;
  z-index: 0;
}}

/* NAV */
nav {{
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(8,8,8,0.92);
  backdrop-filter: blur(20px);
  border-bottom: 1px solid var(--border);
  padding: 0 2rem;
  display: flex;
  align-items: center;
  height: 64px;
  gap: 2rem;
}}

.nav-logo {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 1.6rem;
  color: var(--red);
  letter-spacing: 3px;
  text-decoration: none;
  flex-shrink: 0;
}}

.nav-logo span {{ color: var(--text); }}

nav a {{
  color: var(--muted);
  text-decoration: none;
  font-weight: 600;
  font-size: 0.9rem;
  letter-spacing: 1px;
  text-transform: uppercase;
  padding: 6px 0;
  border-bottom: 2px solid transparent;
  transition: all 0.2s;
}}

nav a:hover, nav a.active {{
  color: var(--text);
  border-bottom-color: var(--red);
}}

.nav-discord {{
  margin-left: auto;
  background: #5865f2;
  color: white !important;
  border: none !important;
  padding: 8px 16px !important;
  border-radius: 4px;
  font-weight: 700 !important;
}}

.nav-discord:hover {{ background: #4752c4; opacity: 1; }}

/* LAYOUT */
main {{
  position: relative;
  z-index: 1;
  max-width: 1200px;
  margin: 0 auto;
  padding: 2rem;
}}

/* HERO */
.hero {{
  text-align: center;
  padding: 5rem 2rem;
  position: relative;
}}

.hero-eyebrow {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.75rem;
  color: var(--red);
  letter-spacing: 4px;
  text-transform: uppercase;
  margin-bottom: 1.5rem;
}}

.hero h1 {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: clamp(4rem, 12vw, 9rem);
  line-height: 0.9;
  letter-spacing: 4px;
  margin-bottom: 1.5rem;
}}

.hero h1 span {{ color: var(--red); }}

.hero-sub {{
  font-size: 1.1rem;
  color: var(--muted);
  max-width: 500px;
  margin: 0 auto 2.5rem;
  line-height: 1.6;
  font-weight: 500;
}}

.btn {{
  display: inline-block;
  padding: 12px 28px;
  background: var(--red);
  color: white;
  text-decoration: none;
  font-weight: 700;
  font-size: 0.9rem;
  letter-spacing: 2px;
  text-transform: uppercase;
  clip-path: polygon(8px 0%, 100% 0%, calc(100% - 8px) 100%, 0% 100%);
  transition: background 0.2s;
}}

.btn:hover {{ background: var(--red-dim); }}

.btn-outline {{
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text);
  clip-path: none;
  margin-left: 1rem;
}}

.btn-outline:hover {{ border-color: var(--red); color: var(--red); background: transparent; }}

/* STATS BAR */
.stats-bar {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1px;
  background: var(--border);
  border: 1px solid var(--border);
  margin: 3rem 0;
}}

.stat-item {{
  background: var(--card);
  padding: 1.5rem;
  text-align: center;
}}

.stat-num {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 2.5rem;
  color: var(--red);
  line-height: 1;
}}

.stat-label {{
  font-size: 0.75rem;
  color: var(--muted);
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-top: 4px;
}}

/* SECTION TITLE */
.section-title {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 2rem;
  letter-spacing: 3px;
  margin-bottom: 1.5rem;
  display: flex;
  align-items: center;
  gap: 1rem;
}}

.section-title::after {{
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}}

/* LEADERBOARD TABLE */
.table-wrapper {{
  border: 1px solid var(--border);
  overflow: hidden;
  margin-bottom: 2rem;
}}

table {{
  width: 100%;
  border-collapse: collapse;
}}

thead {{
  background: var(--card);
  border-bottom: 2px solid var(--red);
}}

th {{
  padding: 12px 16px;
  font-size: 0.7rem;
  letter-spacing: 2px;
  text-transform: uppercase;
  color: var(--muted);
  text-align: left;
  font-family: 'JetBrains Mono', monospace;
}}

td {{
  padding: 14px 16px;
  border-bottom: 1px solid var(--border);
  font-weight: 500;
}}

tr:last-child td {{ border-bottom: none; }}

tr:hover td {{ background: rgba(255,70,85,0.04); }}

.rank-pos {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 1.2rem;
  color: var(--muted);
  width: 50px;
}}

.rank-pos.gold {{ color: #ffd700; }}
.rank-pos.silver {{ color: #c0c0c0; }}
.rank-pos.bronze {{ color: #cd7f32; }}

.player-name {{
  font-weight: 700;
  font-size: 1rem;
}}

.elo-badge {{
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85rem;
  font-weight: 700;
  padding: 3px 8px;
  border-radius: 2px;
}}

.rank-name {{
  font-size: 0.8rem;
  color: var(--muted);
  margin-top: 2px;
}}

.wr-bar {{
  display: flex;
  align-items: center;
  gap: 8px;
}}

.wr-track {{
  width: 80px;
  height: 4px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
}}

.wr-fill {{
  height: 100%;
  background: var(--red);
  border-radius: 2px;
}}

/* MATCH CARDS */
.matches-grid {{
  display: flex;
  flex-direction: column;
  gap: 1px;
  background: var(--border);
  border: 1px solid var(--border);
}}

.match-card {{
  background: var(--card);
  padding: 16px 20px;
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  gap: 1rem;
  align-items: center;
  transition: background 0.2s;
}}

.match-card:hover {{ background: #1a1a1c; }}

.match-team {{ font-weight: 600; font-size: 0.9rem; line-height: 1.6; }}
.match-team.right {{ text-align: right; }}

.match-center {{
  text-align: center;
  min-width: 140px;
}}

.match-score {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 1.8rem;
  letter-spacing: 4px;
  color: var(--text);
}}

.match-meta {{
  font-size: 0.7rem;
  color: var(--muted);
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-top: 2px;
  font-family: 'JetBrains Mono', monospace;
}}

.map-tag {{
  display: inline-block;
  background: rgba(255,70,85,0.1);
  border: 1px solid rgba(255,70,85,0.3);
  color: var(--red);
  font-size: 0.65rem;
  letter-spacing: 1px;
  padding: 2px 8px;
  font-family: 'JetBrains Mono', monospace;
  margin-top: 4px;
}}

/* QUEUE CARDS */
.queues-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
  gap: 1rem;
  margin-bottom: 3rem;
}}

.queue-card {{
  background: var(--card);
  border: 1px solid var(--border);
  padding: 1.5rem;
  position: relative;
  overflow: hidden;
}}

.queue-card::before {{
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  background: var(--red);
}}

.queue-name {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 1.3rem;
  letter-spacing: 2px;
  margin-bottom: 0.5rem;
}}

.queue-stats {{ color: var(--muted); font-size: 0.85rem; }}

/* PLAYER PROFILE */
.profile-header {{
  background: var(--card);
  border: 1px solid var(--border);
  padding: 2rem;
  margin-bottom: 1.5rem;
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 2rem;
  align-items: center;
}}

.profile-avatar {{
  width: 72px;
  height: 72px;
  background: var(--border);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: 'Bebas Neue', sans-serif;
  font-size: 2rem;
  color: var(--red);
}}

.profile-name {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 2rem;
  letter-spacing: 2px;
}}

.profile-riot {{
  color: var(--muted);
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.85rem;
  margin-top: 4px;
}}

.profile-elo {{
  text-align: right;
}}

.elo-big {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 3rem;
  line-height: 1;
}}

/* PAGE HEADER */
.page-header {{
  margin-bottom: 2rem;
  padding-bottom: 1.5rem;
  border-bottom: 1px solid var(--border);
}}

.page-header h1 {{
  font-family: 'Bebas Neue', sans-serif;
  font-size: 2.5rem;
  letter-spacing: 3px;
}}

.page-header p {{
  color: var(--muted);
  margin-top: 4px;
  font-weight: 500;
}}

/* TABS */
.tabs {{
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border);
  margin-bottom: 1.5rem;
}}

.tab {{
  padding: 10px 20px;
  font-weight: 700;
  font-size: 0.8rem;
  letter-spacing: 1px;
  text-transform: uppercase;
  color: var(--muted);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  text-decoration: none;
  transition: all 0.2s;
}}

.tab.active, .tab:hover {{ color: var(--text); border-bottom-color: var(--red); }}

/* FOOTER */
footer {{
  border-top: 1px solid var(--border);
  margin-top: 4rem;
  padding: 2rem;
  text-align: center;
  color: var(--muted);
  font-size: 0.8rem;
  letter-spacing: 1px;
  position: relative;
  z-index: 1;
}}

/* EMPTY STATE */
.empty {{
  text-align: center;
  padding: 4rem 2rem;
  color: var(--muted);
}}

.empty-icon {{ font-size: 3rem; margin-bottom: 1rem; }}
.empty h3 {{ font-family: 'Bebas Neue', sans-serif; font-size: 1.5rem; letter-spacing: 2px; margin-bottom: 0.5rem; color: var(--text); }}

/* RESPONSIVE */
@media (max-width: 768px) {{
  nav {{ padding: 0 1rem; gap: 1rem; }}
  main {{ padding: 1rem; }}
  .match-card {{ grid-template-columns: 1fr; text-align: center; }}
  .match-team.right {{ text-align: center; }}
  .profile-header {{ grid-template-columns: 1fr; text-align: center; }}
  .profile-elo {{ text-align: center; }}
}}

/* ANIMATIONS */
@keyframes fadeIn {{
  from {{ opacity: 0; transform: translateY(10px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}

main > * {{ animation: fadeIn 0.4s ease both; }}
main > *:nth-child(2) {{ animation-delay: 0.1s; }}
main > *:nth-child(3) {{ animation-delay: 0.2s; }}
</style>
</head>
<body>
<nav>
  <a href="/" class="nav-logo">CRAZY<span>INHOUSE</span></a>
  <a href="/" class="{'active' if active_page == 'home' else ''}">Accueil</a>
  <a href="/leaderboard" class="{'active' if active_page == 'leaderboard' else ''}">Classement</a>
  <a href="/matches" class="{'active' if active_page == 'matches' else ''}">Matchs</a>
  <a href="https://discord.gg/TON_LIEN" class="nav-discord" target="_blank">Discord</a>
</nav>
<main>
{content}
</main>
<footer>
  CRAZY INHOUSE &nbsp;·&nbsp; Bot Discord &nbsp;·&nbsp; 2026
</footer>
</body>
</html>"""

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    conn = get_db()
    total_players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    total_matches = conn.execute("SELECT COUNT(*) FROM matches WHERE status='completed'").fetchone()[0]
    top3 = conn.execute("SELECT username, elo, wins, losses FROM players ORDER BY elo DESC LIMIT 3").fetchall()
    recent = conn.execute("""
        SELECT match_id, team1, team2, map, created_at
        FROM matches WHERE status='completed'
        ORDER BY created_at DESC LIMIT 5
    """).fetchall()
    conn.close()

    # Top 3
    top3_html = ""
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(top3):
        rname, rcolor, _ = get_rank(p["elo"])
        top3_html += f"""
        <div style="background:var(--card);border:1px solid var(--border);padding:1.2rem 1.5rem;display:flex;align-items:center;gap:1rem;margin-bottom:1px;">
          <span style="font-size:1.5rem">{medals[i]}</span>
          <div style="flex:1">
            <div style="font-weight:700;font-size:1rem">{p['username']}</div>
            <div style="font-size:0.75rem;color:{rcolor}">{rname}</div>
          </div>
          <span style="font-family:'Bebas Neue',sans-serif;font-size:1.5rem;color:var(--red)">{p['elo']}</span>
        </div>"""

    # Recent matches
    recent_html = ""
    for m in recent:
        try:
            t1 = json.loads(m["team1"])
            t2 = json.loads(m["team2"])
            recent_html += f"""
            <div class="match-card">
              <div class="match-team">Team 1<br><small style="color:var(--muted);font-size:0.75rem">{len(t1)} joueurs</small></div>
              <div class="match-center">
                <div class="match-score">VS</div>
                <div class="map-tag">{m['map'] or '—'}</div>
              </div>
              <div class="match-team right">Team 2<br><small style="color:var(--muted);font-size:0.75rem">{len(t2)} joueurs</small></div>
            </div>"""
        except:
            pass

    if not recent_html:
        recent_html = '<div class="empty"><div class="empty-icon">⚔️</div><h3>Aucun match encore</h3><p>Les matchs apparaîtront ici</p></div>'

    content = f"""
    <div class="hero">
      <div class="hero-eyebrow">// Serveur inhouse Valorant //</div>
      <h1>CRAZY<br><span>INHOUSE</span></h1>
      <p class="hero-sub">Un espace sérieux pour des joueurs sérieux. Compétition interne, ELO personnalisé, matchs équilibrés.</p>
      <a href="/leaderboard" class="btn">Voir le classement</a>
      <a href="https://discord.gg/TON_LIEN" class="btn btn-outline" target="_blank">Rejoindre Discord</a>
    </div>

    <div class="stats-bar">
      <div class="stat-item"><div class="stat-num">{total_players}</div><div class="stat-label">Joueurs inscrits</div></div>
      <div class="stat-item"><div class="stat-num">{total_matches}</div><div class="stat-label">Matchs joués</div></div>
      <div class="stat-item"><div class="stat-num">3</div><div class="stat-label">Queues actives</div></div>
      <div class="stat-item"><div class="stat-num">10v10</div><div class="stat-label">Format</div></div>
    </div>

    <div class="section-title">Top 3</div>
    <div style="margin-bottom:2.5rem">{top3_html if top3_html else '<div class="empty"><div class="empty-icon">🏆</div><h3>Pas encore de joueurs</h3></div>'}</div>

    <div class="section-title">Derniers matchs</div>
    <div class="matches-grid">{recent_html}</div>
    """
    return html_page("Accueil", content, "home")


@app.route("/leaderboard")
@app.route("/leaderboard/<queue_id>")
def leaderboard(queue_id=None):
    conn = get_db()

    if queue_id and queue_id in QUEUES:
        players = conn.execute("""
            SELECT p.username, p.discord_id, p.wins, p.losses,
                   q.elo, q.wins as qwins, q.losses as qlosses, q.streak, q.best_streak, q.placement_done
            FROM player_queue_elo q
            JOIN players p ON p.discord_id = q.discord_id
            WHERE q.queue_id = ? AND q.placement_done = 1
            ORDER BY q.elo DESC
        """, (queue_id,)).fetchall()
        queue_name = QUEUES[queue_id]["name"]
    else:
        queue_id = None
        players = conn.execute("""
            SELECT username, discord_id, elo, wins, losses, streak, best_streak
            FROM players ORDER BY elo DESC
        """).fetchall()
        queue_name = "Global"
    conn.close()

    tabs_html = f'<a href="/leaderboard" class="tab {"active" if not queue_id else ""}">Global</a>'
    for qid, q in QUEUES.items():
        tabs_html += f'<a href="/leaderboard/{qid}" class="tab {"active" if queue_id == qid else ""}">{q["emoji"]} {q["name"]}</a>'

    rows_html = ""
    for i, p in enumerate(players):
        elo = p["elo"] if "elo" in p.keys() else 1000
        w = p["wins"] if "wins" in p.keys() else 0
        l = p["losses"] if "losses" in p.keys() else 0
        total = w + l
        wr = round(w / total * 100) if total > 0 else 0
        rname, rcolor, _ = get_rank(elo)

        pos_class = ["gold", "silver", "bronze"][i] if i < 3 else ""
        pos_sym = ["🥇", "🥈", "🥉"][i] if i < 3 else f"#{i+1}"

        rows_html += f"""<tr>
          <td><span class="rank-pos {pos_class}">{pos_sym}</span></td>
          <td>
            <div class="player-name">{p['username']}</div>
            <div class="rank-name" style="color:{rcolor}">{rname}</div>
          </td>
          <td><span class="elo-badge" style="background:{rcolor}22;color:{rcolor}">{elo}</span></td>
          <td style="font-family:'JetBrains Mono',monospace;font-size:0.85rem">{w}W / {l}L</td>
          <td>
            <div class="wr-bar">
              <div class="wr-track"><div class="wr-fill" style="width:{wr}%"></div></div>
              <span style="font-size:0.8rem;font-family:'JetBrains Mono',monospace">{wr}%</span>
            </div>
          </td>
        </tr>"""

    if not rows_html:
        rows_html = f'<tr><td colspan="5"><div class="empty"><div class="empty-icon">🏆</div><h3>Aucun joueur classé</h3><p>Les joueurs apparaissent après {10} matchs de placement</p></div></td></tr>'

    content = f"""
    <div class="page-header">
      <h1>CLASSEMENT</h1>
      <p>{queue_name} — {len(players)} joueur(s) classé(s)</p>
    </div>
    <div class="tabs">{tabs_html}</div>
    <div class="table-wrapper">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Joueur</th>
            <th>ELO</th>
            <th>W / L</th>
            <th>Winrate</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """
    return html_page(f"Classement {queue_name}", content, "leaderboard")


@app.route("/matches")
def matches():
    conn = get_db()
    all_matches = conn.execute("""
        SELECT match_id, team1, team2, map, winner, score_winner, score_loser, created_at, season, queue_id
        FROM matches WHERE status='completed'
        ORDER BY created_at DESC LIMIT 50
    """).fetchall()

    # Get usernames
    players_db = {r["discord_id"]: r["username"] for r in conn.execute("SELECT discord_id, username FROM players").fetchall()}
    conn.close()

    cards_html = ""
    for m in all_matches:
        try:
            t1_ids = json.loads(m["team1"])
            t2_ids = json.loads(m["team2"])
            t1_names = [players_db.get(pid, "Bot") for pid in t1_ids[:5]]
            t2_names = [players_db.get(pid, "Bot") for pid in t2_ids[:5]]
            winner = m["winner"] if m["winner"] else 0
            sw = m["score_winner"] or "?"
            sl = m["score_loser"] or "?"
            score = f"{sw} — {sl}" if winner else "VS"
            date_str = m["created_at"][:10] if m["created_at"] else ""
            queue_label = QUEUES.get(m["queue_id"] or "", {}).get("name", "") if m["queue_id"] else ""

            t1_style = "color:var(--red);font-weight:700" if winner == 1 else ""
            t2_style = "color:var(--red);font-weight:700" if winner == 2 else ""

            cards_html += f"""
            <div class="match-card">
              <div class="match-team" style="{t1_style}">{"🏆 " if winner == 1 else ""}Team 1<br>
                <small style="color:var(--muted);font-size:0.75rem;font-weight:400">{', '.join(t1_names[:3])}{"..." if len(t1_names) > 3 else ""}</small>
              </div>
              <div class="match-center">
                <div class="match-score" style="font-size:1.4rem">{score}</div>
                <div class="map-tag">{m['map'] or '—'}</div>
                <div class="match-meta" style="margin-top:6px">{date_str}{" · " + queue_label if queue_label else ""}</div>
              </div>
              <div class="match-team right" style="{t2_style}">{"🏆 " if winner == 2 else ""}Team 2<br>
                <small style="color:var(--muted);font-size:0.75rem;font-weight:400">{', '.join(t2_names[:3])}{"..." if len(t2_names) > 3 else ""}</small>
              </div>
            </div>"""
        except:
            pass

    if not cards_html:
        cards_html = '<div class="empty"><div class="empty-icon">⚔️</div><h3>Aucun match terminé</h3><p>Les matchs apparaîtront ici une fois terminés</p></div>'

    content = f"""
    <div class="page-header">
      <h1>HISTORIQUE DES MATCHS</h1>
      <p>50 derniers matchs terminés</p>
    </div>
    <div class="matches-grid">{cards_html}</div>
    """
    return html_page("Matchs", content, "matches")


@app.route("/player/<discord_id>")
def player_profile(discord_id):
    conn = get_db()
    p = conn.execute("SELECT * FROM players WHERE discord_id=?", (discord_id,)).fetchone()
    if not p:
        conn.close()
        abort(404)

    queue_elos = conn.execute(
        "SELECT * FROM player_queue_elo WHERE discord_id=?", (discord_id,)
    ).fetchall()

    recent_matches = conn.execute("""
        SELECT match_id, team1, team2, winner, map, score_winner, score_loser, created_at
        FROM matches
        WHERE (team1 LIKE ? OR team2 LIKE ?) AND status='completed'
        ORDER BY created_at DESC LIMIT 10
    """, (f'%{discord_id}%', f'%{discord_id}%')).fetchall()
    conn.close()

    rname, rcolor, _ = get_rank(p["elo"])
    total = p["wins"] + p["losses"]
    wr = round(p["wins"] / total * 100) if total > 0 else 0
    initials = p["username"][:2].upper()

    queue_rows = ""
    for q in queue_elos:
        qname = QUEUES.get(q["queue_id"], {}).get("name", q["queue_id"])
        qr, qcolor, _ = get_rank(q["elo"])
        qt = q["wins"] + q["losses"]
        qwr = round(q["wins"] / qt * 100) if qt > 0 else 0
        status = "Classé" if q["placement_done"] else f"Placement ({qt}/10)"
        queue_rows += f"""<tr>
          <td style="font-weight:600">{qname}</td>
          <td><span class="elo-badge" style="background:{qcolor}22;color:{qcolor}">{q['elo']}</span></td>
          <td style="color:{qcolor};font-size:0.85rem">{qr}</td>
          <td style="font-family:'JetBrains Mono',monospace;font-size:0.85rem">{q['wins']}W / {q['losses']}L ({qwr}%)</td>
          <td style="color:var(--muted);font-size:0.8rem">{status}</td>
        </tr>"""

    match_rows = ""
    for m in recent_matches:
        try:
            t1_ids = json.loads(m["team1"])
            in_t1 = discord_id in t1_ids
            my_team = 1 if in_t1 else 2
            won = m["winner"] == my_team
            result_color = "#57f287" if won else "#ed4245"
            result = "✅ Victoire" if won else "❌ Défaite"
            score = f"{m['score_winner']}—{m['score_loser']}" if m["score_winner"] else "—"
            match_rows += f"""<tr>
              <td style="color:{result_color};font-weight:700">{result}</td>
              <td>{m['map'] or '—'}</td>
              <td style="font-family:'JetBrains Mono',monospace;font-size:0.85rem">{score}</td>
              <td style="color:var(--muted);font-size:0.8rem">{(m['created_at'] or '')[:10]}</td>
            </tr>"""
        except:
            pass

    content = f"""
    <div class="profile-header">
      <div class="profile-avatar">{initials}</div>
      <div>
        <div class="profile-name">{p['username']}</div>
        <div class="profile-riot" style="margin-top:4px">{p['riot_id'] or 'Riot ID non renseigné'}</div>
      </div>
      <div class="profile-elo">
        <div class="elo-big" style="color:{rcolor}">{p['elo']}</div>
        <div style="color:{rcolor};font-size:0.85rem">{rname}</div>
        <div style="color:var(--muted);font-size:0.8rem;margin-top:4px">{p['wins']}V / {p['losses']}D · {wr}%</div>
      </div>
    </div>

    <div class="section-title">ELO par Queue</div>
    <div class="table-wrapper" style="margin-bottom:2rem">
      <table>
        <thead><tr><th>Queue</th><th>ELO</th><th>Rang</th><th>Stats</th><th>Statut</th></tr></thead>
        <tbody>{queue_rows if queue_rows else '<tr><td colspan="5" style="text-align:center;padding:2rem;color:var(--muted)">Aucune queue jouée</td></tr>'}</tbody>
      </table>
    </div>

    <div class="section-title">Derniers matchs</div>
    <div class="table-wrapper">
      <table>
        <thead><tr><th>Résultat</th><th>Map</th><th>Score</th><th>Date</th></tr></thead>
        <tbody>{match_rows if match_rows else '<tr><td colspan="4" style="text-align:center;padding:2rem;color:var(--muted)">Aucun match</td></tr>'}</tbody>
      </table>
    </div>
    """
    return html_page(p["username"], content, "")


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    stats = {
        "players": conn.execute("SELECT COUNT(*) FROM players").fetchone()[0],
        "matches": conn.execute("SELECT COUNT(*) FROM matches WHERE status='completed'").fetchone()[0],
        "active": conn.execute("SELECT COUNT(*) FROM matches WHERE status='active'").fetchone()[0],
    }
    conn.close()
    return jsonify(stats)


def run_web():
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

def start_web_server():
    t = Thread(target=run_web, daemon=True)
    t.start()
    print(f"✅ Serveur web démarré sur le port {os.getenv('PORT', 8080)}")
