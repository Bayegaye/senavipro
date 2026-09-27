"""Données de démarrage : compte admin et produits par défaut.

ensure_seed_data() est idempotente : elle ne crée que ce qui manque encore
(comptes, produits) et ne touche jamais aux données déjà présentes. Elle est
appelée à chaque démarrage de l'application (app.py) ainsi que par
init_db.py, pour que les nouveaux produits par défaut apparaissent aussi sur
une base de données déjà en service.
"""
import os
import secrets

from models import db, User, Product, DeliveryTier

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

# Compte technique (sans mot de passe utilisable — voir plus bas) auquel sont
# rattachées les commandes passées par les clients depuis le site public
# (/commander), qui n'agissent pas au nom d'un membre de l'équipe.
SYSTEM_USERNAME = "boutique-en-ligne"

# Paliers de prix de livraison par défaut (en FCFA), selon la quantité totale
# du panier (tous produits confondus) — proposition initiale, modifiable à
# tout moment par l'administrateur sur la page Livraison > Tarifs.
DEFAULT_DELIVERY_TIERS = [
    (1, 5, 1000),
    (6, 15, 1500),
    (16, 30, 2500),
    (31, None, 4000),
]


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

    if not User.query.filter_by(username=SYSTEM_USERNAME).first():
        # Compte désactivé (active=False) : la connexion vérifie toujours
        # user.active avant d'accepter un mot de passe, donc ce compte ne
        # peut jamais servir à se connecter, quel que soit le mot de passe
        # (aléatoire et non communiqué) qui lui est attribué ici. Il n'existe
        # que pour satisfaire la contrainte "user_id obligatoire" sur les
        # commandes/ventes, dont l'auteur réel est un client du site public.
        system_user = User(
            username=SYSTEM_USERNAME,
            full_name="Boutique en ligne (compte technique)",
            role="employe",
            active=False,
        )
        system_user.set_password(secrets.token_hex(32))
        db.session.add(system_user)
        if verbose:
            print("Compte technique 'boutique-en-ligne' créé (commandes du site public).")

    if DeliveryTier.query.count() == 0:
        for qmin, qmax, prix in DEFAULT_DELIVERY_TIERS:
            db.session.add(DeliveryTier(quantite_min=qmin, quantite_max=qmax, prix=prix))
        if verbose:
            print("Paliers de livraison par défaut créés (modifiables sur la page Livraison > Tarifs).")

    db.session.commit()
