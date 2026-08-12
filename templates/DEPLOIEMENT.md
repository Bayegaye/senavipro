# Déployer SENAVIPRO en ligne

Cette application est prête pour l'hébergement en ligne en plus de l'usage
local (`start.bat` / `start.sh` continuent de fonctionner normalement, sans
rien configurer). Ce guide couvre trois façons courantes de la mettre en
ligne, de la plus simple à la plus autonome.

## Important : deux fichiers de dépendances

- `requirements.txt` : dépendances communes (usage local et en ligne).
- `requirements-postgres.txt` : pilote PostgreSQL, **à installer uniquement
  sur le serveur/hébergeur** (pas en local). Ce paquet nécessite une
  compilation qui échoue sur Windows sans les "Microsoft C++ Build Tools" —
  `start.bat`/`start.sh` ne l'installent volontairement pas, l'usage local
  fonctionne avec SQLite sans lui.

## Avant de déployer — checklist sécurité

- [ ] Définir `SECRET_KEY` (valeur secrète unique, ne jamais la partager)
- [ ] Définir `ADMIN_PASSWORD` (sinon le mot de passe par défaut `senavipro2026`
      reste actif et connu de quiconque lit ce dépôt)
- [ ] Utiliser PostgreSQL via `DATABASE_URL` plutôt que SQLite (voir pourquoi
      ci-dessous)
- [ ] Une fois en ligne, connectez-vous et changez le mot de passe admin
      depuis l'application (page **Utilisateurs**), même si `ADMIN_PASSWORD`
      a été défini

**Pourquoi pas SQLite en hébergement ?** La plupart des hébergeurs (Render,
Railway, Heroku...) redémarrent l'application sur un disque neuf à chaque
déploiement ou redémarrage : un fichier SQLite local serait effacé et vous
perdriez toutes vos données. PostgreSQL (fourni par ces mêmes hébergeurs)
est persistant.

---

## Étape préalable — mettre le code sur GitHub (si ce n'est pas déjà fait)

Render (comme Railway) déploie depuis un dépôt Git. Si vous n'avez pas encore
de compte GitHub ni de dépôt, voici le chemin le plus simple, sans avoir
besoin d'installer Git :

1. Créez un compte sur [github.com](https://github.com/join) (gratuit).
2. Une fois connecté, cliquez sur le **+** en haut à droite → **New repository**.
   Nom : `senavipro` (ou autre), visibilité **Private** conseillée, ne cochez
   aucune case d'initialisation (pas de README). Cliquez **Create repository**.
3. Sur la page du dépôt vide, cliquez le lien **uploading an existing file**.
4. Extrayez le zip SENAVIPRO sur votre ordinateur si ce n'est pas déjà fait,
   puis **glissez-déposez tout le contenu du dossier extrait** (pas le
   dossier lui-même, son contenu : `app.py`, `templates/`, `static/`,
   `render.yaml`, etc.) dans la zone d'upload de GitHub. Les navigateurs
   récents (Chrome, Edge) acceptent de glisser des sous-dossiers entiers.
5. En bas de page, ajoutez un message (ex. "Version initiale") et cliquez
   **Commit changes**.

Votre code est maintenant sur GitHub, prêt à être relié à Render à l'étape
suivante.

## Option 1 — Render ou Railway (le plus simple)

Ces hébergeurs déploient directement depuis un dépôt Git (GitHub/GitLab) et
gèrent HTTPS automatiquement.

**Étapes générales (Render) :**
1. Le code est sur GitHub (voir étape préalable ci-dessus si pas encore fait).
2. Créez un compte sur [render.com](https://render.com) — vous pouvez vous
   inscrire directement avec votre compte GitHub, ce qui simplifie l'étape
   suivante.
3. Dans le tableau de bord Render : **New** → **Blueprint**. Autorisez Render
   à accéder à votre compte GitHub si demandé, puis sélectionnez le dépôt
   `senavipro`. Le fichier `render.yaml` fourni configure automatiquement le
   service web et une base PostgreSQL gratuite reliés entre eux.
   - Sans Blueprint : **New** → **Web Service**, build command
     `pip install -r requirements.txt -r requirements-postgres.txt`, start command
     `python init_db.py && gunicorn wsgi:app`.
4. Render vous demandera de définir `ADMIN_PASSWORD` (variable marquée
   `sync: false`) — indiquez un mot de passe fort.
5. Sans Blueprint uniquement : ajoutez une base PostgreSQL (**New** →
   **PostgreSQL**), et reliez sa `Connection String` à la variable
   `DATABASE_URL` du service web.
6. Cliquez **Apply** / **Create Web Service**. Le premier déploiement prend
   quelques minutes. L'URL fournie par Render (`https://senavipro.onrender.com`
   ou similaire) est accessible publiquement, en HTTPS automatiquement.

**Railway** fonctionne de façon similaire : *New Project* → *Deploy from
GitHub repo*, ajoutez un service PostgreSQL depuis le catalogue (Railway
relie automatiquement `DATABASE_URL`), puis définissez `SECRET_KEY` et
`ADMIN_PASSWORD` dans l'onglet *Variables*. Build command :
`pip install -r requirements.txt -r requirements-postgres.txt`. Start command :
`python init_db.py && gunicorn wsgi:app`.

Coût : ces deux hébergeurs proposent un palier gratuit ou très économique
suffisant pour une petite entreprise (vérifiez leurs conditions actuelles,
elles évoluent régulièrement).

---

## Option 2 — Votre propre serveur (VPS) avec Docker

Fonctionne sur n'importe quel serveur Linux avec Docker installé (OVH,
Contabo, DigitalOcean, Hetzner...). Inclut PostgreSQL, pas de configuration
manuelle de base de données.

