import csv
import io
import os
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request, flash, session,
    Response, abort
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from sqlalchemy import func, inspect, text

from models import db, User, Partner, Product, Transaction, Expense, Sale, Order

APP_NAME = "SENAVIPRO"


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


def _ensure_schema_upgrades():
    """Applique en base les petites évolutions de schéma qui ne sont pas gérées par
    db.create_all() (celui-ci ne crée que les tables manquantes, il ne modifie pas
    les tables déjà existantes). Comme le projet n'utilise pas d'outil de migration
    (Alembic/Flask-Migrate), on fait ici une mise à jour idempotente, compatible
    SQLite (local) et PostgreSQL (production) : ajoute la colonne sale_id à la
    table transactions si elle n'existe pas encore (nécessaire pour regrouper
    plusieurs produits vendus au même client sous une seule facture)."""
    inspector = inspect(db.engine)
    if "transactions" not in inspector.get_table_names():
        return
    colonnes = {c["name"] for c in inspector.get_columns("transactions")}
    if "sale_id" not in colonnes:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN sale_id INTEGER"))


# Crée automatiquement les tables et données par défaut manquantes à chaque
# démarrage (opération sûre, sans effet si elles existent déjà) — évite les
# erreurs "no such table" et fait apparaître les nouveaux produits par
# défaut même sur une base de données déjà en service.
with app.app_context():
    db.create_all()
    _ensure_schema_upgrades()
    from seed import ensure_seed_data
    ensure_seed_data(verbose=False)

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
    return {"app_name": APP_NAME, "now": datetime.utcnow()}


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


@app.route("/")
@login_required
def index():
    return redirect(url_for("dashboard"))


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
        benefice_mois=ventes_mois - achats_mois - depenses_mois,
        ventes_jour=ventes_jour,
        achats_jour=achats_jour,
        depenses_jour=depenses_jour,
        benefice_jour=ventes_jour - achats_jour - depenses_jour,
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
        quantity = float(request.form["quantity"])
        unit_price = float(request.form["unit_price"])
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

    tr = Transaction(
        type=type_,
        product_id=product.id,
        partner_id=int(partner_id) if partner_id else None,
        quantity=quantity,
        unit_price=unit_price,
        total=quantity * unit_price,
        date=datetime.strptime(tdate, "%Y-%m-%d").date(),
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
    return render_template(
        "transactions.html",
        type_="vente",
        titre="Ventes",
        transactions=_list_transactions("vente"),
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
    return render_template(
        "transactions.html",
        type_="achat",
        titre="Achats",
        transactions=_list_transactions("achat"),
        produits=Product.query.order_by(Product.name).all(),
        partenaires=Partner.query.filter_by(type="fournisseur").order_by(Partner.name).all(),
        today=date.today().isoformat(),
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


# ---------- Ventes multi-produits (facture unique par client) ----------

def _next_sale_numero():
    annee = datetime.utcnow().year
    total = Sale.query.count()
    return f"FAC-{annee}-{total + 1:05d}"


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
                qty = float(qty_raw)
                price = float(price_raw) if price_raw else 0.0
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
            quantity = float(request.form["quantity"])
            unit_price = float(request.form["unit_price"])
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
            flash(
                f"Stock disponible insuffisant pour {product.name} "
                f"(disponible après commandes en attente : {disponible:g} {product.unit}).",
                "danger",
            )
            return redirect(url_for("commandes"))

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
    q = Order.query
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


# ---------- Produits ----------

@app.route("/produits", methods=["GET", "POST"])
@login_required
@admin_required
def produits():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        unit = request.form.get("unit", "").strip()
        try:
            stock_initial = float(request.form.get("stock_initial") or 0)
            seuil_alerte = float(request.form.get("seuil_alerte") or 0)
            prix_vente_defaut = float(request.form.get("prix_vente_defaut") or 0)
            prix_achat_defaut = float(request.form.get("prix_achat_defaut") or 0)
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
        seuil_alerte = float(request.form.get("seuil_alerte", p.seuil_alerte))
        prix_vente_defaut = float(request.form.get("prix_vente_defaut", p.prix_vente_defaut))
        prix_achat_defaut = float(request.form.get("prix_achat_defaut", p.prix_achat_defaut))
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
            amount = float(request.form["amount"])
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
            nouveau_stock = float(request.form["nouveau_stock"])
            seuil_alerte = float(request.form.get("seuil_alerte", 0))
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

    return render_template(
        "rapports.html",
        date_debut=date_debut,
        date_fin=date_fin,
        ventes_total=ventes_total,
        achats_total=achats_total,
        depenses_total=depenses_total,
        benefice=ventes_total - achats_total - depenses_total,
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
