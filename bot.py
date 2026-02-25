import discord
from discord.ext import commands, tasks
from discord import app_commands
import sqlite3
import json
import random
import os
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", 0))

# ─────────────────────────────────────────────
#  INTENTS & BOT
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─────────────────────────────────────────────
#  CONSTANTES
# ─────────────────────────────────────────────
QUEUE_SIZE         = 10
K_FACTOR           = 32
K_FACTOR_PLACEMENT = 64     # K-factor doublé pendant les placements
PLACEMENT_MATCHES  = 10     # nombre de matchs de placement
QUEUE_TIMEOUT      = 30     # minutes avant kick auto de la queue
ABANDON_COOLDOWN   = 15     # minutes de cooldown après abandon en MATCH (inchangé)
# Cooldown progressif pour quitter la QUEUE (pas le match)
QUEUE_LEAVE_COOLDOWNS = [0, 0, 1, 3, 10, 20]  # 0 leave: 0min, 1: 0min, 2: 1min, 3: 3min, 4: 10min, 5+: 20min
REPORT_THRESHOLD   = 3      # nb de reports pour alerter les admins

# ── Multi-Queue Config ──────────────────────────────────────────────
QUEUES = {
    "radiant": {
        "id":          "radiant",
        "name":        "👑 Radiant / Immo3",
        "role":        "Queue Radiant",      # Rôle Discord requis
        "color":       0xffd700,
        "emoji":       "👑",
        "channel":     "queue-radiant",      # Nom du channel staff global
    },
    "ascendant": {
        "id":          "ascendant",
        "name":        "💎 Ascendant / Immo",
        "role":        "Queue Ascendant",
        "color":       0x9b59b6,
        "emoji":       "💎",
        "channel":     "queue-ascendant",
    },
    "gamechangers": {
        "id":          "gamechangers",
        "name":        "🌸 Game Changers",
        "role":        "Queue GC",
        "color":       0xff69b4,
        "emoji":       "🌸",
        "channel":     "queue-gamechangers",
    },
}

VALORANT_MAPS = [
    "Abyss", "Ascent", "Bind", "Breeze", "Fracture",
    "Haven", "Icebox", "Lotus", "Pearl", "Split", "Sunset"
]

# ─────────────────────────────────────────────
#  STATE EN MÉMOIRE
# ─────────────────────────────────────────────
# Une queue par type : queues["radiant"] = [{"id":..., "name":..., "role":..., "queue_id":...}, ...]
queues: dict[str, list[dict]] = {qid: [] for qid in QUEUES}
active_matches: dict = {}
queue_message_refs: dict[str, dict] = {qid: {} for qid in QUEUES}  # queue_id -> {channel_id, message_id}
cooldowns: dict = {}
queue_leave_counts: dict = {}   # uid -> {"count": int, "last_reset": datetime}
test_mode: bool = False
test_queue_size: int = QUEUE_SIZE

def all_queued_players():
    """Retourne tous les joueurs en queue toutes queues confondues."""
    return [p for q in queues.values() for p in q]

def get_player_queue_id(uid: str) -> str | None:
    """Retourne l'ID de queue dans laquelle se trouve le joueur."""
    for qid, q in queues.items():
        if any(p["id"] == uid for p in q):
            return qid
    return None

# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
def get_db():
    db_path = os.getenv("DB_PATH", "inhouse.db")
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS players (
            discord_id        TEXT PRIMARY KEY,
            username          TEXT NOT NULL,
            riot_id           TEXT DEFAULT NULL,
            elo               INTEGER DEFAULT 1000,
            wins              INTEGER DEFAULT 0,
            losses            INTEGER DEFAULT 0,
            streak            INTEGER DEFAULT 0,
            best_streak       INTEGER DEFAULT 0,
            mvp_count         INTEGER DEFAULT 0,
            placement_done    INTEGER DEFAULT 0,
            season            INTEGER DEFAULT 1,
            created_at        TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS applications (
            discord_id   TEXT PRIMARY KEY,
            username     TEXT,
            riot_id      TEXT,
            rank         TEXT,
            age          TEXT,
            presentation TEXT,
            status       TEXT DEFAULT 'pending',
            submitted_at TEXT DEFAULT (datetime('now')),
            reviewed_by  TEXT,
            reviewed_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS player_queue_elo (
            discord_id   TEXT NOT NULL,
            queue_id     TEXT NOT NULL,
            elo          INTEGER DEFAULT 1000,
            wins         INTEGER DEFAULT 0,
            losses       INTEGER DEFAULT 0,
            streak       INTEGER DEFAULT 0,
            best_streak  INTEGER DEFAULT 0,
            mvp_count    INTEGER DEFAULT 0,
            placement_done INTEGER DEFAULT 0,
            season       INTEGER DEFAULT 1,
            PRIMARY KEY (discord_id, queue_id)
        );

        CREATE TABLE IF NOT EXISTS matches (
            match_id    TEXT PRIMARY KEY,
            team1       TEXT NOT NULL,
            team2       TEXT NOT NULL,
            map         TEXT,
            winner      INTEGER,
            mvp         TEXT,
            elo_changes TEXT,
            status      TEXT DEFAULT 'active',
            season      INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
            ended_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            reporter_id TEXT NOT NULL,
            reported_id TEXT NOT NULL,
            match_id    TEXT NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS seasons (
            season_id  INTEGER PRIMARY KEY,
            started_at TEXT DEFAULT (datetime('now')),
            ended_at   TEXT
        );
        INSERT OR IGNORE INTO seasons (season_id) VALUES (1);

        CREATE TABLE IF NOT EXISTS player_channels (
            discord_id      TEXT PRIMARY KEY,
            category_id     INTEGER,
            queue_channel   INTEGER,
            profil_channel  INTEGER,
            notifs_channel  INTEGER,
            history_channel INTEGER
        );
    """)
    conn.commit()

    # Migrations sécurisées — ignorées si la colonne existe déjà
    migrations = [
        "ALTER TABLE players ADD COLUMN riot_id TEXT DEFAULT NULL",
        "ALTER TABLE player_channels ADD COLUMN queue_message_id INTEGER DEFAULT NULL",
        "ALTER TABLE players ADD COLUMN streak INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN best_streak INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN mvp_count INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN placement_done INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN season INTEGER DEFAULT 1",
        "ALTER TABLE matches ADD COLUMN map TEXT",
        "ALTER TABLE matches ADD COLUMN mvp TEXT",
        "ALTER TABLE matches ADD COLUMN season INTEGER DEFAULT 1",
    ]
    # Create extra tables if missing
    for tbl_sql in [
        """CREATE TABLE IF NOT EXISTS applications (
                discord_id   TEXT PRIMARY KEY,
                username     TEXT,
                riot_id      TEXT,
                rank         TEXT,
                age          TEXT,
                presentation TEXT,
                status       TEXT DEFAULT 'pending',
                submitted_at TEXT DEFAULT (datetime('now')),
                reviewed_by  TEXT,
                reviewed_at  TEXT
            )""",
        """CREATE TABLE IF NOT EXISTS player_channels (
                discord_id      TEXT PRIMARY KEY,
                category_id     INTEGER,
                queue_channel   INTEGER,
                profil_channel  INTEGER,
                notifs_channel  INTEGER,
                history_channel INTEGER
            )""",
        """CREATE TABLE IF NOT EXISTS player_queue_elo (
                discord_id   TEXT NOT NULL,
                queue_id     TEXT NOT NULL,
                elo          INTEGER DEFAULT 1000,
                wins         INTEGER DEFAULT 0,
                losses       INTEGER DEFAULT 0,
                streak       INTEGER DEFAULT 0,
                best_streak  INTEGER DEFAULT 0,
                mvp_count    INTEGER DEFAULT 0,
                placement_done INTEGER DEFAULT 0,
                season       INTEGER DEFAULT 1,
                PRIMARY KEY (discord_id, queue_id)
            )"""
    ]:
        try:
            conn.execute(tbl_sql)
            conn.commit()
        except Exception:
            pass
    for migration in migrations:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # Colonne déjà existante, on ignore

    conn.close()

def get_current_season() -> int:
    conn = get_db()
    row = conn.execute("SELECT MAX(season_id) as s FROM seasons").fetchone()
    conn.close()
    return row["s"] or 1

# ─────────────────────────────────────────────
#  MULTI-QUEUE ELO HELPERS
# ─────────────────────────────────────────────
def get_queue_elo(discord_id: str, queue_id: str) -> dict:
    """Retourne les stats d'un joueur pour une queue spécifique."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM player_queue_elo WHERE discord_id=? AND queue_id=?",
        (discord_id, queue_id)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT OR IGNORE INTO player_queue_elo (discord_id, queue_id) VALUES (?,?)",
            (discord_id, queue_id)
        )
        conn.commit()
        conn.close()
        return {"elo": 1000, "wins": 0, "losses": 0, "streak": 0,
                "best_streak": 0, "mvp_count": 0, "placement_done": 0}
    conn.close()
    return dict(row)

def update_queue_elo(discord_id: str, queue_id: str, conn=None, **kwargs):
    """Met à jour les stats d'un joueur pour une queue spécifique."""
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO player_queue_elo (discord_id, queue_id) VALUES (?,?)",
        (discord_id, queue_id)
    )
    set_clause = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [discord_id, queue_id]
    conn.execute(f"UPDATE player_queue_elo SET {set_clause} WHERE discord_id=? AND queue_id=?", vals)
    if own_conn:
        conn.commit()
        conn.close()

def player_has_queue_access(member: discord.Member, queue_id: str) -> bool:
    """Vérifie si le joueur a le rôle Discord pour accéder à cette queue."""
    required_role = QUEUES[queue_id]["role"]
    return any(r.name == required_role for r in member.roles)

# ─────────────────────────────────────────────
#  COOLDOWN PROGRESSIF — QUITTER LA QUEUE
# ─────────────────────────────────────────────
def get_leave_cooldown_minutes(uid: str) -> int:
    """Retourne le cooldown en minutes selon le nombre de leaves consécutifs."""
    now = datetime.now(timezone.utc)
    data = queue_leave_counts.get(uid)

    # Reset automatique si le dernier leave date de plus de 30 min
    if data and (now - data["last_reset"]).total_seconds() > 1800:
        queue_leave_counts.pop(uid, None)
        return 0

    count = data["count"] if data else 0
    idx = min(count, len(QUEUE_LEAVE_COOLDOWNS) - 1)
    return QUEUE_LEAVE_COOLDOWNS[idx]


def register_queue_leave(uid: str) -> int:
    """Incrémente le compteur de leaves et retourne le cooldown à appliquer."""
    now = datetime.now(timezone.utc)
    data = queue_leave_counts.get(uid)

    # Reset si dernier leave > 30 min
    if data and (now - data["last_reset"]).total_seconds() > 1800:
        data = None

    if data is None:
        queue_leave_counts[uid] = {"count": 1, "last_reset": now}
        count = 1
    else:
        data["count"] += 1
        data["last_reset"] = now
        count = data["count"]

    idx = min(count, len(QUEUE_LEAVE_COOLDOWNS) - 1)
    minutes = QUEUE_LEAVE_COOLDOWNS[idx]
    if minutes > 0:
        cooldowns[uid] = now + timedelta(minutes=minutes)
    return minutes


# ─────────────────────────────────────────────
#  SYSTÈME DE CANDIDATURE
# ─────────────────────────────────────────────
RANKS_VALORANT = [
    "Fer", "Bronze", "Argent", "Or", "Platine",
    "Diamant", "Ascendant", "Immortel", "Immortel 2", "Immortel 3", "Radiant"
]

class ApplicationModal(discord.ui.Modal, title="📝 Candidature Crazy Inhouse"):
    riot_id = discord.ui.TextInput(
        label="Riot ID (ex: Rēyu#EUW)",
        placeholder="Nom#Tag",
        required=True,
        max_length=50
    )
    rank = discord.ui.TextInput(
        label="Rank actuel Valorant",
        placeholder="ex: Immortel 2, Radiant...",
        required=True,
        max_length=30
    )
    age = discord.ui.TextInput(
        label="Ton âge",
        placeholder="ex: 19",
        required=True,
        max_length=3
    )
    presentation = discord.ui.TextInput(
        label="Présente-toi en quelques mots",
        placeholder="D'où tu viens, comment tu joues, pourquoi tu veux rejoindre...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        conn = get_db()

        # Vérifier si déjà une candidature en cours
        existing = conn.execute(
            "SELECT status FROM applications WHERE discord_id=?", (uid,)
        ).fetchone()
        if existing:
            conn.close()
            status = existing["status"]
            if status == "pending":
                await interaction.response.send_message(
                    "⚠️ Tu as déjà une candidature en attente de validation !", ephemeral=True
                )
            elif status == "accepted":
                await interaction.response.send_message(
                    "✅ Ta candidature a déjà été acceptée !", ephemeral=True
                )
            elif status == "rejected":
                await interaction.response.send_message(
                    "❌ Ta candidature a été refusée. Contacte un admin si tu penses que c'est une erreur.", ephemeral=True
                )
            return

        # Enregistrer la candidature
        conn.execute(
            "INSERT OR REPLACE INTO applications (discord_id, username, riot_id, rank, age, presentation) VALUES (?,?,?,?,?,?)",
            (uid, interaction.user.display_name, self.riot_id.value, self.rank.value, self.age.value, self.presentation.value)
        )
        conn.commit()
        conn.close()

        await interaction.response.send_message(
            embed=discord.Embed(
                title="✅ Candidature envoyée !",
                description="Ta candidature a bien été reçue. Un admin va l'examiner prochainement.\n\nTu seras notifié par DM ou dans ce salon.",
                color=0x3ba55d
            ),
            ephemeral=True
        )

        # Poster dans #candidatures-en-cours
        guild = interaction.guild
        review_ch = next((ch for ch in guild.text_channels if "candidatures-en-cours" in ch.name), None)
        if review_ch:
            embed = discord.Embed(
                title=f"📋 Nouvelle candidature — {interaction.user.display_name}",
                color=0xffa600,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            embed.add_field(name="👤 Joueur", value=f"{interaction.user.mention}", inline=True)
            embed.add_field(name="🎮 Riot ID", value=f"`{self.riot_id.value}`", inline=True)
            embed.add_field(name="🏅 Rank", value=f"**{self.rank.value}**", inline=True)
            embed.add_field(name="🎂 Âge", value=self.age.value, inline=True)
            embed.add_field(name="📝 Présentation", value=self.presentation.value, inline=False)
            embed.set_footer(text=f"ID: {uid}")
            await review_ch.send(
                embed=embed,
                view=ApplicationReviewView(uid, interaction.user.display_name)
            )


class ApplicationReviewView(discord.ui.View):
    """Boutons Accepter / Refuser dans #candidatures-en-cours."""
    def __init__(self, applicant_id: str, applicant_name: str):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.applicant_name = applicant_name

    @discord.ui.button(label="✅ Accepter", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
            return
        await self._process(interaction, "accepted")

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
            return
        await self._process(interaction, "rejected")

    async def _process(self, interaction: discord.Interaction, decision: str):
        uid = self.applicant_id
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = guild.get_member(int(uid))

        conn = get_db()
        conn.execute(
            "UPDATE applications SET status=?, reviewed_by=?, reviewed_at=datetime('now') WHERE discord_id=?",
            (decision, str(interaction.user.id), uid)
        )
        conn.commit()
        conn.close()

        if decision == "accepted":
            # Donner le rôle Membre
            membre_role = discord.utils.get(guild.roles, name="Membre")
            if not membre_role:
                membre_role = await guild.create_role(name="Membre", color=discord.Color(0x3ba55d), hoist=True)
            if member:
                await member.add_roles(membre_role)

            # Retirer le rôle Candidat si présent
            candidat_role = discord.utils.get(guild.roles, name="Candidat")
            if candidat_role and member and candidat_role in member.roles:
                await member.remove_roles(candidat_role)

            # Créer l'espace privé du joueur
            if member:
                await create_player_space(guild, member)

            # Notifier le candidat
            if member:
                try:
                    dm_embed = discord.Embed(
                        title="🎉 Candidature acceptée !",
                        description=f"Bienvenue sur **Crazy Inhouse** ! Tu as maintenant accès au serveur.\n\nUtilise `/register` dans ton salon privé pour créer ton profil et commencer à jouer !",
                        color=0x3ba55d
                    )
                    await member.send(embed=dm_embed)
                except Exception:
                    pass

            # Mettre à jour le message de candidature
            embed = interaction.message.embeds[0]
            embed.color = 0x3ba55d
            embed.title = f"✅ Accepté — {self.applicant_name}"
            embed.add_field(name="Décision", value=f"✅ Accepté par {interaction.user.mention}", inline=False)
            await interaction.message.edit(embed=embed, view=None)
            await interaction.followup.send(f"✅ **{self.applicant_name}** accepté et rôle Membre attribué !", ephemeral=True)

        else:
            # Notifier le candidat du refus
            if member:
                try:
                    dm_embed = discord.Embed(
                        title="❌ Candidature refusée",
                        description="Ta candidature sur **Crazy Inhouse** n'a pas été retenue cette fois.\nContacte un admin si tu as des questions.",
                        color=0xed4245
                    )
                    await member.send(embed=dm_embed)
                except Exception:
                    pass

            embed = interaction.message.embeds[0]
            embed.color = 0xed4245
            embed.title = f"❌ Refusé — {self.applicant_name}"
            embed.add_field(name="Décision", value=f"❌ Refusé par {interaction.user.mention}", inline=False)
            await interaction.message.edit(embed=embed, view=None)
            await interaction.followup.send(f"❌ **{self.applicant_name}** refusé.", ephemeral=True)


class ApplicationButtonView(discord.ui.View):
    """Bouton dans #candidatures pour ouvrir le formulaire."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📝 Postuler", style=discord.ButtonStyle.primary, custom_id="open_application")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApplicationModal())


# ─────────────────────────────────────────────
#  ELO & RANG
# ─────────────────────────────────────────────
def calc_elo_change(winner_elo: int, loser_elo: int, k: int = K_FACTOR) -> tuple[int, int]:
    expected = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    gain = round(k * (1 - expected))
    loss = round(k * expected)
    return gain, loss

def is_in_placement(wins: int, losses: int) -> bool:
    return (wins + losses) < PLACEMENT_MATCHES

def get_k_factor(wins: int, losses: int) -> int:
    return K_FACTOR_PLACEMENT if is_in_placement(wins, losses) else K_FACTOR

def placement_progress(wins: int, losses: int) -> str:
    done = wins + losses
    if done >= PLACEMENT_MATCHES:
        return None
    return f"{done}/{PLACEMENT_MATCHES}" 

RANKS = [
    (900,  "Iron",     "⬛", 0x555555),
    (1050, "Bronze",   "🟫", 0xad6f3b),
    (1200, "Silver",   "⬜", 0xc0c0c0),
    (1350, "Gold",     "🟨", 0xffd700),
    (1500, "Platinum", "🟦", 0x00b4d8),
    (1700, "Diamond",  "💎", 0x9b5de5),
    (9999, "Radiant",  "👑", 0xff6b35),
]

def get_rank(elo: int) -> tuple[str, str, int]:
    for threshold, name, icon, color in RANKS:
        if elo < threshold:
            return name, icon, color
    return "Radiant", "👑", 0xff6b35

# ─────────────────────────────────────────────
#  RÔLES DISCORD AUTOMATIQUES
# ─────────────────────────────────────────────
RANK_NAMES = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Radiant"]
RANK_COLORS = {
    "Iron":     discord.Color.from_str("#555555"),
    "Bronze":   discord.Color.from_str("#ad6f3b"),
    "Silver":   discord.Color.from_str("#c0c0c0"),
    "Gold":     discord.Color.from_str("#ffd700"),
    "Platinum": discord.Color.from_str("#00b4d8"),
    "Diamond":  discord.Color.from_str("#9b5de5"),
    "Radiant":  discord.Color.from_str("#ff6b35"),
}

async def sync_rank_role(guild: discord.Guild, member: discord.Member, elo: int):
    rank_name, _, _ = get_rank(elo)
    for rname in RANK_NAMES:
        role = discord.utils.get(guild.roles, name=rname)
        if role and role in member.roles and rname != rank_name:
            try:
                await member.remove_roles(role)
            except Exception:
                pass
    target_role = discord.utils.get(guild.roles, name=rank_name)
    if not target_role:
        try:
            target_role = await guild.create_role(name=rank_name, color=RANK_COLORS.get(rank_name, discord.Color.default()))
        except Exception:
            return
    try:
        if target_role not in member.roles:
            await member.add_roles(target_role)
    except Exception:
        pass

# ─────────────────────────────────────────────
#  TEAM BALANCING
# ─────────────────────────────────────────────
def balance_teams(players: list[dict]) -> tuple[list, list]:
    from itertools import combinations
    conn = get_db()
    elos = {}
    for p in players:
        row = conn.execute("SELECT elo FROM players WHERE discord_id = ?", (p["id"],)).fetchone()
        elos[p["id"]] = row["elo"] if row else 1000
    conn.close()

    best_diff = float("inf")
    best_splits = []  # Toutes les combinaisons au même écart minimum
    ids = [p["id"] for p in players]

    for combo in combinations(range(10), 5):
        t1_ids = [ids[i] for i in combo]
        t2_ids = [ids[i] for i in range(10) if i not in combo]
        diff = abs(sum(elos[i] for i in t1_ids) - sum(elos[i] for i in t2_ids))
        if diff < best_diff:
            best_diff = diff
            best_splits = [(t1_ids, t2_ids)]
        elif diff == best_diff:
            best_splits.append((t1_ids, t2_ids))

    # Choisir aléatoirement parmi les splits optimaux → équipes différentes à chaque fois
    best_split = random.choice(best_splits)

    id_to_player = {p["id"]: p for p in players}
    return [id_to_player[i] for i in best_split[0]], [id_to_player[i] for i in best_split[1]]

# ─────────────────────────────────────────────
#  ESPACE PRIVÉ JOUEUR
# ─────────────────────────────────────────────
async def create_player_space(guild: discord.Guild, member: discord.Member):
    """Crée la catégorie privée + 4 channels pour un nouveau joueur."""
    uid = str(member.id)
    conn = get_db()
    existing = conn.execute("SELECT * FROM player_channels WHERE discord_id=?", (uid,)).fetchone()
    if existing:
        conn.close()
        return  # Déjà créé

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_messages=True),
    }
    # Staff/Coach voient aussi
    coach_role = discord.utils.get(guild.roles, name="Coach")
    if coach_role:
        overwrites[coach_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    try:
        cat = await guild.create_category(f"👤 {member.display_name}", overwrites=overwrites)
        ch_queue   = await guild.create_text_channel("🎮︱queue",       category=cat, overwrites=overwrites)
        ch_profil  = await guild.create_text_channel("📊︱profil",      category=cat, overwrites=overwrites)
        ch_notifs  = await guild.create_text_channel("🔔︱notifications", category=cat, overwrites=overwrites)
        ch_history = await guild.create_text_channel("📜︱historique",  category=cat, overwrites=overwrites)

        conn.execute(
            "INSERT OR REPLACE INTO player_channels (discord_id, category_id, queue_channel, profil_channel, notifs_channel, history_channel) VALUES (?,?,?,?,?,?)",
            (uid, cat.id, ch_queue.id, ch_profil.id, ch_notifs.id, ch_history.id)
        )
        conn.commit()
        conn.close()

        # Message initial dans chaque channel
        await _init_queue_channel(ch_queue, member)
        await _init_profil_channel(ch_profil, member, guild)
        await ch_notifs.send(embed=discord.Embed(
            title="🔔 Notifications",
            description="Tu recevras ici toutes les alertes : match trouvé, résultat, changement de rang, etc.",
            color=0x5865f2
        ))
        await ch_history.send(embed=discord.Embed(
            title="📜 Historique",
            description="Tes derniers matchs apparaîtront ici automatiquement après chaque partie.",
            color=0x5865f2
        ))
    except Exception as e:
        conn.close()
        print(f"Erreur create_player_space pour {member.display_name}: {e}")


def get_player_channels(uid: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM player_channels WHERE discord_id=?", (uid,)).fetchone()
    conn.close()
    return row


async def _init_queue_channel(channel: discord.TextChannel, member: discord.Member):
    """Poste l'embed de queue personnalisé avec boutons de rôle."""
    embed = discord.Embed(
        title="🎮 Rejoindre la queue",
        description=f"Bienvenue **{member.display_name}** ! Choisis ton rôle et rejoins la file d'attente.",
        color=0xff4655
    )
    embed.add_field(name="📋 Comment ça marche", value="1. Clique sur ton rôle ci-dessous\n2. Tu rejoins automatiquement la queue\n3. Dès 10 joueurs, le match se lance !", inline=False)
    embed.add_field(name="File d'attente", value="*Chargement...*", inline=False)
    embed.set_footer(text="Ce salon est privé et visible uniquement par toi et le staff.")
    view = PersonalQueueView(str(member.id), member.display_name)
    queue_msg = await channel.send(embed=embed, view=view)
    # Sauvegarder l'ID du message pour le retrouver directement plus tard
    conn_save = get_db()
    conn_save.execute(
        "UPDATE player_channels SET queue_message_id=? WHERE discord_id=?",
        (queue_msg.id, str(member.id))
    )
    conn_save.commit()
    conn_save.close()


async def _init_profil_channel(channel: discord.TextChannel, member: discord.Member, guild: discord.Guild):
    """Poste l'embed de profil initial."""
    uid = str(member.id)
    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE discord_id=?", (uid,)).fetchone()
    conn.close()

    if not row:
        embed = discord.Embed(
            title="📊 Mon Profil",
            description="Tu n'es pas encore inscrit !\nUtilise `/register` pour créer ton profil.",
            color=0x555555
        )
    else:
        rname, ricon, color = get_rank(row["elo"])
        total = row["wins"] + row["losses"]
        wr = round(row["wins"] / total * 100) if total > 0 else 0
        in_place = is_in_placement(row["wins"], row["losses"])
        embed = discord.Embed(title=f"📊 Profil de {member.display_name}", color=0x555555 if in_place else color)
        embed.set_thumbnail(url=member.display_avatar.url)
        if in_place:
            embed.add_field(name="Rang", value=f"🔰 Placement ({total}/{PLACEMENT_MATCHES})", inline=True)
            embed.add_field(name="ELO", value="*Masqué*", inline=True)
        else:
            embed.add_field(name="Rang", value=f"{ricon} **{rname}**", inline=True)
            embed.add_field(name="ELO", value=f"**{row['elo']}** pts", inline=True)
        embed.add_field(name="Record", value=f"**{row['wins']}W / {row['losses']}L** ({wr}%)", inline=True)
        embed.add_field(name="Streak", value=f"🔥 {row['streak']} (record: {row['best_streak']})", inline=True)
        embed.add_field(name="MVP", value=f"🌟 {row['mvp_count']}", inline=True)
        if row["riot_id"]:
            embed.add_field(name="Compte Riot", value=f"`{row['riot_id']}`", inline=True)
    embed.set_footer(text="Mis à jour automatiquement après chaque match.")
    await channel.send(embed=embed)


async def update_player_profil(guild: discord.Guild, uid: str):
    """Met à jour le channel profil d'un joueur après un match."""
    pch = get_player_channels(uid)
    if not pch or not pch["profil_channel"]:
        return
    channel = guild.get_channel(pch["profil_channel"])
    if not channel:
        return
    member = guild.get_member(int(uid))
    if not member:
        return

    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE discord_id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        return

    rname, ricon, color = get_rank(row["elo"])
    total = row["wins"] + row["losses"]
    wr = round(row["wins"] / total * 100) if total > 0 else 0
    in_place = is_in_placement(row["wins"], row["losses"])

    embed = discord.Embed(title=f"📊 Profil de {member.display_name}", color=0x555555 if in_place else color)
    embed.set_thumbnail(url=member.display_avatar.url)
    if in_place:
        embed.add_field(name="Rang", value=f"🔰 Placement ({total}/{PLACEMENT_MATCHES})", inline=True)
        embed.add_field(name="ELO", value="*Masqué pendant les placements*", inline=True)
    else:
        embed.add_field(name="Rang", value=f"{ricon} **{rname}**", inline=True)
        embed.add_field(name="ELO", value=f"**{row['elo']}** pts", inline=True)
    embed.add_field(name="Record", value=f"**{row['wins']}W / {row['losses']}L** ({wr}%)", inline=True)
    embed.add_field(name="Streak", value=f"🔥 {row['streak']} (record: {row['best_streak']})", inline=True)
    embed.add_field(name="MVP", value=f"🌟 {row['mvp_count']}", inline=True)
    if row["riot_id"]:
        embed.add_field(name="Compte Riot", value=f"`{row['riot_id']}`", inline=True)
    embed.set_footer(text=f"Mis à jour — {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC")

    # Éditer le dernier message du bot dans le channel
    async for msg in channel.history(limit=10):
        if msg.author == guild.me and msg.embeds:
            await msg.edit(embed=embed)
            return
    await channel.send(embed=embed)


async def update_player_history(guild: discord.Guild, uid: str, match_id: str, won: bool, elo_change: dict, map_name: str):
    """Poste le résultat du match dans le channel historique du joueur."""
    pch = get_player_channels(uid)
    if not pch or not pch["history_channel"]:
        return
    channel = guild.get_channel(pch["history_channel"])
    if not channel:
        return

    result_emoji = "✅" if won else "❌"
    result_text = "Victoire" if won else "Défaite"
    change = elo_change.get("change", "?")
    old_elo = elo_change.get("old", "?")
    new_elo = elo_change.get("new", "?")
    rname, ricon, color = get_rank(new_elo if isinstance(new_elo, int) else 1000)
    in_place = elo_change.get("placement", False)

    embed = discord.Embed(
        title=f"{result_emoji} {result_text} — {map_name}",
        color=0x00ff88 if won else 0xff4444,
        timestamp=datetime.now(timezone.utc)
    )
    if in_place:
        embed.add_field(name="ELO", value=f"🔰 Placement (`{change}`)", inline=True)
    else:
        embed.add_field(name="ELO", value=f"{old_elo} ➜ **{new_elo}** (`{change}`) {ricon}", inline=True)
    embed.add_field(name="Match", value=f"`{match_id[-6:]}`", inline=True)
    await channel.send(embed=embed)


async def notify_player(guild: discord.Guild, uid: str, embed: discord.Embed):
    """Envoie une notification dans le channel notifs du joueur."""
    pch = get_player_channels(uid)
    if not pch or not pch["notifs_channel"]:
        return
    channel = guild.get_channel(pch["notifs_channel"])
    if channel:
        await channel.send(embed=embed)


async def update_personal_queue_embeds(guild: discord.Guild):
    """Met à jour l'embed de queue dans tous les channels personnels."""
    conn = get_db()
    rows = conn.execute("SELECT discord_id, queue_channel, queue_message_id FROM player_channels").fetchall()
    conn.close()
    for row in rows:
        try:
            ch = guild.get_channel(row["queue_channel"])
            if not ch:
                continue

            uid = row["discord_id"]
            msg = None

            # Méthode 1 : ID direct stocké en DB (fiable)
            if row["queue_message_id"]:
                try:
                    msg = await ch.fetch_message(row["queue_message_id"])
                except Exception:
                    msg = None

            # Méthode 2 : fallback scan historique (limit élevée)
            if msg is None:
                async for m in ch.history(limit=50):
                    if m.author == guild.me and m.embeds and m.components:
                        msg = m
                        # Sauvegarder l'ID trouvé pour la prochaine fois
                        conn2 = get_db()
                        conn2.execute("UPDATE player_channels SET queue_message_id=? WHERE discord_id=?", (m.id, uid))
                        conn2.commit()
                        conn2.close()
                        break

            if not msg:
                continue

            old_embed = msg.embeds[0]
            new_embed = discord.Embed(
                title=old_embed.title,
                description=old_embed.description,
                color=old_embed.color.value if old_embed.color else 0xff4655
            )
            for field in old_embed.fields:
                if field.name not in ("File d'attente", "🎯 Statut"):
                    new_embed.add_field(name=field.name, value=field.value, inline=field.inline)

            qid = get_player_queue_id(uid)
            in_queue = qid is not None
            in_match = any(uid in [p["id"] for p in m["team1"] + m["team2"]] for m in active_matches.values())
            current_size = test_queue_size if test_mode else QUEUE_SIZE

            if in_match:
                new_embed.add_field(name="🎯 Statut", value="⚔️ **Match en cours !** Va dans ton salon scoreboard.", inline=False)
            elif in_queue:
                q_name = QUEUES[qid]["emoji"] + " " + QUEUES[qid]["name"]
                q = queues[qid]
                pos = next((i+1 for i, p in enumerate(q) if p["id"] == uid), "?")
                new_embed.add_field(name="🎯 Statut", value=f"✅ **En queue** {q_name} — Position **{pos}/{current_size}** ({len(q)} joueur(s))", inline=False)
            else:
                new_embed.add_field(name="🎯 Statut", value="⏸️ Pas en queue — Choisis ton rôle pour rejoindre !", inline=False)

            if old_embed.footer:
                new_embed.set_footer(text=old_embed.footer.text)
            await msg.edit(embed=new_embed)
        except Exception as e:
            print(f"Erreur update_personal_queue_embeds [{row['discord_id']}]: {e}")


# ─────────────────────────────────────────────
#  COMPOSITION ANALYZER
# ─────────────────────────────────────────────
# Rôles considérés "défensifs/utilitaires" (1 minimum recommandé par équipe)
ROLE_LIMITS = {
    "Duelliste":  2,   # max recommandé par équipe
    "Initiateur": 2,
    "Contrôleur": 2,
    "Sentinelle": 2,
    "Flex":       5,   # pas de limite
}
ESSENTIAL_ROLES = ["Contrôleur", "Sentinelle"]  # au moins 1 de chaque recommandé

def analyze_composition(team: list[dict]) -> dict:
    """
    Analyse la composition d'une équipe et retourne :
    - warnings : liste de problèmes détectés
    - suggestions : liste de suggestions de flex
    - score : 0 (parfait) à 3 (très déséquilibré)
    """
    from collections import Counter
    role_count = Counter(p.get("role", "Flex") for p in team)
    warnings = []
    suggestions = []
    score = 0

    # Vérifie les rôles en sureffectif
    for role, count in role_count.items():
        limit = ROLE_LIMITS.get(role, 2)
        if role != "Flex" and count > limit:
            excess = count - limit
            warnings.append(f"**{count} {role}s** (max recommandé : {limit})")
            score += excess
            # Trouver les joueurs qui pourraient flex
            candidates = [p for p in team if p.get("role") == role]
            # Prioriser les Flex ou ceux avec le moins de parties
            for candidate in candidates[-excess:]:  # les derniers ajoutés
                missing = [r for r in ESSENTIAL_ROLES if role_count.get(r, 0) == 0]
                if missing:
                    suggestions.append(f"→ **{candidate['name']}** pourrait jouer **{missing[0]}**")
                else:
                    suggestions.append(f"→ **{candidate['name']}** pourrait flex sur un autre rôle")

    # Vérifie les rôles essentiels manquants
    for role in ESSENTIAL_ROLES:
        if role_count.get(role, 0) == 0:
            warnings.append(f"Aucun **{role}** !")
            score += 2
            # Suggérer le(s) Flex ou Duelliste(s) en surplus
            flex_players = [p for p in team if p.get("role") == "Flex"]
            duel_players = [p for p in team if p.get("role") == "Duelliste"]
            candidates = flex_players or duel_players
            if candidates:
                suggestions.append(f"→ **{candidates[0]['name']}** pourrait jouer **{role}**")

    return {"warnings": warnings, "suggestions": suggestions, "score": score}

def build_composition_field(team: list[dict], team_num: int) -> tuple[str, str] | None:
    """Retourne (title, value) pour un embed field, ou None si composition OK."""
    analysis = analyze_composition(team)
    if analysis["score"] == 0:
        return None

    emoji = "⚠️" if analysis["score"] <= 2 else "🚨"
    title = f"{emoji} Composition Team {team_num}"
    lines = []
    if analysis["warnings"]:
        lines.append("**Problèmes :**")
        lines.extend(f"• {w}" for w in analysis["warnings"])
    if analysis["suggestions"]:
        lines.append("**Suggestions :**")
        lines.extend(analysis["suggestions"])
    lines.append("*Les joueurs peuvent s'arranger en vocal.*")
    return title, "\n".join(lines)


# ─────────────────────────────────────────────
#  QUEUE EMBED
# ─────────────────────────────────────────────
def build_queue_embed() -> discord.Embed:
    current_size = test_queue_size if test_mode else QUEUE_SIZE
    title = "🧪 [MODE TEST] In-House Valorant — File d'attente" if test_mode else "🎮 In-House Valorant — File d'attente"
    color = 0xffa500 if test_mode else 0xff4655
    embed = discord.Embed(title=title, color=color)
    if test_mode:
        embed.description = f"⚠️ **Mode test actif** — Queue réduite à **{current_size} joueurs**"
    all_players = all_queued_players()
    if all_players:
        player_lines = []
        for i, p in enumerate(all_players):
            role_tag = f" `{p.get('role', '—')}`" if p.get('role') else ""
            bot_tag = " 🤖" if p.get('is_bot') else ""
            qid = p.get('queue_id', '')
            q_emoji = QUEUES[qid]['emoji'] if qid in QUEUES else ""
            player_lines.append(f"{i+1}. **{p['name']}**{role_tag}{bot_tag} {q_emoji}")
        embed.add_field(name=f"Joueurs ({len(all_players)}/{current_size})", value="\n".join(player_lines), inline=False)
    else:
        embed.add_field(name=f"Joueurs (0/{current_size})", value="*Personne en queue...*", inline=False)
    embed.set_footer(text="Clique sur Rejoindre • Timeout auto 30 min • Cooldown 15 min après abandon")
    return embed

# ─────────────────────────────────────────────
#  VIEWS
# ─────────────────────────────────────────────
class PersonalQueueView(discord.ui.View):
    """Vue persistante dans le channel queue personnel du joueur."""
    def __init__(self, uid: str, username: str):
        super().__init__(timeout=None)
        self.uid = uid
        self.username = username

        # Row 0 : boutons de rôle
        roles = [
            ("⚔️ Duelliste",  "Duelliste",  discord.ButtonStyle.danger,   f"pq_duel_{uid}"),
            ("🔦 Initiateur", "Initiateur", discord.ButtonStyle.primary,  f"pq_init_{uid}"),
            ("💨 Contrôleur", "Contrôleur", discord.ButtonStyle.success,  f"pq_ctrl_{uid}"),
            ("🛡️ Sentinelle", "Sentinelle", discord.ButtonStyle.secondary, f"pq_sent_{uid}"),
            ("🔄 Flex",       "Flex",       discord.ButtonStyle.secondary, f"pq_flex_{uid}"),
        ]
        for label, role, style, cid in roles:
            btn = discord.ui.Button(label=label, style=style, custom_id=cid, row=0)
            btn.callback = self._make_join_callback(role)
            self.add_item(btn)

        # Row 1 : bouton quitter
        leave_btn = discord.ui.Button(label="❌ Quitter la queue", style=discord.ButtonStyle.danger, custom_id=f"pq_leave_{uid}", row=1)
        leave_btn.callback = self._leave_callback
        self.add_item(leave_btn)

    def _make_join_callback(self, role: str):
        async def callback(interaction: discord.Interaction):
            if str(interaction.user.id) != self.uid:
                await interaction.response.send_message("❌ Ce salon ne t'appartient pas.", ephemeral=True)
                return
            uid = self.uid
            member = interaction.user

            # Vérifications de base
            conn = get_db()
            row = conn.execute("SELECT * FROM players WHERE discord_id=?", (uid,)).fetchone()
            conn.close()
            if not row:
                await interaction.response.send_message("❌ Tu n'es pas inscrit ! Utilise `/register`.", ephemeral=True)
                return
            if uid in cooldowns and datetime.now(timezone.utc) < cooldowns[uid]:
                remaining = int((cooldowns[uid] - datetime.now(timezone.utc)).total_seconds() / 60) + 1
                await interaction.response.send_message(f"⏳ Cooldown actif : encore **{remaining} min**.", ephemeral=True)
                return
            for m in active_matches.values():
                if uid in [p["id"] for p in m["team1"] + m["team2"]]:
                    await interaction.response.send_message("⚠️ Tu as déjà un match en cours !", ephemeral=True)
                    return
            if get_player_queue_id(uid):
                await interaction.response.send_message("⚠️ Tu es déjà en queue !", ephemeral=True)
                return

            # Trouver les queues accessibles au joueur
            accessible = [qid for qid in QUEUES if player_has_queue_access(member, qid)]
            if not accessible:
                await interaction.response.send_message(
                    "❌ Tu n'as accès à aucune queue ! Contacte un admin pour obtenir ton rôle (Queue Radiant / Queue Ascendant / Queue GC).",
                    ephemeral=True
                )
                return
            if len(accessible) == 1:
                # Une seule queue → rejoindre directement
                await _join_queue(interaction, uid, self.username, role, accessible[0])
            else:
                # Plusieurs queues → menu déroulant pour choisir
                select = QueueSelectDropdown(uid, self.username, role, accessible)
                view = discord.ui.View(timeout=60)
                view.add_item(select)
                await interaction.response.send_message(
                    embed=discord.Embed(
                        title=f"🎯 Dans quelle queue veux-tu jouer **{role}** ?",
                        description="\n".join(f"{QUEUES[qid]['emoji']} **{QUEUES[qid]['name']}**" for qid in accessible),
                        color=0xff4655
                    ),
                    view=view,
                    ephemeral=True
                )
        return callback

    async def _leave_callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("❌ Ce salon ne t'appartient pas.", ephemeral=True)
            return
        uid = self.uid
        qid = get_player_queue_id(uid)
        if not qid:
            await interaction.response.send_message("⚠️ Tu n'es pas en queue !", ephemeral=True)
            return
        queues[qid] = [p for p in queues[qid] if p["id"] != uid]
        minutes = register_queue_leave(uid)
        if minutes > 0:
            msg = f"👋 Retiré de la queue. Cooldown **{minutes} min** (leave #{queue_leave_counts.get(uid, {}).get('count', 1)})."
        else:
            msg = "👋 Retiré de la queue."
        await interaction.response.send_message(msg, ephemeral=True)
        await update_personal_queue_embeds(interaction.guild)


class QueueSelectDropdown(discord.ui.Select):
    """Menu déroulant pour choisir la queue quand le joueur a accès à plusieurs."""
    def __init__(self, uid: str, username: str, role: str, queue_ids: list[str]):
        self.uid = uid
        self.username = username
        self.role = role
        options = [
            discord.SelectOption(
                label=QUEUES[qid]["name"],
                value=qid,
                emoji=QUEUES[qid]["emoji"],
                description=f"Rejoindre la queue {QUEUES[qid]['name']} en {role}"
            )
            for qid in queue_ids
        ]
        super().__init__(
            placeholder="Choisis ta queue...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("❌ Ce menu ne t'appartient pas.", ephemeral=True)
            return
        queue_id = self.values[0]
        await _join_queue(interaction, self.uid, self.username, self.role, queue_id)


async def _join_queue(interaction: discord.Interaction, uid: str, username: str, role: str, queue_id: str):
    """Ajoute un joueur à une queue spécifique et déclenche le match si plein."""
    q_info = QUEUES[queue_id]
    queues[queue_id].append({
        "id": uid, "name": username, "role": role,
        "joined_at": datetime.now(timezone.utc), "queue_id": queue_id
    })
    current_size = test_queue_size if test_mode else QUEUE_SIZE
    nb = len(queues[queue_id])
    await interaction.response.send_message(
        f"✅ Tu rejoins **{q_info['name']}** en **{role}** ! ({nb}/{current_size})",
        ephemeral=True
    )
    await update_personal_queue_embeds(interaction.guild)
    if nb >= current_size:
        await start_match(interaction, queue_id=queue_id)


class RoleSelectView(discord.ui.View):
    def __init__(self, uid: str, username: str):
        super().__init__(timeout=60)
        self.uid = uid
        self.username = username

    @discord.ui.select(
        placeholder="Choisis ton rôle pour ce match...",
        custom_id="role_select_queue",
        options=[
            discord.SelectOption(label="Duelliste",  emoji="⚔️"),
            discord.SelectOption(label="Initiateur", emoji="🔦"),
            discord.SelectOption(label="Contrôleur", emoji="💨"),
            discord.SelectOption(label="Sentinelle", emoji="🛡️"),
            discord.SelectOption(label="Flex",       emoji="🔄"),
        ]
    )
    async def role_chosen(self, interaction: discord.Interaction, select: discord.ui.Select):
        uid = self.uid
        role = select.values[0]

        if uid in cooldowns and datetime.now(timezone.utc) < cooldowns[uid]:
            remaining = int((cooldowns[uid] - datetime.now(timezone.utc)).total_seconds() / 60) + 1
            await interaction.response.send_message(
                f"⏳ Cooldown actif : encore **{remaining} min** avant de pouvoir rejoindre.", ephemeral=True
            )
            return

        for m in active_matches.values():
            if uid in [p["id"] for p in m["team1"] + m["team2"]]:
                await interaction.response.send_message("⚠️ Tu as déjà un match en cours !", ephemeral=True)
                return
        if get_player_queue_id(uid):
            await interaction.response.send_message("⚠️ Tu es déjà en queue !", ephemeral=True)
            return

        # Legacy RoleSelectView - redirige vers le salon personnel
        await interaction.response.send_message("⚠️ Utilise ton salon 🎮︱queue personnel !", ephemeral=True)



class QueueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Rejoindre", style=discord.ButtonStyle.success, custom_id="queue_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        conn = get_db()
        row = conn.execute("SELECT * FROM players WHERE discord_id = ?", (uid,)).fetchone()
        conn.close()
        if not row:
            await interaction.response.send_message("❌ Tu n'es pas inscrit ! Utilise `/register`.", ephemeral=True)
            return
        view = RoleSelectView(uid, interaction.user.display_name)
        await interaction.response.send_message("🎯 Quel est ton rôle pour ce match ?", view=view, ephemeral=True)

    @discord.ui.button(label="❌ Quitter", style=discord.ButtonStyle.danger, custom_id="queue_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        qid = get_player_queue_id(uid)
        if not qid:
            await interaction.response.send_message("⚠️ Tu n'es pas en queue !", ephemeral=True)
            return
        queues[qid] = [p for p in queues[qid] if p["id"] != uid]
        minutes = register_queue_leave(uid)
        leave_msg = f"👋 Retiré de la queue. Cooldown **{minutes} min** (leave #{queue_leave_counts.get(uid, {}).get('count', 1)})." if minutes > 0 else "👋 Retiré de la queue."
        await interaction.response.send_message(leave_msg, ephemeral=True)

        await update_queue_message(interaction)



class MVPVoteView(discord.ui.View):
    def __init__(self, match_id: str, all_players: list[dict]):
        super().__init__(timeout=300)
        self.match_id = match_id
        self.votes: dict = {}
        options = [discord.SelectOption(label=p["name"], value=p["id"]) for p in all_players]
        self.add_item(MVPSelect(match_id, options, self.votes, all_players))


class MVPSelect(discord.ui.Select):
    def __init__(self, match_id: str, options: list, votes: dict, all_players: list):
        super().__init__(placeholder="Vote pour le MVP...", custom_id=f"mvp_{match_id}", options=options)
        self.match_id = match_id
        self.votes = votes
        self.all_players = all_players

    async def callback(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        match = active_matches.get(self.match_id)
        all_ids = [p["id"] for p in self.all_players]
        if uid not in all_ids:
            await interaction.response.send_message("❌ Tu ne fais pas partie de ce match.", ephemeral=True)
            return
        if self.values[0] == uid:
            await interaction.response.send_message("❌ Tu ne peux pas voter pour toi-même !", ephemeral=True)
            return
        self.votes[uid] = self.values[0]
        await interaction.response.send_message("✅ Vote MVP enregistré !", ephemeral=True)

        if len(self.votes) >= len(all_ids) - 1:
            from collections import Counter
            count = Counter(self.votes.values())
            mvp_id, _ = count.most_common(1)[0]
            mvp_player = next((p for p in self.all_players if p["id"] == mvp_id), None)
            if mvp_player:
                conn = get_db()
                conn.execute("UPDATE players SET mvp_count = mvp_count + 1 WHERE discord_id = ?", (mvp_id,))
                conn.execute("UPDATE matches SET mvp = ? WHERE match_id = ?", (mvp_id, self.match_id))
                conn.commit()
                conn.close()
                await interaction.channel.send(f"🌟 **MVP du match : {mvp_player['name']}** ! Félicitations ! 🎉")


class ScoreModal(discord.ui.Modal, title="Entrer le score du match"):
    def __init__(self, match_id: str, winner: int):
        super().__init__()
        self.match_id = match_id
        self.winner = winner

    score_winner = discord.ui.TextInput(
        label="Rounds gagnés par l'équipe gagnante",
        placeholder="Ex: 13",
        min_length=1, max_length=2
    )
    score_loser = discord.ui.TextInput(
        label="Rounds gagnés par l'équipe perdante",
        placeholder="Ex: 7",
        min_length=1, max_length=2
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            sw = int(self.score_winner.value)
            sl = int(self.score_loser.value)
            if sw < 1 or sl < 0 or sw > 25 or sl > 25:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Score invalide (ex: 13 et 7).", ephemeral=True)
            return
        await finalize_match(interaction, self.match_id, self.winner, score_winner=sw, score_loser=sl)


class MatchResultView(discord.ui.View):
    def __init__(self, match_id: str):
        super().__init__(timeout=None)
        self.match_id = match_id

    def is_coach_or_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        coach_role = discord.utils.get(interaction.guild.roles, name="Coach")
        return coach_role in interaction.user.roles if coach_role else False

    async def check_screenshot(self, interaction: discord.Interaction) -> bool:
        """Vérifie qu'au moins une image a été postée dans le channel scoreboard."""
        match = active_matches.get(self.match_id)
        if not match:
            return False
        sb_channel_id = match.get("scoreboard_channel")
        if not sb_channel_id:
            return True  # Pas de channel scoreboard = pas de vérification
        sb_channel = interaction.guild.get_channel(sb_channel_id)
        if not sb_channel:
            return True
        async for msg in sb_channel.history(limit=20):
            if msg.attachments:
                for att in msg.attachments:
                    if any(att.filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]):
                        return True
        return False

    @discord.ui.button(label="🏆 Team 1 a gagné", style=discord.ButtonStyle.success, custom_id="win_team1")
    async def team1_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.is_coach_or_admin(interaction):
            await interaction.response.send_message("❌ Seul un **Admin** ou **Coach** peut valider le résultat.", ephemeral=True)
            return
        has_screenshot = await self.check_screenshot(interaction)
        if not has_screenshot:
            match = active_matches.get(self.match_id)
            sb_id = match.get("scoreboard_channel") if match else None
            sb_mention = f"<#{sb_id}>" if sb_id else "le channel scoreboard"
            await interaction.response.send_message(
                f"❌ **Aucun screenshot posté !**\nLes joueurs doivent d'abord poster le scoreboard dans {sb_mention} avant de pouvoir valider.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(ScoreModal(self.match_id, 1))

    @discord.ui.button(label="🏆 Team 2 a gagné", style=discord.ButtonStyle.success, custom_id="win_team2")
    async def team2_win(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.is_coach_or_admin(interaction):
            await interaction.response.send_message("❌ Seul un **Admin** ou **Coach** peut valider le résultat.", ephemeral=True)
            return
        has_screenshot = await self.check_screenshot(interaction)
        if not has_screenshot:
            match = active_matches.get(self.match_id)
            sb_id = match.get("scoreboard_channel") if match else None
            sb_mention = f"<#{sb_id}>" if sb_id else "le channel scoreboard"
            await interaction.response.send_message(
                f"❌ **Aucun screenshot posté !**\nLes joueurs doivent d'abord poster le scoreboard dans {sb_mention} avant de pouvoir valider.",
                ephemeral=True
            )
            return
        await interaction.response.send_modal(ScoreModal(self.match_id, 2))


class ReportView(discord.ui.View):
    def __init__(self, match_id: str, players: list[dict]):
        super().__init__(timeout=300)
        options = [discord.SelectOption(label=p["name"], value=p["id"]) for p in players]
        self.add_item(ReportSelect(match_id, options))


class ReportSelect(discord.ui.Select):
    def __init__(self, match_id: str, options: list):
        super().__init__(placeholder="Sélectionne le joueur à reporter...", custom_id=f"report_{match_id}", options=options)
        self.match_id = match_id

    async def callback(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        reported = self.values[0]
        if reported == uid:
            await interaction.response.send_message("❌ Tu ne peux pas te reporter toi-même.", ephemeral=True)
            return
        conn = get_db()
        existing = conn.execute(
            "SELECT id FROM reports WHERE reporter_id = ? AND reported_id = ? AND match_id = ?",
            (uid, reported, self.match_id)
        ).fetchone()
        if existing:
            conn.close()
            await interaction.response.send_message("⚠️ Tu as déjà reporté ce joueur pour ce match.", ephemeral=True)
            return
        conn.execute("INSERT INTO reports (reporter_id, reported_id, match_id) VALUES (?, ?, ?)", (uid, reported, self.match_id))
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM reports WHERE reported_id = ? AND created_at > datetime('now', '-7 days')",
            (reported,)
        ).fetchone()["cnt"]
        conn.close()
        await interaction.response.send_message("✅ Report envoyé.", ephemeral=True)

        if count >= REPORT_THRESHOLD:
            guild = interaction.guild
            for ch in guild.text_channels:
                if any(x in ch.name.lower() for x in ["admin", "staff", "mod"]):
                    await ch.send(f"⚠️ **Alert Report** : <@{reported}> a reçu **{count} reports** en 7 jours. Match : `{self.match_id}`")
                    break


class AdminMatchView(discord.ui.View):
    def __init__(self, match_id: str):
        super().__init__(timeout=None)
        self.match_id = match_id

    @discord.ui.button(label="⚙️ Forcer Team 1", style=discord.ButtonStyle.secondary, custom_id="admin_t1")
    async def force_t1(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
            return
        await finalize_match(interaction, self.match_id, 1, forced=True)

    @discord.ui.button(label="⚙️ Forcer Team 2", style=discord.ButtonStyle.secondary, custom_id="admin_t2")
    async def force_t2(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
            return
        await finalize_match(interaction, self.match_id, 2, forced=True)

    @discord.ui.button(label="🚫 Annuler", style=discord.ButtonStyle.danger, custom_id="admin_cancel")
    async def cancel_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
            return
        await cancel_match_logic(interaction, self.match_id)

# ─────────────────────────────────────────────
#  MATCH LOGIC
# ─────────────────────────────────────────────
async def update_queue_message(interaction: discord.Interaction = None, guild: discord.Guild = None):
    g = guild or (interaction.guild if interaction else None)
    if not g:
        return
    # Mettre à jour tous les messages de queue enregistrés
    for qid, ref in queue_message_refs.items():
        if ref.get("message_id"):
            try:
                ch = g.get_channel(ref["channel_id"])
                if ch:
                    msg = await ch.fetch_message(ref["message_id"])
                    await msg.edit(embed=build_queue_embed())
            except Exception:
                pass
    try:
        await update_personal_queue_embeds(g)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  MAP VETO
# ─────────────────────────────────────────────
class MapVetoView(discord.ui.View):
    def __init__(self, match_id: str, maps: list, cap1: dict, cap2: dict, on_complete):
        super().__init__(timeout=300)
        self.match_id = match_id
        self.remaining = list(maps)
        self.cap1_id = cap1["id"]
        self.cap2_id = cap2["id"]
        self.cap1_name = cap1["name"]
        self.cap2_name = cap2["name"]
        self.turn = 0
        self.on_complete = on_complete
        self._rebuild_buttons()

    def current_captain_id(self):
        return self.cap1_id if self.turn % 2 == 0 else self.cap2_id

    def current_captain_name(self):
        return self.cap1_name if self.turn % 2 == 0 else self.cap2_name

    def _rebuild_buttons(self):
        self.clear_items()
        for i, m in enumerate(self.remaining):
            btn = discord.ui.Button(
                label=f"❌ Ban {m}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"veto_{self.match_id}_{m}",
                row=i // 4
            )
            btn.callback = self._make_ban_callback(m)
            self.add_item(btn)

    def _make_ban_callback(self, map_name: str):
        async def callback(interaction: discord.Interaction):
            uid = str(interaction.user.id)
            if uid != self.current_captain_id():
                cap = self.current_captain_name()
                await interaction.response.send_message(
                    f"❌ C'est au tour de **{cap}** de banner une map.", ephemeral=True
                )
                return
            self.remaining.remove(map_name)
            self.turn += 1
            if len(self.remaining) == 1:
                chosen = self.remaining[0]
                self.stop()
                embed = discord.Embed(
                    title="🗺️ Map sélectionnée",
                    description=f"**{chosen}**\n\nLe veto est terminé. Bonne chance !",
                    color=0xff4655
                )
                embed.set_footer(text=f"Map bannée par {interaction.user.display_name} : {map_name}")
                await interaction.response.edit_message(embed=embed, view=None)
                await self.on_complete(chosen)
            else:
                next_cap = self.current_captain_name()
                team_label = "🔴 Team 1" if self.turn % 2 == 0 else "🔵 Team 2"
                embed = discord.Embed(
                    title="🗺️ Veto de Map",
                    description=(
                        f"**{interaction.user.display_name}** a banni **{map_name}**\n\n"
                        f"Maps restantes : {len(self.remaining)}\n"
                        f"Au tour de **{team_label} — {next_cap}** de banner"
                    ),
                    color=0xff4655
                )
                embed.add_field(
                    name="Maps disponibles",
                    value=" • ".join(self.remaining),
                    inline=False
                )
                self._rebuild_buttons()
                await interaction.response.edit_message(embed=embed, view=self)
        return callback


async def start_match(interaction: discord.Interaction, queue_id: str = None, guild_override: discord.Guild = None):
    # Déterminer quelle queue utiliser
    if queue_id is None:
        # Legacy: prendre la première queue non vide
        queue_id = next((qid for qid, q in queues.items() if q), None)
        if not queue_id:
            return
    current_size = test_queue_size if test_mode else QUEUE_SIZE
    players = queues[queue_id][:current_size]
    queues[queue_id] = queues[queue_id][current_size:]

    # En mode test avec moins de 10 joueurs, on split simplement en 2
    if len(players) < QUEUE_SIZE:
        mid = len(players) // 2
        team1 = players[:mid]
        team2 = players[mid:]
    else:
        team1, team2 = balance_teams(players)
    match_id = f"match_{int(datetime.now(timezone.utc).timestamp())}"
    chosen_map = "🗺️ Veto en cours..."  # Sera défini par le veto
    season = get_current_season()

    q_info = QUEUES.get(queue_id, list(QUEUES.values())[0])

    def avg_elo(team):
        elos = [get_queue_elo(p["id"], queue_id)["elo"] for p in team]
        return round(sum(elos) / len(elos))
    elo_t1 = avg_elo(team1)
    elo_t2 = avg_elo(team2)

    active_matches[match_id] = {"team1": team1, "team2": team2, "votes": {}, "channels": [], "map": chosen_map, "queue_id": queue_id}

    conn = get_db()
    conn.execute(
        "INSERT INTO matches (match_id, team1, team2, map, status, season) VALUES (?, ?, ?, ?, 'active', ?)",
        (match_id, json.dumps([p["id"] for p in team1]), json.dumps([p["id"] for p in team2]), chosen_map, season)
    )
    conn.commit()
    conn.close()

    guild = guild_override or interaction.guild
    try:
        coach_role = discord.utils.get(guild.roles, name="Coach")

        # Catégorie visible par tout le monde
        cat = await guild.create_category(f"🎮 Match {match_id[-6:]}")

        # Vocaux simples — pas de permissions complexes, le bot a admin donc ça passe toujours
        vc1 = await guild.create_voice_channel("🔴 Team 1", category=cat)
        vc2 = await guild.create_voice_channel("🔵 Team 2", category=cat)
        vc_coach = await guild.create_voice_channel("🎙️ Coach", category=cat)

        # Channel notes coach — privé (coach + admin seulement)
        overwrites_coach = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if coach_role:
            overwrites_coach[coach_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        tc_coach = await guild.create_text_channel("📋 notes-coach", category=cat, overwrites=overwrites_coach)

        # Channel scoreboard — visible par les joueurs réels du match + staff
        overwrites_sb = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
        }
        if coach_role:
            overwrites_sb[coach_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True)
        for p in team1 + team2:
            if p.get("is_bot"):
                continue  # Ignorer les faux joueurs — pas de vrai membre Discord
            try:
                member = guild.get_member(int(p["id"]))
                if member:
                    overwrites_sb[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True)
            except Exception:
                pass
        tc_scoreboard = await guild.create_text_channel(f"📸 scoreboard-{match_id[-6:]}", category=cat, overwrites=overwrites_sb)

        active_matches[match_id]["channels"] = [cat.id, vc1.id, vc2.id, vc_coach.id, tc_coach.id, tc_scoreboard.id]
        active_matches[match_id]["scoreboard_channel"] = tc_scoreboard.id

        # Message de briefing dans le channel coach
        t1_names = ", ".join(p["name"] for p in team1)
        t2_names = ", ".join(p["name"] for p in team2)
        coach_embed = discord.Embed(title=f"📋 Briefing Coach — Match {match_id[-6:]}", color=0xffd700)
        coach_embed.add_field(name="🔴 Team 1", value=t1_names, inline=False)
        coach_embed.add_field(name="🔵 Team 2", value=t2_names, inline=False)
        coach_embed.add_field(name="🗺️ Map", value=chosen_map, inline=True)
        coach_embed.add_field(name="📊 Écart ELO", value=f"{abs(elo_t1-elo_t2)} pts", inline=True)
        coach_embed.add_field(name="ℹ️ Instructions", value="Utilisez ce channel pour vos notes de match. Validez le résultat avec les boutons dans le channel principal.", inline=False)
        await tc_coach.send(embed=coach_embed)

        # Message d'instructions dans le scoreboard
        # Composition des équipes pour la trace
        def team_trace(team, num):
            lines = []
            for p in team:
                role = p.get("role", "—")
                conn_tmp = get_db()
                prow = conn_tmp.execute("SELECT elo, wins, losses FROM players WHERE discord_id=?", (p["id"],)).fetchone()
                conn_tmp.close()
                elo = prow["elo"] if prow else 1000
                w = prow["wins"] if prow else 0
                l = prow["losses"] if prow else 0
                rname, ricon, _ = get_rank(elo)
                in_place = is_in_placement(w, l)
                elo_str = f"ELO: {elo} {ricon}" if not in_place else "🔰 Placement"
                # Afficher mention pour vrais joueurs, nom bold pour bots
                name_str = f"**{p['name']}** 🤖" if p.get("is_bot") else f"<@{p['id']}>"
                lines.append(f"• {name_str} `{role}` — {elo_str}")
            return "\n".join(lines)

        sb_embed = discord.Embed(
            title=f"📸 Salon du Match — {match_id[-6:]}",
            description="Ce salon contient tout le suivi de votre match.\n\n**Une fois la partie terminée :** postez le screenshot du scoreboard ici, puis le staff valide le résultat avec les boutons ci-dessous.",
            color=0xff4655,
            timestamp=datetime.now(timezone.utc)
        )
        sb_embed.add_field(
            name=f"🔴 Team 1 (ELO moy. {elo_t1})",
            value=team_trace(team1, 1),
            inline=True
        )
        sb_embed.add_field(
            name=f"🔵 Team 2 (ELO moy. {elo_t2})",
            value=team_trace(team2, 2),
            inline=True
        )
        sb_embed.add_field(name="🗺️ Map", value=f"**{chosen_map}**", inline=False)
        sb_embed.add_field(
            name="📋 Comment poster le scoreboard",
            value="1. Fin de partie → Tab pour afficher le scoreboard\n2. Screenshot (Win+Shift+S ou F12 Steam)\n3. Colle l'image ici (Ctrl+V ou glisse le fichier)",
            inline=False
        )
        sb_embed.set_footer(text=f"Saison {season} • Match {match_id} • {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC")
        # ── LANCEMENT DU VETO ────────────────────────────────────────────────────────────────
        def get_captain(team):
            return max(team, key=lambda p: get_queue_elo(p["id"], queue_id)["elo"])
        cap1 = get_captain(team1)
        cap2 = get_captain(team2)
        veto_embed = discord.Embed(
            title="🗺️ Veto de Map",
            description=(
                "Le veto commence !\n\n"
                f"**Capitaine Team 1** : {cap1['name']}\n"
                f"**Capitaine Team 2** : {cap2['name']}\n\n"
                "Bannissez les maps à tour de rôle jusqu'à ce qu'il en reste une.\n"
                f"**{cap1['name']}** commence !"
            ),
            color=0xff4655
        )
        veto_embed.add_field(name="Maps disponibles", value=" • ".join(VALORANT_MAPS), inline=False)

        async def on_veto_complete(final_map: str):
            active_matches[match_id]["map"] = final_map
            conn2 = get_db()
            conn2.execute("UPDATE matches SET map=? WHERE match_id=?", (final_map, match_id))
            conn2.commit()
            conn2.close()
            try:
                coach_ch = guild.get_channel(active_matches[match_id]["channels"][3])
                if coach_ch:
                    await coach_ch.send(f"🗺️ Map sélectionnée par veto : **{final_map}**")
            except Exception:
                pass
            for p in team1 + team2:
                if p.get("is_bot"):
                    continue
                try:
                    pch = get_player_channels(p["id"])
                    if pch and pch["notifs_channel"]:
                        notif_ch = guild.get_channel(pch["notifs_channel"])
                        if notif_ch:
                            team_num = 1 if p in team1 else 2
                            await notif_ch.send(embed=discord.Embed(
                                title="🗺️ Map sélectionnée",
                                description=f"**{final_map}** — Team {team_num}\nBonne chance !",
                                color=0xff4655
                            ))
                except Exception:
                    pass

        # Si les deux capitaines sont des bots → veto automatique (mode test)
        both_bots = cap1.get("is_bot") or cap2.get("is_bot")
        if both_bots:
            final_map = random.choice(VALORANT_MAPS)
            auto_embed = discord.Embed(
                title="🗺️ Map sélectionnée (auto)",
                description=f"**{final_map}**\n\n*Veto automatique — mode test*",
                color=0xff4655
            )
            await tc_scoreboard.send(embed=auto_embed)
            await on_veto_complete(final_map)
        else:
            veto_view = MapVetoView(match_id, VALORANT_MAPS, cap1, cap2, on_veto_complete)
            await tc_scoreboard.send(embed=veto_embed, view=veto_view)
        await tc_scoreboard.send(embed=sb_embed)

    except Exception as e:
        print(f"Erreur channels: {e}")

    def team_display(team):
        return "\n".join(f"• **{p['name']}** `{p.get('role','—')}`" for p in team)

    embed = discord.Embed(title=f"⚔️ Match Trouvé ! — {match_id[-6:]}", color=0xff4655, timestamp=datetime.now(timezone.utc))
    embed.add_field(name=f"🔴 Team 1 (ELO moy. {elo_t1})", value=team_display(team1), inline=True)
    embed.add_field(name=f"🔵 Team 2 (ELO moy. {elo_t2})", value=team_display(team2), inline=True)
    embed.add_field(name="🗺️ Map", value=f"**{chosen_map}**", inline=False)
    embed.add_field(name="📊 Équité", value=f"Écart ELO : **{abs(elo_t1-elo_t2)}** pts", inline=False)
    sb_id = active_matches[match_id].get("scoreboard_channel")
    sb_mention = f"<#{sb_id}>" if sb_id else "le channel scoreboard"
    embed.add_field(
        name="📋 Instructions",
        value=f"1. Jouez votre partie sur Valorant\n2. Postez le scoreboard dans {sb_mention}\n3. Le staff valide le résultat",
        inline=False
    )

    # Analyse des compositions
    comp1 = build_composition_field(team1, 1)
    comp2 = build_composition_field(team2, 2)
    if comp1:
        embed.add_field(name=comp1[0], value=comp1[1], inline=False)
    if comp2:
        embed.add_field(name=comp2[0], value=comp2[1], inline=False)
    if not comp1 and not comp2:
        embed.add_field(name="✅ Compositions", value="Les deux équipes ont des compositions équilibrées !", inline=False)

    embed.set_footer(text=f"Saison {season} • {match_id}")

    # DM notification
    for p in team1 + team2:
        try:
            member = guild.get_member(int(p["id"]))
            if member:
                team_num = 1 if p in team1 else 2
                dm = discord.Embed(title="🎮 Match trouvé !", description=f"Tu es en **Team {team_num}**\nMap : **{chosen_map}**\nRejoin le vocal de ton équipe !", color=0xff4655)
                await member.send(embed=dm)
        except Exception:
            pass

    mentions = " ".join(f"<@{p['id']}>" if not p.get("is_bot") else f"**{p['name']}**" for p in team1 + team2)

    # Envoyer le recap + boutons dans le scoreboard, pas dans la queue
    if active_matches[match_id].get("scoreboard_channel"):
        sc = guild.get_channel(active_matches[match_id]["scoreboard_channel"])
        if sc:
            await sc.send(content=f"🎮 **Match lancé !** {mentions}", embed=embed, view=MatchResultView(match_id))
            await sc.send("**🛠️ Contrôles Admin :**", view=AdminMatchView(match_id))
    else:
        # Fallback sur le channel queue si pas de scoreboard
        await interaction.channel.send(content=f"🎮 **Match lancé !** {mentions}", embed=embed, view=MatchResultView(match_id))
        await interaction.channel.send("**🛠️ Contrôles Admin :**", view=AdminMatchView(match_id))

    await update_queue_message(guild=guild)


async def finalize_match(interaction: discord.Interaction, match_id: str, winner: int, forced: bool = False, score_winner: int = 13, score_loser: int = 0):
    match = active_matches.get(match_id)
    if not match:
        return

    winners = match[f"team{winner}"]
    losers  = match[f"team{2 if winner == 1 else 1}"]
    guild   = interaction.guild

    conn = get_db()
    elo_changes = {}

    def get_elo(pid):
        row = conn.execute("SELECT elo FROM players WHERE discord_id = ?", (pid,)).fetchone()
        return row["elo"] if row else 1000

    queue_id = match.get("queue_id", list(QUEUES.keys())[0])
    avg_w = sum(get_queue_elo(p["id"], queue_id)["elo"] for p in winners) / 5
    avg_l = sum(get_queue_elo(p["id"], queue_id)["elo"] for p in losers) / 5

    # Multiplicateur de score : 13-0 = 1.3x, 13-12 = 0.85x, 13-7 = 1.0x (baseline)
    total_rounds = score_winner + score_loser
    if total_rounds > 0:
        dominance = score_winner / total_rounds   # 0.5 à 1.0
        # Normalise autour de 1.0 : 13-7 (0.65) = 1.0x, 13-0 (1.0) = 1.3x, 13-12 (0.52) = 0.85x
        score_multiplier = round(0.5 + dominance, 2)
        score_multiplier = max(0.75, min(1.35, score_multiplier))
    else:
        score_multiplier = 1.0

    score_label = f"{score_winner}-{score_loser}"

    for p in winners:
        old_elo = get_elo(p["id"])
        prow = get_queue_elo(p["id"], queue_id)
        k = get_k_factor(prow["wins"], prow["losses"])
        base_gain, _ = calc_elo_change(int(avg_w), int(avg_l), k)
        gain = round(base_gain * score_multiplier)
        new_elo = old_elo + gain
        new_streak = prow["streak"] + 1
        new_best = max(prow["best_streak"], new_streak)
        total_after = prow["wins"] + 1 + prow["losses"]
        placement_done = 1 if total_after >= PLACEMENT_MATCHES else 0
        update_queue_elo(p["id"], queue_id, conn=conn, elo=new_elo, wins=prow["wins"]+1,
                         streak=new_streak, best_streak=new_best, placement_done=placement_done)
        # Aussi mettre à jour l'ELO global pour le leaderboard/rang
        conn.execute("UPDATE players SET elo=?, wins=wins+1 WHERE discord_id=?", (new_elo, p["id"]))
        in_place = is_in_placement(prow["wins"], prow["losses"])
        elo_changes[p["id"]] = {"old": old_elo, "new": new_elo, "change": f"+{gain}", "placement": in_place, "k": k, "streak": new_streak, "new_best": new_streak == new_best and new_streak >= 3}

    for p in losers:
        old_elo = get_elo(p["id"])
        prow = get_queue_elo(p["id"], queue_id)
        k = get_k_factor(prow["wins"], prow["losses"])
        _, base_loss = calc_elo_change(int(avg_w), int(avg_l), k)
        loss = round(base_loss * score_multiplier)
        new_elo = max(100, old_elo - loss)
        total_after = prow["wins"] + prow["losses"] + 1
        placement_done = 1 if total_after >= PLACEMENT_MATCHES else 0
        update_queue_elo(p["id"], queue_id, conn=conn, elo=new_elo, losses=prow["losses"]+1,
                         streak=0, placement_done=placement_done)
        conn.execute("UPDATE players SET elo=?, losses=losses+1, streak=0 WHERE discord_id=?", (new_elo, p["id"]))
        in_place = is_in_placement(prow["wins"], prow["losses"])
        elo_changes[p["id"]] = {"old": old_elo, "new": new_elo, "change": f"-{loss}", "placement": in_place, "k": k}

    conn.execute(
        "UPDATE matches SET winner=?, elo_changes=?, status='finished', ended_at=datetime('now') WHERE match_id=?",
        (winner, json.dumps(elo_changes), match_id)
    )
    conn.commit()
    conn.close()

    # Sync rôles + update espaces privés
    for p in winners + losers:
        if p.get("is_bot"):
            continue
        member = guild.get_member(int(p["id"]))
        if member:
            ch = elo_changes[p["id"]]
            await sync_rank_role(guild, member, ch["new"])
            await update_player_profil(guild, p["id"])
            await update_player_history(guild, p["id"], match_id, p in winners, ch, match.get("map", "?"))

    # Notif match terminé dans les channels personnels
    for p in winners + losers:
        if p.get("is_bot"):
            continue
        ch = elo_changes[p["id"]]
        won = p in winners
        rname, ricon, _ = get_rank(ch["new"])
        notif = discord.Embed(
            title=f"{'✅ Victoire !' if won else '❌ Défaite'}",
            description=f"Match **{match_id[-6:]}** terminé sur **{match.get('map','?')}**",
            color=0x00ff88 if won else 0xff4444,
            timestamp=datetime.now(timezone.utc)
        )
        notif.add_field(name="ELO", value=f"{ch['old']} ➜ **{ch['new']}** (`{ch['change']}`)", inline=True)
        notif.add_field(name="Rang", value=f"{ricon} {rname}", inline=True)
        await notify_player(guild, p["id"], notif)

    def result_lines(team):
        lines = []
        for p in team:
            ch = elo_changes[p["id"]]
            rname, ricon, _ = get_rank(ch["new"])
            placement_tag = " 🔰" if ch.get("placement") else ""
            k_tag = f" *(K={ch.get('k', K_FACTOR)})*" if ch.get("placement") else ""
            lines.append(f"• **{p['name']}** {ch['old']} ➜ **{ch['new']}** (`{ch['change']}`){k_tag} {ricon}{placement_tag}")
        return "\n".join(lines)

    embed = discord.Embed(title=f"🏆 Résultat — Match {match_id[-6:]}", color=0x00ff88, timestamp=datetime.now(timezone.utc))
    embed.add_field(name=f"🥇 Team {winner} — Victoire", value=result_lines(winners), inline=False)
    embed.add_field(name=f"💀 Team {2 if winner==1 else 1} — Défaite", value=result_lines(losers), inline=False)
    embed.add_field(name="🗺️ Map", value=match.get("map", "?"), inline=True)
    embed.add_field(name="🎯 Score", value=f"**{score_label}**", inline=True)
    embed.add_field(name="📊 Modificateur", value=f"x**{score_multiplier}**", inline=True)
    embed.add_field(name="📅 Saison", value=str(get_current_season()), inline=True)
    if forced:
        embed.set_footer(text=f"Forcé par un admin • {match_id}")
    else:
        embed.set_footer(text=f"Validé par coach/admin • {match_id}")

    # Récupérer le channel scoreboard pour y envoyer tous les résultats
    sb_channel_id = match.get("scoreboard_channel")
    result_channel = guild.get_channel(sb_channel_id) if sb_channel_id else interaction.channel

    await result_channel.send(embed=embed)

    # Annonces de streaks
    streak_msgs = []
    for p in winners:
        ch = elo_changes[p["id"]]
        streak = ch.get("streak", 0)
        is_new_best = ch.get("new_best", False)
        if streak == 3:
            streak_msgs.append(f"🔥 **{p['name']}** est en feu ! **3 victoires** d'affilée !")
        elif streak == 5:
            streak_msgs.append(f"🔥🔥 **{p['name']}** est UNSTOPPABLE ! **5 victoires** d'affilée !")
        elif streak >= 7 and is_new_best:
            streak_msgs.append(f"👑 **{p['name']}** est LÉGENDAIRE ! **{streak} victoires** d'affilée — nouveau record personnel !")
    if streak_msgs:
        streak_embed = discord.Embed(
            title="🔥 Hot Streak !",
            description="\n".join(streak_msgs),
            color=0xff6b00
        )
        await result_channel.send(embed=streak_embed)

    # Annonce de promotion de rang
    rank_up_msgs = []
    for p in winners + losers:
        ch = elo_changes[p["id"]]
        if not ch.get("placement"):
            old_rank = get_rank(ch["old"])[0]
            new_rank = get_rank(ch["new"])[0]
            if old_rank != new_rank:
                new_rname, new_ricon, _ = get_rank(ch["new"])
                if ch["new"] > ch["old"]:
                    rank_up_msgs.append(f"⬆️ **{p['name']}** monte en **{new_ricon} {new_rname}** !")
                else:
                    rank_up_msgs.append(f"⬇️ **{p['name']}** descend en **{new_ricon} {new_rname}**.")
    if rank_up_msgs:
        rank_embed = discord.Embed(
            title="📈 Changements de rang",
            description="\n".join(rank_up_msgs),
            color=0x00ff88
        )
        await result_channel.send(embed=rank_embed)

    # Archiver le scoreboard (read-only pour tous)
    sb_channel_id = match.get("scoreboard_channel")
    if sb_channel_id:
        sb_channel = guild.get_channel(sb_channel_id)
        if sb_channel:
            try:
                # Rendre le channel read-only pour archivage
                await sb_channel.set_permissions(guild.default_role, view_channel=False)
                for p in winners + losers:
                    member = guild.get_member(int(p["id"]))
                    if member:
                        await sb_channel.set_permissions(member, view_channel=True, send_messages=False)
                archive_embed = discord.Embed(
                    title="🔒 Match Archivé",
                    description=f"Ce channel est maintenant archivé. Score final : **{score_label}**",
                    color=0x555555
                )
                await sb_channel.send(embed=archive_embed)
            except Exception as e:
                print(f"Erreur archivage: {e}")

    all_players = winners + losers
    mvp_embed = discord.Embed(title="🌟 Vote MVP !", description="5 minutes pour voter. Tu ne peux pas voter pour toi-même.", color=0xffd700)
    await result_channel.send(embed=mvp_embed, view=MVPVoteView(match_id, all_players))

    report_embed = discord.Embed(title="🚨 Reporter un joueur ?", description="Comportement toxique ? Signale-le.", color=0xff4444)
    await result_channel.send(embed=report_embed, view=ReportView(match_id, all_players))

    await update_leaderboard(guild)
    del active_matches[match_id]

    # Mettre à jour les embeds personnels immédiatement
    try:
        await update_personal_queue_embeds(guild)
    except Exception as e:
        print(f"Erreur update personal embeds after match: {e}")

    # Supprimer les channels après 10 min (laisse le temps pour MVP + reports)
    await result_channel.send(
        embed=discord.Embed(
            title="⏳ Salon archivé dans 10 minutes",
            description="Tu as **10 minutes** pour voter le MVP et reporter un joueur si besoin.\nAprès ça, le salon sera automatiquement supprimé.",
            color=0x555555
        )
    )
    async def delayed_cleanup():
        await asyncio.sleep(600)  # 10 minutes
        await cleanup_channels(guild, match)
    asyncio.create_task(delayed_cleanup())


async def cancel_match_logic(interaction: discord.Interaction, match_id: str):
    match = active_matches.get(match_id)
    if not match:
        await interaction.response.send_message("❌ Match introuvable.", ephemeral=True)
        return
    conn = get_db()
    conn.execute("UPDATE matches SET status='cancelled' WHERE match_id=?", (match_id,))
    conn.commit()
    conn.close()
    embed = discord.Embed(title=f"🚫 Match Annulé — {match_id[-6:]}", description="Annulé par un admin. Aucun ELO modifié.", color=0xff0000)
    sb_id = match.get("scoreboard_channel")
    if sb_id:
        sc = interaction.guild.get_channel(sb_id)
        if sc:
            await sc.send(embed=embed)
    await interaction.response.send_message("✅ Match annulé.", ephemeral=True)
    await cleanup_channels(interaction.guild, match)
    del active_matches[match_id]

    # Mettre à jour les embeds personnels (statut match en cours -> disponible)
    try:
        await update_personal_queue_embeds(interaction.guild)
    except Exception as e:
        print(f"Erreur update personal embeds after match: {e}")


async def cleanup_channels(guild: discord.Guild, match: dict):
    for ch_id in match.get("channels", []):
        try:
            ch = guild.get_channel(ch_id)
            if ch:
                await ch.delete()
        except Exception:
            pass

# ─────────────────────────────────────────────
#  LEADERBOARD AUTO
# ─────────────────────────────────────────────
def build_leaderboard_embed(queue_id: str, season: int) -> discord.Embed:
    """Construit l'embed leaderboard pour une queue spécifique."""
    q_info = QUEUES[queue_id]
    conn = get_db()
    rows = conn.execute(
        """SELECT p.username, pqe.elo, pqe.wins, pqe.losses, pqe.streak, pqe.mvp_count, pqe.placement_done
           FROM player_queue_elo pqe
           JOIN players p ON p.discord_id = pqe.discord_id
           WHERE pqe.queue_id = ? AND pqe.season = ?
           ORDER BY pqe.elo DESC LIMIT 20""",
        (queue_id, season)
    ).fetchall()
    conn.close()

    embed = discord.Embed(
        title=f"{q_info['emoji']} Leaderboard {q_info['name']} — Saison {season}",
        color=q_info["color"],
        timestamp=datetime.now(timezone.utc)
    )
    lines = []
    real_rank = 0
    for row in rows:
        total = row["wins"] + row["losses"]
        wr = round(row["wins"] / total * 100) if total > 0 else 0
        streak_str = f" 🔥{row['streak']}" if row["streak"] >= 3 else ""
        mvp_str = f" 🌟{row['mvp_count']}" if row["mvp_count"] > 0 else ""
        in_place = is_in_placement(row["wins"], row["losses"])
        if in_place:
            lines.append(f"🔰 **{row['username']}** Placement ({total}/{PLACEMENT_MATCHES}){mvp_str}")
        else:
            real_rank += 1
            rname, ricon, _ = get_rank(row["elo"])
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(real_rank, f"`{real_rank:>2}.`")
            lines.append(f"{medal} **{row['username']}** {ricon} **{row['elo']}** | {row['wins']}W/{row['losses']}L ({wr}%){streak_str}{mvp_str}")

    embed.description = "\n".join(lines) if lines else "*Aucun joueur dans cette queue.*"
    embed.set_footer(text="🔰 = en placement • 🔥 = streak ≥3 • 🌟 = MVP • Mis à jour après chaque match")
    return embed


async def update_leaderboard(guild: discord.Guild):
    """Met à jour les 3 leaderboards (un par queue)."""
    season = get_current_season()
    for queue_id, q_info in QUEUES.items():
        ch_name = f"leaderboard-{queue_id}"
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if not ch:
            # Essayer aussi l'ancien "leaderboard" pour compat
            if queue_id == list(QUEUES.keys())[0]:
                ch = discord.utils.get(guild.text_channels, name="leaderboard")
            if not ch:
                continue
        embed = build_leaderboard_embed(queue_id, season)
        async for msg in ch.history(limit=5):
            if msg.author == guild.me:
                await msg.edit(embed=embed)
                break
        else:
            await ch.send(embed=embed)

# ─────────────────────────────────────────────
#  SLASH COMMANDS
# ─────────────────────────────────────────────
@tree.command(name="register", description="S'inscrire au système in-house")
async def register(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    conn = get_db()
    existing = conn.execute("SELECT discord_id FROM players WHERE discord_id=?", (uid,)).fetchone()
    if existing:
        conn.close()
        await interaction.response.send_message("⚠️ Tu es déjà inscrit !", ephemeral=True)
        return
    conn.execute("INSERT INTO players (discord_id, username) VALUES (?, ?)", (uid, interaction.user.display_name))
    conn.commit()
    conn.close()
    await sync_rank_role(interaction.guild, interaction.user, 1000)

    # Créer l'espace privé si pas encore fait (au cas où on_member_join a raté)
    await create_player_space(interaction.guild, interaction.user)

    # Mettre à jour le profil dans l'espace privé
    await update_player_profil(interaction.guild, uid)

    embed = discord.Embed(title="✅ Inscription réussie !", description=f"Bienvenue **{interaction.user.display_name}** ! Tu commences avec **1000 ELO** 🟫 Bronze.", color=0x00ff88)
    embed.add_field(name="Prochaine étape", value="Va dans ton salon privé `🎮︱queue` pour rejoindre la file d'attente !")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="setriot", description="Lier ton compte Riot/Valorant à ton profil")
@app_commands.describe(riot_id="Ton Riot ID (ex: Pseudo#TAG)")
async def set_riot(interaction: discord.Interaction, riot_id: str):
    if "#" not in riot_id:
        await interaction.response.send_message("❌ Format invalide ! Exemple : `MonPseudo#EUW`", ephemeral=True)
        return
    uid = str(interaction.user.id)
    conn = get_db()
    conn.execute("UPDATE players SET riot_id=? WHERE discord_id=?", (riot_id, uid))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Compte Riot lié : **{riot_id}**", ephemeral=True)


@tree.command(name="rank", description="Voir ses stats et son rang")
@app_commands.describe(user="Joueur à consulter (optionnel)")
async def rank_cmd(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    uid = str(target.id)
    conn = get_db()
    row = conn.execute("SELECT * FROM players WHERE discord_id=?", (uid,)).fetchone()
    pos = conn.execute("SELECT COUNT(*) as cnt FROM players WHERE elo > (SELECT elo FROM players WHERE discord_id=?)", (uid,)).fetchone()["cnt"] + 1
    total_players = conn.execute("SELECT COUNT(*) as cnt FROM players").fetchone()["cnt"]
    conn.close()

    if not row:
        await interaction.response.send_message(f"❌ {'Tu n\'es pas inscrit' if not user else f'{target.display_name} n\'est pas inscrit'}. Utilise `/register` !", ephemeral=True)
        return

    rname, ricon, color = get_rank(row["elo"])
    total = row["wins"] + row["losses"]
    wr = round(row["wins"] / total * 100) if total > 0 else 0
    in_placement = is_in_placement(row["wins"], row["losses"])
    place_progress = placement_progress(row["wins"], row["losses"])

    embed = discord.Embed(title=f"📊 Stats de {target.display_name}", color=0x5865f2 if in_placement else color)
    embed.set_thumbnail(url=target.display_avatar.url)

    if in_placement:
        embed.add_field(name="Rang",       value=f"🔰 **Placement** ({place_progress})", inline=True)
        embed.add_field(name="ELO",        value=f"*Masqué pendant les placements*",      inline=True)
        embed.add_field(name="​",     value="​",                                 inline=True)
    else:
        embed.add_field(name="Rang",       value=f"{ricon} **{rname}**",          inline=True)
        embed.add_field(name="ELO",        value=f"**{row['elo']}** pts",          inline=True)
        embed.add_field(name="Classement", value=f"**#{pos}** / {total_players}", inline=True)

    embed.add_field(name="Victoires",       value=f"**{row['wins']}**",            inline=True)
    embed.add_field(name="Défaites",        value=f"**{row['losses']}**",          inline=True)
    embed.add_field(name="Winrate",         value=f"**{wr}%**",                    inline=True)
    embed.add_field(name="Streak actuel",   value=f"🔥 **{row['streak']}**",       inline=True)
    embed.add_field(name="Meilleur streak", value=f"🏅 **{row['best_streak']}**",  inline=True)
    embed.add_field(name="MVP",             value=f"🌟 **{row['mvp_count']}**",    inline=True)
    if row["riot_id"]:
        embed.add_field(name="Compte Riot", value=f"`{row['riot_id']}`",           inline=False)

    if in_placement:
        embed.set_footer(text=f"🔰 Matchs de placement : {place_progress} • Saison {get_current_season()} • K={K_FACTOR_PLACEMENT}")
    else:
        embed.set_footer(text=f"Saison {get_current_season()}")
    await interaction.response.send_message(embed=embed)


@tree.command(name="fillchannels", description="[ADMIN] Remplir les salons règles, faq, annonces, candidatures")
async def fill_channels_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("\u274c Admin seulement.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    me = guild.me

    async def clear_and_post(ch, embeds):
        async for msg in ch.history(limit=20):
            if msg.author == me:
                await msg.delete()
        for embed in embeds:
            await ch.send(embed=embed)

    # #regles
    ch_regles = next((c for c in guild.text_channels if "r-gles" in c.name or "gles" in c.name), None)
    if ch_regles:
        e1 = discord.Embed(title="\U0001f3af Crazy Inhouse \u2014 L'essentiel", color=0xff4655)
        e1.description = (
            "**Crazy Inhouse** est un espace de jeu comp\u00e9titif r\u00e9serv\u00e9 aux joueurs s\u00e9rieux, "
            "qu'ils visent le niveau professionnel ou qu'ils cherchent un environnement plus exigeant que la ranked classique.\n\n"
            "L'objectif est simple\u00a0: **jouer au meilleur niveau possible**, s'am\u00e9liorer, et \u00e9voluer dans un cadre structur\u00e9."
        )
        e2 = discord.Embed(title="\u2694\ufe0f R\u00e8gles de jeu", color=0xff4655)
        e2.description = (
            "**1. Fair-play absolu**\n"
            "Aucune toxicit\u00e9, aucun flame en jeu ou sur le serveur. On est l\u00e0 pour progresser, pas pour se tirer dans les pattes.\n\n"
            "**2. S\u00e9rieux et disponibilit\u00e9**\n"
            "Quand tu rejoins une queue, tu t'engages \u00e0 jouer le match jusqu'au bout. Les no-show r\u00e9p\u00e9t\u00e9s entra\u00eenent une sanction.\n\n"
            "**3. Respect des d\u00e9cisions**\n"
            "Les r\u00e9sultats sont valid\u00e9s par un coach ou un admin. Toute tentative de manipulation de score est bannable.\n\n"
            "**4. Communication**\n"
            "Utilise les vocaux d\u00e9di\u00e9s pendant les matchs. Comms claires, constructives, sans drama.\n\n"
            "**5. Int\u00e9grit\u00e9**\n"
            "Smurfing, boosting ou toute forme de triche = ban imm\u00e9diat et d\u00e9finitif."
        )
        e3 = discord.Embed(title="\U0001f3c6 Format des matchs", color=0x2f3136)
        e3.description = (
            "\u2022 **10 joueurs** \u2014 5v5 full team\n"
            "\u2022 **Draft de carte** au d\u00e9but de chaque match\n"
            "\u2022 **Validation du score** obligatoire via screenshot en fin de match\n"
            "\u2022 **MVP** vot\u00e9 par les joueurs apr\u00e8s chaque match\n"
            "\u2022 **ELO** mis \u00e0 jour automatiquement apr\u00e8s validation\n\n"
            "Les 10 premi\u00e8res parties sont des **parties de placement** \u2014 ton ELO r\u00e9el n'est pas affich\u00e9 pendant cette p\u00e9riode."
        )
        e4 = discord.Embed(title="\u26a0\ufe0f Sanctions", color=0xed4245)
        e4.description = (
            "**Abandon en cours de match** \u2192 cooldown 15 min + perte d'ELO\n"
            "**Toxicit\u00e9 r\u00e9p\u00e9t\u00e9e** \u2192 mute temporaire ou kick\n"
            "**Manipulation de score** \u2192 ban d\u00e9finitif\n"
            "**Smurfing / triche** \u2192 ban d\u00e9finitif\n\n"
            "*Les d\u00e9cisions des admins sont finales.*"
        )
        await clear_and_post(ch_regles, [e1, e2, e3, e4])

    # #faq
    ch_faq = next((c for c in guild.text_channels if "faq" in c.name), None)
    if ch_faq:
        ef = discord.Embed(title="\u2753 Questions fr\u00e9quentes", color=0xff4655)
        ef.add_field(name="Comment s'inscrire ?", value="Poste ta candidature dans \U0001f39f\ufe0f\ufe0f\ufe0e\ufe0f\u200b\u200b\u200bcandidat\u200bures. Un admin la validera. Une fois accept\u00e9, utilise `/register` dans ton salon priv\u00e9.", inline=False)
        ef.add_field(name="Comment rejoindre une queue ?", value="Va dans ton salon priv\u00e9 \U0001f3ae\ufe0f\u200b\u200bqueue et clique sur ton r\u00f4le. D\u00e8s que 10 joueurs sont pr\u00eats, le match se lance automatiquement.", inline=False)
        ef.add_field(name="C'est quoi les diff\u00e9rentes queues ?", value="\U0001f451 **Radiant / Immo3** \u2014 niveau pro / ex-pro / top radiant\n\U0001f48e **Ascendant / Immo** \u2014 niveau haut pour un cadre plus s\u00e9rieux\n\U0001f338 **Game Changers** \u2014 r\u00e9serv\u00e9e aux joueuses", inline=False)
        ef.add_field(name="Comment est calcul\u00e9 l'ELO ?", value="Base 1000 pts. +/- selon r\u00e9sultat, ELO moyen des \u00e9quipes et MVP. Les 10 premi\u00e8res parties sont des placements (ELO masqu\u00e9).", inline=False)
        ef.add_field(name="Score contest\u00e9 ?", value="Contacte un admin dans le salon de match. Le screenshot du scoreboard fait foi.", inline=False)
        ef.add_field(name="Abandon involontaire ?", value="Contacte un admin. Les abandons involontaires (crash, urgence) peuvent \u00eatre excus\u00e9s si signal\u00e9s rapidement.", inline=False)
        ef.add_field(name="Voir mes stats ?", value="Ton salon priv\u00e9 \U0001f4ca\ufe0f\u200b\u200bprofil est mis \u00e0 jour apr\u00e8s chaque match. Tu peux aussi utiliser `/stats`.", inline=False)
        await clear_and_post(ch_faq, [ef])

    # #annonces
    ch_ann = next((c for c in guild.text_channels if "annonces" in c.name), None)
    if ch_ann:
        ea = discord.Embed(title="\U0001f4e2 Bienvenue sur Crazy Inhouse", color=0xff4655, timestamp=datetime.now(timezone.utc))
        ea.description = (
            "Le serveur est **officiellement ouvert**.\n\n"
            "Crazy Inhouse est un projet pens\u00e9 pour donner aux joueurs ambitieux un espace de travail s\u00e9rieux \u2014 "
            "que tu vises le niveau pro ou que tu cherches simplement mieux que la ranked classique.\n\n"
            "**Les queues sont ouvertes.** Si tu n'es pas encore inscrit, passe par \U0001f39f\ufe0f\u200b\u200bcandidat\u200bures.\n\n"
            "Bonne chance \u00e0 tous. \U0001f3af"
        )
        ea.set_footer(text="Crazy Inhouse \u2022 Saison 1")
        await clear_and_post(ch_ann, [ea])

    # #candidatures
    ch_cand = next((c for c in guild.text_channels if "candidatures" in c.name and "en-cours" not in c.name), None)
    if ch_cand:
        ec = discord.Embed(title="\U0001f3ae Rejoindre Crazy Inhouse", color=0xff4655)
        ec.description = (
            "**Crazy Inhouse** est un serveur de jeu comp\u00e9titif sur **candidature**.\n\n"
            "Notre priorit\u00e9 est de cr\u00e9er un **espace de travail s\u00e9rieux** pour des joueurs qui ont pour objectif "
            "d'atteindre le niveau professionnel \u2014 ou qui le sont d\u00e9j\u00e0.\n\n"
            "On accueille \u00e9galement les joueurs **Ascendant et bas Immortel** qui cherchent un environnement "
            "plus exigeant que la ranked classique pour continuer \u00e0 progresser s\u00e9rieusement.\n\n"
            "**Niveaux accept\u00e9s :**\n"
            "\U0001f451 Radiant / Immortel 3 \u2014 Queue Radiant\n"
            "\U0001f48e Ascendant / Immortel \u2014 Queue Ascendant\n"
            "\U0001f338 Game Changers \u2014 Queue d\u00e9di\u00e9e aux joueuses\n\n"
            "**Ce qu'on attend de toi :**\n"
            "\u2022 S\u00e9rieux et investissement dans le jeu\n"
            "\u2022 Fair-play et communication constructive\n"
            "\u2022 Disponibilit\u00e9 r\u00e9guli\u00e8re pour jouer\n\n"
            "Si tu te reconnais l\u00e0-dedans, postule ci-dessous. \U0001f447"
        )
        ec.set_footer(text="Crazy Inhouse \u2022 Candidature sur invitation")
        async for msg in ch_cand.history(limit=20):
            if msg.author == me:
                await msg.delete()
        await ch_cand.send(embed=ec, view=ApplicationButtonView())

    await interaction.followup.send("\u2705 Salons remplis !", ephemeral=True)

@tree.command(name="valider", description="[ADMIN] Accepter la candidature d'un joueur")
@app_commands.describe(user="Joueur à accepter")
async def valider_cmd(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    uid = str(user.id)
    conn = get_db()
    row = conn.execute("SELECT * FROM applications WHERE discord_id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        await interaction.response.send_message(f"❌ Aucune candidature trouvée pour {user.display_name}.", ephemeral=True)
        return
    if row["status"] == "accepted":
        await interaction.response.send_message(f"⚠️ {user.display_name} est déjà accepté.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # Mettre à jour le statut
    conn = get_db()
    conn.execute("UPDATE applications SET status='accepted', reviewed_by=?, reviewed_at=datetime('now') WHERE discord_id=?",
                 (str(interaction.user.id), uid))
    conn.commit()
    conn.close()

    # Donner rôle Membre, retirer Candidat
    membre_role = discord.utils.get(guild.roles, name="Membre")
    if not membre_role:
        membre_role = await guild.create_role(name="Membre", color=discord.Color(0x3ba55d), hoist=True)
    await user.add_roles(membre_role)
    candidat_role = discord.utils.get(guild.roles, name="Candidat")
    if candidat_role and candidat_role in user.roles:
        await user.remove_roles(candidat_role)

    # Créer espace privé
    await create_player_space(guild, user)

    # DM
    try:
        await user.send(embed=discord.Embed(
            title="🎉 Candidature acceptée !",
            description="Bienvenue sur **Crazy Inhouse** ! Tu as maintenant accès au serveur.\nUtilise `/register` dans ton salon privé pour créer ton profil !",
            color=0x3ba55d
        ))
    except Exception:
        pass

    await interaction.followup.send(f"✅ **{user.display_name}** accepté ! Rôle Membre attribué et espace privé créé.", ephemeral=True)


@tree.command(name="refuser", description="[ADMIN] Refuser la candidature d'un joueur")
@app_commands.describe(user="Joueur à refuser", raison="Raison du refus (optionnel)")
async def refuser_cmd(interaction: discord.Interaction, user: discord.Member, raison: str = ""):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    uid = str(user.id)
    conn = get_db()
    row = conn.execute("SELECT * FROM applications WHERE discord_id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        await interaction.response.send_message(f"❌ Aucune candidature trouvée pour {user.display_name}.", ephemeral=True)
        return

    conn = get_db()
    conn.execute("UPDATE applications SET status='rejected', reviewed_by=?, reviewed_at=datetime('now') WHERE discord_id=?",
                 (str(interaction.user.id), uid))
    conn.commit()
    conn.close()

    try:
        desc = "Ta candidature sur **Crazy Inhouse** n'a pas été retenue."
        if raison:
            desc += f"\n\n**Raison :** {raison}"
        await user.send(embed=discord.Embed(title="❌ Candidature refusée", description=desc, color=0xed4245))
    except Exception:
        pass

    await interaction.response.send_message(f"❌ **{user.display_name}** refusé.", ephemeral=True)


@tree.command(name="candidatures", description="[ADMIN] Voir les candidatures en attente")
async def candidatures_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM applications WHERE status='pending' ORDER BY submitted_at DESC LIMIT 10"
    ).fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("✅ Aucune candidature en attente !", ephemeral=True)
        return
    embed = discord.Embed(title=f"📋 Candidatures en attente ({len(rows)})", color=0xffa600)
    for row in rows:
        embed.add_field(
            name=f"{row['username']} — {row['rank']}",
            value=f"Riot: `{row['riot_id']}` • Age: {row['age']} | {row['presentation'][:80]}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="initserver", description="[ADMIN] Initialiser toute la structure du serveur Crazy Inhouse")
async def init_server_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    me = guild.me

    # ── Créer les rôles de base ──────────────────────────────────────
    async def get_or_create_role(name, color, hoist=False, mentionable=False):
        role = discord.utils.get(guild.roles, name=name)
        if not role:
            role = await guild.create_role(name=name, color=discord.Color(color), hoist=hoist, mentionable=mentionable)
        return role

    role_admin    = discord.utils.get(guild.roles, name="Admin") or await get_or_create_role("Admin", 0xff4655, hoist=True)
    role_coach    = discord.utils.get(guild.roles, name="Coach") or await get_or_create_role("Coach", 0xffd700, hoist=True)
    role_membre   = await get_or_create_role("Membre", 0x3ba55d, hoist=True)
    role_candidat = await get_or_create_role("Candidat", 0x747f8d, hoist=False)

    # Rôles de queue (créés au on_ready mais sécuriser ici aussi)
    for qid, qi in QUEUES.items():
        await get_or_create_role(qi["role"], qi["color"])

    everyone = guild.default_role

    # ── Helper permissions ───────────────────────────────────────────
    def ow_hidden():
        return discord.PermissionOverwrite(view_channel=False)
    def ow_read():
        return discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True)
    def ow_write():
        return discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    def ow_full():
        return discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True, read_message_history=True)

    async def get_or_create_category(name, overwrites=None):
        cat = discord.utils.get(guild.categories, name=name)
        if not cat:
            cat = await guild.create_category(name, overwrites=overwrites or {})
        return cat

    async def get_or_create_text(name, category, overwrites, topic=""):
        ch = discord.utils.get(guild.text_channels, name=name)
        if not ch:
            ch = await guild.create_text_channel(name, category=category, overwrites=overwrites, topic=topic)
        return ch

    async def get_or_create_voice(name, category, overwrites=None):
        ch = discord.utils.get(guild.voice_channels, name=name)
        if not ch:
            ch = await guild.create_voice_channel(name, category=category, overwrites=overwrites or {})
        return ch

    # ── CATÉGORIE : BIENVENUE (visible par tout le monde) ────────────
    ow_bienvenue = {
        everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
        me: ow_full(),
        role_admin: ow_full(),
    }
    cat_bienvenue = await get_or_create_category("🏠 BIENVENUE")
    ch_regles     = await get_or_create_text("📋︱règles",      cat_bienvenue, ow_bienvenue, "Règles du serveur Crazy Inhouse")
    ch_faq        = await get_or_create_text("❓︱faq",         cat_bienvenue, ow_bienvenue, "Questions fréquentes")
    ch_annonces   = await get_or_create_text("📢︱annonces",    cat_bienvenue, ow_bienvenue, "Annonces officielles")

    # Salon candidatures — tout le monde peut écrire
    ow_candidatures = {
        everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
        role_membre: ow_hidden(),   # Les membres ne voient plus ce salon
        me: ow_full(),
        role_admin: ow_full(),
    }
    ch_candidatures = await get_or_create_text("🎟️︱candidatures", cat_bienvenue, ow_candidatures, "Postule pour rejoindre Crazy Inhouse")

    # Poster l'embed de candidature
    async for msg in ch_candidatures.history(limit=5):
        if msg.author == me:
            break
    else:
        embed_cand = discord.Embed(
            title="🎮 Rejoindre Crazy Inhouse",
            description=(
                "**Bienvenue !** Ce serveur est sur **candidature**.\n\n"
                "Pour accéder au serveur, clique sur le bouton ci-dessous et remplis le formulaire.\n"
                "Un admin examinera ta candidature et te donnera accès si elle est acceptée.\n\n"
                "**Ce qu'on recherche :**\n"
                "• Joueurs sérieux et fair-play\n"
                "• Niveau Ascendant minimum (ou Game Changers)\n"
                "• Disponibles pour jouer régulièrement"
            ),
            color=0xff4655
        )
        embed_cand.set_footer(text="Crazy Inhouse • Candidature sur invitation")
        await ch_candidatures.send(embed=embed_cand, view=ApplicationButtonView())

    # ── CATÉGORIE : STAFF ────────────────────────────────────────────
    ow_staff = {
        everyone:     ow_hidden(),
        me:           ow_full(),
        role_admin:   ow_full(),
        role_coach:   ow_write(),
    }
    cat_staff = await get_or_create_category("🛡️ STAFF")
    await get_or_create_text("candidatures-en-cours", cat_staff, ow_staff, "Candidatures en attente de validation")
    await get_or_create_text("admin-logs",            cat_staff, ow_staff, "Logs et commandes admin")
    await get_or_create_text("queue-radiant",         cat_staff, ow_staff, "Supervision queue Radiant/Immo3")
    await get_or_create_text("queue-ascendant",       cat_staff, ow_staff, "Supervision queue Ascendant/Immo")
    await get_or_create_text("queue-gamechangers",    cat_staff, ow_staff, "Supervision queue Game Changers")

    # ── CATÉGORIE : GÉNÉRAL (membres seulement) ──────────────────────
    ow_general = {
        everyone:     ow_hidden(),
        me:           ow_full(),
        role_admin:   ow_full(),
        role_coach:   ow_write(),
        role_membre:  ow_write(),
    }
    cat_general = await get_or_create_category("💬 GÉNÉRAL")
    await get_or_create_text("💬︱général",          cat_general, ow_general, "Discussion générale")
    await get_or_create_text("🎬︱clips-highlights", cat_general, ow_general, "Partage de clips et highlights")
    await get_or_create_text("😂︱memes",            cat_general, ow_general, "Memes Valorant")
    await get_or_create_text("🗑️︱off-topic",        cat_general, ow_general, "Discussion hors-sujet")
    await get_or_create_text("🔍︱recherche-duo",    cat_general, ow_general, "Trouver un duo / équipe")
    await get_or_create_voice("🔊 Lobby",            cat_general, {
        everyone:    ow_hidden(),
        role_membre: discord.PermissionOverwrite(view_channel=True, connect=True),
        role_admin:  discord.PermissionOverwrite(view_channel=True, connect=True),
        me:          discord.PermissionOverwrite(view_channel=True, connect=True),
    })

    # ── CATÉGORIE : CLASSEMENTS (membres seulement, read-only) ───────
    ow_lb = {
        everyone:    ow_hidden(),
        me:          ow_full(),
        role_admin:  ow_full(),
        role_membre: ow_read(),
    }
    cat_lb = await get_or_create_category("📊 CLASSEMENTS")
    season = get_current_season()
    for queue_id, q_info in QUEUES.items():
        ch_lb = await get_or_create_text(f"leaderboard-{queue_id}", cat_lb, ow_lb, f"Classement {q_info['name']}")
        embed_lb = build_leaderboard_embed(queue_id, season)
        async for msg in ch_lb.history(limit=5):
            if msg.author == me:
                await msg.edit(embed=embed_lb)
                break
        else:
            await ch_lb.send(embed=embed_lb)

    # ── Rôle Candidat automatique au on_member_join ──────────────────
    # Stocké pour référence dans on_member_join

    await interaction.followup.send(
        embed=discord.Embed(
            title="✅ Serveur initialisé !",
            description=(
                "**Catégories créées :**\n"
                "• 🏠 BIENVENUE — visible par tous\n"
                "• 🛡️ STAFF — staff seulement\n"
                "• 💬 GÉNÉRAL — membres seulement\n"
                "• 📊 CLASSEMENTS — membres seulement\n\n"
                "**Rôles créés :**\n"
                "• Admin, Coach, Membre, Candidat\n"
                "• Queue Radiant, Queue Ascendant, Queue GC\n\n"
                "**Prochaines étapes :**\n"
                "1. `/setupspaces` — créer les espaces privés\n"
                "2. `/setqueue @joueur` — attribuer les queues\n"
                "3. `/addcoach @staff` — désigner les coachs"
            ),
            color=0x3ba55d
        ),
        ephemeral=True
    )


@tree.command(name="setup", description="[ADMIN] Créer les salons leaderboard par queue")
async def setup_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    season = get_current_season()

    # Trouver ou créer une catégorie INHOUSE
    cat = discord.utils.get(guild.categories, name="📊 CLASSEMENTS")
    if not cat:
        cat = await guild.create_category("📊 CLASSEMENTS")

    created = []
    for queue_id, q_info in QUEUES.items():
        ch_name = f"leaderboard-{queue_id}"
        ch = discord.utils.get(guild.text_channels, name=ch_name)
        if not ch:
            ch = await guild.create_text_channel(
                ch_name,
                category=cat,
                topic=f"Classement {q_info['name']} — mis à jour automatiquement"
            )
            created.append(q_info["name"])
        # Poster/mettre à jour l'embed
        embed = build_leaderboard_embed(queue_id, season)
        async for msg in ch.history(limit=5):
            if msg.author == guild.me:
                await msg.edit(embed=embed)
                break
        else:
            await ch.send(embed=embed)

    msg = "✅ Salons leaderboard mis à jour !"
    if created:
        msg += "\n📝 Créés : " + ", ".join(created)
    await interaction.followup.send(msg, ephemeral=True)


@tree.command(name="leaderboard", description="Afficher le classement d'une queue")
@app_commands.describe(queue="Queue à afficher")
@app_commands.choices(queue=[
    app_commands.Choice(name="👑 Radiant / Immo3", value="radiant"),
    app_commands.Choice(name="💎 Ascendant / Immo", value="ascendant"),
    app_commands.Choice(name="🌸 Game Changers", value="gamechangers"),
])
async def leaderboard_cmd(interaction: discord.Interaction, queue: str = "radiant"):
    season = get_current_season()
    embed = build_leaderboard_embed(queue, season)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="history", description="Historique des derniers matchs")
@app_commands.describe(count="Nombre de matchs (max 10)")
async def history_cmd(interaction: discord.Interaction, count: int = 5):
    count = min(count, 10)
    conn = get_db()
    rows = conn.execute("SELECT * FROM matches WHERE status='finished' ORDER BY ended_at DESC LIMIT ?", (count,)).fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("Aucun match terminé.", ephemeral=True)
        return
    embed = discord.Embed(title=f"📜 Derniers {count} matchs", color=0x5865f2)
    for row in rows:
        w = row["winner"]
        ended = (row["ended_at"] or "?")[:10]
        mvp_str = f" • MVP: <@{row['mvp']}>" if row["mvp"] else ""
        embed.add_field(
            name=f"Match {row['match_id'][-6:]} — {ended} • {row['map'] or '?'}",
            value=f"🔴 T1 {'**✓**' if w==1 else '✗'} | 🔵 T2 {'**✓**' if w==2 else '✗'}{mvp_str}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)


@tree.command(name="queue", description="[ADMIN] Afficher le panneau queue")
async def queue_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    view = QueueView()
    await interaction.response.send_message(embed=build_queue_embed(), view=view)
    msg = await interaction.original_response()
    queue_message_refs["global"] = {"channel_id": interaction.channel_id, "message_id": msg.id}


@tree.command(name="clearqueue", description="[ADMIN] Vider la queue")
async def clear_queue(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
    for qid in QUEUES:
        queues[qid].clear()
    await interaction.response.send_message("✅ Toutes les queues vidées.", ephemeral=True)
    await update_queue_message(interaction)
    await update_queue_message(interaction)


@tree.command(name="setelo", description="[ADMIN] Modifier l'ELO d'un joueur")
@app_commands.describe(user="Joueur", elo="Nouvel ELO")
async def set_elo(interaction: discord.Interaction, user: discord.Member, elo: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    conn = get_db()
    conn.execute("UPDATE players SET elo=? WHERE discord_id=?", (elo, str(user.id)))
    conn.commit()
    conn.close()
    await sync_rank_role(interaction.guild, user, elo)
    await interaction.response.send_message(f"✅ ELO de {user.display_name} → **{elo}**.", ephemeral=True)
    await update_leaderboard(interaction.guild)


@tree.command(name="resetplayer", description="[ADMIN] Remettre un joueur à 1000 ELO")
@app_commands.describe(user="Joueur à reset")
async def reset_player(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    conn = get_db()
    conn.execute("UPDATE players SET elo=1000, wins=0, losses=0, streak=0 WHERE discord_id=?", (str(user.id),))
    conn.commit()
    conn.close()
    await sync_rank_role(interaction.guild, user, 1000)
    await interaction.response.send_message(f"✅ {user.display_name} remis à 1000 ELO.", ephemeral=True)


@tree.command(name="newseason", description="[ADMIN] Démarrer une nouvelle saison (soft reset ELO)")
async def new_season(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    conn = get_db()
    current = get_current_season()
    conn.execute("UPDATE seasons SET ended_at=datetime('now') WHERE season_id=?", (current,))
    new_s = current + 1
    conn.execute("INSERT INTO seasons (season_id) VALUES (?)", (new_s,))
    conn.execute("UPDATE players SET elo=CAST((elo+1000)/2 AS INTEGER), wins=0, losses=0, streak=0, placement_done=0, season=?", (new_s,))
    conn.commit()
    conn.close()
    embed = discord.Embed(
        title=f"🏁 Saison {new_s} lancée !",
        description=f"Saison **{current}** terminée.\nELO soft-resetté (moyenne avec 1000).\nBonne chance pour la **Saison {new_s}** !",
        color=0xff4655
    )
    await interaction.response.send_message(embed=embed)
    await update_leaderboard(interaction.guild)


@tree.command(name="setqueue", description="[ADMIN] Attribuer une queue à un joueur")
@app_commands.describe(user="Joueur", queue="Queue à attribuer")
@app_commands.choices(queue=[
    app_commands.Choice(name="👑 Radiant / Immo3", value="radiant"),
    app_commands.Choice(name="💎 Ascendant / Immo", value="ascendant"),
    app_commands.Choice(name="🌸 Game Changers", value="gamechangers"),
])
async def set_queue_cmd(interaction: discord.Interaction, user: discord.Member, queue: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    q_info = QUEUES[queue]
    role = discord.utils.get(interaction.guild.roles, name=q_info["role"])
    if not role:
        role = await interaction.guild.create_role(
            name=q_info["role"],
            color=discord.Color(q_info["color"]),
            hoist=False
        )
    # Retirer les autres rôles de queue si présents
    for qid, qi in QUEUES.items():
        if qid != queue:
            other_role = discord.utils.get(interaction.guild.roles, name=qi["role"])
            if other_role and other_role in user.roles:
                await user.remove_roles(other_role)
    await user.add_roles(role)
    # Initialiser les stats ELO pour cette queue
    get_queue_elo(str(user.id), queue)
    await interaction.response.send_message(
        f"✅ **{user.display_name}** a accès à la queue **{q_info['name']}** !",
        ephemeral=True
    )


@tree.command(name="addqueue", description="[ADMIN] Ajouter une queue supplémentaire à un joueur (accès multi-queue)")
@app_commands.describe(user="Joueur", queue="Queue à ajouter")
@app_commands.choices(queue=[
    app_commands.Choice(name="👑 Radiant / Immo3", value="radiant"),
    app_commands.Choice(name="💎 Ascendant / Immo", value="ascendant"),
    app_commands.Choice(name="🌸 Game Changers", value="gamechangers"),
])
async def add_queue_cmd(interaction: discord.Interaction, user: discord.Member, queue: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    q_info = QUEUES[queue]
    role = discord.utils.get(interaction.guild.roles, name=q_info["role"])
    if not role:
        role = await interaction.guild.create_role(name=q_info["role"], color=discord.Color(q_info["color"]))
    await user.add_roles(role)
    get_queue_elo(str(user.id), queue)
    await interaction.response.send_message(
        f"✅ **{user.display_name}** peut maintenant aussi jouer en **{q_info['name']}** !",
        ephemeral=True
    )


@tree.command(name="removequeue", description="[ADMIN] Retirer l'accès à une queue")
@app_commands.describe(user="Joueur", queue="Queue à retirer")
@app_commands.choices(queue=[
    app_commands.Choice(name="👑 Radiant / Immo3", value="radiant"),
    app_commands.Choice(name="💎 Ascendant / Immo", value="ascendant"),
    app_commands.Choice(name="🌸 Game Changers", value="gamechangers"),
])
async def remove_queue_cmd(interaction: discord.Interaction, user: discord.Member, queue: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    q_info = QUEUES[queue]
    role = discord.utils.get(interaction.guild.roles, name=q_info["role"])
    if role and role in user.roles:
        await user.remove_roles(role)
        await interaction.response.send_message(f"✅ Accès **{q_info['name']}** retiré à **{user.display_name}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ {user.display_name} n'a pas accès à cette queue.", ephemeral=True)


@tree.command(name="queuestatus", description="Voir l'état de toutes les queues")
async def queue_status_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 État des queues", color=0xff4655, timestamp=datetime.now(timezone.utc))
    current_size = test_queue_size if test_mode else QUEUE_SIZE
    for qid, q_info in QUEUES.items():
        q = queues[qid]
        if q:
            names = "\n".join(f"• **{p['name']}** `{p.get('role','—')}`" for p in q)
        else:
            names = "*Personne en queue*"
        embed.add_field(
            name=f"{q_info['emoji']} {q_info['name']} ({len(q)}/{current_size})",
            value=names,
            inline=False
        )
    if test_mode:
        embed.set_footer(text=f"🧪 Mode test actif — queue à {current_size} joueurs")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="setupspaces", description="[ADMIN] Créer les espaces privés pour tous les membres existants")
async def setup_spaces_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    count = 0
    for member in guild.members:
        if member.bot:
            continue
        conn = get_db()
        existing = conn.execute("SELECT discord_id FROM player_channels WHERE discord_id=?", (str(member.id),)).fetchone()
        conn.close()
        if not existing:
            await create_player_space(guild, member)
            count += 1
            await asyncio.sleep(0.5)  # Éviter le rate limit Discord
    await interaction.followup.send(f"✅ **{count}** espace(s) privé(s) créé(s) !", ephemeral=True)


@tree.command(name="addcoach", description="[ADMIN] Attribuer le rôle Coach à un membre")
@app_commands.describe(user="Membre à promouvoir coach")
async def add_coach(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    coach_role = discord.utils.get(interaction.guild.roles, name="Coach")
    if not coach_role:
        coach_role = await interaction.guild.create_role(
            name="Coach",
            color=discord.Color.from_str("#ffd700"),
            hoist=True
        )
    await user.add_roles(coach_role)
    await interaction.response.send_message(
        f"✅ **{user.display_name}** est maintenant **Coach** ! Il peut valider les résultats et accède aux channels vocaux des deux équipes.",
        ephemeral=True
    )


@tree.command(name="removecoach", description="[ADMIN] Retirer le rôle Coach à un membre")
@app_commands.describe(user="Membre à rétrograder")
async def remove_coach(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    coach_role = discord.utils.get(interaction.guild.roles, name="Coach")
    if coach_role and coach_role in user.roles:
        await user.remove_roles(coach_role)
        await interaction.response.send_message(f"✅ Rôle Coach retiré à **{user.display_name}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ {user.display_name} n'a pas le rôle Coach.", ephemeral=True)


@tree.command(name="coaches", description="Voir la liste des coachs actifs")
async def coaches_cmd(interaction: discord.Interaction):
    coach_role = discord.utils.get(interaction.guild.roles, name="Coach")
    if not coach_role or not coach_role.members:
        await interaction.response.send_message("Aucun coach enregistré.", ephemeral=True)
        return
    names = "\n".join(f"• **{m.display_name}**" for m in coach_role.members)
    embed = discord.Embed(title="🎙️ Coachs actifs", description=names, color=0xffd700)
    await interaction.response.send_message(embed=embed)


@tree.command(name="stats", description="Voir l'historique détaillé des matchs d'un joueur")
@app_commands.describe(user="Joueur à consulter (optionnel)")
async def stats_cmd(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    uid = str(target.id)
    conn = get_db()
    player = conn.execute("SELECT * FROM players WHERE discord_id=?", (uid,)).fetchone()
    if not player:
        conn.close()
        await interaction.response.send_message("❌ Joueur non inscrit.", ephemeral=True)
        return

    # Derniers matchs du joueur
    all_matches = conn.execute(
        "SELECT * FROM matches WHERE status='finished' ORDER BY ended_at DESC LIMIT 10"
    ).fetchall()
    conn.close()

    player_matches = []
    for m in all_matches:
        t1 = json.loads(m["team1"])
        t2 = json.loads(m["team2"])
        if uid in t1:
            player_matches.append({"match": m, "team": 1, "won": m["winner"] == 1})
        elif uid in t2:
            player_matches.append({"match": m, "team": 2, "won": m["winner"] == 2})

    rname, ricon, color = get_rank(player["elo"])
    total = player["wins"] + player["losses"]
    wr = round(player["wins"] / total * 100) if total > 0 else 0
    in_place = is_in_placement(player["wins"], player["losses"])

    embed = discord.Embed(title=f"📊 Historique de {target.display_name}", color=color)
    embed.set_thumbnail(url=target.display_avatar.url)

    if in_place:
        embed.add_field(name="Rang", value=f"🔰 Placement ({total}/{PLACEMENT_MATCHES})", inline=True)
    else:
        embed.add_field(name="Rang", value=f"{ricon} **{rname}** — {player['elo']} ELO", inline=True)

    embed.add_field(name="Record", value=f"**{player['wins']}W** / **{player['losses']}L** ({wr}%)", inline=True)
    embed.add_field(name="MVP", value=f"🌟 {player['mvp_count']}", inline=True)

    if player_matches:
        lines = []
        for pm in player_matches[:8]:
            m = pm["match"]
            result = "✅ Victoire" if pm["won"] else "❌ Défaite"
            elo_ch = {}
            if m["elo_changes"]:
                elo_ch = json.loads(m["elo_changes"]).get(uid, {})
            change_str = f"`{elo_ch.get('change','?')}`" if elo_ch else ""
            mvp_str = " 🌟" if m["mvp"] == uid else ""
            date_str = (m["ended_at"] or "?")[:10]
            lines.append(f"{result} {change_str} — **{m['map'] or '?'}** ({date_str}){mvp_str}")
        embed.add_field(name="Derniers matchs", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Derniers matchs", value="*Aucun match joué.*", inline=False)

    await interaction.response.send_message(embed=embed)


@tree.command(name="compare", description="Comparer deux joueurs")
@app_commands.describe(user1="Premier joueur", user2="Deuxième joueur")
async def compare_cmd(interaction: discord.Interaction, user1: discord.Member, user2: discord.Member):
    conn = get_db()
    p1 = conn.execute("SELECT * FROM players WHERE discord_id=?", (str(user1.id),)).fetchone()
    p2 = conn.execute("SELECT * FROM players WHERE discord_id=?", (str(user2.id),)).fetchone()
    conn.close()

    if not p1 or not p2:
        await interaction.response.send_message("❌ Un des joueurs n'est pas inscrit.", ephemeral=True)
        return

    def player_block(p, member):
        rname, ricon, _ = get_rank(p["elo"])
        total = p["wins"] + p["losses"]
        wr = round(p["wins"] / total * 100) if total > 0 else 0
        in_place = is_in_placement(p["wins"], p["losses"])
        rank_str = f"🔰 Placement" if in_place else f"{ricon} {rname}"
        elo_str = "?" if in_place else str(p["elo"])
        return (
            f"{rank_str}\n"
            f"ELO : **{elo_str}**\n"
            f"Record : **{p['wins']}W/{p['losses']}L** ({wr}%)\n"
            f"Streak : 🔥 {p['streak']}\n"
            f"MVP : 🌟 {p['mvp_count']}"
        )

    embed = discord.Embed(title=f"⚔️ {user1.display_name} vs {user2.display_name}", color=0xff4655)
    embed.add_field(name=f"👤 {user1.display_name}", value=player_block(p1, user1), inline=True)
    embed.add_field(name="vs", value="\u200b", inline=True)
    embed.add_field(name=f"👤 {user2.display_name}", value=player_block(p2, user2), inline=True)

    # Qui a le meilleur ELO ?
    if not is_in_placement(p1["wins"], p1["losses"]) and not is_in_placement(p2["wins"], p2["losses"]):
        diff = abs(p1["elo"] - p2["elo"])
        better = user1.display_name if p1["elo"] > p2["elo"] else user2.display_name
        embed.add_field(name="📊 Écart ELO", value=f"**{better}** est devant de **{diff}** pts", inline=False)

    await interaction.response.send_message(embed=embed)


@tree.command(name="matchinfo", description="Voir les infos d'un match actif")
async def match_info(interaction: discord.Interaction):
    if not active_matches:
        await interaction.response.send_message("Aucun match actif en ce moment.", ephemeral=True)
        return
    embed = discord.Embed(title="🎮 Matchs en cours", color=0xff4655)
    for mid, m in active_matches.items():
        t1 = ", ".join(p["name"] for p in m["team1"])
        t2 = ", ".join(p["name"] for p in m["team2"])
        embed.add_field(
            name=f"Match {mid[-6:]} — {m.get('map','?')}",
            value=f"🔴 {t1}\n🔵 {t2}",
            inline=False
        )
    await interaction.response.send_message(embed=embed)


@tree.command(name="testmode", description="[ADMIN] Activer/désactiver le mode test (queue réduite)")
@app_commands.describe(players="Nombre de joueurs pour lancer un match (2-10, défaut: 2)")
async def test_mode_cmd(interaction: discord.Interaction, players: int = 2):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    global test_mode, test_queue_size
    players = max(2, min(10, players))
    if test_mode and test_queue_size == players:
        # Désactiver
        test_mode = False
        test_queue_size = QUEUE_SIZE
        embed = discord.Embed(
            title="✅ Mode test désactivé",
            description=f"La queue revient à **{QUEUE_SIZE} joueurs** normalement.",
            color=0x00ff88
        )
    else:
        # Activer
        test_mode = True
        test_queue_size = players
        embed = discord.Embed(
            title="🧪 Mode test activé !",
            description=f"La queue se lance dès **{players} joueur(s)**. Utilise `/testmode` sans argument pour désactiver.",
            color=0xffa500
        )
        embed.add_field(
            name="💡 Comment tester",
            value=f"1. `/register` avec ton compte principal\n2. Rejoins la queue\n3. Rejoins avec ton 2ème compte (ou `/fillqueue` pour remplir avec des bots fictifs)\n4. Le match se lance à {players} joueurs",
            inline=False
        )
    await interaction.response.send_message(embed=embed)
    await update_queue_message(interaction)


@tree.command(name="fillqueue", description="[ADMIN] Remplir la queue avec des joueurs fictifs pour tester")
async def fill_queue_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return

    current_size = test_queue_size if test_mode else QUEUE_SIZE
    # Fillqueue utilise la queue ascendant par défaut pour les tests
    fill_qid = next((qid for qid, q in queues.items() if len(q) < current_size), list(QUEUES.keys())[0])
    spots_left = current_size - len(queues[fill_qid])
    if spots_left <= 0:
        await interaction.response.send_message("⚠️ La queue est déjà pleine !", ephemeral=True)
        return

    fake_names = [
        "TenZ_bot", "Yay_bot", "Aspas_bot", "Nats_bot", "Derke_bot",
        "Chronicle_bot", "CNed_bot", "Marved_bot", "FNS_bot", "Sacy_bot"
    ]
    fake_roles = ["Duelliste", "Duelliste", "Initiateur", "Contrôleur", "Sentinelle",
                  "Flex", "Duelliste", "Initiateur", "Contrôleur", "Sentinelle"]

    # Éviter les doublons de noms
    used_names = {p["name"] for p in all_queued_players()}
    available = [(n, r) for n, r in zip(fake_names, fake_roles) if n not in used_names]

    added = 0
    for i in range(min(spots_left, len(available))):
        name, role = available[i]
        fake_id = f"fake_{i}_{int(datetime.now(timezone.utc).timestamp())}"

        # Insérer en DB avec un ELO aléatoire réaliste
        fake_elo = random.randint(950, 1400)
        conn = get_db()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO players (discord_id, username, elo) VALUES (?, ?, ?)",
                (fake_id, name, fake_elo)
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

        queues[fill_qid].append({
            "id": fake_id,
            "name": name,
            "role": role,
            "joined_at": datetime.now(timezone.utc),
            "is_bot": True,
            "queue_id": fill_qid,
        })
        added += 1

    embed = discord.Embed(
        title=f"🤖 {added} joueur(s) fictif(s) ajoutés",
        description=f"Queue **{QUEUES[fill_qid]['name']}** : **{len(queues[fill_qid])}/{current_size}**\n\nCes joueurs sont marqués 🤖 dans la queue.",
        color=0xffa500
    )
    if len(queues[fill_qid]) >= current_size:
        embed.add_field(name="🚀", value="Queue pleine — le match va se lancer !", inline=False)

    guild = interaction.guild
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await update_queue_message(guild=guild)

    if len(queues[fill_qid]) >= current_size:
        try:
            await start_match(interaction, queue_id=fill_qid, guild_override=guild)
        except Exception as e:
            print(f"❌ Erreur start_match depuis fillqueue: {e}")
            import traceback; traceback.print_exc()


@tree.command(name="testveto", description="[ADMIN] Tester le système de veto de map")
async def test_veto_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return

    # Simule deux capitaines = toi-même (tu peux banner pour les deux équipes)
    fake_cap = {"id": str(interaction.user.id), "name": interaction.user.display_name, "is_bot": False}
    test_match_id = f"test_{int(datetime.now(timezone.utc).timestamp())}"

    async def on_veto_done(final_map: str):
        await interaction.channel.send(
            embed=discord.Embed(
                title="✅ Test veto terminé",
                description=f"Map sélectionnée : **{final_map}**\n\nLe veto fonctionne correctement !",
                color=0x57f287
            )
        )

    veto_embed = discord.Embed(
        title="🗺️ Test Veto de Map",
        description=(
            f"**Capitaine T1 & T2** : {interaction.user.mention}\n\n"
            "Tu joues les deux rôles pour tester.\n"
            "Banne les maps jusqu\'à ce qu\'il en reste une."
        ),
        color=0xff4655
    )
    veto_embed.add_field(name="Maps disponibles", value=" • ".join(VALORANT_MAPS), inline=False)

    view = MapVetoView(test_match_id, VALORANT_MAPS, fake_cap, fake_cap, on_veto_done)
    await interaction.response.send_message(embed=veto_embed, view=view)


@tree.command(name="clearfake", description="[ADMIN] Supprimer les joueurs fictifs de la DB")
async def clear_fake_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    conn = get_db()
    conn.execute("DELETE FROM players WHERE discord_id LIKE 'fake_%'")
    conn.execute("DELETE FROM matches WHERE team1 LIKE '%fake_%' OR team2 LIKE '%fake_%'")
    conn.commit()
    conn.close()
    # Vider aussi les faux joueurs de la queue en mémoire
    for qid in QUEUES:
        queues[qid] = [p for p in queues[qid] if not p.get("is_bot")]
    await interaction.response.send_message("✅ Joueurs fictifs supprimés de la DB et de la queue.", ephemeral=True)
    await update_queue_message(interaction)


@tree.command(name="reports", description="[ADMIN] Voir les joueurs les plus reportés (30 derniers jours)")
async def reports_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    conn = get_db()
    rows = conn.execute("""
        SELECT reported_id, COUNT(*) as cnt FROM reports
        WHERE created_at > datetime('now', '-30 days')
        GROUP BY reported_id ORDER BY cnt DESC LIMIT 10
    """).fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("Aucun report ces 30 derniers jours.", ephemeral=True)
        return
    embed = discord.Embed(title="🚨 Reports (30 derniers jours)", color=0xff0000)
    embed.description = "\n".join(f"<@{r['reported_id']}> — **{r['cnt']}** reports" for r in rows)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  TASK : timeout queue
# ─────────────────────────────────────────────
@tasks.loop(minutes=5)
async def queue_timeout_check():
    now = datetime.now(timezone.utc)
    for qid in QUEUES:
        to_remove = [p for p in queues[qid] if (now - p.get("joined_at", now)).total_seconds() > QUEUE_TIMEOUT * 60]
        for p in to_remove:
            queues[qid].remove(p)
            print(f"⏰ Timeout queue {qid} : {p['name']}")


@tree.command(name="lobbycode", description="Partager le code du lobby custom à tous les joueurs du match")
@app_commands.describe(code="Code du lobby Valorant (ex: ABCD)")
async def lobbycode_cmd(interaction: discord.Interaction, code: str):
    uid = str(interaction.user.id)

    # Trouver le match du joueur
    match_id = None
    match_data = None
    for mid, m in active_matches.items():
        if uid in [p["id"] for p in m["team1"] + m["team2"]]:
            match_id = mid
            match_data = m
            break

    if not match_data:
        await interaction.response.send_message("❌ Tu n'as pas de match en cours !", ephemeral=True)
        return

    # Vérifier que le joueur est capitaine (le plus haut ELO de son équipe)
    queue_id = match_data.get("queue_id", "ascendant")
    team1, team2 = match_data["team1"], match_data["team2"]

    def get_captain(team):
        return max(team, key=lambda p: get_queue_elo(p["id"], queue_id)["elo"])

    cap1 = get_captain(team1)
    cap2 = get_captain(team2)
    is_captain = uid in [cap1["id"], cap2["id"]]

    if not is_captain and not interaction.user.guild_permissions.administrator:
        cap_names = f"{cap1['name']} (T1) ou {cap2['name']} (T2)"
        await interaction.response.send_message(
            f"❌ Seul le capitaine peut partager le code.\nCapitaines : **{cap_names}**", ephemeral=True
        )
        return
        return

    code = code.upper().strip()
    await interaction.response.defer(ephemeral=True)

    # Poster dans le scoreboard
    guild = interaction.guild
    sb_ch = guild.get_channel(match_data.get("scoreboard_channel"))
    if sb_ch:
        embed = discord.Embed(
            title="🎮 Code Lobby",
            description=f"```{code}```\nCopiez ce code dans Valorant → **Jeu Personnalisé** → **Rejoindre**",
            color=0xff4655
        )
        embed.set_footer(text=f"Partagé par {interaction.user.display_name}")
        await sb_ch.send(embed=embed)

    # Envoyer dans les notifs privées de chaque joueur
    sent = 0
    for p in team1 + team2:
        if p.get("is_bot"):
            continue
        try:
            pch = get_player_channels(p["id"])
            if pch and pch["notifs_channel"]:
                notif_ch = guild.get_channel(pch["notifs_channel"])
                if notif_ch:
                    notif_embed = discord.Embed(
                        title="🎮 Code Lobby — Match en cours",
                        description=f"```{code}```\nValorant → **Jeu Personnalisé** → **Rejoindre**",
                        color=0xff4655
                    )
                    await notif_ch.send(embed=notif_embed)
                    sent += 1
        except Exception:
            pass

    await interaction.followup.send(f"✅ Code **{code}** envoyé à {sent} joueurs !", ephemeral=True)


# ─────────────────────────────────────────────
#  BOT EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_member_join(member: discord.Member):
    """Donne le rôle Candidat au nouveau membre — il ne voit que #candidatures."""
    guild = member.guild

    # Donner le rôle Candidat
    candidat_role = discord.utils.get(guild.roles, name="Candidat")
    if not candidat_role:
        candidat_role = await guild.create_role(name="Candidat", color=discord.Color(0x747f8d))
    await member.add_roles(candidat_role)

    # Envoyer un message de bienvenue en DM
    try:
        embed = discord.Embed(
            title="👋 Bienvenue sur Crazy Inhouse !",
            description=(
                "Pour accéder au serveur, tu dois d'abord soumettre une candidature.\n\n"
                "Va dans le salon **🎟️︱candidatures** et clique sur **📝 Postuler** !"
            ),
            color=0xff4655
        )
        await member.send(embed=embed)
    except Exception:
        pass


@bot.event
async def on_ready():
    init_db()
    # Vues statiques persistantes
    bot.add_view(ApplicationButtonView())
    bot.add_view(QueueView())

    # Réenregistrer les PersonalQueueView de chaque joueur (custom_id dynamique)
    conn = get_db()
    rows = conn.execute("SELECT discord_id, username FROM players").fetchall()
    conn.close()
    count = 0
    for row in rows:
        try:
            bot.add_view(PersonalQueueView(row["discord_id"], row["username"] or ""))
            count += 1
        except Exception:
            pass
    print(f"✅ {count} vues personnelles réenregistrées")
    print(f"✅ Bot connecté : {bot.user} ({bot.user.id})")

    # Créer les rôles de queue s'ils n'existent pas
    real_guild = bot.get_guild(GUILD_ID)
    if real_guild:
        for qid, q_info in QUEUES.items():
            role = discord.utils.get(real_guild.roles, name=q_info["role"])
            if not role:
                await real_guild.create_role(
                    name=q_info["role"],
                    color=discord.Color(q_info["color"]),
                    hoist=False,
                    mentionable=False
                )
                print(f"✅ Rôle créé : {q_info['role']}")

    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    try:
        synced = await tree.sync(guild=guild)
        print(f"✅ {len(synced)} commandes synchronisées sur le serveur")
    except Exception as e:
        print(f"❌ Erreur sync: {e}")
    bot.add_view(QueueView())
    queue_timeout_check.start()
    print("✅ Bot prêt !")


if __name__ == "__main__":
    bot.run(TOKEN)
