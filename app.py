import csv
import io
import os
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request, flash, session,
    Response, abort, jsonify
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from sqlalchemy import func, inspect, text

from models import db, User, Partner, Product, Transaction, Expense, Sale, Order, Loss, OrderGroup, DeliveryTier, SupplierPayment, CapitalSettings, CapitalSnapshot

APP_NAME = "SENAVIPRO"

# Images illustrant chaque produit sur la page de commande en ligne
# (/commander). La correspondance se fait par le début du nom du produit
# (insensible à la casse) : par exemple "Oeufs de table - Gros calibre"
# correspond à la clé "oeufs de table". Un produit dont le nom ne correspond
# à aucune clé ci-dessous s'affiche simplement sans image.
PRODUCT_IMAGES = {
    "oeufs de table": "img/products/oeufs_table.jpg",
    "poulets de chair": "img/products/poulets_chair.jpg",
    "bissap": "img/products/bissap.jpg",
    "bouye": "img/products/bouye.jpg",
    "gingembre": "img/products/gingembre.jpg",
    "niebe": "img/products/niebe.jpg",
    "niébé": "img/products/niebe.jpg",
}


def _product_image(product_name):
    """Retourne l'URL statique de l'image correspondant à un produit, ou None
    si aucune image n'est disponible pour ce produit."""
    name_lower = product_name.strip().lower()
    for key, path in PRODUCT_IMAGES.items():
        if name_lower.startswith(key):
            return url_for("static", filename=path)
    return None

# Coordonnées de l'entreprise affichées sur les factures et sur le site public.
COMPANY_INFO = {
    "rccm": "RCCM N° 2025M076",
    "ninea": "NINEA N° 009458987 212",
    "fra": "FRA N° 1839/2025/FRA",
    "address": "Sangalkam, Rufisque, Dakar",
    "phone": "221 78 207 87 87",
    # Numéro au format international sans espaces ni "+", pour les liens
    # tel:/wa.me du site public (ex: appels et WhatsApp en un clic).
    "phone_intl": "221782078787",
}


def _database_uri():
    """Lit DATABASE_URL (fourni par la plupart des hébergeurs pour Postgres).
    À défaut, utilise un fichier SQLite local — pratique pour l'usage local/hors ligne.
    Certains hébergeurs (Render, Railway, Heroku) fournissent une URL commençant
    par 'postgres://' ou 'postgresql://' ; on la convertit vers le dialecte
    'postgresql+psycopg://' pour utiliser le pilote psycopg (v3, voir
    requirements-postgres.txt) plutôt que psycopg2 par défaut."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "sqlite:///senavipro.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _parse_date(value, default=None):
    """Convertit une chaîne 'YYYY-MM-DD' (ex: venant de request.args) en objet
    date Python. Nécessaire pour PostgreSQL, qui refuse de comparer une
    colonne DATE à une chaîne de texte brute (contrairement à SQLite, plus
    permissif) : sans cette conversion, les filtres par date provoquaient une
    erreur 500 en production ("operator does not exist: date >= character
    varying")."""
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default


def _parse_decimal(value):
    """Convertit une chaîne saisie par l'utilisateur en nombre flottant, en
    acceptant aussi bien le point que la virgule comme séparateur décimal.
    Nécessaire car le clavier numérique de nombreux téléphones/tablettes en
    français insère une virgule par défaut, ce que le simple float() de
    Python refuse (ValueError) — ce qui bloquait silencieusement la création
    ou la modification des ventes, achats, dépenses, etc. depuis mobile."""
    return float(str(value).strip().replace(",", "."))


IS_PRODUCTION = os.environ.get("DATABASE_URL") is not None

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "senavipro-secret-key-change-en-production")
app.config["SQLALCHEMY_DATABASE_URI"] = _database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Cookies de session sécurisés (HTTPS uniquement) une fois en ligne derrière
# un hébergeur qui termine le TLS (Render, Railway...). Sans effet en local.
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION

if IS_PRODUCTION and app.config["SECRET_KEY"] == "senavipro-secret-key-change-en-production":
    raise RuntimeError(
        "SECRET_KEY par défaut détectée en production ! "
        "Définissez la variable d'environnement SECRET_KEY avant de déployer "
        "(voir DEPLOIEMENT.md)."
    )

db.init_app(app)


def _ensure_column(inspector, table, column, ddl_type="INTEGER"):
    """Ajoute une colonne à une table existante si elle manque encore —
    db.create_all() ne crée que les tables absentes, il ne modifie jamais une
    table déjà présente en base. Sans effet (et sûr à ré-exécuter) si la
    colonne existe déjà."""
    if table not in inspector.get_table_names():
        return
    colonnes = {c["name"] for c in inspector.get_columns(table)}
    if column not in colonnes:
        with db.engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def _ensure_schema_upgrades():
    """Applique en base les petites évolutions de schéma qui ne sont pas gérées par
    db.create_all() (celui-ci ne crée que les tables manquantes, il ne modifie pas
    les tables déjà existantes). Comme le projet n'utilise pas d'outil de migration
    (Alembic/Flask-Migrate), on fait ici une mise à jour idempotente, compatible
    SQLite (local) et PostgreSQL (production) :
    - ajoute la colonne sale_id à la table transactions si elle n'existe pas
      encore (nécessaire pour regrouper plusieurs produits vendus au même
      client sous une seule facture) ;
    - ajoute la colonne sale_id à la table orders si elle n'existe pas encore
      (nécessaire pour relier une commande confirmée à la facture générée —
      une table orders a pu être créée par un déploiement antérieur à
      l'ajout de cette colonne au modèle Order) ;
    - ajoute la colonne order_group_id à la table orders si elle n'existe pas
      encore (relie une ligne de commande à son panier d'origine côté site
      public — voir OrderGroup — sans effet sur les commandes internes
      existantes, saisies sans panier et dont cette colonne reste vide)."""
    inspector = inspect(db.engine)
    _ensure_column(inspector, "transactions", "sale_id", "INTEGER")
    _ensure_column(inspector, "orders", "sale_id", "INTEGER")
    _ensure_column(inspector, "orders", "order_group_id", "INTEGER")


def _ensure_frais_livraison_fixe():
    """Passe la livraison à un tarif unique de 2000 FCFA quelle que soit la
    quantité commandée, en remplaçant les anciens paliers par quantité
    (1-5, 6-15, 16-30, 31+) par un seul palier ouvert (1 à l'infini, 2000
    FCFA). Idempotente et sûre à ré-exécuter à chaque démarrage : si un seul
    palier est déjà configuré (que ce soit celui-ci ou un autre tarif choisi
    depuis la page Livraison > Tarifs), cette fonction ne touche à rien —
    elle ne fait la consolidation qu'une fois, tant que plusieurs paliers
    coexistent encore."""
    tiers = DeliveryTier.query.all()
    if len(tiers) <= 1:
        return
    for tier in tiers:
        db.session.delete(tier)
    db.session.add(DeliveryTier(quantite_min=1, quantite_max=None, prix=2000))
    db.session.commit()
    print("Livraison consolidée sur un tarif unique de 2000 FCFA (quelle que soit la quantité).")


def _generer_factures_manquantes():
    """Génère rétroactivement une facture individuelle pour chaque vente
    enregistrée avant l'ajout de la facturation automatique sur le
    formulaire « Vente rapide » (ces ventes existent en base sans sale_id,
    donc sans lien facture affiché). Idempotente et sûre à ré-exécuter à
    chaque démarrage : ne traite que les ventes qui n'ont encore aucune
    facture associée."""
    ventes_sans_facture = (
        Transaction.query.filter_by(type="vente", sale_id=None)
        .order_by(Transaction.date, Transaction.created_at, Transaction.id)
        .all()
    )
    if not ventes_sans_facture:
        return
    annee_courante = datetime.utcnow().year

    # Comme _next_sale_numero(), on se base sur le plus grand numéro déjà
    # utilisé PAR ANNÉE (jamais sur un simple compteur global) : une
    # facture supprimée entre-temps ne doit jamais faire réattribuer un
    # numéro déjà pris, sous peine de "duplicate key" sur sales.numero.
    prochain_numero_par_annee = {}

    def _prochain_numero(annee):
        if annee not in prochain_numero_par_annee:
            prefix = f"FAC-{annee}-"
            max_seq = 0
            for (numero,) in db.session.query(Sale.numero).filter(Sale.numero.like(f"{prefix}%")).all():
                try:
                    seq = int(numero.rsplit("-", 1)[-1])
                except (ValueError, AttributeError):
                    continue
                max_seq = max(max_seq, seq)
            prochain_numero_par_annee[annee] = max_seq + 1
        seq = prochain_numero_par_annee[annee]
        prochain_numero_par_annee[annee] += 1
        return seq

    for tr in ventes_sans_facture:
        annee = tr.date.year if tr.date else annee_courante
        vente = Sale(
            numero=f"FAC-{annee}-{_prochain_numero(annee):05d}",
            partner_id=tr.partner_id,
            total=tr.total,
            date=tr.date,
            user_id=tr.user_id,
            created_at=tr.created_at,
        )
        db.session.add(vente)
        db.session.flush()  # pour obtenir vente.id avant de l'associer
        tr.sale_id = vente.id
    db.session.commit()


# Crée automatiquement les tables et données par défaut manquantes à chaque
# démarrage (opération sûre, sans effet si elles existent déjà) — évite les
# erreurs "no such table" et fait apparaître les nouveaux produits par
# défaut même sur une base de données déjà en service.
with app.app_context():
    db.create_all()
    _ensure_schema_upgrades()
    _generer_factures_manquantes()
    from seed import ensure_seed_data
    ensure_seed_data(verbose=False)
    _ensure_frais_livraison_fixe()

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Veuillez vous connecter pour accéder à cette page."
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Accès réservé à l'administrateur.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    return {"app_name": APP_NAME, "now": datetime.utcnow(), "company_info": COMPANY_INFO}


# ---------- Authentification ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.active and user.check_password(password):
            login_user(user)
            flash(f"Bienvenue, {user.full_name}.", "success")
            return redirect(url_for("dashboard"))
        flash("Identifiant ou mot de passe incorrect.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Vous êtes déconnecté(e).", "info")
    return redirect(url_for("login"))


# ---------- Application Android (Trusted Web Activity) ----------
#
# Fichier de vérification exigé par Android pour lier ce domaine à
# l'application Android "Senavi Livraison" (package com.senavipro.app) :
# sans lui, l'application s'affiche avec la barre d'adresse du navigateur
# au lieu du plein écran natif. Doit rester accessible sans authentification
# à l'URL exacte /.well-known/assetlinks.json.

@app.route("/.well-known/assetlinks.json")
def android_asset_links():
    return jsonify([
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.senavipro.app",
                "sha256_cert_fingerprints": [
                    "C1:A1:F4:03:6C:AF:B4:1A:C5:B4:D3:E6:A0:64:D6:BC:2F:F1:48:A4:DD:AF:78:0B:52:1D:1A:43:AC:14:5A:C3"
                ],
            },
        }
    ])


# ---------- Site public (vitrine) ----------
#
# Pages publiques présentant Senavi Pro (poulets de chair, œufs de table,
# produits locaux) aux visiteurs, sans authentification. Le personnel connecté
# qui arrive sur "/" est automatiquement redirigé vers son tableau de bord
# interne, comme avant l'ajout de ce site public.

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("public_accueil.html")


@app.route("/nos-produits")
def produits_public():
    return render_template("public_produits.html")


@app.route("/a-propos")
def apropos_public():
    return render_template("public_apropos.html")


@app.route("/contact")
def contact_public():
    return render_template("public_contact.html")


@app.route("/politique-de-confidentialite")
def confidentialite_public():
    return render_template("public_confidentialite.html")


