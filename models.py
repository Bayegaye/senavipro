from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    full_name = db.Column(db.String(128), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="employe")  # admin | employe
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    def get_id(self):
        return str(self.id)


class Partner(db.Model):
    """Client ou fournisseur."""
    __tablename__ = "partners"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # client | fournisseur
    phone = db.Column(db.String(32))
    address = db.Column(db.String(256))
    note = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship("Transaction", backref="partner", lazy="dynamic")


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)  # ex: "Oeufs de table", "Poulets de chair"
    unit = db.Column(db.String(32), nullable=False)  # ex: "plateau", "unite", "kg"
    stock = db.Column(db.Float, nullable=False, default=0)
    seuil_alerte = db.Column(db.Float, nullable=False, default=0)  # seuil de stock bas
    prix_vente_defaut = db.Column(db.Float, default=0)
    prix_achat_defaut = db.Column(db.Float, default=0)
    # Produit retiré (archivé) : masqué des formulaires de vente, d'achat, de
    # commande et de la boutique en ligne, mais conservé pour l'historique
    # (factures, rapports). Réactivable à tout moment depuis la page Produits.
    actif = db.Column(db.Boolean, nullable=False, default=True)
    # Identifiant stable des produits créés par défaut (seed.py) : permet de
    # les renommer ou de les retirer sans qu'ils soient recréés au démarrage.
    seed_key = db.Column(db.String(64))

    transactions = db.relationship("Transaction", backref="product", lazy="dynamic")


class Expense(db.Model):
    """Dépense générale de l'entreprise (hors achat de marchandises) :
    aliment volaille, vétérinaire, transport, salaires, loyer, électricité, etc."""
    __tablename__ = "expenses"
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(64), nullable=False)
    description = db.Column(db.String(256))
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")


class Loss(db.Model):
    """Produits cassés, périmés ou perdus : sortent du stock comme une vente
    mais ne rapportent aucun revenu — leur valeur d'achat est donc soustraite
    du bénéfice réel plutôt que d'être ignorée."""
    __tablename__ = "losses"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    reason = db.Column(db.String(128))
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product")
    user = db.relationship("User")


class Sale(db.Model):
    """Une facture de vente : regroupe l'achat d'un ou plusieurs produits par un
    même client, réglés ensemble, sous un seul numéro de facture. Chaque produit
    de la facture correspond à une ligne (Transaction de type 'vente') rattachée
    à cette Sale via Transaction.sale_id."""
    __tablename__ = "sales"
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(30), unique=True, nullable=False)
    partner_id = db.Column(db.Integer, db.ForeignKey("partners.id"), nullable=True)
    total = db.Column(db.Float, nullable=False, default=0)  # total des produits
    # Frais de livraison facturés au client, en plus des produits. Ils ne
    # sont pas comptés dans `total` (chiffre d'affaires produits), mais
    # s'ajoutent au montant à payer affiché sur la facture.
    frais_livraison = db.Column(db.Float, nullable=False, default=0)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def total_a_payer(self):
        return (self.total or 0) + (self.frais_livraison or 0)

    partner = db.relationship("Partner")
    user = db.relationship("User")
    lignes = db.relationship(
        "Transaction", backref="sale", lazy="dynamic", order_by="Transaction.id"
    )


class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(10), nullable=False)  # vente | achat
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    partner_id = db.Column(db.Integer, db.ForeignKey("partners.id"), nullable=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=True)
    quantity = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    note = db.Column(db.String(256))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")


