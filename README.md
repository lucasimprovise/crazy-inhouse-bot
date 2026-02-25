# 🎮 Crazy Inhouse — Bot Discord Valorant

Bot Discord complet pour gérer un serveur inhouse Valorant avec système ELO, queues multiples, espaces privés, map veto et site web public.

---

## ✅ Fonctionnalités

### Système de queues
- **3 queues indépendantes** : Radiant/Immo3, Ascendant/Plat, Game Changers
- **Équilibrage optimal** par ELO (teste toutes les combinaisons possibles parmi les 252 splits de 5v5)
- **Rôles sélectionnables** : Duelliste, Initiateur, Contrôleur, Sentinelle, Flex
- **Cooldown progressif** en cas de leaves répétés (0 → 1 → 3 → 10 → 20 min)

### Espaces privés joueurs
- Catégorie privée créée automatiquement à la validation de la candidature
- 4 salons : `🎮 queue`, `📊 profil`, `🔔 notifications`, `📜 historique`
- Statut mis à jour en temps réel (en queue / match en cours / disponible)

### Système de matchs
- **Création automatique** de catégorie + vocaux + scoreboard à chaque match
- **Map veto interactif** : les capitaines (ELO le plus élevé) bannissent à tour de rôle
- **Lobby code** partageable par le capitaine via `/lobbycode`
- **Score personnalisé** avec multiplicateur selon la dominance (13-0 vs 13-12)
- Calcul ELO par queue + ELO global

### Système ELO & Rangs
| Rang | ELO |
|------|-----|
| ⚪ Argent | < 500 |
| 🟡 Or | 500–799 |
| 🔵 Platine | 800–999 |
| 🩵 Diamant | 1000–1299 |
| 🟢 Ascendant | 1300–1599 |
| 🟠 Immortel | 1600–1999 |
| 🔴 Radiant | 2000+ |

- **Matchs de placement** (10 matchs avant d'être classé)
- **Facteur K** variable selon l'expérience
- **Streaks** de victoires suivis et affichés

### Candidatures
- Formulaire complet (Riot ID, rang, âge, présentation)
- Interface staff pour accepter/refuser avec message personnalisé
- Création automatique de l'espace privé à l'acceptation

### Site web public
- **`/`** — Accueil avec stats globales, top 3, derniers matchs
- **`/leaderboard`** — Classement global et par queue
- **`/matches`** — Historique des 50 derniers matchs
- **`/player/<discord_id>`** — Profil détaillé d'un joueur

---

## 🚀 Installation locale

### 1. Prérequis
- Python 3.11+
- Un compte Discord Developer

### 2. Créer le bot Discord

1. Va sur https://discord.com/developers/applications
2. **New Application** → donne un nom
3. Va dans **Bot** → **Add Bot**
4. Active ces **Privileged Gateway Intents** :
   - ✅ Server Members Intent
   - ✅ Message Content Intent
5. Copie le **Token**
6. Va dans **OAuth2 → URL Generator** :
   - Scopes : `bot` + `applications.commands`
   - Permissions : `Administrator`
7. Invite le bot sur ton serveur

### 3. Configurer et lancer

```bash
pip install -r requirements.txt
cp .env.example .env
# Remplis DISCORD_TOKEN et GUILD_ID dans .env
python bot.py
```

---

## ☁️ Déploiement Railway

### 1. GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/TON_USERNAME/crazy-inhouse-bot.git
git push -u origin main
```

### 2. Railway
1. Va sur https://railway.app → **New Project** → **Deploy from GitHub**
2. Sélectionne ton repo
3. Onglet **Variables** → ajoute :
   - `DISCORD_TOKEN` — ton token bot
   - `GUILD_ID` — l'ID de ton serveur
4. Ajoute un **Volume** monté sur `/data` → ajoute `DB_PATH=/data/inhouse.db`
5. **Settings → Networking → Generate Domain** pour le site web public

### 3. Mises à jour futures
```bash
git add .
git commit -m "description"
git push
```
Railway redéploie automatiquement.

---

## 📋 Commandes

### Joueurs
| Commande | Description |
|----------|-------------|
| `/register` | S'inscrire / mettre à jour son Riot ID |
| `/rank` | Voir ses stats et ELO |
| `/history` | Historique de ses matchs |

### Candidatures
| Commande | Description |
|----------|-------------|
| `/apply` | Soumettre une candidature |

### Admin — Serveur
| Commande | Description |
|----------|-------------|
| `/initserver` | Créer tous les salons et catégories |
| `/fillchannels` | Mettre à jour le contenu des salons info |
| `/setupspaces` | Créer les espaces privés (membres validés uniquement) |

### Admin — Queue & Matchs
| Commande | Description |
|----------|-------------|
| `/setqueue` | Afficher le panneau queue dans le salon courant |
| `/clearqueue` | Vider toutes les queues |
| `/fillqueue` | Remplir la queue avec des joueurs fictifs (tests) |
| `/clearfake` | Supprimer les joueurs fictifs de la DB |
| `/setelo @user 1200` | Modifier l'ELO d'un joueur |
| `/setqueueelo @user queue 1200` | Modifier l'ELO par queue |
| `/resetplayer @user` | Remettre à 1000 ELO |
| `/forcefinish` | Forcer la fin d'un match |
| `/lobbycode CODE` | Partager le code lobby (capitaines uniquement) |

### Admin — Tests & Debug
| Commande | Description |
|----------|-------------|
| `/testmode` | Activer le mode test (queue réduite) |
| `/testveto` | Tester le système de veto seul |
| `/dbstats` | Voir l'état de la base de données |

---

## 📁 Structure du projet

```
valorant-inhouse-bot/
├── bot.py          # Bot Discord principal (~3300 lignes)
├── web.py          # Serveur web Flask public
├── requirements.txt
├── Procfile        # Démarrage Railway
├── runtime.txt     # Version Python
├── .env.example    # Template variables d'environnement
└── .gitignore
```

---

## ❓ FAQ

**Les commandes n'apparaissent pas ?**
Les slash commands peuvent prendre jusqu'à 1h. Avec `GUILD_ID` configuré, elles apparaissent en quelques secondes.

**La DB est perdue après un redémarrage ?**
Assure-toi d'avoir un Volume Railway monté sur `/data` et la variable `DB_PATH=/data/inhouse.db`. Vérifie avec `/dbstats` que le chemin commence par `/data`.

**Un match est bloqué après un redémarrage ?**
Les matchs actifs sont en mémoire. Utilise `/forcefinish` s'il est encore en cours, sinon nettoie manuellement la catégorie Discord et la DB.
