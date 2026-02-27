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
HENRIK_API_KEY = os.getenv("HENRIK_API_KEY", "")
RIOT_REGION = os.getenv("RIOT_REGION", "eu")  # eu, na, ap, kr, latam, br
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
POINTS_WIN         = 15     # points gagnés par victoire
POINTS_LOSS        = 5      # points gagnés par défaite
POINTS_MVP         = 5      # bonus MVP

# ── Multi-Queue Config ──────────────────────────────────────────────
QUEUES = {
    "radiant": {
        "id":          "radiant",
        "name":        "👑 Radiant / Immo3",
        "role":        "Queue Radiant",
        "color":       0xffd700,
        "emoji":       "👑",
        "channel":     "queue-radiant",
        "chat":        "radiant-chat",
    },
    "ascendant": {
        "id":          "ascendant",
        "name":        "💎 Ascendant / Immo3",
        "role":        "Queue Ascendant",
        "color":       0x9b59b6,
        "emoji":       "💎",
        "channel":     "queue-ascendant-immo",
        "chat":        "ascendant-chat",
    },
    "gamechangers": {
        "id":          "gamechangers",
        "name":        "🌸 Game Changers",
        "role":        "Queue GC",
        "color":       0xff69b4,
        "emoji":       "🌸",
        "channel":     "queue-gamechangers",
        "chat":        "gc-chat",
    },
}

# État des pings par queue — anti-spam
# first_at/mid_at = datetime du dernier ping, None si pas encore envoyé
PING_COOLDOWN_MINUTES = 10  # délai minimum entre deux sessions de ping
queue_ping_state: dict = {qid: {"first_at": None, "mid": False} for qid in ["radiant", "ascendant", "gamechangers"]}

# Joueurs qui ont reçu le DM de warning timeout mais n'ont pas encore répondu
# {uid: datetime_du_warning}
queue_timeout_warned: dict = {}

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
def restore_db_if_needed():

    """Restaure la DB depuis DB_SEED (base64) si le fichier n'existe pas encore."""
    db_path = os.getenv("DB_PATH", "inhouse.db")
    db_seed = os.getenv("DB_SEED", "")
    if db_seed and not os.path.exists(db_path):
        import base64
        os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None
        with open(db_path, "wb") as f:
            f.write(base64.b64decode(db_seed))
        print(f"✅ DB restaurée depuis DB_SEED → {db_path}")

restore_db_if_needed()