def _paliers_livraison():
    return DeliveryTier.query.order_by(DeliveryTier.quantite_min).all()


def _tarif_livraison(quantite_totale):
    """Frais de livraison correspondant à une quantité totale de panier
    (tous produits confondus), selon les paliers définis par l'administrateur
    (page Livraison > Tarifs). Renvoie 0 si aucun palier ne correspond (ex.
    panier vide) ou si aucun palier n'est configuré."""
    if quantite_totale <= 0:
        return 0.0
    tier = (
        DeliveryTier.query.filter(DeliveryTier.quantite_min <= quantite_totale)
        .filter(db.or_(DeliveryTier.quantite_max.is_(None), DeliveryTier.quantite_max >= quantite_totale))
        .order_by(DeliveryTier.quantite_min.desc())
        .first()
    )
    if tier:
        return tier.prix
    # Quantité au-delà de tous les paliers définis (ex. administrateur n'a
    # configuré que jusqu'à 30 et le client en commande 50) : on applique le
    # tarif du palier le plus élevé plutôt que de ne rien facturer.
    dernier = DeliveryTier.query.order_by(DeliveryTier.quantite_min.desc()).first()
    return dernier.prix if dernier else 0.0


def _next_order_group_numero():
    """Numéro de commande en ligne "CMD-<année>-<séquence>", même logique que
    _next_sale_numero() (séquence par année, basée sur le plus grand numéro
    déjà utilisé plutôt qu'un COUNT(*), pour éviter tout conflit après une
    suppression)."""
    annee = datetime.utcnow().year
    prefix = f"CMD-{annee}-"
    max_seq = 0
    existants = db.session.query(OrderGroup.numero).filter(OrderGroup.numero.like(f"{prefix}%")).all()
    for (numero,) in existants:
        try:
            seq = int(numero.rsplit("-", 1)[-1])
        except (ValueError, AttributeError):
            continue
        max_seq = max(max_seq, seq)
    return f"{prefix}{max_seq + 1:05d}"


def _get_or_create_client_by_phone(name, phone):
    """Retrouve un client existant par téléphone (prioritaire, car un même
    client peut donner des variantes de son nom d'une commande à l'autre) ou,
    à défaut, par nom (voir _get_or_create_client) ; sinon en crée un nouveau.
    Utilisé pour les commandes passées depuis le site public, où le client
    n'est pas sélectionné dans un répertoire mais saisit ses coordonnées
    lui-même. Ne fait pas de commit : à intégrer dans la transaction en
    cours."""
    phone = (phone or "").strip()
    name = (name or "").strip() or "Client site public"
    if phone:
        client = Partner.query.filter(Partner.type == "client", Partner.phone == phone).first()
        if client:
            return client
    client = _get_or_create_client(name)
    if client and phone and not client.phone:
        client.phone = phone
    return client


def _system_user_id():
    """Identifiant du compte technique 'boutique-en-ligne' (voir seed.py),
    utilisé comme auteur des commandes passées par des clients depuis le site
    public — celles-ci ne sont l'action d'aucun membre de l'équipe, mais
    Order.user_id / Transaction.user_id sont obligatoires."""
    user = User.query.filter_by(username="boutique-en-ligne").first()
    return user.id if user else current_user_or_admin_id()


def current_user_or_admin_id():
    """Repli si le compte technique n'a pas encore été créé (ne devrait pas
    arriver, ensure_seed_data() le crée au démarrage) : utilise le premier
    compte administrateur trouvé plutôt que de faire échouer la commande."""
    admin = User.query.filter_by(role="admin").order_by(User.id).first()
    return admin.id if admin else None


@app.route("/commander", methods=["GET", "POST"])
def commander():
    """Page publique de commande en ligne : le client voit les produits
    disponibles avec leur prix de vente, compose son panier (plusieurs
    produits et quantités), et le prix de livraison se recalcule selon la
    quantité totale commandée. La commande apparaît ensuite sur la page
    Livraisons de la plateforme interne pour que l'équipe organise la
    livraison — comme pour les commandes internes (page Commandes), aucun
    stock n'est débité tant que la commande n'est pas confirmée."""
    # Catégories d'œufs autorisées à la commande en ligne : si un ancien
    # produit "Oeufs de table" générique (sans calibre) traîne encore en
    # base, il est exclu ici sans toucher à son historique de ventes.
    CALIBRES_OEUFS_AUTORISES = {
        "Oeufs de table - Petit calibre",
        "Oeufs de table - Moyen calibre",
        "Oeufs de table - Gros calibre",
    }
    produits = [
        p for p in (
            Product.query.filter(Product.prix_vente_defaut > 0)
            .order_by(Product.name)
            .all()
        )
        if not p.name.lower().startswith("oeufs de table") or p.name in CALIBRES_OEUFS_AUTORISES
    ]

    if request.method == "POST":
        client_name = request.form.get("client_name", "").strip()
        client_phone = request.form.get("client_phone", "").strip()
        client_address = request.form.get("client_address", "").strip()
        note = request.form.get("note", "").strip()
        product_ids = request.form.getlist("product_id[]")
        quantities = request.form.getlist("quantity[]")

        if not client_phone:
            flash("Merci d'indiquer un numéro de téléphone pour être contacté(e).", "danger")
            return redirect(url_for("commander"))
        if not client_address:
            flash("Merci d'indiquer une adresse de livraison.", "danger")
            return redirect(url_for("commander"))

        produits_par_id = {p.id: p for p in produits}
        lignes = []
        for pid_raw, qty_raw in zip(product_ids, quantities):
            if not pid_raw or not qty_raw:
                continue
            try:
                pid = int(pid_raw)
                qty = _parse_decimal(qty_raw)
            except ValueError:
                continue
            if qty <= 0 or pid not in produits_par_id:
                continue
            lignes.append({"product": produits_par_id[pid], "quantity": qty})

        if not lignes:
            flash("Votre panier est vide — choisissez au moins un produit.", "danger")
            return redirect(url_for("commander"))

        quantite_totale = sum(l["quantity"] for l in lignes)
        total_produits = sum(l["quantity"] * l["product"].prix_vente_defaut for l in lignes)
        frais_livraison = _tarif_livraison(quantite_totale)

        client = _get_or_create_client_by_phone(client_name, client_phone)
        system_uid = _system_user_id()
        if not client or not system_uid:
            flash("Impossible d'enregistrer la commande pour le moment. Merci de nous contacter directement.", "danger")
            return redirect(url_for("commander"))

        og = OrderGroup(
            numero=_next_order_group_numero(),
            client_id=client.id,
            client_phone=client_phone,
            client_address=client_address,
            delivery_fee=frais_livraison,
            total_produits=total_produits,
            total=total_produits + frais_livraison,
            status="nouvelle",
            note=note,
        )
        db.session.add(og)
        db.session.flush()  # obtenir og.id avant de créer les lignes

        for ligne in lignes:
            product = ligne["product"]
            db.session.add(Order(
                client_id=client.id,
                product_id=product.id,
                quantity=ligne["quantity"],
                unit_price=product.prix_vente_defaut,
                total=ligne["quantity"] * product.prix_vente_defaut,
                status="en_attente",
                date=date.today(),
                note=f"Commande en ligne {og.numero}",
                user_id=system_uid,
                order_group_id=og.id,
            ))

        db.session.commit()
        return redirect(url_for("commande_confirmation", gid=og.id))

    disponibilites = {p.id: _stock_disponible(p) for p in produits}
    images_produits = {p.id: _product_image(p.name) for p in produits}
    return render_template(
        "commander.html",
        produits=produits,
        disponibilites=disponibilites,
        images_produits=images_produits,
        paliers=_paliers_livraison(),
    )


@app.route("/commander/confirmation/<int:gid>")
def commande_confirmation(gid):
    og = db.session.get(OrderGroup, gid)
    if not og:
        abort(404)
    lignes = og.lignes.all()
    return render_template("commande_confirmation.html", og=og, lignes=lignes)


# Alias explicite (utilisé par url_for('accueil_public') dans les templates
# publics) pour ne pas dépendre du nom historique "index".
app.add_url_rule("/", endpoint="accueil_public", view_func=index)


def _stock_reserve(product_id, exclude_order_id=None):
    """Quantité totale réservée par les commandes clients en attente pour un
    produit donné — permet de calculer le stock réellement disponible à la
    vente (stock physique moins ce qui est déjà promis à d'autres clients)."""
    q = db.session.query(func.coalesce(func.sum(Order.quantity), 0.0)).filter(
        Order.product_id == product_id, Order.status == "en_attente"
    )
    if exclude_order_id:
        q = q.filter(Order.id != exclude_order_id)
    return q.scalar() or 0.0


def _stock_disponible(product, exclude_order_id=None):
    return product.stock - _stock_reserve(product.id, exclude_order_id=exclude_order_id)


def _get_or_create_client(name):
    """Retrouve un client existant par son nom (insensible à la casse) ou en
    crée un nouveau à la volée — permet de saisir directement le nom du
    client lors d'une vente ou d'une commande, sans passer par la page
    Clients. Ne fait pas de commit : à intégrer dans la transaction en cours."""
    name = (name or "").strip()
    if not name:
        return None
    client = Partner.query.filter(
        Partner.type == "client", func.lower(Partner.name) == name.lower()
    ).first()
    if client:
        return client
    client = Partner(name=name, type="client")
    db.session.add(client)
    db.session.flush()  # obtenir client.id sans commit prématuré
    return client


# ---------- Tableau de bord ----------

def _prix_achat_reel(product_id):
    """Prix d'achat par défaut d'un produit, tel que défini sur sa fiche
    produit (champ « prix d'achat par défaut » de la page Produits). Utilisé
    uniquement comme repli quand aucun achat n'a encore été enregistré pour ce
    produit (ex. stock initial saisi sans transaction d'achat) — dans tous les
    autres cas, _prix_achat_moyen_pondere() ci-dessous est la référence à
    utiliser, car un même produit est souvent acheté à des prix différents
    selon les fournisseurs ou les périodes."""
    product = db.session.get(Product, product_id)
    return product.prix_achat_defaut if product else 0.0


def _prix_achat_moyen_pondere(product_id):
    """Prix d'achat moyen pondéré (CMUP) d'un produit : moyenne de tous les
    prix d'achat réellement payés pour ce produit (transactions de type
    « achat », tout l'historique confondu), pondérée par la quantité de
    chaque achat — c.-à-d. somme des montants achetés ÷ somme des quantités
    achetées. Un même produit étant souvent acheté à des prix différents
    (fournisseurs, saisons, négociations...), cette moyenne pondérée reflète
    le coût réel bien mieux qu'un prix d'achat unique figé sur la fiche
    produit. On ne restreint pas ce calcul à la période du rapport : les
    produits vendus pendant la période ont pu être achetés avant elle, donc on
    utilise l'historique complet des achats pour estimer leur coût.
    Si aucun achat n'a jamais été enregistré pour ce produit, on retombe sur
    le prix d'achat par défaut de la fiche produit.

    Recalculé à chaque appel (pas de cache) : le prix moyen doit évoluer dès
    qu'un nouvel achat est enregistré, y compris au sein d'une même requête."""
    qte_totale, montant_total = db.session.query(
        func.coalesce(func.sum(Transaction.quantity), 0.0),
        func.coalesce(func.sum(Transaction.total), 0.0),
    ).filter(
        Transaction.type == "achat", Transaction.product_id == product_id
    ).first()
    if qte_totale:
        return montant_total / qte_totale
    return _prix_achat_reel(product_id)


