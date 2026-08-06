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
from sqlalchemy import func

from models import db, User, Partner, Product, Transaction, Expense

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

# Crée automatiquement les tables et données par défaut manquantes à chaque
# démarrage (opération sûre, sans effet si elles existent déjà) — évite les
# erreurs "no such table" et fait apparaître les nouveaux produits par
# défaut même sur une base de données déjà en service.
with app.app_context():
    db.create_all()
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


# ---------- Tableau de bord ----------

def _prix_achat_moyen(product_id):
    """Coût d'achat moyen pondéré d'un produit, calculé à partir de tous ses
    achats enregistrés (total acheté / quantité achetée). À défaut d'achat
    enregistré, utilise le prix d'achat par défaut défini sur le produit.
    Ainsi le bénéfice reflète le coût réel des marchandises vendues, et non
    le volume global des achats sur la période."""
    qte, total = db.session.query(
        func.coalesce(func.sum(Transaction.quantity), 0.0),
        func.coalesce(func.sum(Transaction.total), 0.0),
    ).filter(Transaction.product_id == product_id, Transaction.type == "achat").first()
    if qte and qte > 0:
        return total / qte
    product = db.session.get(Product, product_id)
    return product.prix_achat_defaut if product else 0.0


def _cout_marchandises_vendues(start=None, end=None):
    """Coût des marchandises vendues (COGS) sur une période : pour chaque
    produit vendu, quantité vendue × son coût d'achat moyen. C'est ce montant
    qui est soustrait du chiffre d'affaires pour calculer le bénéfice réel —
    pas le total des achats de la période, qui peut inclure du stock pas
    encore vendu."""
    q = db.session.query(Transaction.product_id, func.sum(Transaction.quantity)).filter(
        Transaction.type == "vente"
    )
    if start:
        q = q.filter(Transaction.date >= start)
    if end:
        q = q.filter(Transaction.date <= end)
    total_cout = 0.0
    for product_id, qte_vendue in q.group_by(Transaction.product_id).all():
        total_cout += (qte_vendue or 0.0) * _prix_achat_moyen(product_id)
    return total_cout


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
    cout_vendu_mois = _cout_marchandises_vendues(start_month, today)
    depenses_mois = sum_expenses(start_month, today)
    ventes_jour = sum_total("vente", today, today)
    achats_jour = sum_total("achat", today, today)
    cout_vendu_jour = _cout_marchandises_vendues(today, today)
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
        cout_vendu_mois=cout_vendu_mois,
        depenses_mois=depenses_mois,
        benefice_mois=ventes_mois - cout_vendu_mois - depenses_mois,
        ventes_jour=ventes_jour,
        achats_jour=achats_jour,
        cout_vendu_jour=cout_vendu_jour,
        depenses_jour=depenses_jour,
        benefice_jour=ventes_jour - cout_vendu_jour - depenses_jour,
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
    db.session.delete(tr)
    db.session.commit()
    flash("Transaction supprimée.", "info")
    return redirect(url_for("ventes" if type_ == "vente" else "achats"))


