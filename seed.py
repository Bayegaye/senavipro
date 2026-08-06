"""Données de démarrage : compte admin et produits par défaut.

ensure_seed_data() est idempotente : elle ne crée que ce qui manque encore
(comptes, produits) et ne touche jamais aux données déjà présentes. Elle est
appelée à chaque démarrage de l'application (app.py) ainsi que par
init_db.py, pour que les nouveaux produits par défaut apparaissent aussi sur
une base de données déjà en service.
"""
import os

from models import db, User, Product

# (nom, unité, seuil d'alerte, prix de vente par défaut, prix d'achat par défaut)
DEFAULT_PRODUCTS = [
    ("Oeufs de table - Petit calibre", "plateau", 10, 1800, 1500),
    ("Oeufs de table - Moyen calibre", "plateau", 10, 2000, 1700),
    ("Oeufs de table - Gros calibre", "plateau", 10, 2200, 1900),
    ("Poulets de chair", "unite", 20, 4500, 3500),
]

# En hébergement en ligne, définissez la variable d'environnement ADMIN_PASSWORD
# pour éviter que le mot de passe admin par défaut ne reste utilisable publiquement.
DEFAULT_ADMIN_PASSWORD = "senavipro2026"


def ensure_seed_data(verbose=False):
    if not User.query.filter_by(username="admin").first():
        admin_password = os.environ.get("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
        admin = User(username="admin", full_name="Administrateur SENAVIPRO", role="admin")
        admin.set_password(admin_password)
        db.session.add(admin)
        if verbose:
            if admin_password == DEFAULT_ADMIN_PASSWORD:
                print(f"Compte admin créé -> identifiant: admin / mot de passe: {DEFAULT_ADMIN_PASSWORD}")
            else:
                print("Compte admin créé -> identifiant: admin / mot de passe : celui défini dans ADMIN_PASSWORD")
    elif verbose:
        print("Le compte admin existe déjà.")

    for name, unit, seuil, prix_vente, prix_achat in DEFAULT_PRODUCTS:
        if not Product.query.filter_by(name=name).first():
            db.session.add(Product(
                name=name, unit=unit, stock=0, seuil_alerte=seuil,
                prix_vente_defaut=prix_vente, prix_achat_defaut=prix_achat,
            ))
            if verbose:
                print(f"Produit '{name}' créé.")

    db.session.commit()
