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


class Sale(db.Model):
    """Une facture de vente : regroupe l'achat d'un ou plusieurs produits par un
    même client, réglés ensemble, sous un seul numéro de facture. Chaque produit
    de la facture correspond à une ligne (Transaction de type 'vente') rattachée
    à cette Sale via Transaction.sale_id."""
    __tablename__ = "sales"
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(30), unique=True, nullable=False)
    partner_id = db.Column(db.Integer, db.ForeignKey("partners.id"), nullable=True)
    total = db.Column(db.Float, nullable=False, default=0)
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    confirmation)."""
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    client = db.relationship("Partner")
    product = db.relationship("Product")
    user = db.relationship("User")
    sale = db.relationship("Sale")
