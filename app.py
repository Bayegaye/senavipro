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

db.init_app(app
