"""Initialise la base de données SENAVIPRO avec un compte admin et les produits de base."""
from app import app
from models import db
from seed import ensure_seed_data

with app.app_context():
    db.create_all()
    ensure_seed_data(verbose=True)
    print("Base de données initialisée avec succès.")