# Démarrer le serveur web en parallèle du bot
from web import start_web_server
start_web_server()


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
        CREATE TABLE IF NOT EXISTS follows (
            follower_id  TEXT NOT NULL,
            followed_id  TEXT NOT NULL,
            PRIMARY KEY (follower_id, followed_id)
        );
        CREATE TABLE IF NOT EXISTS cosmetics (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            type         TEXT NOT NULL,  -- 'role', 'badge', 'prefix'
            description  TEXT,
            price        INTEGER NOT NULL,
            role_color   INTEGER DEFAULT NULL,  -- hex color pour type=role
            emoji        TEXT DEFAULT NULL,     -- emoji pour badge/prefix
            is_rotating  INTEGER DEFAULT 0,
            available_until TEXT DEFAULT NULL,
            active       INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS player_cosmetics (
            player_id    TEXT NOT NULL,
            cosmetic_id  INTEGER NOT NULL,
            equipped     INTEGER DEFAULT 0,
            bought_at    TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (player_id, cosmetic_id)
        );
    """)
    conn.commit()

    # Migrations sécurisées — ignorées si la colonne existe déjà
    migrations = [
        "ALTER TABLE players ADD COLUMN riot_id TEXT DEFAULT NULL",
        "ALTER TABLE players ADD COLUMN points INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN val_rank TEXT DEFAULT NULL",
        "ALTER TABLE players ADD COLUMN val_elo INTEGER DEFAULT NULL",
        "ALTER TABLE players ADD COLUMN val_rank_updated TEXT DEFAULT NULL",
        "ALTER TABLE player_channels ADD COLUMN queue_message_id INTEGER DEFAULT NULL",
        "ALTER TABLE players ADD COLUMN streak INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN best_streak INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN mvp_count INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN placement_done INTEGER DEFAULT 0",
        "ALTER TABLE players ADD COLUMN season INTEGER DEFAULT 1",
        "ALTER TABLE matches ADD COLUMN map TEXT",
        "ALTER TABLE matches ADD COLUMN mvp TEXT",
        "ALTER TABLE matches ADD COLUMN season INTEGER DEFAULT 1",
        "ALTER TABLE player_channels ADD COLUMN notif_enabled INTEGER DEFAULT 1",
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

# Rang Valorant → ELO approximatif pour l'équilibrage
VAL_TIER_ELO = {
    "Iron 1": 100, "Iron 2": 133, "Iron 3": 166,
    "Bronze 1": 200, "Bronze 2": 233, "Bronze 3": 266,
    "Silver 1": 300, "Silver 2": 333, "Silver 3": 366,
    "Gold 1": 400, "Gold 2": 433, "Gold 3": 466,
    "Platinum 1": 500, "Platinum 2": 533, "Platinum 3": 566,
    "Diamond 1": 600, "Diamond 2": 633, "Diamond 3": 666,
    "Ascendant 1": 700, "Ascendant 2": 733, "Ascendant 3": 766,
    "Immortal 1": 800, "Immortal 2": 833, "Immortal 3": 866,
    "Radiant": 1000,
}

async def fetch_riot_rank(riot_id: str) -> dict | None:
    """Récupère le rang actuel d'un joueur via l'API Henrik3.
    Retourne {"tier": "Diamond 1", "rr": 38, "elo": 1538} ou None si erreur."""
    if not HENRIK_API_KEY or "#" not in riot_id:
        return None
    name, tag = riot_id.split("#", 1)
    url = f"https://api.henrikdev.xyz/valorant/v3/mmr/{RIOT_REGION}/pc/{name}/{tag}"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"Authorization": HENRIK_API_KEY}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    print(f"[RIOT API] {resp.status} pour {riot_id}")
                    return None
                data = await resp.json()
                current = data.get("data", {}).get("current", {})
                tier_name = current.get("tier", {}).get("name")
                rr = current.get("rr", 0)
                elo = current.get("elo", 0)
                if not tier_name:
                    return None
                return {"tier": tier_name, "rr": rr, "elo": elo}
    except Exception as e:
        print(f"[RIOT API] Erreur fetch {riot_id}: {e}")
        return None


async def sync_player_rank(uid: str, riot_id: str, conn=None) -> dict | None:
    """Sync le rang Riot d'un joueur et le stocke en BDD."""
    rank_data = await fetch_riot_rank(riot_id)
    if not rank_data:
        return None
    close = conn is None
    if conn is None:
        conn = get_db()
    conn.execute(
        "UPDATE players SET val_rank=?, val_elo=?, val_rank_updated=datetime('now') WHERE discord_id=?",
        (rank_data["tier"], rank_data["elo"], uid)
    )
    conn.commit()
    if close:
        conn.close()
    return rank_data

def balance_teams(players: list[dict]) -> tuple[list, list]:
    from itertools import combinations
    conn = get_db()
    elos = {}
    for p in players:
        row = conn.execute("SELECT elo, val_elo FROM players WHERE discord_id = ?", (p["id"],)).fetchone()
        internal_elo = row["elo"] if row else 1000
        # Si on a un rang Riot vérifié, on mixe les deux (60% interne + 40% Riot)
        if row and row["val_elo"]:
            val_elo_normalized = row["val_elo"] / 2.0  # val_elo va jusqu'à ~2000+, on normalise
            elos[p["id"]] = round(internal_elo * 0.6 + val_elo_normalized * 0.4)
        else:
            elos[p["id"]] = internal_elo
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
    """Enregistre le joueur en BDD et lui envoie un DM de bienvenue."""
    uid = str(member.id)
    conn = get_db()
    existing = conn.execute("SELECT * FROM player_channels WHERE discord_id=?", (uid,)).fetchone()
    if existing:
        conn.close()
        return
    conn.execute(
        "INSERT OR REPLACE INTO player_channels (discord_id, notif_enabled) VALUES (?, 1)",
        (uid,)
    )
    conn.commit()
    conn.close()

    # DM de bienvenue
    try:
        embed = discord.Embed(
            title="👋 Bienvenue sur Crazy Inhouse !",
            description=(
                "Tu as maintenant accès au serveur.\n\n"
                "**Pour commencer :**\n"
                "• `/register` — créer ton profil\n"
                "• Rejoins la queue depuis le salon 🎮︱queue\n\n"
                "**Tes stats et ton historique sont disponibles sur le site.**\n"
                "Les notifs de match arrivent ici par DM."
            ),
            color=0x6553e8
        )
        await member.send(embed=embed)
    except Exception:
        pass  # DMs fermés


def get_queue_channel(guild: discord.Guild, queue_id: str):
    """Retourne le salon queue partagé pour une queue donnée."""
    ch_name = QUEUES[queue_id]["channel"]
    for cat in guild.categories:
        if "QUEUES" in cat.name.upper():
            for ch in cat.channels:
                if ch.name == ch_name:
                    return ch
    return discord.utils.get(guild.text_channels, name=ch_name)


def get_queue_chat_channel(guild: discord.Guild, queue_id: str):
    """Retourne le salon chat d'une queue."""
    ch_name = QUEUES[queue_id]["chat"]
    for cat in guild.categories:
        if "QUEUES" in cat.name.upper():
            for ch in cat.channels:
                if ch.name == ch_name:
                    return ch
    return discord.utils.get(guild.text_channels, name=ch_name)


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


class NotifToggleView(discord.ui.View):
    """Bouton persistant pour activer/désactiver les alertes queue."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔔 Alertes queue : ON", style=discord.ButtonStyle.success, custom_id="notif_toggle")
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        conn = get_db()
        row = conn.execute("SELECT notif_enabled FROM player_channels WHERE discord_id=?", (uid,)).fetchone()
        if not row:
            conn.close()
            await interaction.response.send_message("❌ Espace non trouvé.", ephemeral=True)
            return
        new_val = 0 if row["notif_enabled"] else 1
        conn.execute("UPDATE player_channels SET notif_enabled=? WHERE discord_id=?", (new_val, uid))
        conn.commit()
        conn.close()
        if new_val:
            button.label = "🔔 Alertes queue : ON"
            button.style = discord.ButtonStyle.success
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("✅ Tu seras pингué quand quelqu'un rejoint une queue.", ephemeral=True)
        else:
            button.label = "🔕 Alertes queue : OFF"
            button.style = discord.ButtonStyle.secondary
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("🔕 Tu ne recevras plus d'alertes queue.", ephemeral=True)


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
        embed.add_field(name="💰 Points boutique", value=f"**{row['points'] or 0}** pts", inline=True)
        embed.add_field(name="Record", value=f"**{row['wins']}W / {row['losses']}L** ({wr}%)", inline=True)
        embed.add_field(name="Streak", value=f"🔥 {row['streak']} (record: {row['best_streak']})", inline=True)
        embed.add_field(name="MVP", value=f"🌟 {row['mvp_count']}", inline=True)
        if row["riot_id"]:
            embed.add_field(name="Compte Riot", value=f"`{row['riot_id']}`", inline=True)
    embed.set_footer(text="Mis à jour automatiquement après chaque match.")
    await channel.send(embed=embed)

    # Bouton toggle notifications
    conn2 = get_db()
    notif_row = conn2.execute("SELECT notif_enabled FROM player_channels WHERE discord_id=?", (uid,)).fetchone()
    conn2.close()
    enabled = notif_row["notif_enabled"] if notif_row and notif_row["notif_enabled"] is not None else 1
    view = NotifToggleView()
    view.children[0].label = "🔔 Alertes queue : ON" if enabled else "🔕 Alertes queue : OFF"
    view.children[0].style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
    notif_embed = discord.Embed(
        title="🔔 Alertes Queue",
        description="Reçois une mention dans ce salon dès que quelqu'un rejoint une queue.",
        color=0x6553e8
    )
    await channel.send(embed=notif_embed, view=view)


async def update_player_profil(guild: discord.Guild, uid: str):
    """Envoie le profil mis à jour par DM après un match."""
    member = guild.get_member(int(uid))
    if not member:
        return
    channel = None  # on passe par DM, mais on garde la logique commune ci-dessous

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
        embed.add_field(name="💰 Points boutique", value=f"**{row['points'] or 0}** pts", inline=True)
    embed.add_field(name="Record", value=f"**{row['wins']}W / {row['losses']}L** ({wr}%)", inline=True)
    embed.add_field(name="Streak", value=f"🔥 {row['streak']} (record: {row['best_streak']})", inline=True)
    embed.add_field(name="MVP", value=f"🌟 {row['mvp_count']}", inline=True)
    if row["riot_id"]:
        embed.add_field(name="Compte Riot", value=f"`{row['riot_id']}`", inline=True)
    embed.set_footer(text=f"Mis à jour — {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC")

    # Envoyer le profil mis à jour par DM
    try:
        await member.send(embed=embed)
    except Exception:
        pass


async def update_player_history(guild: discord.Guild, uid: str, match_id: str, won: bool, elo_change: dict, map_name: str):
    """Envoie le résultat du match par DM au joueur."""
    member = guild.get_member(int(uid))
    if not member:
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
    try:
        await member.send(embed=embed)
    except Exception:
        pass


# Rôles Discord requis par queue
QUEUE_ROLES = {
    "radiant":      "Queue Radiant",
    "ascendant":    "Queue Ascendant",
    "gamechangers": "Queue GC",
}

async def ping_queue_watchers(guild: discord.Guild, joiner_uid: str, joiner_name: str, queue_id: str, nb: int, size: int):
    """Ping dans le salon chat de la queue — une fois au 1er joueur, une fois à mi-chemin.
    Anti-spam : ignore si le ping de session a déjà été envoyé."""
    global queue_ping_state
    q_info = QUEUES[queue_id]
    mid = size // 2
    state = queue_ping_state[queue_id]

    # Détermine si on doit pinger
    now = datetime.now(timezone.utc)
    cooldown = timedelta(minutes=PING_COOLDOWN_MINUTES)
    first_ok = state["first_at"] is None or (now - state["first_at"]) > cooldown
    should_ping_first = nb == 1 and first_ok
    should_ping_mid   = nb == mid and not state["mid"] and state["first_at"] is not None
    if not should_ping_first and not should_ping_mid:
        return

    # Récupère le rôle requis pour mentionner
    required_role_name = QUEUE_ROLES.get(queue_id)
    required_role = discord.utils.get(guild.roles, name=required_role_name) if required_role_name else None
    if required_role_name and not required_role:
        return

    # Récupère le salon chat
    chat_ch = get_queue_chat_channel(guild, queue_id)
    if not chat_ch:
        return

    # Construit le message
    if should_ping_first:
        state["first_at"] = datetime.now(timezone.utc)
        state["mid"] = False
        title = f"🟢 Queue lancée — {q_info['name']}"
        desc = f"**{joiner_name}** a ouvert la queue ! Qui est chaud ?"
    else:
        state["mid"] = True
        title = f"⏳ Mi-chemin — {q_info['name']}"
        desc = f"**{nb}/{size}** joueurs en queue. Plus que {size - nb} pour lancer !"

    embed = discord.Embed(title=title, description=desc, color=q_info["color"])
    embed.set_footer(text="Rejoins depuis le salon queue • /notifications pour tes alertes DM")

    mention = required_role.mention if required_role else ""
    try:
        await chat_ch.send(content=mention, embed=embed)
    except Exception as e:
        print(f"[PING] Erreur envoi chat {queue_id}: {e}")


async def notify_player(guild: discord.Guild, uid: str, embed: discord.Embed):
    """Envoie une notification par DM au joueur."""
    try:
        member = guild.get_member(int(uid))
        if member:
            await member.send(embed=embed)
    except Exception:
        pass


async def update_personal_queue_embeds(guild: discord.Guild):
    """Met à jour l'embed dans chaque salon queue partagé (un par queue)."""
    current_size = test_queue_size if test_mode else QUEUE_SIZE
    for queue_id, q_info in QUEUES.items():
        ch = get_queue_channel(guild, queue_id)
        if not ch:
            continue
        try:
            nb = len(queues[queue_id])
            bar = "🟩" * nb + "⬛" * (current_size - nb)
            embed = discord.Embed(
                title=f"{q_info['emoji']} {q_info['name']}",
                description="Choisis ton rôle et rejoins la file d'attente.",
                color=q_info["color"]
            )
            embed.add_field(name="File d'attente", value=f"{bar} **{nb}/{current_size}**", inline=False)
            if queues[queue_id]:
                players_list = "\n".join(
                    f"• {p['name']} — {p['role']}" for p in queues[queue_id]
                )
                embed.add_field(name="Joueurs en attente", value=players_list, inline=False)
            embed.set_footer(text="Choisis un rôle pour rejoindre • /notifications pour gérer tes alertes DM")

            msg = None
            async for m in ch.history(limit=20):
                if m.author == guild.me and m.components:
                    msg = m
                    break
            if msg:
                await msg.edit(embed=embed)
            else:
                await ch.send(embed=embed, view=SharedQueueView(queue_id))
        except Exception as e:
            print(f"Erreur update_queue_embed [{queue_id}]: {e}")


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

    # Ping les joueurs qui ont les notifs activées (sauf celui qui vient de rejoindre)
    await ping_queue_watchers(interaction.guild, uid, username, queue_id, nb, current_size)

    # Notifier les followers
    await notify_followers(interaction.guild, uid, username, queue_id)

    if nb >= current_size:
        # Reset complet au match lancé — prochaine session peut pinger immédiatement
        queue_ping_state[queue_id] = {"first_at": None, "mid": False}
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



class SharedQueueView(discord.ui.View):
    """Vue persistante dans le salon queue partagé — un par queue."""
    def __init__(self, queue_id: str):
        super().__init__(timeout=None)
        self.queue_id = queue_id

        roles = [
            ("⚔️ Duelliste",  "Duelliste",  discord.ButtonStyle.danger,   f"sq_duel_{queue_id}"),
            ("🔦 Initiateur", "Initiateur", discord.ButtonStyle.primary,  f"sq_init_{queue_id}"),
            ("💨 Contrôleur", "Contrôleur", discord.ButtonStyle.success,  f"sq_ctrl_{queue_id}"),
            ("🛡️ Sentinelle", "Sentinelle", discord.ButtonStyle.secondary, f"sq_sent_{queue_id}"),
            ("🔄 Flex",       "Flex",       discord.ButtonStyle.secondary, f"sq_flex_{queue_id}"),
        ]
        for label, role, style, cid in roles:
            btn = discord.ui.Button(label=label, style=style, custom_id=cid, row=0)
            btn.callback = self._make_join_cb(role)
            self.add_item(btn)

        leave_btn = discord.ui.Button(
            label="❌ Quitter la queue", style=discord.ButtonStyle.danger,
            custom_id=f"sq_leave_{queue_id}", row=1
        )
        leave_btn.callback = self._leave_cb
        self.add_item(leave_btn)

    def _make_join_cb(self, role: str):
        queue_id = self.queue_id
        async def callback(interaction: discord.Interaction):
            uid = str(interaction.user.id)
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
            if not player_has_queue_access(interaction.user, queue_id):
                await interaction.response.send_message(
                    f"❌ Tu n'as pas accès à cette queue.", ephemeral=True
                )
                return
            await _join_queue(interaction, uid, interaction.user.display_name, role, queue_id)
        return callback

    async def _leave_cb(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        qid = get_player_queue_id(uid)
        if not qid:
            await interaction.response.send_message("⚠️ Tu n'es pas en queue !", ephemeral=True)
            return
        queues[qid] = [p for p in queues[qid] if p["id"] != uid]
        minutes = register_queue_leave(uid)
        msg = f"👋 Retiré de la queue. Cooldown **{minutes} min**." if minutes > 0 else "👋 Retiré de la queue."
        await interaction.response.send_message(msg, ephemeral=True)
        # Reset mid si queue vide (first_at garde le cooldown temporel)
        if len(queues[qid]) == 0:
            queue_ping_state[qid]["mid"] = False
        await update_personal_queue_embeds(interaction.guild)


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
                conn.execute("UPDATE players SET mvp_count = mvp_count + 1, points=COALESCE(points,0)+? WHERE discord_id = ?", (POINTS_MVP, mvp_id,))
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
        scout_role = discord.utils.get(guild.roles, name="Scout")

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
        if scout_role:
            overwrites_coach[scout_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
        tc_coach = await guild.create_text_channel("📋 notes-coach", category=cat, overwrites=overwrites_coach)

        # Channel scoreboard — visible par les joueurs réels du match + staff
        overwrites_sb = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_messages=True),
        }
        if coach_role:
            overwrites_sb[coach_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True)
        if scout_role:
            overwrites_sb[scout_role] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
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
                    member_p = guild.get_member(int(p["id"]))
                    if member_p:
                        team_num = 1 if p in team1 else 2
                        await member_p.send(embed=discord.Embed(
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
        conn.execute("UPDATE players SET elo=?, wins=wins+1, points=COALESCE(points,0)+? WHERE discord_id=?", (new_elo, POINTS_WIN, p["id"]))
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
        conn.execute("UPDATE players SET elo=?, losses=losses+1, streak=0, points=COALESCE(points,0)+? WHERE discord_id=?", (new_elo, POINTS_LOSS, p["id"]))
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
    embed.add_field(name="Prochaine étape", value="Lie ton compte Riot avec `/setriot`, puis rejoins la queue depuis le salon correspondant à ton niveau !")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="setriot", description="Lier ton compte Riot/Valorant à ton profil")
@app_commands.describe(riot_id="Ton Riot ID (ex: Pseudo#TAG)")
async def set_riot(interaction: discord.Interaction, riot_id: str):
    if "#" not in riot_id:
        await interaction.response.send_message("❌ Format invalide ! Exemple : `MonPseudo#EUW`", ephemeral=True)
        return
    uid = str(interaction.user.id)
    await interaction.response.defer(ephemeral=True)
    conn = get_db()
    conn.execute("UPDATE players SET riot_id=? WHERE discord_id=?", (riot_id, uid))
    conn.commit()
    conn.close()
    rank_data = await sync_player_rank(uid, riot_id)
    if rank_data:
        await interaction.followup.send(
            f"✅ Compte Riot lié : **{riot_id}**\n🎯 Rang actuel : **{rank_data['tier']}** ({rank_data['rr']} RR)",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"✅ Compte Riot lié : **{riot_id}**\n⚠️ Rang non récupéré (vérifie le format ou réessaie plus tard).",
            ephemeral=True
        )



@tree.command(name="syncrank", description="Synchroniser ton rang Riot manuellement")
async def syncrank_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    conn = get_db()
    row = conn.execute("SELECT riot_id FROM players WHERE discord_id=?", (uid,)).fetchone()
    conn.close()
    if not row or not row["riot_id"]:
        await interaction.response.send_message("❌ Aucun compte Riot lié. Utilise `/setriot` d'abord.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    rank_data = await sync_player_rank(uid, row["riot_id"])
    if rank_data:
        await interaction.followup.send(
            f"✅ Rang mis à jour : **{rank_data['tier']}** ({rank_data['rr']} RR)",
            ephemeral=True
        )
    else:
        await interaction.followup.send("❌ Impossible de récupérer le rang. Réessaie plus tard.", ephemeral=True)


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
        not_registered = "Tu n'es pas inscrit" if not user else f"{target.display_name} n'est pas inscrit"
        await interaction.response.send_message(f"❌ {not_registered}. Utilise `/register` !", ephemeral=True)
        return

    rname, ricon, color = get_rank(row["elo"])
    total = row["wins"] + row["losses"]
    wr = round(row["wins"] / total * 100) if total > 0 else 0
    in_placement = is_in_placement(row["wins"], row["losses"])
    place_progress = placement_progress(row["wins"], row["losses"])

    prefix = get_player_prefix(uid)
    badges = get_player_badges(uid)
    title_name = f"{prefix}{target.display_name}" + (f" {badges}" if badges else "")
    embed = discord.Embed(title=f"📊 Stats de {title_name}", color=0x5865f2 if in_placement else color)
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
    embed.add_field(name="💰 Points",       value=f"**{row['points'] or 0}** pts", inline=True)
    if row["riot_id"]:
        riot_val = f"`{row['riot_id']}`"
        try:
            if row["val_rank"]:
                riot_val += f"\n🎯 **{row['val_rank']}**"
        except (IndexError, KeyError):
            pass
        embed.add_field(name="Compte Riot", value=riot_val, inline=False)

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
        ef.add_field(name="C'est quoi les diff\u00e9rentes queues ?", value="\U0001f451 **Radiant / Immo3** \u2014 niveau pro / ex-pro / top radiant\n\U0001f48e **Ascendant / Immo3** \u2014 niveau haut pour un cadre plus s\u00e9rieux\n\U0001f338 **Game Changers** \u2014 r\u00e9serv\u00e9e aux joueuses", inline=False)
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
    role_scout = discord.utils.get(guild.roles, name="Scout") or await get_or_create_role("Scout", 0x00bcd4, hoist=True)
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
        ch = discord.utils.get(guild.text_channels, name=name, category=category)
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
        # Les coachs n'ont pas accès aux salons staff
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

    # Salon commandes — membres peuvent écrire pour /register, /rank, /stats etc.
    ow_cmds = {
        everyone:    ow_hidden(),
        me:          ow_full(),
        role_admin:  ow_full(),
        role_membre: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, use_application_commands=True),
        role_candidat: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True, use_application_commands=True),
    }
    ch_cmds = await get_or_create_text("📝︱commandes", cat_general, ow_cmds, "Utilise ici /register, /rank, /stats, /setriot")
    async for msg in ch_cmds.history(limit=5):
        if msg.author == me and msg.embeds:
            break
    else:
        await ch_cmds.send(embed=discord.Embed(
            title="📝 Commandes disponibles",
            description=(
                "Utilise ce salon pour toutes les commandes du bot :\n\n"
                "• `/register` — créer ton profil\n"
                "• `/setriot` — lier ton compte Riot\n"
                "• `/rank` — voir ton rang et tes stats\n"
                "• `/stats` — historique détaillé\n"
                "• `/notifications` — activer/désactiver les alertes DM"
            ),
            color=0x6553e8
        ))

    # ── CATÉGORIE : QUEUES — un salon par queue ─────────────────────────
    ow_queue_shared = {
        everyone:    ow_hidden(),
        me:          ow_full(),
        role_admin:  ow_full(),
        role_membre: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True, use_application_commands=True),
    }
    cat_queues = await get_or_create_category("🎮 QUEUES")
    current_size = test_queue_size if test_mode else QUEUE_SIZE

    # Permissions chat — membres peuvent écrire
    ow_queue_chat = {
        everyone:    ow_hidden(),
        me:          ow_full(),
        role_admin:  ow_full(),
        role_membre: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }

    for qid, q_info in QUEUES.items():
        # Salon embed queue (read-only)
        ch_q = await get_or_create_text(q_info["channel"], cat_queues, ow_queue_shared, f"Queue {q_info['name']}")
        has_msg = False
        async for msg in ch_q.history(limit=10):
            if msg.author == me and msg.components:
                has_msg = True
                break
        if not has_msg:
            embed_q = discord.Embed(
                title=f"{q_info['emoji']} {q_info['name']}",
                description="Choisis ton rôle et rejoins la file d'attente.",
                color=q_info["color"]
            )
            embed_q.add_field(name="File d'attente", value=f"{'⬛' * current_size} **0/{current_size}**", inline=False)
            embed_q.set_footer(text="Choisis un rôle pour rejoindre • /notifications pour gérer tes alertes DM")
            await ch_q.send(embed=embed_q, view=SharedQueueView(qid))

        # Salon chat (membres peuvent parler)
        required_role = discord.utils.get(guild.roles, name=q_info["role"])
        ow_chat = dict(ow_queue_chat)
        if required_role:
            ow_chat[required_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        await get_or_create_text(q_info["chat"], cat_queues, ow_chat, f"Chat {q_info['name']}")

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
    app_commands.Choice(name="💎 Ascendant / Immo3", value="ascendant"),
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
    await interaction.response.send_message(
        "ℹ️ Les embeds de queue sont maintenant dans les salons `🎮 QUEUES`. Lance `/initserver` pour les (re)créer.",
        ephemeral=True
    )


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
    app_commands.Choice(name="💎 Ascendant / Immo3", value="ascendant"),
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
    app_commands.Choice(name="💎 Ascendant / Immo3", value="ascendant"),
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
    app_commands.Choice(name="💎 Ascendant / Immo3", value="ascendant"),
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
    membre_role = discord.utils.get(guild.roles, name="Membre")
    if not membre_role:
        await interaction.followup.send("❌ Rôle **Membre** introuvable sur ce serveur.", ephemeral=True)
        return

    count = 0
    skipped = 0
    for member in guild.members:
        if member.bot:
            continue
        # Seulement les membres avec le rôle "Membre" (candidatures validées)
        if membre_role not in member.roles:
            skipped += 1
            continue
        conn = get_db()
        existing = conn.execute("SELECT discord_id FROM player_channels WHERE discord_id=?", (str(member.id),)).fetchone()
        conn.close()
        if not existing:
            await create_player_space(guild, member)
            count += 1
            await asyncio.sleep(0.5)  # Éviter le rate limit Discord
    await interaction.followup.send(
        f"✅ **{count}** espace(s) créé(s).\n⏭️ **{skipped}** membre(s) ignoré(s) (pas encore validés).",
        ephemeral=True
    )



# ─────────────────────────────────────────────
#  HELPERS COSMÉTIQUES
# ─────────────────────────────────────────────

def get_player_prefix(uid: str) -> str:
    """Retourne le préfixe équipé du joueur, ou chaîne vide."""
    conn = get_db()
    row = conn.execute("""
        SELECT c.emoji FROM cosmetics c
        JOIN player_cosmetics pc ON pc.cosmetic_id = c.id
        WHERE pc.player_id=? AND pc.equipped=1 AND c.type='prefix'
    """, (uid,)).fetchone()
    conn.close()
    return (row["emoji"] + " ") if row else ""


def get_player_badges(uid: str) -> str:
    """Retourne les badges équipés du joueur sous forme de string."""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.emoji FROM cosmetics c
        JOIN player_cosmetics pc ON pc.cosmetic_id = c.id
        WHERE pc.player_id=? AND pc.equipped=1 AND c.type='badge'
        ORDER BY pc.bought_at
    """, (uid,)).fetchall()
    conn.close()
    return " ".join(r["emoji"] for r in rows) if rows else ""


def build_shop_embed(rotating: bool = False) -> discord.Embed:
    """Construit l'embed de la boutique."""
    conn = get_db()
    if rotating:
        rows = conn.execute("""
            SELECT * FROM cosmetics WHERE active=1 AND is_rotating=1
            AND (available_until IS NULL OR available_until > datetime('now'))
            ORDER BY price
        """).fetchall()
        title = "🔄 Boutique Rotative"
        color = 0xff9900
    else:
        rows = conn.execute("""
            SELECT * FROM cosmetics WHERE active=1 AND is_rotating=0
            ORDER BY type, price
        """).fetchall()
        title = "🛒 Boutique"
        color = 0x6553e8
    conn.close()

    embed = discord.Embed(title=title, color=color)
    if not rows:
        embed.description = "Aucun article disponible pour l'instant."
        return embed

    type_labels = {"role": "🎨 Rôles colorés", "badge": "🏅 Badges", "prefix": "✨ Préfixes"}
    by_type: dict = {}
    for r in rows:
        t = r["type"]
        by_type.setdefault(t, []).append(r)

    for t, items in by_type.items():
        lines = []
        for item in items:
            icon = item["emoji"] or ""
            lines.append(f"`#{item['id']}` {icon} **{item['name']}** — **{item['price']} pts**\n*{item['description'] or ''}*")
        embed.add_field(name=type_labels.get(t, t), value="\n".join(lines), inline=False)

    embed.set_footer(text="Utilise /acheter <id> pour acheter • /equiper <id> pour équiper")
    return embed


@tree.command(name="boutique", description="Voir la boutique des cosmétiques")
@app_commands.describe(type="Type de boutique")
@app_commands.choices(type=[
    app_commands.Choice(name="Boutique fixe", value="fixed"),
    app_commands.Choice(name="Boutique rotative", value="rotating"),
])
async def boutique_cmd(interaction: discord.Interaction, type: str = "fixed"):
    embed = build_shop_embed(rotating=(type == "rotating"))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="acheter", description="Acheter un cosmétique avec tes points")