class Order(db.Model):
    """Commande d'un client : réserve une quantité de stock avant d'être
    transformée en vente (à la confirmation) ou annulée (la réservation est
    alors libérée sans impact sur le stock, qui n'est modifié qu'à la
    confirmation).

    Une ligne peut soit être saisie directement par un membre de l'équipe
    (order_group_id vide, cas historique — page Commandes), soit provenir
    d'un panier passé par un client sur le site public (order_group_id
    renseigné, plusieurs lignes rattachées à un même OrderGroup — voir
    ci-dessous)."""
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("partners.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Float, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)
    total = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="en_attente")  # en_attente | confirmee | annulee
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    date_confirmation = db.Column(db.Date, nullable=True)
    note = db.Column(db.String(256))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=True)
    order_group_id = db.Column(db.Integer, db.ForeignKey("order_groups.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    client = db.relationship("Partner")
    product = db.relationship("Product")
    user = db.relationship("User")
    sale = db.relationship("Sale")


class DeliveryTier(db.Model):
    """Palier de prix de livraison en fonction de la quantité totale commandée
    (tous produits confondus) sur le panier du site public — modifiable par
    l'administrateur (page Livraison > Tarifs)."""
    __tablename__ = "delivery_tiers"
    id = db.Column(db.Integer, primary_key=True)
    quantite_min = db.Column(db.Float, nullable=False)
    quantite_max = db.Column(db.Float, nullable=True)  # vide = pas de plafond
    prix = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class OrderGroup(db.Model):
    """Un panier passé par un client depuis le site public (page /commander) :
    regroupe une ou plusieurs lignes Order (une par produit du panier) ainsi
    que les informations de livraison (contact, adresse, frais de livraison
    calculé selon la quantité totale). Distincte de Sale (qui représente une
    facture déjà validée) : un OrderGroup ne devient une Sale qu'à la
    confirmation par un membre de l'équipe (page Livraisons), qui vérifie le
    stock réel avant de débiter quoi que ce soit — jusque-là, comme pour Order,
    aucun stock n'est modifié."""
    __tablename__ = "order_groups"
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(30), unique=True, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("partners.id"), nullable=False)
    client_phone = db.Column(db.String(32))
    client_address = db.Column(db.String(256))
    delivery_fee = db.Column(db.Float, nullable=False, default=0)
    total_produits = db.Column(db.Float, nullable=False, default=0)
    total = db.Column(db.Float, nullable=False, default=0)
    # nouvelle | confirmee | en_livraison | livree | annulee
    status = db.Column(db.String(20), nullable=False, default="nouvelle")
    note = db.Column(db.String(256))
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    confirmed_at = db.Column(db.DateTime, nullable=True)
    delivered_at = db.Column(db.DateTime, nullable=True)

    client = db.relationship("Partner")
    sale = db.relationship("Sale")
    lignes = db.relationship(
        "Order", backref="order_group", lazy="dynamic", order_by="Order.id",
        foreign_keys="Order.order_group_id",
    )


class SupplierPayment(db.Model):
    """Versement (avance) remis a un fournisseur, independamment de toute
    livraison precise de marchandise.

    Le solde encore disponible chez un fournisseur donne se calcule comme :
        somme des versements (SupplierPayment) - valeur des marchandises deja
        recues de ce fournisseur (somme des Transaction de type "achat" liees
        a ce partenaire).
    """
    __tablename__ = "supplier_payments"
    id = db.Column(db.Integer, primary_key=True)
    partner_id = db.Column(db.Integer, db.ForeignKey("partners.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    note = db.Column(db.String(256))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    partner = db.relationship("Partner", backref=db.backref("payments", lazy="dynamic"))
    user = db.relationship("User")


class CapitalSettings(db.Model):
    """Reglage unique (une seule ligne) pour le suivi du capital de l'entreprise :
    le solde de caisse (argent liquide) au moment ou le suivi a demarre, avant
    toute vente/depense deja enregistree dans l'application.

    Situation financiere totale en temps reel =
        valeur du stock (quantite x prix d'achat)
        + argent disponible chez les fournisseurs (versements - marchandises recues)
        + argent liquide en caisse (solde initial + ventes - depenses - versements aux fournisseurs)
    """
    __tablename__ = "capital_settings"
    id = db.Column(db.Integer, primary_key=True)
    solde_caisse_initial = db.Column(db.Float, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CapitalSnapshot(db.Model):
    """Relevé du capital total de l'entreprise (situation financière calculée
    par la route /capital) à une date donnée, conservé pour pouvoir analyser
    son évolution dans le temps : augmentation ou diminution, en valeur
    absolue et en pourcentage.

    Une seule ligne par jour (date unique) : enregistrée/mise à jour
    automatiquement à chaque consultation de la page Capital ce jour-là (voir
    _enregistrer_releve_capital_du_jour() dans app.py), donc toujours à jour
    pour le jour courant, et figée (historique) pour les jours passés."""
    __tablename__ = "capital_snapshots"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, unique=True)
    situation_financiere = db.Column(db.Float, nullable=False)
    valeur_stock = db.Column(db.Float, nullable=False, default=0)
    argent_disponible_fournisseurs = db.Column(db.Float, nullable=False, default=0)
    argent_liquide = db.Column(db.Float, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
