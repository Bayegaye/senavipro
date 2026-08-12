# SENAVIPRO — Plateforme de gestion des ventes et achats

Application web pour l'enregistrement et la gestion des ventes et achats
d'œufs de table et de poulets de chair. Utilisable localement sur votre
ordinateur (aucune connexion internet requise), ou hébergée en ligne pour un
accès depuis n'importe où — voir **[DEPLOIEMENT.md](DEPLOIEMENT.md)**.

## Usage local — démarrage rapide

**Prérequis : Python 3.9+ installé** (https://www.python.org/downloads/,
cocher "Add Python to PATH" à l'installation).

### Windows
Double-cliquez sur **`start.bat`**. Le script installe tout automatiquement
au premier lancement, puis ouvre l'application dans votre navigateur.

### Mac / Linux
Dans un terminal, à la racine du dossier `senavipro` :
```bash
./start.sh
```

Les lancements suivants sont plus rapides (l'installation ne se refait pas).
Pour arrêter l'application, fermez la fenêtre/terminal ou faites Ctrl+C.

## Compte administrateur par défaut

- Identifiant : `admin`
- Mot de passe : `senavipro2026`

Changez ce mot de passe dès la première utilisation : connectez-vous, allez
dans **Utilisateurs**, créez votre propre compte admin, puis désactivez le
compte `admin` par défaut.

## Fonctionnalités

- **Ventes et achats** : produit, client/fournisseur, quantité, prix, date
  (le prix par défaut du produit se pré-remplit automatiquement)
- **Dépenses générales** : aliment volaille, vétérinaire, transport, salaires,
  loyer, etc. — distinctes des achats de marchandises
- **Tableau de bord** : chiffre d'affaires, achats, dépenses générales et
  **bénéfice réel** (ventes − achats − dépenses) du jour et du mois
- **Produits** : ajout, modification et suppression de produits (au-delà des
  deux par défaut), avec unité, prix par défaut et seuil d'alerte de stock
- **Stock** : mis à jour automatiquement à chaque vente/achat, alertes de stock bas
- **Clients et fournisseurs** : répertoire avec historique des montants
- **Rapports** : chiffre d'affaires, achats, dépenses, bénéfice réel sur une
  période choisie, répartition des dépenses par catégorie, graphique
  d'évolution, export CSV (transactions et dépenses séparément)
- **Deux rôles** : Administrateur (prix, produits, stock, utilisateurs, suppressions) et
  Employé (enregistre les ventes/achats/dépenses, consulte le reste)

## Utilisation en réseau local (plusieurs postes)

Pour que d'autres ordinateurs du même réseau (Wi-Fi/local) accèdent à la
plateforme depuis le poste qui héberge l'application :

1. Notez l'adresse IP locale du poste hôte (ex. `192.168.1.20`).
2. Les autres postes ouvrent `http://192.168.1.20:5000` dans leur navigateur.
3. Le pare-feu Windows peut demander une autorisation la première fois —
   acceptez l'accès réseau pour Python.

Toutes les données restent stockées uniquement sur le poste hôte, dans le
fichier `instance/senavipro.db`.

## Dépannage

**"ERR_CONNECTION_REFUSED" / "127.0.0.1 n'autorise pas la connexion"**
Le navigateur s'est ouvert un instant avant que le serveur soit prêt.
Revenez à la fenêtre noire (invite de commandes) : si vous voyez la ligne
`Running on http://127.0.0.1:5000`, le serveur tourne — rechargez simplement
la page (F5) dans le navigateur. Si la fenêtre affiche un message d'erreur
à la place, lisez-le : il indique généralement la cause (Python manquant,
dépendances non installées, etc.) et le script vous donne des instructions.

**"Python n'est pas reconnu" / "python n'est pas installe"**
Installez Python depuis https://www.python.org/downloads/ et cochez bien
la case "Add Python to PATH" pendant l'installation, puis relancez `start.bat`.

**Le port 5000 est déjà utilisé**
Un autre programme utilise peut-être ce port. Fermez les autres instances de
SENAVIPRO en cours d'exécution, ou modifiez le port dans `app.py`
(dernière ligne : `app.run(..., port=5000)`) et remplacez-le par un autre
numéro, ex. `5050`.

**Rien ne se passe au double-clic sur start.bat**
Assurez-vous d'avoir bien extrait tout le contenu du zip dans un dossier
(clic droit sur le zip → "Extraire tout...") avant de lancer `start.bat`
depuis ce dossier extrait — lancer le script depuis l'intérieur du zip
peut empêcher la création correcte de l'environnement.

## Sauvegarde des données

Toutes les données (ventes, achats, stock, clients) sont dans un seul
fichier : `instance/senavipro.db`. Pour sauvegarder, copiez simplement ce
fichier ailleurs (clé USB, cloud, etc.). Pour restaurer, remettez-le à sa
place avant de relancer l'application.

## Gestion des produits

Quatre produits sont créés par défaut :

- Œufs de table - Petit calibre (unité : plateau)
- Œufs de table - Moyen calibre (unité : plateau)
- Œufs de table - Gros calibre (unité : plateau)
- Poulets de chair (unité : unité)

Ces produits par défaut apparaissent automatiquement au démarrage, même sur
une base de données déjà en service (aucune donnée existante n'est modifiée
ou supprimée).

Pour ajouter d'autres produits (ex. œufs extra, poulets locaux, sujets d'un
jour...), connectez-vous en tant qu'administrateur et ouvrez la page
**Produits** dans le menu : renseignez le nom, l'unité, le stock initial, le
seuil d'alerte et les prix par défaut. Les prix par défaut se pré-remplissent
ensuite automatiquement dans les formulaires de vente/achat (modifiables au
cas par cas). Un produit ne peut être supprimé que s'il n'a aucune
transaction liée et un stock à zéro.

## Structure du projet

```
senavipro/
├── start.bat / start.sh      # Lancement en un clic (usage local)
├── app.py                     # Routes et logique de l'application
├── models.py                   # Modèles de données
├── seed.py                      # Données de démarrage (admin, produits)
├── init_db.py                    # Initialisation de la base
├── wsgi.py                        # Point d'entrée production (gunicorn)
├── Procfile                        # Commande de démarrage (Render/Railway)
├── Dockerfile / docker-compose.yml  # Déploiement Docker (VPS)
├── render.yaml                      # Déploiement Render en un clic
├── .env.example                     # Variables d'environnement à définir
├── requirements.txt
├── requirements-postgres.txt        # Pilote PostgreSQL (serveur uniquement)
├── DEPLOIEMENT.md                    # Guide de mise en ligne
├── templates/                         # Pages HTML (Bootstrap 5)
└── static/img/logo.jpg                # Logo SENAVIPRO
```