@app_commands.describe(id="ID de l'article (visible dans /boutique)")
async def acheter_cmd(interaction: discord.Interaction, id: int):
    uid = str(interaction.user.id)
    conn = get_db()

    # Vérif article
    item = conn.execute("SELECT * FROM cosmetics WHERE id=? AND active=1", (id,)).fetchone()
    if not item:
        conn.close()
        await interaction.response.send_message("❌ Article introuvable.", ephemeral=True)
        return

    # Vérif article rotatif encore dispo
    if item["is_rotating"] and item["available_until"]:
        from datetime import datetime as dt
        if dt.fromisoformat(item["available_until"]) < dt.now():
            conn.close()
            await interaction.response.send_message("❌ Cette offre rotative n'est plus disponible.", ephemeral=True)
            return

    # Vérif déjà possédé
    owned = conn.execute("SELECT 1 FROM player_cosmetics WHERE player_id=? AND cosmetic_id=?", (uid, id)).fetchone()
    if owned:
        conn.close()
        await interaction.response.send_message("⚠️ Tu possèdes déjà cet article !", ephemeral=True)
        return

    # Vérif points
    player = conn.execute("SELECT points FROM players WHERE discord_id=?", (uid,)).fetchone()
    if not player:
        conn.close()
        await interaction.response.send_message("❌ Tu n'es pas inscrit ! Utilise `/register`.", ephemeral=True)
        return
    if (player["points"] or 0) < item["price"]:
        conn.close()
        await interaction.response.send_message(
            f"❌ Pas assez de points ! Il te faut **{item['price']} pts** (tu as **{player['points'] or 0} pts**).",
            ephemeral=True
        )
        return

    # Achat
    conn.execute("UPDATE players SET points=points-? WHERE discord_id=?", (item["price"], uid))
    conn.execute("INSERT INTO player_cosmetics (player_id, cosmetic_id) VALUES (?,?)", (uid, id))
    conn.commit()

    # Si c'est un rôle, l'attribuer sur Discord
    if item["type"] == "role" and item["role_color"]:
        role_name = f"✨ {item['name']}"
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if not role:
            role = await interaction.guild.create_role(
                name=role_name,
                color=discord.Color(item["role_color"]),
                hoist=False
            )
        await interaction.user.add_roles(role)
        # Sauvegarder le role_id
        conn.execute("UPDATE cosmetics SET role_color=? WHERE id=?", (item["role_color"], id))
        conn.commit()

    conn.close()

    icon = item["emoji"] or ""
    new_pts = (player["points"] or 0) - item["price"]
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"✅ Achat réussi !",
            description=f"{icon} **{item['name']}** acheté pour **{item['price']} pts**\nIl te reste **{new_pts} pts**\n\nUtilise `/equiper {id}` pour l'activer !",
            color=0x00ff88
        ),
        ephemeral=True
    )


