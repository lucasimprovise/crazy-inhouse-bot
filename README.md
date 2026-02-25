# 🎮 Valorant In-House Bot

Bot Discord complet pour gérer des in-house Valorant avec système ELO, queue équilibrée et résultats par boutons.

---

## ✅ Fonctionnalités

- **Queue 10 joueurs** avec boutons Join/Leave
- **Équilibrage par ELO** (minimise l'écart entre les deux équipes)
- **Channels vocaux** créés automatiquement à chaque match
- **Vote résultat** par boutons Discord (6/10 votes pour valider)
- **Système ELO** (K=32, algorithme Elo standard)
- **Leaderboard auto** mis à jour après chaque match
- **Historique des matchs**
- **Commandes admin** : forcer résultat, annuler match, modifier ELO

### Paliers de rang
| Rang      | ELO       |
|-----------|-----------|
| ⬛ Iron   | < 900     |
| 🟫 Bronze | 900–1049  |
| ⬜ Silver | 1050–1199 |
| 🟨 Gold   | 1200–1349 |
| 🟦 Platinum | 1350–1499 |
| 💎 Diamond | 1500–1699 |
| 👑 Radiant | 1700+    |

---

## 🚀 Installation

### 1. Prérequis
- Python 3.10+
- Un compte Discord Developer

### 2. Créer le bot Discord

1. Va sur https://discord.com/developers/applications
2. Clique **New Application** → donne un nom
3. Va dans **Bot** → **Add Bot**
4. Active ces **Privileged Gateway Intents** :
   - ✅ Server Members Intent
   - ✅ Message Content Intent
5. Copie le **Token** (garde-le secret !)
6. Va dans **OAuth2 → URL Generator** :
   - Scopes : `bot` + `applications.commands`
   - Bot Permissions : `Administrator` (ou personnalisé : Manage Channels, Send Messages, Read Messages, Connect, Manage Roles)
7. Copie l'URL générée et invite le bot sur ton serveur

### 3. Configurer le bot

```bash
# Clone / télécharge le projet
cd valorant-inhouse-bot

# Installe les dépendances
pip install -r requirements.txt

# Crée ton fichier .env
cp .env.example .env
```

Ouvre `.env` et remplis :
```
DISCORD_TOKEN=ton_token_ici
GUILD_ID=l_id_de_ton_serveur
```

> **Comment trouver ton Guild ID ?**  
> Active le mode développeur dans Discord (Paramètres → Avancé → Mode développeur),  
> puis fais clic droit sur ton serveur → **Copier l'identifiant**

### 4. Créer les channels nécessaires

Sur ton serveur Discord, crée :
- Un channel texte nommé **`queue`** (pour afficher la file)
- Un channel texte nommé **`leaderboard`** (mis à jour automatiquement)
- Un channel texte nommé **`match-results`** ou utilise n'importe quel channel

### 5. Lancer le bot

```bash
python bot.py
```

### 6. Initialiser la queue

Dans le channel `#queue`, tape :
```
/queue
```
Le bot va afficher le panneau avec les boutons Join/Leave. **C'est le seul message que tu dois créer manuellement.**

---

## 📋 Commandes

### Joueurs
| Commande | Description |
|----------|-------------|
| `/register` | S'inscrire (1000 ELO de base) |
| `/rank` | Voir ses stats |
| `/rank @user` | Voir les stats d'un autre joueur |
| `/leaderboard` | Top 10 joueurs |
| `/history` | Derniers matchs (défaut: 5) |

### Admin
| Commande | Description |
|----------|-------------|
| `/queue` | Afficher le panneau queue (à faire 1 fois) |
| `/clearqueue` | Vider la queue |
| `/setelo @user 1200` | Modifier l'ELO d'un joueur |
| `/resetplayer @user` | Remettre un joueur à 1000 ELO |

### Boutons en jeu
- **🏆 Team 1/2 a gagné** — Vote pour le résultat (6/10 pour valider)
- **⚙️ Forcer Team 1/2** — Admin force le résultat
- **🚫 Annuler le match** — Admin annule sans modifier les ELO

---

## 🔧 Hébergement 24/7

### Option A — Railway (gratuit jusqu'à 5$/mois de crédit)
1. Va sur https://railway.app
2. New Project → Deploy from GitHub (upload tes fichiers)
3. Ajoute les variables d'environnement dans Settings
4. C'est tout !

### Option B — VPS (Contabo, OVH, DigitalOcean)
```bash
# Installer screen pour garder le bot actif
sudo apt install screen
screen -S inhouse-bot
python bot.py
# Ctrl+A puis D pour détacher
```

### Option C — Raspberry Pi
Même méthode que VPS.

---

## ❓ FAQ

**Q: Les commandes n'apparaissent pas ?**  
R: Les slash commands peuvent prendre jusqu'à 1 heure à se propager. Si `GUILD_ID` est bien configuré, elles apparaissent en quelques secondes.

**Q: Le bot plante après un redémarrage et perd les matchs actifs ?**  
R: Les matchs actifs sont en mémoire RAM. Si le bot redémarre pendant un match, un admin devra utiliser `/clearqueue` et recréer le match. Pour une solution persistante, il faudrait sauvegarder `active_matches` en DB.

**Q: Comment changer la taille de la queue (ex: 6v6) ?**  
R: Change `QUEUE_SIZE = 10` en haut de `bot.py` et adapte le seuil de vote (actuellement 6/10).