def _cout_produits_vendus(start=None, end=None):
    """Coût d'achat (au prix d'achat moyen pondéré de chaque produit — voir
    _prix_achat_moyen_pondere) des produits vendus sur la période — utilisé
    pour calculer la marge réelle des ventes, plutôt que de comparer les
    ventes du jour aux achats du jour (qui n'ont souvent aucun lien direct :
    un produit vendu aujourd'hui peut avoir été acheté un autre jour)."""
    q = db.session.query(
        Transaction.product_id, func.coalesce(func.sum(Transaction.quantity), 0.0)
    ).filter(Transaction.type == "vente")
    if start:
        q = q.filter(Transaction.date >= start)
    if end:
        q = q.filter(Transaction.date <= end)
    total_cout = 0.0
    for product_id, qte_vendue in q.group_by(Transaction.product_id).all():
        total_cout += (qte_vendue or 0.0) * _prix_achat_moyen_pondere(product_id)
    return total_cout


def _valeur_pertes(start=None, end=None):
    """Valeur d'achat (au prix d'achat moyen pondéré du produit — voir
    _prix_achat_moyen_pondere) des produits cassés/périmés/perdus sur la
    période — cette valeur est une perte sèche pour l'entreprise (le produit
    est sorti du stock sans générer de revenu) et doit donc être soustraite du
    bénéfice réel."""
    q = db.session.query(
        Loss.product_id, func.coalesce(func.sum(Loss.quantity), 0.0)
    )
    if start:
        q = q.filter(Loss.date >= start)
    if end:
        q = q.filter(Loss.date <= end)
    total = 0.0
    for product_id, qte_perdue in q.group_by(Loss.product_id).all():
        total += (qte_perdue or 0.0) * _prix_achat_moyen_pondere(product_id)
    return total


def _benefice_par_produit(start=None, end=None):
    """Bénéfice réel par produit sur la période : chiffre d'affaires réellement
    encaissé sur les ventes de ce produit sur la période (donc déjà basé sur le
    prix de vente moyen pondéré réellement appliqué, puisqu'on additionne les
    montants réels de chaque vente, à des prix parfois différents), moins le
    coût d'achat de ce même produit au prix d'achat moyen pondéré (voir
    _prix_achat_moyen_pondere) appliqué aux quantités vendues sur la période.

    Un même produit étant souvent vendu et acheté à des prix différents selon
    les jours/clients/fournisseurs, on utilise systématiquement des moyennes
    pondérées par la quantité plutôt qu'un prix unique théorique : côté vente,
    la moyenne pondérée découle naturellement de la somme des montants réels
    (chiffre_affaires ÷ quantite_vendue) ; côté achat, elle est calculée
    explicitement sur l'historique des achats.

    Le bénéfice total tous produits confondus de la période s'obtient en
    faisant la somme de ces bénéfices par produit, puis en déduisant de ce
    total toutes les autres dépenses de la période qui ne sont pas le coût
    d'achat des produits (dépenses générales + valeur des pertes) — ce coût
    d'achat étant déjà déduit ci-dessus, produit par produit. Voir rapports()
    pour ce calcul du bénéfice total."""
    q = db.session.query(
        Transaction.product_id,
        func.coalesce(func.sum(Transaction.quantity), 0.0),
        func.coalesce(func.sum(Transaction.total), 0.0),
    ).filter(Transaction.type == "vente")
    if start:
        q = q.filter(Transaction.date >= start)
    if end:
        q = q.filter(Transaction.date <= end)

    resultats = []
    for product_id, qte_vendue, chiffre_affaires in q.group_by(Transaction.product_id).all():
        product = db.session.get(Product, product_id)
        if not product:
            continue
        qte_vendue = qte_vendue or 0.0
        chiffre_affaires = chiffre_affaires or 0.0
        prix_vente_moyen = (chiffre_affaires / qte_vendue) if qte_vendue else 0.0
        prix_achat_moyen = _prix_achat_moyen_pondere(product_id)
        cout_achat = qte_vendue * prix_achat_moyen
        resultats.append({
            "product": product,
            "quantite_vendue": qte_vendue,
            "prix_vente_moyen": prix_vente_moyen,
            "prix_achat_moyen": prix_achat_moyen,
            "chiffre_affaires": chiffre_affaires,
            "cout_achat": cout_achat,
            "benefice": chiffre_affaires - cout_achat,
        })
    resultats.sort(key=lambda r: r["benefice"], reverse=True)
    return resultats


@app.route("/dashboard")
@login_required
def dashboard():
    today = date.today()
    start_month = today.replace(day=1)

    def sum_total(type_, start=None, end=None):
        q = db.session.query(func.coalesce(func.sum(Transaction.total), 0.0)).filter(Transaction.type == type_)
        if start:
            q = q.filter(Transaction.date >= start)
        if end:
            q = q.filter(Transaction.date <= end)
        return q.scalar() or 0.0

    def sum_expenses(start=None, end=None):
        q = db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
        if start:
            q = q.filter(Expense.date >= start)
        if end:
            q = q.filter(Expense.date <= end)
        return q.scalar() or 0.0

    ventes_mois = sum_total("vente", start_month, today)
    achats_mois = sum_total("achat", start_month, today)
    depenses_mois = sum_expenses(start_month, today)
    ventes_jour = sum_total("vente", today, today)
    achats_jour = sum_total("achat", today, today)
    depenses_jour = sum_expenses(today, today)

    # Bénéfice = marge réelle sur les produits vendus (prix de vente − coût
    # d'achat réel de ces mêmes produits, indépendamment du jour où ils ont
    # été achetés), moins les dépenses de la période et moins la valeur
    # d'achat des produits cassés/périmés/perdus sur la période — et non plus
    # ventes − achats de la période, qui ne reflète pas la rentabilité réelle
    # si les achats et les ventes ne portent pas sur les mêmes produits/jours.
    cout_vendus_mois = _cout_produits_vendus(start_month, today)
    cout_vendus_jour = _cout_produits_vendus(today, today)
    pertes_mois = _valeur_pertes(start_month, today)
    pertes_jour = _valeur_pertes(today, today)
    marge_mois = ventes_mois - cout_vendus_mois
    marge_jour = ventes_jour - cout_vendus_jour

    produits = Product.query.order_by(Product.name).all()
    dernieres_transactions = (
        Transaction.query.order_by(Transaction.created_at.desc()).limit(8).all()
    )
    alertes_stock = [p for p in produits if p.stock <= p.seuil_alerte]

    return render_template(
        "dashboard.html",
        ventes_mois=ventes_mois,
        achats_mois=achats_mois,
        depenses_mois=depenses_mois,
        pertes_mois=pertes_mois,
        benefice_mois=marge_mois - depenses_mois - pertes_mois,
        ventes_jour=ventes_jour,
        achats_jour=achats_jour,
        depenses_jour=depenses_jour,
        pertes_jour=pertes_jour,
        benefice_jour=marge_jour - depenses_jour - pertes_jour,
        produits=produits,
        dernieres_transactions=dernieres_transactions,
        alertes_stock=alertes_stock,
    )


# ---------- Ventes / Achats (transactions) ----------

def _list_transactions(type_):
    q = Transaction.query.filter_by(type=type_)
    product_id = request.args.get("product_id", type=int)
    partner_id = request.args.get("partner_id", type=int)
    date_debut = _parse_date(request.args.get("date_debut"))
    date_fin = _parse_date(request.args.get("date_fin"))

    if product_id:
        q = q.filter(Transaction.product_id == product_id)
    if partner_id:
        q = q.filter(Transaction.partner_id == partner_id)
    if date_debut:
        q = q.filter(Transaction.date >= date_debut)
    if date_fin:
        q = q.filter(Transaction.date <= date_fin)

    return q.order_by(Transaction.date.desc(), Transaction.created_at.desc()).all()


def _create_transaction(type_):
    try:
        product_id = int(request.form["product_id"])
        quantity = _parse_decimal(request.form["quantity"])
        unit_price = _parse_decimal(request.form["unit_price"])
        partner_id = request.form.get("partner_id") or None
        tdate = request.form.get("date") or date.today().isoformat()
        note = request.form.get("note", "").strip()
    except (KeyError, ValueError):
        flash("Formulaire invalide. Vérifiez les champs saisis.", "danger")
        return

    if quantity <= 0 or unit_price < 0:
        flash("Quantité ou prix invalide.", "danger")
        return

    product = db.session.get(Product, product_id)
    if not product:
        flash("Produit introuvable.", "danger")
        return

    if type_ == "vente" and product.stock < quantity:
        flash(
            f"Stock insuffisant pour {product.name} (disponible : {product.stock:g} {product.unit}).",
            "danger",
        )
        return

    vente_date = datetime.strptime(tdate, "%Y-%m-%d").date()

    sale_id = None
    if type_ == "vente":
        # Chaque vente — même via ce formulaire rapide à un seul produit —
        # génère désormais sa propre facture, comme « Nouvelle facture »,
        # pour que le lien facture apparaisse pour toutes les ventes et pas
        # seulement celles créées via le formulaire multi-produits.
        vente = Sale(
            numero=_next_sale_numero(),
            partner_id=int(partner_id) if partner_id else None,
            total=quantity * unit_price,
            date=vente_date,
            user_id=current_user.id,
        )
        db.session.add(vente)
        db.session.flush()  # pour obtenir vente.id avant de créer la ligne
        sale_id = vente.id

    tr = Transaction(
        type=type_,
        product_id=product.id,
        partner_id=int(partner_id) if partner_id else None,
        sale_id=sale_id,
        quantity=quantity,
        unit_price=unit_price,
        total=quantity * unit_price,
        date=vente_date,
        note=note,
        user_id=current_user.id,
    )
    if type_ == "vente":
        product.stock -= quantity
    else:
        product.stock += quantity

    db.session.add(tr)
    db.session.commit()
    flash(
        ("Vente" if type_ == "vente" else "Achat") + " enregistré(e) avec succès.",
        "success",
    )


@app.route("/ventes", methods=["GET", "POST"])
@login_required
def ventes():
    if request.method == "POST":
        _create_transaction("vente")
        return redirect(url_for("ventes"))
    transactions = _list_transactions("vente")
    return render_template(
        "transactions.html",
        type_="vente",
        titre="Ventes",
        transactions=transactions,
        quantite_totale=sum(t.quantity for t in transactions),
        total_montant=sum(t.total for t in transactions),
        produits=Product.query.order_by(Product.name).all(),
        partenaires=Partner.query.filter_by(type="client").order_by(Partner.name).all(),
        today=date.today().isoformat(),
    )


@app.route("/achats", methods=["GET", "POST"])
@login_required
def achats():
    if request.method == "POST":
        _create_transaction("achat")
        return redirect(url_for("achats"))
    transactions = _list_transactions("achat")
    return render_template(
        "transactions.html",
        type_="achat",
        titre="Achats",
        transactions=transactions,
        quantite_totale=sum(t.quantity for t in transactions),
        total_montant=sum(t.total for t in transactions),
        produits=Product.query.order_by(Product.name).all(),
        partenaires=Partner.query.filter_by(type="fournisseur").order_by(Partner.name).all(),
        today=date.today().isoformat(),
    )


@app.route("/ventes/export.csv")
@login_required
def export_ventes_csv():
    return _export_transactions_csv("vente", "senavipro_ventes")


@app.route("/achats/export.csv")
@login_required
def export_achats_csv():
    return _export_transactions_csv("achat", "senavipro_achats")


def _export_transactions_csv(type_, filename_prefix):
    """Exporte au format CSV (ouvrable directement dans Excel) les
    transactions correspondant aux mêmes filtres (produit, client/fournisseur,
    période) que ceux actuellement appliqués sur la page Ventes/Achats."""
    transactions = _list_transactions(type_)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date", "Produit", "Client" if type_ == "vente" else "Fournisseur",
        "Quantité", "Prix unitaire", "Total", "Note", "Enregistré par",
    ])
    for t in transactions:
        writer.writerow([
            t.date.isoformat(),
            t.product.name if t.product else "",
            t.partner.name if t.partner else "",
            t.quantity, t.unit_price, t.total,
            t.note or "",
            t.user.full_name if t.user else "",
        ])
    writer.writerow([])
    writer.writerow(["", "", "Quantité totale", sum(t.quantity for t in transactions)])
    writer.writerow(["", "", "Total", "", "", sum(t.total for t in transactions)])
    today_str = date.today().isoformat()
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename_prefix}_{today_str}.csv"},
    )