@tree.command(name="equiper", description="Équiper ou déséquiper un cosmétique")
@app_commands.describe(id="ID du cosmétique à équiper/déséquiper")
async def equiper_cmd(interaction: discord.Interaction, id: int):
    uid = str(interaction.user.id)
    conn = get_db()

    owned = conn.execute(
        "SELECT pc.equipped, c.type, c.name, c.emoji FROM player_cosmetics pc JOIN cosmetics c ON c.id=pc.cosmetic_id WHERE pc.player_id=? AND pc.cosmetic_id=?",
        (uid, id)
    ).fetchone()
    if not owned:
        conn.close()
        await interaction.response.send_message("❌ Tu ne possèdes pas cet article.", ephemeral=True)
        return

    new_equipped = 0 if owned["equipped"] else 1

    # Pour prefix : déséquiper l'ancien si on en équipe un nouveau
    if owned["type"] == "prefix" and new_equipped == 1:
        conn.execute("""
            UPDATE player_cosmetics SET equipped=0
            WHERE player_id=? AND cosmetic_id IN (
                SELECT id FROM cosmetics WHERE type='prefix'
            )
        """, (uid,))

    conn.execute("UPDATE player_cosmetics SET equipped=? WHERE player_id=? AND cosmetic_id=?", (new_equipped, uid, id))
    conn.commit()
    conn.close()

    icon = owned["emoji"] or ""
    status = "✅ Équipé" if new_equipped else "🔕 Déséquipé"
    await interaction.response.send_message(
        f"{status} : {icon} **{owned['name']}**",
        ephemeral=True
    )