```bash
# Sur le serveur, après avoir copié le dossier du projet :
cp .env.example .env
# Modifiez .env : SECRET_KEY, ADMIN_PASSWORD (ou éditez docker-compose.yml
# directement pour ces valeurs)

docker compose up -d --build
```

L'application est alors accessible sur `http://adresse-du-serveur:5000`.

Pour un vrai nom de domaine et du HTTPS, placez un reverse proxy devant
(Nginx + Certbot/Let's Encrypt, ou [Caddy](https://caddyserver.com/) qui gère
le HTTPS automatiquement). Exemple minimal avec Caddy : faites pointer votre
domaine vers le serveur, installez Caddy, et utilisez un `Caddyfile` :

```
votredomaine.com {
    reverse_proxy localhost:5000
}
```

---

## Option 3 — Hébergeur Python classique (PythonAnywhere, etc.)

Ces hébergeurs attendent une application WSGI. Pointez leur configuration
vers `wsgi:app` (le fichier `wsgi.py` fourni), installez les dépendances
depuis `requirements.txt` ET `requirements-postgres.txt`, définissez les variables d'environnement
(`SECRET_KEY`, `DATABASE_URL`, `ADMIN_PASSWORD`) dans leur interface, puis
exécutez `python init_db.py` une première fois via leur console avant de
démarrer le service.

---

## Variables d'environnement (résumé)

| Variable         | Obligatoire en ligne | Rôle                                              |
|------------------|:---------------------:|----------------------------------------------------|
| `SECRET_KEY`     | Oui                    | Sécurité des sessions/cookies                      |
| `DATABASE_URL`   | Fortement recommandé   | Connexion PostgreSQL (sinon SQLite local, non persistant) |
| `ADMIN_PASSWORD` | Recommandé             | Mot de passe du compte admin créé au 1er démarrage |
| `PORT`           | Non (auto)             | Port d'écoute — la plupart des hébergeurs le définissent eux-mêmes |

Voir `.env.example` pour le détail.

## Après le déploiement

- Connectez-vous avec `admin` et le mot de passe défini (`ADMIN_PASSWORD` ou
  `senavipro2026` par défaut), puis créez votre propre compte administrateur
  et désactivez le compte `admin` par défaut (page **Utilisateurs**).
- Vérifiez que l'URL est bien en `https://` (cadenas dans le navigateur).
- Pensez aux sauvegardes régulières de la base PostgreSQL (la plupart des
  hébergeurs proposent des sauvegardes automatiques ou des exports manuels).