@app.route("/transactions/<int:tid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_transaction(tid):
    tr = db.session.get(Transaction, tid)
    if not tr:
        abort(404)
    product = db.session.get(Product, tr.product_id)
    # Réajuster le stock lors de la suppression
    if product:
        if tr.type == "vente":
            product.stock += tr.quantity
        else:
            product.stock -= tr.quantity
    type_ = tr.type
    vente = db.session.get(Sale, tr.sale_id) if tr.sale_id else None
    db.session.delete(tr)
    db.session.flush()
    if vente:
        # Recalcule le total de la facture (ou la supprime s'il ne reste plus
        # aucun produit dedans) pour que la facture reste cohérente.
        lignes_restantes = vente.lignes.all()
        if lignes_restantes:
            vente.total = sum(l.total for l in lignes_restantes)
        else:
            db.session.delete(vente)
    db.session.commit()
    flash("Transaction supprimée.", "info")
    return redirect(url_for("ventes" if type_ == "vente" else "achats"))


@app.route("/transactions/<int:tid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_transaction(tid):
    """Permet à l'administrateur de corriger une vente ou un achat déjà
    enregistré (produit, quantité, prix, client/fournisseur, date, note) en
    cas d'erreur de saisie. Le stock est réajusté : l'effet de l'ancienne
    valeur est d'abord annulé, puis celui de la nouvelle est appliqué."""
    tr = db.session.get(Transaction, tid)
    if not tr:
        abort(404)
    type_ = tr.type

    try:
        product_id = int(request.form["product_id"])
        quantity = _parse_decimal(request.form["quantity"])
        unit_price = _parse_decimal(request.form["unit_price"])
        partner_id = request.form.get("partner_id") or None
        tdate = request.form.get("date") or tr.date.isoformat()
        note = request.form.get("note", "").strip()
    except (KeyError, ValueError):
        flash("Formulaire invalide. Vérifiez les champs saisis.", "danger")
        return redirect(url_for("ventes" if type_ == "vente" else "achats"))

    if quantity <= 0 or unit_price < 0:
        flash("Quantité ou prix invalide.", "danger")
        return redirect(url_for("ventes" if type_ == "vente" else "achats"))

    new_product = db.session.get(Product, product_id)
    if not new_product:
        flash("Produit introuvable.", "danger")
        return redirect(url_for("ventes" if type_ == "vente" else "achats"))

    try:
        new_date = datetime.strptime(tdate, "%Y-%m-%d").date()
    except ValueError:
        flash("Date invalide.", "danger")
        return redirect(url_for("ventes" if type_ == "vente" else "achats"))

    # Annule l'effet de l'ancienne transaction sur le stock du produit d'origine.
    old_product = db.session.get(Product, tr.product_id)
    if old_product:
        if type_ == "vente":
            old_product.stock += tr.quantity
        else:
            old_product.stock -= tr.quantity

    # Vérifie que le nouveau produit/quantité reste cohérent avant d'appliquer.
    stock_apres_annulation = new_product.stock if new_product.id != (old_product.id if old_product else None) else old_product.stock
    if type_ == "vente" and stock_apres_annulation < quantity:
        db.session.rollback()
        flash(
            f"Stock insuffisant pour {new_product.name} "
            f"(disponible : {stock_apres_annulation:g} {new_product.unit}).",
            "danger",
        )
        return redirect(url_for("ventes"))

    tr.product_id = new_product.id
    tr.partner_id = int(partner_id) if partner_id else None
    tr.quantity = quantity
    tr.unit_price = unit_price
    tr.total = quantity * unit_price
    tr.date = new_date
    tr.note = note

    # Applique l'effet de la nouvelle transaction sur le stock du nouveau produit.
    if type_ == "vente":
        new_product.stock -= quantity
    else:
        new_product.stock += quantity

    # Si la transaction fait partie d'une facture, on met à jour le total de
    # la facture pour qu'il reste cohérent avec la ligne modifiée.
    if tr.sale_id:
        vente = db.session.get(Sale, tr.sale_id)
        if vente:
            vente.partner_id = tr.partner_id
            vente.total = sum(l.total for l in vente.lignes)

    db.session.commit()
    flash(("Vente" if type_ == "vente" else "Achat") + " modifié(e) avec succès.", "success")
    return redirect(url_for("ventes" if type_ == "vente" else "achats"))


# ---------- Ventes multi-produits (facture unique par client) ----------

def _next_sale_numero():
    """Génère le prochain numéro de facture "FAC-<année>-<séquence>".

    Important : on se base sur le plus grand numéro déjà utilisé cette
    année (et non sur un simple COUNT(*) des ventes), car une facture
    supprimée (voir supprimer_facture / suppression du dernier produit
    d'une transaction) réduit le nombre total de ventes sans libérer son
    numéro — un COUNT(*) + 1 finit alors par recalculer un numéro déjà
    attribué à une facture existante, ce qui provoque une erreur
    "duplicate key" (contrainte d'unicité sur sales.numero) au moment de
    l'INSERT, typiquement en confirmant une commande ou en enregistrant
    une vente."""
    annee = datetime.utcnow().year
    prefix = f"FAC-{annee}-"
    max_seq = 0
    existants = db.session.query(Sale.numero).filter(Sale.numero.like(f"{prefix}%")).all()
    for (numero,) in existants:
        try:
            seq = int(numero.rsplit("-", 1)[-1])
        except (ValueError, AttributeError):
            continue
        max_seq = max(max_seq, seq)
    return f"{prefix}{max_seq + 1:05d}"


@app.route("/ventes/nouvelle", methods=["GET", "POST"])
@login_required
def nouvelle_vente():
    """Enregistre en une seule fois la vente de plusieurs produits à un même
    client, avec génération d'une facture unique regroupant toutes les lignes."""
    if request.method == "POST":
        partner_id = request.form.get("partner_id") or None
        vdate_raw = request.form.get("date") or date.today().isoformat()
        note = request.form.get("note", "").strip()

        product_ids = request.form.getlist("product_id[]")
        quantities = request.form.getlist("quantity[]")
        unit_prices = request.form.getlist("unit_price[]")

        try:
            vdate = datetime.strptime(vdate_raw, "%Y-%m-%d").date()
        except ValueError:
            flash("Date invalide.", "danger")
            return redirect(url_for("nouvelle_vente"))

        lignes = []
        for pid_raw, qty_raw, price_raw in zip(product_ids, quantities, unit_prices):
            if not pid_raw or not qty_raw:
                continue
            try:
                pid = int(pid_raw)
                qty = _parse_decimal(qty_raw)
                price = _parse_decimal(price_raw) if price_raw else 0.0
            except ValueError:
                flash("Une ligne de la facture contient une valeur invalide.", "danger")
                return redirect(url_for("nouvelle_vente"))
            if qty <= 0 or price < 0:
                continue
            lignes.append({"product_id": pid, "quantity": qty, "unit_price": price})

        if not lignes:
            flash("Ajoutez au moins un produit à la facture.", "danger")
            return redirect(url_for("nouvelle_vente"))

        # On vérifie tous les produits et les stocks avant de créer quoi que ce
        # soit, pour ne jamais enregistrer une facture partiellement valide.
        produits_par_id = {}
        stock_demande = {}
        for ligne in lignes:
            pid = ligne["product_id"]
            product = db.session.get(Product, pid)
            if not product:
                flash("Un des produits sélectionnés est introuvable.", "danger")
                return redirect(url_for("nouvelle_vente"))
            produits_par_id[pid] = product
            stock_demande[pid] = stock_demande.get(pid, 0) + ligne["quantity"]

        for pid, qty_totale in stock_demande.items():
            product = produits_par_id[pid]
            if product.stock < qty_totale:
                flash(
                    f"Stock insuffisant pour {product.name} "
                    f"(disponible : {product.stock:g} {product.unit}, demandé : {qty_totale:g}).",
                    "danger",
                )
                return redirect(url_for("nouvelle_vente"))

        total = sum(l["quantity"] * l["unit_price"] for l in lignes)

        vente = Sale(
            numero=_next_sale_numero(),
            partner_id=int(partner_id) if partner_id else None,
            total=total,
            date=vdate,
            user_id=current_user.id,
        )
        db.session.add(vente)
        db.session.flush()  # pour obtenir vente.id avant de créer les lignes

        for ligne in lignes:
            product = produits_par_id[ligne["product_id"]]
            product.stock -= ligne["quantity"]
            db.session.add(Transaction(
                type="vente",
                product_id=product.id,
                partner_id=int(partner_id) if partner_id else None,
                sale_id=vente.id,
                quantity=ligne["quantity"],
                unit_price=ligne["unit_price"],
                total=ligne["quantity"] * ligne["unit_price"],
                date=vdate,
                note=note,
                user_id=current_user.id,
            ))

        db.session.commit()
        flash(
            f"Facture {vente.numero} enregistrée : {len(lignes)} produit(s) pour un total de "
            f"{total:.0f} FCFA.",
            "success",
        )
        return redirect(url_for("facture_detail", sid=vente.id))

    return render_template(
        "vente_nouvelle.html",
        produits=Product.query.order_by(Product.name).all(),
        clients=Partner.query.filter_by(type="client").order_by(Partner.name).all(),
        today=date.today().isoformat(),
    )


@app.route("/factures")
@login_required
def factures():
    liste = Sale.query.order_by(Sale.created_at.desc()).limit(300).all()
    return render_template("factures.html", liste=liste)


@app.route("/factures/<int:sid>")
@login_required
def facture_detail(sid):
    vente = db.session.get(Sale, sid)
    if not vente:
        abort(404)
    lignes = vente.lignes.all()
    return render_template("facture.html", vente=vente, lignes=lignes)


@app.route("/factures/<int:sid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_facture(sid):
    vente = db.session.get(Sale, sid)
    if not vente:
        abort(404)
    for ligne in vente.lignes.all():
        product = db.session.get(Product, ligne.product_id)
        if product:
            product.stock += ligne.quantity
        db.session.delete(ligne)
    db.session.delete(vente)
    db.session.commit()
    flash("Facture supprimée et stock restitué.", "info")
    return redirect(url_for("factures"))


# ---------- Commandes clients ----------

@app.route("/commandes", methods=["GET", "POST"])
@login_required
def commandes():
    if request.method == "POST":
        try:
            product_id = int(request.form["product_id"])
            quantity = _parse_decimal(request.form["quantity"])
            unit_price = _parse_decimal(request.form["unit_price"])
            cdate = request.form.get("date") or date.today().isoformat()
            note = request.form.get("note", "").strip()
        except (KeyError, ValueError):
            flash("Formulaire invalide. Vérifiez les champs saisis.", "danger")
            return redirect(url_for("commandes"))

        if quantity <= 0 or unit_price < 0:
            flash("Quantité ou prix invalide.", "danger")
            return redirect(url_for("commandes"))

        client_name = request.form.get("client_name", "").strip()
        if not client_name:
            flash("Le nom du client est obligatoire.", "danger")
            return redirect(url_for("commandes"))
        # Le client est saisi directement au clavier (avec suggestions) : on
        # retrouve le client existant ou on le crée à la volée.
        client = _get_or_create_client(client_name)
        product = db.session.get(Product, product_id)
        if not product:
            flash("Produit introuvable.", "danger")
            return redirect(url_for("commandes"))

        disponible = _stock_disponible(product)
        if quantity > disponible:
            # On autorise l'enregistrement d'une commande même en rupture de
            # stock disponible : elle reste "en attente" et se régularise
            # dès qu'un nouvel achat reconstitue le stock, avant confirmation
            # en vente (qui, elle, vérifie toujours le stock physique réel).
            flash(
                f"Commande enregistrée en rupture de stock pour {product.name} "
                f"(disponible après commandes en attente : {disponible:g} {product.unit}). "
                f"Elle pourra être confirmée dès qu'un nouvel achat reconstituera le stock.",
                "warning",
            )

        try:
            order_date = datetime.strptime(cdate, "%Y-%m-%d").date()
        except ValueError:
            flash("Date invalide.", "danger")
            return redirect(url_for("commandes"))

        o = Order(
            client_id=client.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=unit_price,
            total=quantity * unit_price,
            status="en_attente",
            date=order_date,
            note=note,
            user_id=current_user.id,
        )
        db.session.add(o)
        db.session.commit()
        flash("Commande enregistrée.", "success")
        return redirect(url_for("commandes"))

    statut_filtre = request.args.get("statut", "en_attente")
    # Exclut les lignes issues d'un panier du site public (order_group_id
    # renseigné) : celles-ci se gèrent sur la page Livraisons, par panier
    # complet plutôt que ligne par ligne, avec les frais de livraison et le
    # circuit de statut propres aux commandes en ligne.
    q = Order.query.filter(Order.order_group_id.is_(None))
    if statut_filtre in ("en_attente", "confirmee", "annulee"):
        q = q.filter_by(status=statut_filtre)
    liste = q.order_by(Order.date.desc(), Order.created_at.desc()).all()

    produits = Product.query.order_by(Product.name).all()
    disponibilites = {p.id: _stock_disponible(p) for p in produits}

    return render_template(
        "commandes.html",
        liste=liste,
        produits=produits,
        disponibilites=disponibilites,
        clients=Partner.query.filter_by(type="client").order_by(Partner.name).all(),
        statut_filtre=statut_filtre,
        today=date.today().isoformat(),
        quantite_totale=sum(o.quantity for o in liste),
    )


@app.route("/commandes/<int:oid>/confirmer", methods=["POST"])
@login_required
def confirmer_commande(oid):
    o = db.session.get(Order, oid)
    if not o or o.status != "en_attente":
        flash("Commande introuvable ou déjà traitée.", "danger")
        return redirect(url_for("commandes"))

    product = db.session.get(Product, o.product_id)
    if not product:
        flash("Produit introuvable.", "danger")
        return redirect(url_for("commandes"))

    if product.stock < o.quantity:
        flash(
            f"Stock physique insuffisant pour confirmer cette commande : {product.name} "
            f"(disponible : {product.stock:g} {product.unit}).",
            "danger",
        )
        return redirect(url_for("commandes"))

    vente = Sale(
        numero=_next_sale_numero(),
        partner_id=o.client_id,
        total=o.total,
        date=date.today(),
        user_id=current_user.id,
    )
    db.session.add(vente)
    db.session.flush()  # récupérer vente.id avant de créer la ligne

    tr = Transaction(
        type="vente",
        product_id=product.id,
        partner_id=o.client_id,
        sale_id=vente.id,
        quantity=o.quantity,
        unit_price=o.unit_price,
        total=o.total,
        date=date.today(),
        note=(f"Commande #{o.id} confirmée" + (f" — {o.note}" if o.note else "")),
        user_id=current_user.id,
    )
    product.stock -= o.quantity
    db.session.add(tr)

    o.status = "confirmee"
    o.date_confirmation = date.today()
    o.sale_id = vente.id

    db.session.commit()
    flash(f"Commande transformée en vente {vente.numero} ({o.total:.0f} FCFA). Stock mis à jour.", "success")
    return redirect(url_for("commandes"))


@app.route("/commandes/<int:oid>/annuler", methods=["POST"])
@login_required
def annuler_commande(oid):
    o = db.session.get(Order, oid)
    if not o or o.status != "en_attente":
        flash("Commande introuvable ou déjà traitée.", "danger")
        return redirect(url_for("commandes"))
    o.status = "annulee"
    db.session.commit()
    flash("Commande annulée. Le stock réservé est de nouveau disponible.", "info")
    return redirect(url_for("commandes"))


@app.route("/commandes/<int:oid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_commande(oid):
    """Permet à l'administrateur de corriger une commande en attente (client,
    produit, quantité, prix, date, note) en cas d'erreur de saisie. Les
    commandes déjà confirmées (devenues une vente réelle) ou annulées ne
    peuvent plus être modifiées."""
    o = db.session.get(Order, oid)
    if not o:
        abort(404)
    if o.status != "en_attente":
        flash("Seules les commandes en attente peuvent être modifiées.", "danger")
        return redirect(url_for("commandes"))

    try:
        product_id = int(request.form["product_id"])
        quantity = _parse_decimal(request.form["quantity"])
        unit_price = _parse_decimal(request.form["unit_price"])
        cdate = request.form.get("date") or o.date.isoformat()
        note = request.form.get("note", "").strip()
    except (KeyError, ValueError):
        flash("Formulaire invalide. Vérifiez les champs saisis.", "danger")
        return redirect(url_for("commandes"))

    if quantity <= 0 or unit_price < 0:
        flash("Quantité ou prix invalide.", "danger")
        return redirect(url_for("commandes"))

    client_name = request.form.get("client_name", "").strip()
    if not client_name:
        flash("Le nom du client est obligatoire.", "danger")
        return redirect(url_for("commandes"))
    client = _get_or_create_client(client_name)

    product = db.session.get(Product, product_id)
    if not product:
        flash("Produit introuvable.", "danger")
        return redirect(url_for("commandes"))

    # Le stock disponible doit exclure la réservation de cette commande
    # elle-même, puisqu'on est en train de la modifier (pas d'en créer une
    # nouvelle) — sinon sa propre quantité réservée serait comptée deux fois.
    disponible = _stock_disponible(product, exclude_order_id=o.id)
    if quantity > disponible:
        # Comme à la création, la modification reste possible en rupture de
        # stock disponible : la commande reste "en attente" et se régularise
        # avec un nouvel achat, avant confirmation en vente.
        flash(
            f"Commande modifiée en rupture de stock pour {product.name} "
            f"(disponible après commandes en attente : {disponible:g} {product.unit}). "
            f"Elle pourra être confirmée dès qu'un nouvel achat reconstituera le stock.",
            "warning",
        )

    try:
        order_date = datetime.strptime(cdate, "%Y-%m-%d").date()
    except ValueError:
        flash("Date invalide.", "danger")
        return redirect(url_for("commandes"))

    o.client_id = client.id
    o.product_id = product.id
    o.quantity = quantity
    o.unit_price = unit_price
    o.total = quantity * unit_price
    o.date = order_date
    o.note = note
    db.session.commit()
    flash("Commande modifiée avec succès.", "success")
    return redirect(url_for("commandes"))


@app.route("/commandes/<int:oid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_commande(oid):
    o = db.session.get(Order, oid)
    if o:
        if o.status == "confirmee":
            flash("Impossible de supprimer une commande déjà confirmée (elle correspond à une vente réelle — supprimez plutôt la transaction ou la facture associée si besoin).", "danger")
        else:
            db.session.delete(o)
            db.session.commit()
            flash("Commande supprimée.", "info")
    return redirect(url_for("commandes"))


# ---------- Livraisons (commandes du site public) ----------
#
# Distinctes des "Commandes clients" ci-dessus (saisies directement par
# l'équipe, un seul produit à la fois) : ici, un OrderGroup représente le
# panier complet d'un client passé depuis /commander (plusieurs produits,
# adresse de livraison, frais de livraison). Chaque produit du panier reste
# une ligne Order comme pour les commandes internes (même logique de
# réservation de stock via order_group_id), regroupées sous un même
# OrderGroup.

STATUTS_LIVRAISON = ["nouvelle", "confirmee", "en_livraison", "livree", "annulee"]


@app.route("/livraisons")
@login_required
def livraisons():
    statut_filtre = request.args.get("statut", "actives")
    q = OrderGroup.query
    if statut_filtre == "actives":
        q = q.filter(OrderGroup.status.in_(["nouvelle", "confirmee", "en_livraison"]))
    elif statut_filtre in STATUTS_LIVRAISON:
        q = q.filter_by(status=statut_filtre)
    groupes = q.order_by(OrderGroup.created_at.desc()).limit(300).all()
    lignes_par_groupe = {g.id: g.lignes.all() for g in groupes}
    return render_template(
        "livraisons.html",
        groupes=groupes,
        lignes_par_groupe=lignes_par_groupe,
        statut_filtre=statut_filtre,
    )


@app.route("/livraisons/<int:gid>/confirmer", methods=["POST"])
@login_required
def confirmer_livraison(gid):
    """Vérifie le stock physique réel puis transforme tout le panier en une
    facture unique (une Sale regroupant une Transaction par produit) — même
    principe que "Vente rapide" (nouvelle_vente) et que la confirmation d'une
    commande interne (confirmer_commande), mais pour plusieurs lignes à la
    fois. Le stock n'est débité qu'à cette étape, jamais avant."""
    og = db.session.get(OrderGroup, gid)
    if not og or og.status != "nouvelle":
        flash("Commande introuvable ou déjà traitée.", "danger")
        return redirect(url_for("livraisons"))

    lignes = og.lignes.all()
    if not lignes:
        flash("Cette commande ne contient plus aucune ligne.", "danger")
        return redirect(url_for("livraisons"))

    # Vérifie le stock physique de chaque produit avant de débiter quoi que
    # ce soit, pour ne jamais confirmer une commande partiellement honorable.
    produits_par_id = {}
    demande_par_produit = {}
    for ligne in lignes:
        product = db.session.get(Product, ligne.product_id)
        if not product:
            flash("Un des produits de cette commande n'existe plus.", "danger")
            return redirect(url_for("livraisons"))
        produits_par_id[product.id] = product
        demande_par_produit[product.id] = demande_par_produit.get(product.id, 0) + ligne.quantity

    for pid, qte in demande_par_produit.items():
        product = produits_par_id[pid]
        if product.stock < qte:
            flash(
                f"Stock physique insuffisant pour confirmer cette commande : {product.name} "
                f"(disponible : {product.stock:g} {product.unit}, demandé : {qte:g}).",
                "danger",
            )
            return redirect(url_for("livraisons"))

    vente = Sale(
        numero=_next_sale_numero(),
        partner_id=og.client_id,
        total=og.total_produits,
        date=date.today(),
        user_id=current_user.id,
    )
    db.session.add(vente)
    db.session.flush()  # obtenir vente.id avant de créer les lignes

    for ligne in lignes:
        product = produits_par_id[ligne.product_id]
        product.stock -= ligne.quantity
        db.session.add(Transaction(
            type="vente",
            product_id=product.id,
            partner_id=og.client_id,
            sale_id=vente.id,
            quantity=ligne.quantity,
            unit_price=ligne.unit_price,
            total=ligne.total,
            date=date.today(),
            note=f"Commande en ligne {og.numero} confirmée",
            user_id=current_user.id,
        ))
        ligne.status = "confirmee"
        ligne.date_confirmation = date.today()
        ligne.sale_id = vente.id

    og.status = "confirmee"
    og.sale_id = vente.id
    og.confirmed_at = datetime.utcnow()

    db.session.commit()
    flash(
        f"Commande {og.numero} transformée en vente {vente.numero} "
        f"({og.total_produits:.0f} FCFA + {og.delivery_fee:.0f} FCFA de livraison). Stock mis à jour.",
        "success",
    )
    return redirect(url_for("livraisons"))


@app.route("/livraisons/<int:gid>/statut", methods=["POST"])
@login_required
def changer_statut_livraison(gid):
    """Fait avancer une commande confirmée dans le circuit de livraison :
    confirmee -> en_livraison -> livree. Ne modifie ni le stock ni la facture,
    déjà réglés à la confirmation (confirmer_livraison) : sert uniquement à
    organiser/suivre les livraisons."""
    og = db.session.get(OrderGroup, gid)
    if not og:
        abort(404)
    nouveau_statut = request.form.get("statut")
    transitions_valides = {"confirmee": "en_livraison", "en_livraison": "livree"}
    if transitions_valides.get(og.status) != nouveau_statut:
        flash("Changement de statut invalide.", "danger")
        return redirect(url_for("livraisons"))
    og.status = nouveau_statut
    if nouveau_statut == "livree":
        og.delivered_at = datetime.utcnow()
    db.session.commit()
    flash(f"Commande {og.numero} : statut mis à jour.", "success")
    return redirect(url_for("livraisons"))


@app.route("/livraisons/<int:gid>/annuler", methods=["POST"])
@login_required
def annuler_livraison(gid):
    og = db.session.get(OrderGroup, gid)
    if not og or og.status != "nouvelle":
        flash("Commande introuvable ou déjà traitée.", "danger")
        return redirect(url_for("livraisons"))
    og.status = "annulee"
    for ligne in og.lignes.all():
        ligne.status = "annulee"
    db.session.commit()
    flash(f"Commande {og.numero} annulée. Le stock réservé est de nouveau disponible.", "info")
    return redirect(url_for("livraisons"))


@app.route("/livraison/tarifs", methods=["GET", "POST"])
@login_required
@admin_required
def tarifs_livraison():
    """Gestion des paliers de prix de livraison affichés et appliqués sur la
    page publique /commander, selon la quantité totale du panier."""
    if request.method == "POST":
        try:
            quantite_min = _parse_decimal(request.form["quantite_min"])
            quantite_max_raw = request.form.get("quantite_max", "").strip()
            quantite_max = _parse_decimal(quantite_max_raw) if quantite_max_raw else None
            prix = _parse_decimal(request.form["prix"])
        except (KeyError, ValueError):
            flash("Valeurs invalides.", "danger")
            return redirect(url_for("tarifs_livraison"))

        if quantite_min <= 0 or prix < 0 or (quantite_max is not None and quantite_max < quantite_min):
            flash("Le palier saisi est invalide (vérifiez les quantités min/max et le prix).", "danger")
        else:
            db.session.add(DeliveryTier(quantite_min=quantite_min, quantite_max=quantite_max, prix=prix))
            db.session.commit()
            flash("Palier de livraison ajouté.", "success")
        return redirect(url_for("tarifs_livraison"))

    liste = DeliveryTier.query.order_by(DeliveryTier.quantite_min).all()
    return render_template("livraison_tarifs.html", liste=liste)


@app.route("/livraison/tarifs/<int:tid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_tarif_livraison(tid):
    tier = db.session.get(DeliveryTier, tid)
    if not tier:
        abort(404)
    try:
        quantite_min = _parse_decimal(request.form["quantite_min"])
        quantite_max_raw = request.form.get("quantite_max", "").strip()
        quantite_max = _parse_decimal(quantite_max_raw) if quantite_max_raw else None
        prix = _parse_decimal(request.form["prix"])
    except (KeyError, ValueError):
        flash("Valeurs invalides.", "danger")
        return redirect(url_for("tarifs_livraison"))

    if quantite_min <= 0 or prix < 0 or (quantite_max is not None and quantite_max < quantite_min):
        flash("Le palier saisi est invalide (vérifiez les quantités min/max et le prix).", "danger")
        return redirect(url_for("tarifs_livraison"))

    tier.quantite_min = quantite_min
    tier.quantite_max = quantite_max
    tier.prix = prix
    db.session.commit()
    flash("Palier modifié.", "success")
    return redirect(url_for("tarifs_livraison"))


@app.route("/livraison/tarifs/<int:tid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_tarif_livraison(tid):
    tier = db.session.get(DeliveryTier, tid)
    if tier:
        db.session.delete(tier)
        db.session.commit()
        flash("Palier supprimé.", "info")
    return redirect(url_for("tarifs_livraison"))


# ---------- Produits ----------

@app.route("/produits", methods=["GET", "POST"])
@login_required
@admin_required
def produits():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "").strip()
        try:
            stock_initial = _parse_decimal(request.form.get("stock_initial") or 0)
            seuil_alerte = _parse_decimal(request.form.get("seuil_alerte") or 0)
            prix_vente_defaut = _parse_decimal(request.form.get("prix_vente_defaut") or 0)
            prix_achat_defaut = _parse_decimal(request.form.get("prix_achat_defaut") or 0)
        except ValueError:
            flash("Valeurs numériques invalides.", "danger")
            return redirect(url_for("produits"))

        if not name or not unit:
            flash("Le nom et l'unité sont obligatoires.", "danger")
        elif Product.query.filter_by(name=name).first():
            flash("Un produit avec ce nom existe déjà.", "danger")
        else:
            p = Product(
                name=name, unit=unit, stock=stock_initial, seuil_alerte=seuil_alerte,
                prix_vente_defaut=prix_vente_defaut, prix_achat_defaut=prix_achat_defaut,
            )
            db.session.add(p)
            db.session.commit()
            flash(f"Produit « {name} » ajouté.", "success")
        return redirect(url_for("produits"))

    liste = Product.query.order_by(Product.name).all()
    return render_template("produits.html", liste=liste)


@app.route("/produits/<int:pid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_produit(pid):
    p = db.session.get(Product, pid)
    if not p:
        abort(404)
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "").strip()
    try:
        seuil_alerte = _parse_decimal(request.form.get("seuil_alerte", p.seuil_alerte))
        prix_vente_defaut = _parse_decimal(request.form.get("prix_vente_defaut", p.prix_vente_defaut))
        prix_achat_defaut = _parse_decimal(request.form.get("prix_achat_defaut", p.prix_achat_defaut))
    except ValueError:
        flash("Valeurs numériques invalides.", "danger")
        return redirect(url_for("produits"))

    if name:
        p.name = name
    if unit:
        p.unit = unit
    p.seuil_alerte = seuil_alerte
    p.prix_vente_defaut = prix_vente_defaut
    p.prix_achat_defaut = prix_achat_defaut
    db.session.commit()
    flash("Produit mis à jour.", "success")
    return redirect(url_for("produits"))


@app.route("/produits/<int:pid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_produit(pid):
    p = db.session.get(Product, pid)
    if p:
        if p.transactions.count() > 0:
            flash("Impossible de supprimer : des transactions y sont liées à ce produit.", "danger")
        elif p.stock != 0:
            flash("Impossible de supprimer : le stock de ce produit n'est pas à zéro.", "danger")
        else:
            db.session.delete(p)
            db.session.commit()
            flash("Produit supprimé.", "info")
    return redirect(url_for("produits"))


# ---------- Dépenses générales ----------

CATEGORIES_DEPENSES = [
    "Aliment volaille", "Vétérinaire / Médicaments", "Transport",
    "Salaires", "Électricité / Eau", "Loyer", "Emballage", "Carburant", "Autre",
]


@app.route("/depenses", methods=["GET", "POST"])
@login_required
def depenses():
    if request.method == "POST":
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        edate = request.form.get("date") or date.today().isoformat()
        try:
            amount = _parse_decimal(request.form["amount"])
        except (KeyError, ValueError):
            flash("Montant invalide.", "danger")
            return redirect(url_for("depenses"))

        if not category or amount <= 0:
            flash("Catégorie et montant (positif) sont obligatoires.", "danger")
        else:
            e = Expense(
                category=category,
                description=description,
                amount=amount,
                date=datetime.strptime(edate, "%Y-%m-%d").date(),
                user_id=current_user.id,
            )
            db.session.add(e)
            db.session.commit()
            flash("Dépense enregistrée.", "success")
        return redirect(url_for("depenses"))

    q = Expense.query
    date_debut = _parse_date(request.args.get("date_debut"))
    date_fin = _parse_date(request.args.get("date_fin"))
    if date_debut:
        q = q.filter(Expense.date >= date_debut)
    if date_fin:
        q = q.filter(Expense.date <= date_fin)
    liste = q.order_by(Expense.date.desc(), Expense.created_at.desc()).all()

    return render_template(
        "depenses.html",
        liste=liste,
        categories=CATEGORIES_DEPENSES,
        today=date.today().isoformat(),
    )


@app.route("/depenses/<int:eid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_depense(eid):
    e = db.session.get(Expense, eid)
    if e:
        db.session.delete(e)
        db.session.commit()
        flash("Dépense supprimée.", "info")
    return redirect(url_for("depenses"))


@app.route("/depenses/<int:eid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_depense(eid):
    """Permet à l'administrateur de corriger une dépense déjà enregistrée
    (catégorie, description, montant, date) en cas d'erreur de saisie."""
    e = db.session.get(Expense, eid)
    if not e:
        abort(404)

    category = request.form.get("category", "").strip()
    description = request.form.get("description", "").strip()
    edate = request.form.get("date") or e.date.isoformat()
    try:
        amount = _parse_decimal(request.form["amount"])
    except (KeyError, ValueError):
        flash("Montant invalide.", "danger")
        return redirect(url_for("depenses"))

    if not category or amount <= 0:
        flash("Catégorie et montant (positif) sont obligatoires.", "danger")
        return redirect(url_for("depenses"))

    try:
        e.date = datetime.strptime(edate, "%Y-%m-%d").date()
    except ValueError:
        flash("Date invalide.", "danger")
        return redirect(url_for("depenses"))

    e.category = category
    e.description = description
    e.amount = amount
    db.session.commit()
    flash("Dépense modifiée avec succès.", "success")
    return redirect(url_for("depenses"))


# ---------- Pertes (produits cassés / périmés / perdus) ----------

RAISONS_PERTE = ["Cassé", "Périmé", "Volé", "Détérioré", "Autre"]


@app.route("/pertes", methods=["GET", "POST"])
@login_required
def pertes():
    if request.method == "POST":
        try:
            product_id = int(request.form["product_id"])
            quantity = _parse_decimal(request.form["quantity"])
        except (KeyError, ValueError):
            flash("Formulaire invalide. Vérifiez les champs saisis.", "danger")
            return redirect(url_for("pertes"))

        reason = request.form.get("reason", "").strip()
        ldate = request.form.get("date") or date.today().isoformat()

        if quantity <= 0:
            flash("La quantité doit être positive.", "danger")
            return redirect(url_for("pertes"))

        product = db.session.get(Product, product_id)
        if not product:
            flash("Produit introuvable.", "danger")
            return redirect(url_for("pertes"))

        if product.stock < quantity:
            flash(
                f"Stock insuffisant pour {product.name} "
                f"(disponible : {product.stock:g} {product.unit}).",
                "danger",
            )
            return redirect(url_for("pertes"))

        try:
            perte_date = datetime.strptime(ldate, "%Y-%m-%d").date()
        except ValueError:
            flash("Date invalide.", "danger")
            return redirect(url_for("pertes"))

        perte = Loss(
            product_id=product.id,
            quantity=quantity,
            reason=reason,
            date=perte_date,
            user_id=current_user.id,
        )
        product.stock -= quantity
        db.session.add(perte)
        db.session.commit()
        flash("Perte enregistrée.", "success")
        return redirect(url_for("pertes"))

    q = Loss.query
    date_debut = _parse_date(request.args.get("date_debut"))
    date_fin = _parse_date(request.args.get("date_fin"))
    if date_debut:
        q = q.filter(Loss.date >= date_debut)
    if date_fin:
        q = q.filter(Loss.date <= date_fin)
    liste = q.order_by(Loss.date.desc(), Loss.created_at.desc()).all()

    valeur_totale = sum((l.product.prix_achat_defaut if l.product else 0.0) * l.quantity for l in liste)

    return render_template(
        "pertes.html",
        liste=liste,
        raisons=RAISONS_PERTE,
        produits=Product.query.order_by(Product.name).all(),
        valeur_totale=valeur_totale,
        today=date.today().isoformat(),
    )


@app.route("/pertes/<int:lid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_perte(lid):
    l = db.session.get(Loss, lid)
    if l:
        product = db.session.get(Product, l.product_id)
        if product:
            product.stock += l.quantity
        db.session.delete(l)
        db.session.commit()
        flash("Perte supprimée. Le stock a été restitué.", "info")
    return redirect(url_for("pertes"))


@app.route("/pertes/<int:lid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_perte(lid):
    """Permet à l'administrateur de corriger une perte déjà enregistrée
    (produit, quantité, raison, date) en cas d'erreur de saisie. Le stock est
    réajusté : l'effet de l'ancienne valeur est d'abord annulé, puis celui de
    la nouvelle est appliqué."""
    l = db.session.get(Loss, lid)
    if not l:
        abort(404)

    try:
        product_id = int(request.form["product_id"])
        quantity = _parse_decimal(request.form["quantity"])
    except (KeyError, ValueError):
        flash("Formulaire invalide. Vérifiez les champs saisis.", "danger")
        return redirect(url_for("pertes"))

    if quantity <= 0:
        flash("La quantité doit être positive.", "danger")
        return redirect(url_for("pertes"))

    new_product = db.session.get(Product, product_id)
    if not new_product:
        flash("Produit introuvable.", "danger")
        return redirect(url_for("pertes"))

    reason = request.form.get("reason", "").strip()
    ldate = request.form.get("date") or l.date.isoformat()
    try:
        new_date = datetime.strptime(ldate, "%Y-%m-%d").date()
    except ValueError:
        flash("Date invalide.", "danger")
        return redirect(url_for("pertes"))

    # Annule l'effet de l'ancienne perte sur le stock du produit d'origine.
    old_product = db.session.get(Product, l.product_id)
    if old_product:
        old_product.stock += l.quantity

    stock_apres_annulation = (
        new_product.stock if new_product.id != (old_product.id if old_product else None) else old_product.stock
    )
    if stock_apres_annulation < quantity:
        db.session.rollback()
        flash(
            f"Stock insuffisant pour {new_product.name} "
            f"(disponible : {stock_apres_annulation:g} {new_product.unit}).",
            "danger",
        )
        return redirect(url_for("pertes"))

    l.product_id = new_product.id
    l.quantity = quantity
    l.reason = reason
    l.date = new_date
    new_product.stock -= quantity

    db.session.commit()
    flash("Perte modifiée avec succès.", "success")
    return redirect(url_for("pertes"))


# ---------- Stock ----------

@app.route("/stock", methods=["GET", "POST"])
@login_required
def stock():
    if request.method == "POST":
        if not current_user.is_admin:
            flash("Seul l'administrateur peut ajuster le stock.", "danger")
            return redirect(url_for("stock"))
        try:
            product_id = int(request.form["product_id"])
            nouveau_stock = _parse_decimal(request.form["nouveau_stock"])
            seuil_alerte = _parse_decimal(request.form.get("seuil_alerte", 0))
        except (KeyError, ValueError):
            flash("Formulaire invalide.", "danger")
            return redirect(url_for("stock"))
        product = db.session.get(Product, product_id)
        if product:
            product.stock = nouveau_stock
            product.seuil_alerte = seuil_alerte
            db.session.commit()
            flash(f"Stock de {product.name} mis à jour.", "success")
        return redirect(url_for("stock"))

    produits = Product.query.order_by(Product.name).all()
    return render_template("stock.html", produits=produits)


# ---------- Clients / Fournisseurs ----------

@app.route("/partenaires/<type_>", methods=["GET", "POST"])
@login_required
def partenaires(type_):
    if type_ not in ("client", "fournisseur"):
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Le nom est obligatoire.", "danger")
        else:
            p = Partner(
                name=name,
                type=type_,
                phone=request.form.get("phone", "").strip(),
                address=request.form.get("address", "").strip(),
                note=request.form.get("note", "").strip(),
            )
            db.session.add(p)
            db.session.commit()
            flash(("Client" if type_ == "client" else "Fournisseur") + " ajouté.", "success")
        return redirect(url_for("partenaires", type_=type_))

    liste = Partner.query.filter_by(type=type_).order_by(Partner.name).all()
    stats = {}
    for p in liste:
        total = db.session.query(func.coalesce(func.sum(Transaction.total), 0.0)).filter(
            Transaction.partner_id == p.id
        ).scalar()
        stats[p.id] = total
    return render_template(
        "partenaires.html", type_=type_, liste=liste, stats=stats,
        titre="Clients" if type_ == "client" else "Fournisseurs",
    )


@app.route("/partenaires/<type_>/<int:pid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_partenaire(type_, pid):
    p = db.session.get(Partner, pid)
    if p:
        if p.transactions.count() > 0:
            flash("Impossible de supprimer : des transactions y sont liées.", "danger")
        else:
            db.session.delete(p)
            db.session.commit()
            flash("Supprimé.", "info")
    return redirect(url_for("partenaires", type_=type_))


@app.route("/partenaires/<type_>/<int:pid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_partenaire(type_, pid):
    """Permet à l'administrateur de corriger les informations d'un client ou
    fournisseur déjà enregistré (nom, téléphone, adresse, note)."""
    p = db.session.get(Partner, pid)
    if not p:
        abort(404)

    name = request.form.get("name", "").strip()
    if not name:
        flash("Le nom est obligatoire.", "danger")
        return redirect(url_for("partenaires", type_=type_))

    p.name = name
    p.phone = request.form.get("phone", "").strip()
    p.address = request.form.get("address", "").strip()
    p.note = request.form.get("note", "").strip()
    db.session.commit()
    flash(("Client" if type_ == "client" else "Fournisseur") + " modifié avec succès.", "success")
    return redirect(url_for("partenaires", type_=type_))


# ---------- Rapports ----------

@app.route("/rapports")
@login_required
def rapports():
    date_debut = request.args.get("date_debut") or (date.today() - timedelta(days=30)).isoformat()
    date_fin = request.args.get("date_fin") or date.today().isoformat()
    date_debut_d = _parse_date(date_debut, default=(date.today() - timedelta(days=30)))
    date_fin_d = _parse_date(date_fin, default=date.today())

    base_q = Transaction.query.filter(Transaction.date >= date_debut_d, Transaction.date <= date_fin_d)

    ventes_total = base_q.filter(Transaction.type == "vente").with_entities(
        func.coalesce(func.sum(Transaction.total), 0.0)
    ).scalar()
    achats_total = base_q.filter(Transaction.type == "achat").with_entities(
        func.coalesce(func.sum(Transaction.total), 0.0)
    ).scalar()

    par_produit = (
        db.session.query(
            Product.name, Transaction.type,
            func.sum(Transaction.quantity), func.sum(Transaction.total)
        )
        .join(Product, Product.id == Transaction.product_id)
        .filter(Transaction.date >= date_debut_d, Transaction.date <= date_fin_d)
        .group_by(Product.name, Transaction.type)
        .all()
    )

    par_jour = (
        db.session.query(
            Transaction.date, Transaction.type, func.sum(Transaction.total)
        )
        .filter(Transaction.date >= date_debut_d, Transaction.date <= date_fin_d)
        .group_by(Transaction.date, Transaction.type)
        .order_by(Transaction.date)
        .all()
    )

    labels = sorted({d.isoformat() for d, _, _ in par_jour})
    ventes_par_jour = {d: 0 for d in labels}
    achats_par_jour = {d: 0 for d in labels}
    for d, t, total in par_jour:
        if t == "vente":
            ventes_par_jour[d.isoformat()] = total
        else:
            achats_par_jour[d.isoformat()] = total

    depenses_total = db.session.query(func.coalesce(func.sum(Expense.amount), 0.0)).filter(
        Expense.date >= date_debut_d, Expense.date <= date_fin_d
    ).scalar()

    par_categorie_depense = (
        db.session.query(Expense.category, func.sum(Expense.amount))
        .filter(Expense.date >= date_debut_d, Expense.date <= date_fin_d)
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .all()
    )

    # Bénéfice réel par produit sur la période : pour chaque produit, la
    # différence entre son chiffre d'affaires réel et son coût d'achat réel
    # (prix d'achat de la fiche produit × quantité vendue).
    benefice_par_produit = _benefice_par_produit(date_debut_d, date_fin_d)
    pertes_total = _valeur_pertes(date_debut_d, date_fin_d)

    # Bénéfice total tous produits confondus de la période = somme des
    # bénéfices réels par produit ci-dessus, moins toutes les autres dépenses
    # de la période qui ne sont pas le coût d'achat des produits (déjà déduit
    # produit par produit) : dépenses générales (loyer, salaires, transport,
    # etc.) et valeur d'achat des produits cassés/périmés/perdus.
    marge_totale = sum(r["benefice"] for r in benefice_par_produit)
    benefice = marge_totale - depenses_total - pertes_total

    return render_template(
        "rapports.html",
        date_debut=date_debut,
        date_fin=date_fin,
        ventes_total=ventes_total,
        achats_total=achats_total,
        depenses_total=depenses_total,
        pertes_total=pertes_total,
        benefice=benefice,
        benefice_par_produit=benefice_par_produit,
        par_produit=par_produit,
        par_categorie_depense=par_categorie_depense,
        labels=labels,
        ventes_par_jour=[ventes_par_jour[d] for d in labels],
        achats_par_jour=[achats_par_jour[d] for d in labels],
    )


@app.route("/rapports/export.csv")
@login_required
def export_csv():
    date_debut = request.args.get("date_debut") or (date.today() - timedelta(days=30)).isoformat()
    date_fin = request.args.get("date_fin") or date.today().isoformat()
    date_debut_d = _parse_date(date_debut, default=(date.today() - timedelta(days=30)))
    date_fin_d = _parse_date(date_fin, default=date.today())
    transactions = (
        Transaction.query.filter(Transaction.date >= date_debut_d, Transaction.date <= date_fin_d)
        .order_by(Transaction.date)
        .all()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Type", "Produit", "Partenaire", "Quantité", "Prix unitaire", "Total", "Enregistré par"])
    for t in transactions:
        writer.writerow([
            t.date.isoformat(), t.type, t.product.name,
            t.partner.name if t.partner else "",
            t.quantity, t.unit_price, t.total,
            t.user.full_name if t.user else "",
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=senavipro_transactions_{date_debut}_{date_fin}.csv"},
    )


@app.route("/depenses/export.csv")
@login_required
def export_depenses_csv():
    date_debut = request.args.get("date_debut") or (date.today() - timedelta(days=30)).isoformat()
    date_fin = request.args.get("date_fin") or date.today().isoformat()
    date_debut_d = _parse_date(date_debut, default=(date.today() - timedelta(days=30)))
    date_fin_d = _parse_date(date_fin, default=date.today())
    depenses = (
        Expense.query.filter(Expense.date >= date_debut_d, Expense.date <= date_fin_d)
        .order_by(Expense.date)
        .all()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Catégorie", "Description", "Montant", "Enregistré par"])
    for e in depenses:
        writer.writerow([
            e.date.isoformat(), e.category, e.description or "",
            e.amount, e.user.full_name if e.user else "",
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=senavipro_depenses_{date_debut}_{date_fin}.csv"},
    )


@app.route("/pertes/export.csv")
@login_required
def export_pertes_csv():
    date_debut = request.args.get("date_debut") or (date.today() - timedelta(days=30)).isoformat()
    date_fin = request.args.get("date_fin") or date.today().isoformat()
    date_debut_d = _parse_date(date_debut, default=(date.today() - timedelta(days=30)))
    date_fin_d = _parse_date(date_fin, default=date.today())
    pertes_liste = (
        Loss.query.filter(Loss.date >= date_debut_d, Loss.date <= date_fin_d)
        .order_by(Loss.date)
        .all()
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", "Produit", "Quantité", "Raison", "Valeur (prix d'achat)", "Enregistré par"])
    for l in pertes_liste:
        prix = l.product.prix_achat_defaut if l.product else 0.0
        writer.writerow([
            l.date.isoformat(), l.product.name if l.product else "",
            l.quantity, l.reason or "", prix * l.quantity,
            l.user.full_name if l.user else "",
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=senavipro_pertes_{date_debut}_{date_fin}.csv"},
    )


# ---------- Capital de l'entreprise (situation financiere en temps reel) ----------
#
# Principe :
#   Situation financiere totale = valeur du stock
#                                + argent disponible chez les fournisseurs
#                                + argent liquide en caisse
#
#   - Valeur du stock = somme, pour chaque produit, de (stock actuel x prix
#     d'achat par defaut). On valorise au prix d'achat (cout), pas au prix de
#     vente, pour ne pas compter un benefice non realise dans le capital.
#
#   - Argent disponible chez un fournisseur = somme des versements
#     (SupplierPayment) faits a ce fournisseur - valeur des marchandises deja
#     recues de lui (somme des Transaction de type "achat" liees a ce
#     partenaire).
#
#   - Argent liquide en caisse = solde de caisse initial (reglable par
#     l'administrateur) + total des ventes - total des depenses generales -
#     total des versements faits aux fournisseurs (cet argent a quitte la
#     caisse pour devenir un solde d'avance chez le fournisseur, sinon le
#     meme argent serait compte deux fois dans le total).

def _get_capital_settings():
    settings = CapitalSettings.query.first()
    if not settings:
        settings = CapitalSettings(solde_caisse_initial=0)
        db.session.add(settings)
        db.session.commit()
    return settings


NOMS_MOIS = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril", 5: "Mai", 6: "Juin",
    7: "Juillet", 8: "Août", 9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
}


def _enregistrer_releve_capital_du_jour(situation_financiere, valeur_stock, argent_disponible_fournisseurs, argent_liquide):
    """Enregistre (ou met à jour) le relevé de capital du jour, afin de
    constituer un historique permettant d'analyser son évolution (en valeur
    et en %). Un seul relevé par jour : s'il existe déjà pour aujourd'hui, il
    est simplement mis à jour avec les valeurs actuelles (le relevé du jour
    reste donc à jour jusqu'à minuit) ; les relevés des jours passés, eux, ne
    sont plus modifiés et constituent l'historique figé."""
    today = date.today()
    snap = CapitalSnapshot.query.filter_by(date=today).first()
    if not snap:
        snap = CapitalSnapshot(date=today)
        db.session.add(snap)
    snap.situation_financiere = situation_financiere
    snap.valeur_stock = valeur_stock
    snap.argent_disponible_fournisseurs = argent_disponible_fournisseurs
    snap.argent_liquide = argent_liquide
    db.session.commit()
    return snap


def _dernier_releve_avant(d):
    """Retourne le relevé de capital le plus récent strictement antérieur à
    la date `d`, ou None si l'historique ne remonte pas jusque-là."""
    return (
        CapitalSnapshot.query.filter(CapitalSnapshot.date < d)
        .order_by(CapitalSnapshot.date.desc())
        .first()
    )


def _calcule_evolution(capital_actuel, reference):
    """Calcule la variation du capital entre un relevé de référence passé et
    sa valeur actuelle : variation absolue (FCFA) et relative (%). Retourne
    None si aucun relevé de référence n'est disponible (historique pas
    encore assez ancien)."""
    if reference is None:
        return None
    variation_absolue = capital_actuel - reference.situation_financiere
    variation_pct = (
        (variation_absolue / abs(reference.situation_financiere)) * 100
        if reference.situation_financiere
        else None  # référence nulle : une variation en % n'a pas de sens
    )
    return {
        "reference": reference,
        "variation_absolue": variation_absolue,
        "variation_pct": variation_pct,
    }


@app.route("/capital")
@login_required
def capital():
    produits = Product.query.order_by(Product.name).all()
    stock_detail = [
        {"produit": p, "valeur": (p.stock or 0) * (p.prix_achat_defaut or 0)}
        for p in produits
    ]
    valeur_stock = sum(item["valeur"] for item in stock_detail)

    fournisseurs = Partner.query.filter_by(type="fournisseur").order_by(Partner.name).all()
    fournisseurs_detail = []
    total_verse = 0.0
    total_recu = 0.0
    for f in fournisseurs:
        verse = db.session.query(func.coalesce(func.sum(SupplierPayment.amount), 0.0)).filter(
            SupplierPayment.partner_id == f.id
        ).scalar()
        recu = db.session.query(func.coalesce(func.sum(Transaction.total), 0.0)).filter(
            Transaction.partner_id == f.id, Transaction.type == "achat"
        ).scalar()
        fournisseurs_detail.append({
            "fournisseur": f, "verse": verse, "recu": recu, "solde": verse - recu,
        })
        total_verse += verse
        total_recu += recu

    argent_disponible_fournisseurs = total_verse - total_recu

    settings = _get_capital_settings()
    total_ventes = db.session.query(func.coalesce(func.sum(Transaction.total), 0.0)).filter(
        Transaction.type == "vente"
    ).scalar()
    total_depenses = db.session.query(func.coalesce(func.sum(Expense.amount), 0.0)).scalar()
    total_versements = total_verse

    argent_liquide = (
        settings.solde_caisse_initial + total_ventes - total_depenses - total_versements
    )

    situation_financiere = valeur_stock + argent_disponible_fournisseurs + argent_liquide

    # ---------- Évolution du capital (variation en valeur et en %) ----------
    _enregistrer_releve_capital_du_jour(
        situation_financiere, valeur_stock, argent_disponible_fournisseurs, argent_liquide
    )
    aujourdhui = date.today()
    evolution_veille = _calcule_evolution(situation_financiere, _dernier_releve_avant(aujourdhui))
    evolution_debut_mois = _calcule_evolution(
        situation_financiere, _dernier_releve_avant(aujourdhui.replace(day=1))
    )
    evolution_debut_annee = _calcule_evolution(
        situation_financiere, _dernier_releve_avant(aujourdhui.replace(month=1, day=1))
    )
    premier_releve = CapitalSnapshot.query.order_by(CapitalSnapshot.date.asc()).first()
    evolution_depuis_debut_suivi = (
        _calcule_evolution(situation_financiere, premier_releve)
        if premier_releve and premier_releve.date < aujourdhui
        else None
    )

    # Historique mensuel : on ne garde que le dernier relevé de chaque mois
    # (capital de fin de mois), avec sa variation par rapport au mois
    # précédent, du plus récent au plus ancien.
    dernier_releve_par_mois = {}
    for s in CapitalSnapshot.query.order_by(CapitalSnapshot.date.asc()).all():
        dernier_releve_par_mois[(s.date.year, s.date.month)] = s
    historique_mensuel = []
    releve_precedent = None
    for cle in sorted(dernier_releve_par_mois.keys()):
        s = dernier_releve_par_mois[cle]
        variation_absolue = variation_pct = None
        if releve_precedent is not None:
            variation_absolue = s.situation_financiere - releve_precedent.situation_financiere
            if releve_precedent.situation_financiere:
                variation_pct = variation_absolue / abs(releve_precedent.situation_financiere) * 100
        historique_mensuel.append({
            "nom_mois": NOMS_MOIS[cle[1]],
            "annee": cle[0],
            "date": s.date,
            "situation_financiere": s.situation_financiere,
            "variation_absolue": variation_absolue,
            "variation_pct": variation_pct,
        })
        releve_precedent = s
    historique_mensuel.reverse()

    versements_recents = (
        SupplierPayment.query.order_by(
            SupplierPayment.date.desc(), SupplierPayment.created_at.desc()
        ).limit(20).all()
    )

    return render_template(
        "capital.html",
        stock_detail=stock_detail,
        valeur_stock=valeur_stock,
        fournisseurs=fournisseurs,
        fournisseurs_detail=fournisseurs_detail,
        evolution_veille=evolution_veille,
        evolution_debut_mois=evolution_debut_mois,
        evolution_debut_annee=evolution_debut_annee,
        evolution_depuis_debut_suivi=evolution_depuis_debut_suivi,
        historique_mensuel=historique_mensuel,
        argent_disponible_fournisseurs=argent_disponible_fournisseurs,
        settings=settings,
        total_ventes=total_ventes,
        total_depenses=total_depenses,
        total_versements=total_versements,
        argent_liquide=argent_liquide,
        situation_financiere=situation_financiere,
        versements_recents=versements_recents,
        today=date.today().isoformat(),
    )


@app.route("/capital/solde-initial", methods=["POST"])
@login_required
@admin_required
def maj_solde_initial():
    try:
        montant = _parse_decimal(request.form["solde_caisse_initial"])
    except (KeyError, ValueError):
        flash("Montant invalide.", "danger")
        return redirect(url_for("capital"))
    settings = _get_capital_settings()
    settings.solde_caisse_initial = montant
    db.session.commit()
    flash("Solde de caisse initial mis a jour.", "success")
    return redirect(url_for("capital"))


@app.route("/capital/versement", methods=["POST"])
@login_required
@admin_required
def ajouter_versement():
    try:
        partner_id = int(request.form["partner_id"])
        amount = _parse_decimal(request.form["amount"])
        vdate = request.form.get("date") or date.today().isoformat()
        note = request.form.get("note", "").strip()
    except (KeyError, ValueError):
        flash("Formulaire invalide. Verifiez les champs saisis.", "danger")
        return redirect(url_for("capital"))

    if amount <= 0:
        flash("Le montant du versement doit etre positif.", "danger")
        return redirect(url_for("capital"))

    partner = db.session.get(Partner, partner_id)
    if not partner or partner.type != "fournisseur":
        flash("Fournisseur introuvable.", "danger")
        return redirect(url_for("capital"))

    versement = SupplierPayment(
        partner_id=partner.id,
        amount=amount,
        date=datetime.strptime(vdate, "%Y-%m-%d").date(),
        note=note,
        user_id=current_user.id,
    )
    db.session.add(versement)
    db.session.commit()
    flash(f"Versement de {amount:.0f} FCFA enregistre pour {partner.name}.", "success")
    return redirect(url_for("capital"))


@app.route("/capital/versement/<int:vid>/supprimer", methods=["POST"])
@login_required
@admin_required
def supprimer_versement(vid):
    v = db.session.get(SupplierPayment, vid)
    if v:
        db.session.delete(v)
        db.session.commit()
        flash("Versement supprime.", "info")
    return redirect(url_for("capital"))


# ---------- Utilisateurs (admin) ----------

@app.route("/utilisateurs", methods=["GET", "POST"])
@login_required
@admin_required
def utilisateurs():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "employe")
        if not username or not full_name or not password:
            flash("Tous les champs sont obligatoires.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("Cet identifiant existe déjà.", "danger")
        else:
            u = User(username=username, full_name=full_name, role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            flash("Utilisateur créé.", "success")
        return redirect(url_for("utilisateurs"))

    liste = User.query.order_by(User.username).all()
    return render_template("utilisateurs.html", liste=liste)


@app.route("/utilisateurs/<int:uid>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_utilisateur(uid):
    u = db.session.get(User, uid)
    if u and u.id != current_user.id:
        u.active = not u.active
        db.session.commit()
    return redirect(url_for("utilisateurs"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