@tree.command(name="mescosmétiques", description="Voir tes cosmétiques achetés")
async def mycosmetics_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    conn = get_db()
    rows = conn.execute("""
        SELECT c.id, c.name, c.type, c.emoji, pc.equipped
        FROM cosmetics c JOIN player_cosmetics pc ON pc.cosmetic_id=c.id
        WHERE pc.player_id=?
        ORDER BY c.type, c.name
    """, (uid,)).fetchall()
    player = conn.execute("SELECT points FROM players WHERE discord_id=?", (uid,)).fetchone()
    conn.close()

    embed = discord.Embed(
        title="🎒 Mes cosmétiques",
        description=f"**{player['points'] or 0} pts** disponibles",
        color=0x6553e8
    )
    if not rows:
        embed.add_field(name="Aucun cosmétique", value="Visite `/boutique` pour acheter !", inline=False)
    else:
        type_labels = {"role": "🎨 Rôles", "badge": "🏅 Badges", "prefix": "✨ Préfixes"}
        by_type: dict = {}
        for r in rows:
            by_type.setdefault(r["type"], []).append(r)
        for t, items in by_type.items():
            lines = [f"{'✅' if i['equipped'] else '⬜'} `#{i['id']}` {i['emoji'] or ''} **{i['name']}**" for i in items]
            embed.add_field(name=type_labels.get(t, t), value="\n".join(lines), inline=False)
    embed.set_footer(text="✅ = équipé • /equiper <id> pour changer")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="addcosmétique", description="[ADMIN] Ajouter un article à la boutique")