@app.route("/transactions/<int:tid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_transaction(tid):
    tr = db.session.get(Transaction, tid)
    if not tr:
        abort(404)
    redirect_url = url_for("ventes" if tr.type == "vente" else "achats")
    try:
        new_product_id = int(request.form["product_id"])
        new_quantity = float(request.form["quantity"])
        new_unit_price = float(request.form["unit_price"])
        new_partner_id = request.form.get("partner_id") or None
        new_date_raw = request.form.get("date") or tr.date.isoformat()
        new_note = request.form.get("note", tr.note or "").strip()
    except (KeyError, ValueError):
        flash("Formulaire invalide. Vérifiez les champs saisis.", "danger")
        return redirect(redirect_url)

    if new_quantity <= 0 or new_unit_price < 0:
        flash("Quantité ou prix invalide.", "danger")
        return redirect(redirect_url)

    new_product = db.session.get(Product, new_product_id)
    if not new_product:
        flash("Produit introuvable.", "danger")
        return redirect(redirect_url)

    old_product = db.session.get(Product, tr.product_id)

    # Stock qui serait disponible pour le nouveau produit une fois l'effet de
    # l'ancienne transaction annulé (si c'est le même produit, on "rend" sa
    # quantité avant de vérifier la disponibilité).
    stock_disponible = new_product.stock
    if old_product and old_product.id == new_product.id:
        stock_disponible += tr.quantity if tr.type == "vente" else -tr.quantity

    if tr.type == "vente" and stock_disponible < new_quantity:
        flash(
            f"Stock insuffisant pour {new_product.name} (disponible : {stock_disponible:g} {new_product.unit}).",
            "danger",
        )
        return redirect(redirect_url)

    try:
        new_date = datetime.strptime(new_date_raw, "%Y-%m-%d").date()
    except ValueError:
        flash("Date invalide.", "danger")
        return redirect(redirect_url)

    # Annuler l'effet de l'ancienne transaction sur le stock, puis appliquer
    # l'effet de la nouvelle (gère aussi bien un changement de produit qu'un
    # changement de quantité sur le même produit).
    if old_product:
        if tr.type == "vente":
            old_product.stock += tr.quantity
        else:
            old_product.stock -= tr.quantity

    if tr.type == "vente":
        new_product.stock -= new_quantity
    else:
        new_product.stock += new_quantity

    tr.product_id = new_product.id
    tr.partner_id = int(new_partner_id) if new_partner_id else None
    tr.quantity = new_quantity
    tr.unit_price = new_unit_price
    tr.total = new_quantity * new_unit_price
    tr.date = new_date
    tr.note = new_note

    db.session.commit()
    flash("Transaction mise à jour.", "success")
    return redirect(redirect_url)


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


@app.route("/partenaires/<type_>/<int:pid>/modifier", methods=["POST"])
@login_required
@admin_required
def modifier_partenaire(type_, pid):
    if type_ not in ("client", "fournisseur"):
        abort(404)
    p = db.session.get(Partner, pid)
    if not p:
        abort(404)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Le nom est obligatoire.", "danger")
    else:
        p.name = name
        p.phone = request.form.get("phone", "").strip()
        p.address = request.form.get("address", "").strip()
        p.note = request.form.get("note", "").strip()
        db.session.commit()
        flash("Mis à jour.", "success")
    return redirect(url_for("partenaires", type_=type_))


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
    date_debut = _parse_date(request.args.get("date_debut")) or (date.today() - timedelta(days=30))
    date_fin = _parse_date(request.args.get("date_fin")) or date.today()

    base_q = Transaction.query.filter(Transaction.date >= date_debut, Transaction.date <= date_fin)

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
        .filter(Transaction.date >= date_debut, Transaction.date <= date_fin)
        .group_by(Product.name, Transaction.type)
        .all()
    )

    par_jour = (
        db.session.query(
            Transaction.date, Transaction.type, func.sum(Transaction.total)
        )
        .filter(Transaction.date >= date_debut, Transaction.date <= date_fin)
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
        Expense.date >= date_debut, Expense.date <= date_fin
    ).scalar()

    par_categorie_depense = (
        db.session.query(Expense.category, func.sum(Expense.amount))
        .filter(Expense.date >= date_debut, Expense.date <= date_fin)
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc())
        .all()
    )

    # Coût des marchandises vendues et marge, par produit : le bénéfice se
    # calcule sur les quantités effectivement vendues, au prix d'achat réel
    # de ces marchandises — pas sur le volume total des achats de la période.
    cout_vendu_total = 0.0
    marges_par_produit = []
    ventes_par_produit = (
        db.session.query(
            Product.id, Product.name, Product.unit,
            func.sum(Transaction.quantity), func.sum(Transaction.total)
        )
        .join(Product, Product.id == Transaction.product_id)
        .filter(Transaction.type == "vente", Transaction.date >= date_debut, Transaction.date <= date_fin)
        .group_by(Product.id, Product.name, Product.unit)
        .order_by(Product.name)
        .all()
    )
    for pid, name, unit, qte_vendue, total_vente in ventes_par_produit:
        cout_unitaire = _prix_achat_moyen(pid)
        cout_total = (qte_vendue or 0.0) * cout_unitaire
        cout_vendu_total += cout_total
        marges_par_produit.append({
            "name": name, "unit": unit, "qte": qte_vendue,
            "ventes": total_vente, "cout_unitaire": cout_unitaire,
            "cout_total": cout_total, "marge": total_vente - cout_total,
        })

    return render_template(
        "rapports.html",
        date_debut=date_debut,
        date_fin=date_fin,
        ventes_total=ventes_total,
        achats_total=achats_total,
        cout_vendu_total=cout_vendu_total,
        depenses_total=depenses_total,
        benefice=ventes_total - cout_vendu_total - depenses_total,
        par_produit=par_produit,
        marges_par_produit=marges_par_produit,
        par_categorie_depense=par_categorie_depense,
        labels=labels,
        ventes_par_jour=[ventes_par_jour[d] for d in labels],
        achats_par_jour=[achats_par_jour[d] for d in labels],
    )


@app.route("/rapports/export.csv")
@login_required
def export_csv():
    date_debut = _parse_date(request.args.get("date_debut")) or (date.today() - timedelta(days=30))
    date_fin = _parse_date(request.args.get("date_fin")) or date.today()
    transactions = (
        Transaction.query.filter(Transaction.date >= date_debut, Transaction.date <= date_fin)
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
    date_debut = _parse_date(request.args.get("date_debut")) or (date.today() - timedelta(days=30))
    date_fin = _parse_date(request.args.get("date_fin")) or date.today()
    depenses = (
        Expense.query.filter(Expense.date >= date_debut, Expense.date <= date_fin)
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