@app_commands.describe(
    name="Nom de l'article",
    type="Type : role / badge / prefix",
    price="Prix en points",
    description="Description",
    emoji="Emoji (pour badge/prefix) ou hex color (pour role, ex: ff0000)",
    rotating="Article rotatif ?",
    days="Si rotatif : disponible combien de jours ?"
)
@app_commands.choices(type=[
    app_commands.Choice(name="Rôle coloré", value="role"),
    app_commands.Choice(name="Badge profil", value="badge"),
    app_commands.Choice(name="Préfixe pseudo", value="prefix"),
])
async def addcosmetic_cmd(
    interaction: discord.Interaction,
    name: str, type: str, price: int,
    description: str = "", emoji: str = "",
    rotating: bool = False, days: int = 7
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return

    role_color = None
    available_until = None

    if type == "role" and emoji:
        try:
            role_color = int(emoji.lstrip("#"), 16)
            emoji = ""
        except ValueError:
            await interaction.response.send_message("❌ Couleur invalide, utilise un hex sans # (ex: ff0000).", ephemeral=True)
            return

    if rotating:
        from datetime import datetime as dt, timedelta as td
        available_until = (dt.utcnow() + td(days=days)).isoformat()

    conn = get_db()
    conn.execute(
        "INSERT INTO cosmetics (name, type, description, price, role_color, emoji, is_rotating, available_until) VALUES (?,?,?,?,?,?,?,?)",
        (name, type, description, price, role_color, emoji or None, 1 if rotating else 0, available_until)
    )
    conn.commit()
    item_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    conn.close()

    await interaction.response.send_message(
        f"✅ Article **{name}** ajouté à la boutique (ID: `{item_id}`, prix: **{price} pts**).",
        ephemeral=True
    )


@tree.command(name="donnerpoints", description="[ADMIN] Donner des points à un joueur")
@app_commands.describe(user="Joueur", points="Nombre de points")
async def givepoints_cmd(interaction: discord.Interaction, user: discord.Member, points: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    conn = get_db()
    conn.execute("UPDATE players SET points=COALESCE(points,0)+? WHERE discord_id=?", (points, str(user.id)))
    conn.commit()
    row = conn.execute("SELECT points FROM players WHERE discord_id=?", (str(user.id),)).fetchone()
    conn.close()
    await interaction.response.send_message(
        f"✅ **+{points} pts** donnés à **{user.display_name}** (total: **{row['points']} pts**).",
        ephemeral=True
    )



async def notify_followers(guild: discord.Guild, joiner_uid: str, joiner_name: str, queue_id: str):
    """Envoie un DM aux joueurs qui suivent le joueur qui vient de rejoindre."""
    conn = get_db()
    rows = conn.execute(
        "SELECT follower_id FROM follows WHERE followed_id=?", (joiner_uid,)
    ).fetchall()
    conn.close()

    if not rows:
        return

    q_info = QUEUES[queue_id]
    for row in rows:
        follower_id = row["follower_id"]
        # Pas de notif si le follower est déjà en queue
        if any(p["id"] == follower_id for p in queues[queue_id]):
            continue
        try:
            member = guild.get_member(int(follower_id))
            if not member:
                continue
            embed = discord.Embed(
                title=f"👥 {joiner_name} est en queue !",
                description=f"**{joiner_name}** vient de rejoindre la queue **{q_info['emoji']} {q_info['name']}**.",
                color=q_info["color"]
            )
            embed.set_footer(text="Rejoins-le depuis le salon queue !")
            await member.send(embed=embed)
        except Exception:
            pass


@tree.command(name="suivre", description="Suivre un joueur pour être notifié quand il rejoint une queue")
@app_commands.describe(user="Joueur à suivre")
async def follow_cmd(interaction: discord.Interaction, user: discord.Member):
    uid = str(interaction.user.id)
    target_uid = str(user.id)
    if uid == target_uid:
        await interaction.response.send_message("❌ Tu ne peux pas te suivre toi-même.", ephemeral=True)
        return
    conn = get_db()
    existing = conn.execute("SELECT 1 FROM follows WHERE follower_id=? AND followed_id=?", (uid, target_uid)).fetchone()
    if existing:
        conn.close()
        await interaction.response.send_message(f"⚠️ Tu suis déjà **{user.display_name}**.", ephemeral=True)
        return
    count = conn.execute("SELECT COUNT(*) as c FROM follows WHERE follower_id=?", (uid,)).fetchone()["c"]
    if count >= 20:
        conn.close()
        await interaction.response.send_message("❌ Tu suis déjà 20 joueurs (maximum).", ephemeral=True)
        return
    conn.execute("INSERT INTO follows (follower_id, followed_id) VALUES (?,?)", (uid, target_uid))
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"✅ Tu suis maintenant **{user.display_name}** ! Tu seras notifié par DM quand il/elle rejoint une queue.",
        ephemeral=True
    )


@tree.command(name="nonsuivre", description="Ne plus suivre un joueur")
@app_commands.describe(user="Joueur à ne plus suivre")
async def unfollow_cmd(interaction: discord.Interaction, user: discord.Member):
    uid = str(interaction.user.id)
    target_uid = str(user.id)
    conn = get_db()
    conn.execute("DELETE FROM follows WHERE follower_id=? AND followed_id=?", (uid, target_uid))
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"✅ Tu ne suis plus **{user.display_name}**.", ephemeral=True)


@tree.command(name="mesamis", description="Voir la liste des joueurs que tu suis")
async def myfollows_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    conn = get_db()
    rows = conn.execute("SELECT followed_id FROM follows WHERE follower_id=?", (uid,)).fetchall()
    conn.close()
    if not rows:
        await interaction.response.send_message("Tu ne suis personne pour l'instant. Utilise `/suivre @joueur`.", ephemeral=True)
        return
    names = []
    for row in rows:
        member = interaction.guild.get_member(int(row["followed_id"]))
        names.append(f"• **{member.display_name}**" if member else f"• *Joueur inconnu ({row['followed_id']})*")
    embed = discord.Embed(
        title=f"👥 Joueurs suivis ({len(rows)}/20)",
        description="\n".join(names),
        color=0x6553e8
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="addscout", description="[ADMIN] Attribuer le rôle Scout à un membre")
@app_commands.describe(user="Membre à promouvoir Scout")
async def addscout_cmd(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    scout_role = discord.utils.get(interaction.guild.roles, name="Scout")
    if not scout_role:
        scout_role = await interaction.guild.create_role(
            name="Scout", color=discord.Color.from_str("#00bcd4"), hoist=True
        )
    await user.add_roles(scout_role)
    await interaction.response.send_message(
        f"✅ **{user.display_name}** est maintenant **Scout** ! Il peut observer les matchs en lecture seule.",
        ephemeral=True
    )


@tree.command(name="removescout", description="[ADMIN] Retirer le rôle Scout à un membre")
@app_commands.describe(user="Membre à rétrograder")
async def removescout_cmd(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    scout_role = discord.utils.get(interaction.guild.roles, name="Scout")
    if scout_role and scout_role in user.roles:
        await user.remove_roles(scout_role)
        await interaction.response.send_message(f"✅ Rôle Scout retiré à **{user.display_name}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ {user.display_name} n'a pas le rôle Scout.", ephemeral=True)


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


@tree.command(name="notifications", description="Activer ou désactiver les alertes quand une queue se lance")
async def notifications_cmd(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    conn = get_db()
    row = conn.execute("SELECT notif_enabled, notifs_channel FROM player_channels WHERE discord_id=?", (uid,)).fetchone()
    conn.close()
    if not row:
        await interaction.response.send_message("❌ Tu n'as pas encore d'espace privé.", ephemeral=True)
        return
    new_val = 0 if row["notif_enabled"] else 1
    conn2 = get_db()
    conn2.execute("UPDATE player_channels SET notif_enabled=? WHERE discord_id=?", (new_val, uid))
    conn2.commit()
    conn2.close()
    status = "🔔 **activées**" if new_val else "🔕 **désactivées**"
    await interaction.response.send_message(
        f"Alertes queue {status}. Tu peux aussi changer ça depuis ton salon 📊︱profil.",
        ephemeral=True
    )


@tree.command(name="debugroles", description="[ADMIN] Voir les noms exacts des rôles du serveur")
async def debugroles_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    roles = [r.name for r in interaction.guild.roles if "queue" in r.name.lower() or "gc" in r.name.lower() or "radiant" in r.name.lower() or "ascendant" in r.name.lower() or "game" in r.name.lower()]
    mapped = {k: v for k, v in QUEUE_ROLES.items()}
    found = {}
    for qid, rname in QUEUE_ROLES.items():
        role = discord.utils.get(interaction.guild.roles, name=rname)
        found[qid] = f"✅ `{rname}`" if role else f"❌ `{rname}` INTROUVABLE"
    txt = "**Rôles liés aux queues sur ce serveur :**\n"
    txt += "\n".join(f"• `{r}`" for r in roles) or "*(aucun trouvé)*"
    txt += "\n\n**Mapping actuel dans le bot :**\n"
    txt += "\n".join(f"• {qid} → {status}" for qid, status in found.items())
    await interaction.response.send_message(txt, ephemeral=True)


@tree.command(name="deleteplayer", description="[ADMIN] Supprimer un joueur de la BDD et son espace privé")
@app_commands.describe(user="Joueur à supprimer")
async def deleteplayer_cmd(interaction: discord.Interaction, user: discord.Member):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    uid = str(user.id)
    conn = get_db()

    # Récupère les channels avant suppression
    pch = conn.execute("SELECT * FROM player_channels WHERE discord_id=?", (uid,)).fetchone()

    # Supprime de toutes les tables
    conn.execute("DELETE FROM players WHERE discord_id=?", (uid,))
    conn.execute("DELETE FROM player_channels WHERE discord_id=?", (uid,))
    conn.execute("DELETE FROM player_queue_elo WHERE discord_id=?", (uid,))
    conn.execute("DELETE FROM applications WHERE discord_id=?", (uid,))
    conn.commit()
    conn.close()

    await interaction.followup.send(
        f"✅ **{user.display_name}** supprimé de la BDD.",
        ephemeral=True
    )


@tree.command(name="purgeplayerspaces", description="[ADMIN] Supprimer toutes les catégories privées joueurs (👤 ...)")
async def purgeplayerspaces_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    deleted_cats = 0
    deleted_channels = 0
    errors = 0

    for cat in list(guild.categories):
        if not cat.name.startswith("👤"):
            continue
        for ch in list(cat.channels):
            try:
                await ch.delete()
                deleted_channels += 1
            except Exception:
                errors += 1
        try:
            await cat.delete()
            deleted_cats += 1
        except Exception:
            errors += 1

    # Nettoie les colonnes de channel dans la BDD (garde notif_enabled)
    conn = get_db()
    conn.execute("UPDATE player_channels SET category_id=NULL, queue_channel=NULL, profil_channel=NULL, notifs_channel=NULL, history_channel=NULL, queue_message_id=NULL")
    conn.commit()
    conn.close()

    await interaction.followup.send(
        f"✅ Purge terminée !\n"
        f"• **{deleted_cats}** catégories supprimées\n"
        f"• **{deleted_channels}** salons supprimés\n"
        f"• **{errors}** erreurs\n\n"
        f"Les joueurs conservent leur profil en BDD. Lance `/initserver` pour recréer la structure si besoin.",
        ephemeral=True
    )


@tree.command(name="dbstats", description="[ADMIN] Voir l'état de la base de données")
async def dbstats_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Admin seulement.", ephemeral=True)
        return

    import os
    conn = get_db()
    db_path = os.getenv("DB_PATH", "inhouse.db")

    players      = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    members      = conn.execute("SELECT COUNT(*) FROM player_channels").fetchone()[0]
    matches_done = conn.execute("SELECT COUNT(*) FROM matches WHERE status='completed'").fetchone()[0]
    matches_act  = conn.execute("SELECT COUNT(*) FROM matches WHERE status='active'").fetchone()[0]
    applications = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    apps_pending = conn.execute("SELECT COUNT(*) FROM applications WHERE status='pending'").fetchone()[0]
    db_size      = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    conn.close()

    embed = discord.Embed(
        title="🗄️ État de la Base de Données",
        color=0x57f287,
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="📁 Fichier", value=f"`{db_path}`\n**{db_size/1024:.1f} KB**", inline=False)
    embed.add_field(name="👥 Joueurs inscrits", value=f"**{players}**", inline=True)
    embed.add_field(name="🏠 Espaces privés", value=f"**{members}**", inline=True)
    embed.add_field(name="📋 Candidatures", value=f"**{applications}** total\n**{apps_pending}** en attente", inline=True)
    embed.add_field(name="⚔️ Matchs terminés", value=f"**{matches_done}**", inline=True)
    embed.add_field(name="🔴 Matchs en cours", value=f"**{matches_act}**", inline=True)
    embed.add_field(name="💾 Volume", value="✅ Monté" if db_path.startswith("/data") else "⚠️ Local (pas de volume)", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


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
#  TASK : synchro rangs Riot quotidienne
# ─────────────────────────────────────────────
@tasks.loop(hours=24)
async def daily_rank_sync():
    """Sync les rangs Riot de tous les joueurs une fois par jour."""
    conn = get_db()
    rows = conn.execute("SELECT discord_id, riot_id FROM players WHERE riot_id IS NOT NULL").fetchall()
    conn.close()
    print(f"[RANK SYNC] Démarrage synchro {len(rows)} joueurs...")
    success = 0
    for row in rows:
        await sync_player_rank(row["discord_id"], row["riot_id"])
        success += 1
        await asyncio.sleep(2)  # 2s entre chaque req → ~30 req/min
    print(f"[RANK SYNC] Terminé — {success}/{len(rows)} joueurs syncés")


# ─────────────────────────────────────────────
#  TASK : timeout queue
# ─────────────────────────────────────────────
class StillHereView(discord.ui.View):
    """Bouton envoyé par DM pour confirmer qu'on est encore en queue."""
    def __init__(self, uid: str, queue_id: str):
        super().__init__(timeout=300)  # 5 min pour répondre
        self.uid = uid
        self.queue_id = queue_id

    @discord.ui.button(label="✅ Je suis toujours là !", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        # Reset le joined_at pour repartir à 0
        for p in queues.get(self.queue_id, []):
            if p["id"] == uid:
                p["joined_at"] = datetime.now(timezone.utc)
                break
        # Retirer du warned
        queue_timeout_warned.pop(uid, None)
        button.disabled = True
        button.label = "✅ Confirmé !"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("✅ Timer réinitialisé, tu restes en queue !", ephemeral=True)


@tasks.loop(minutes=1)
async def queue_timeout_check():
    now = datetime.now(timezone.utc)
    warning_threshold = (QUEUE_TIMEOUT - 5) * 60  # 25 min → envoyer warning
    kick_threshold = QUEUE_TIMEOUT * 60             # 30 min → kick

    guild = None
    for g in bot.guilds:
        guild = g
        break

    for qid in QUEUES:
        to_remove = []
        for p in list(queues[qid]):
            elapsed = (now - p.get("joined_at", now)).total_seconds()
            uid = p["id"]

            if elapsed >= kick_threshold:
                # Kick
                to_remove.append(p)
                queue_timeout_warned.pop(uid, None)
                try:
                    member = guild.get_member(int(uid)) if guild else None
                    if member:
                        await member.send(embed=discord.Embed(
                            title="⏰ Retiré de la queue",
                            description=f"Tu as été retiré de la queue **{QUEUES[qid]['name']}** après {QUEUE_TIMEOUT} minutes d'inactivité.",
                            color=0xff4444
                        ))
                except Exception:
                    pass
                print(f"⏰ Timeout kick {qid} : {p['name']}")

            elif elapsed >= warning_threshold and uid not in queue_timeout_warned:
                # Envoyer warning DM
                queue_timeout_warned[uid] = now
                try:
                    member = guild.get_member(int(uid)) if guild else None
                    if member:
                        embed = discord.Embed(
                            title="⚠️ Tu es encore là ?",
                            description=f"Tu es en queue **{QUEUES[qid]['name']}** depuis 25 minutes.\n\nClique sur le bouton dans les **5 minutes** sinon tu seras retiré automatiquement.",
                            color=0xffa500
                        )
                        await member.send(embed=embed, view=StillHereView(uid, qid))
                except Exception:
                    pass

        for p in to_remove:
            if p in queues[qid]:
                queues[qid].remove(p)
        if to_remove and guild:
            await update_personal_queue_embeds(guild)
            if len(queues[qid]) == 0:
                queue_ping_state[qid]["mid"] = False


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

    # Envoyer par DM à chaque joueur
    sent = 0
    for p in team1 + team2:
        if p.get("is_bot"):
            continue
        try:
            member_p = guild.get_member(int(p["id"]))
            if member_p:
                notif_embed = discord.Embed(
                    title="🎮 Code Lobby — Match en cours",
                    description=f"```{code}```\nValorant → **Jeu Personnalisé** → **Rejoindre**",
                    color=0xff4655
                )
                await member_p.send(embed=notif_embed)
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
async def on_member_remove(member: discord.Member):
    """Supprime automatiquement un joueur quand il quitte le serveur."""
    uid = str(member.id)
    conn = get_db()
    pch = conn.execute("SELECT * FROM player_channels WHERE discord_id=?", (uid,)).fetchone()
    conn.execute("DELETE FROM players WHERE discord_id=?", (uid,))
    conn.execute("DELETE FROM player_channels WHERE discord_id=?", (uid,))
    conn.execute("DELETE FROM player_queue_elo WHERE discord_id=?", (uid,))
    conn.execute("DELETE FROM applications WHERE discord_id=?", (uid,))
    conn.commit()
    conn.close()

    print(f"[AUTO] {member.display_name} a quitté — BDD supprimée")


@bot.event
async def on_ready():
    init_db()
    # Vues statiques persistantes
    bot.add_view(ApplicationButtonView())
    for qid in QUEUES:
        bot.add_view(SharedQueueView(qid))

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
    for qid in QUEUES:
        bot.add_view(SharedQueueView(qid))
    queue_timeout_check.start()
    daily_rank_sync.start()
    print("✅ Bot prêt !")


if __name__ == "__main__":
    bot.run(TOKEN)
