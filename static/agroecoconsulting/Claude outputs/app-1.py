import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime
from functools import wraps

import requests
from flask import (
    Flask, render_template, redirect, url_for, request, flash, abort, Response
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from sqlalchemy import inspect, text

from models import db, User, Course, Lesson, Enrollment, Quiz, Question, Choice, QuizAttempt, Submission, CourseResource, LessonResource, LessonAccess

DOCUMENT_MAX_SIZE_MO = 60

APP_NAME = os.environ.get("APP_NAME", "Agro Eco Consulting")
CABINET_NAME = "Cabinet AgroEcoConsult"

# Numéro Wave du cabinet pour recevoir les paiements manuels (en attendant un
# compte Wave Business avec lien de paiement / validation automatique).
WAVE_PAYMENT_NUMBER = "78 830 95 01"

# ---------- Paiement automatique PayDunya ----------
# PayDunya permet d'encaisser Wave, Orange Money, Free Money et carte
# bancaire via une page de paiement hébergée, avec confirmation automatique
# côté serveur (voir /paiement/paydunya/notify plus bas) — sans validation
# manuelle par un administrateur. Le circuit manuel historique (formulaire
# "j'ai déjà payé, voici ma référence") reste disponible en secours,
# notamment pour le virement bancaire que PayDunya ne couvre pas.
#
# (CinetPay a aussi été envisagé, mais son inscription a été refusée en
# raison d'une panne de service au Sénégal au moment de l'intégration —
# PayDunya, opérationnel au Sénégal, a été choisi à la place.)
#
# Pour activer ce circuit : créer un compte marchand sur https://paydunya.com,
# récupérer les 3 clés (Master Key, Private Key, Token) dans le tableau de
# bord ("Intégrez notre API"), puis définir les variables d'environnement
# PAYDUNYA_MASTER_KEY, PAYDUNYA_PRIVATE_KEY et PAYDUNYA_TOKEN (voir
# .env.example et PAYDUNYA.md). Tant qu'elles ne sont pas définies, le
# bouton de paiement automatique reste masqué et seul le circuit manuel est
# proposé — le site fonctionne donc normalement avant toute config.
#
# PAYDUNYA_MODE contrôle l'environnement : "test" (par défaut, aucun vrai
# paiement) ou "live" (paiements réels). Il faut le mettre explicitement à
# "live" pour encaisser de l'argent réel.
PAYDUNYA_MASTER_KEY = os.environ.get("PAYDUNYA_MASTER_KEY", "")
PAYDUNYA_PRIVATE_KEY = os.environ.get("PAYDUNYA_PRIVATE_KEY", "")
PAYDUNYA_TOKEN = os.environ.get("PAYDUNYA_TOKEN", "")
PAYDUNYA_MODE = os.environ.get("PAYDUNYA_MODE", "test").strip().lower()
PAYDUNYA_API_BASE = (
    "https://app.paydunya.com/api/v1" if PAYDUNYA_MODE == "live"
    else "https://app.paydunya.com/sandbox-api/v1"
)
PAYDUNYA_ENABLED = bool(PAYDUNYA_MASTER_KEY and PAYDUNYA_PRIVATE_KEY and PAYDUNYA_TOKEN)


def _paydunya_headers():
    return {
        "Content-Type": "application/json",
        "PAYDUNYA-MASTER-KEY": PAYDUNYA_MASTER_KEY,
        "PAYDUNYA-PRIVATE-KEY": PAYDUNYA_PRIVATE_KEY,
        "PAYDUNYA-TOKEN": PAYDUNYA_TOKEN,
    }


# ---------- Paiement automatique Chariow ----------
# Deuxième circuit de paiement en ligne automatique, en plus de PayDunya :
# Chariow permet d'encaisser via sa propre page de paiement hébergée. Chaque
# formation doit avoir un produit correspondant créé dans le tableau de bord
# Chariow (voir CHARIOW.md), dont l'identifiant est renseigné sur la
# formation (admin > Informations > "Paiement en ligne (Chariow)"). Tant que
# CHARIOW_API_KEY n'est pas défini, le bouton reste masqué et le site
# fonctionne normalement sans cette option.
CHARIOW_API_KEY = os.environ.get("CHARIOW_API_KEY", "")
CHARIOW_PULSE_SECRET = os.environ.get("CHARIOW_PULSE_SECRET", "")
CHARIOW_API_BASE = "https://api.chariow.com/v1"
CHARIOW_ENABLED = bool(CHARIOW_API_KEY)


def _chariow_headers():
    return {
        "Authorization": f"Bearer {CHARIOW_API_KEY}",
        "Content-Type": "application/json",
    }


PAYMENT_METHODS = [
    ("wave", "Wave"),
    ("orange_money", "Orange Money"),
    ("free_money", "Free Money"),
    ("virement", "Virement bancaire"),
    ("chariow", "Chariow"),
    ("autre", "Autre"),
]

# Icône représentative d'une leçon, choisie selon des mots-clés présents dans son
# titre (ordre = priorité : le premier motif qui correspond l'emporte). Couvre le
# vocabulaire SIG/cartographie déjà utilisé, tout en restant générique pour
# d'autres formations (quiz, devoir, vidéo...). Sans correspondance -> 📘.
LESSON_ICON_RULES = [
    (("coordonnée", "projection", "système de référence"), "🧭"),
    (("logiciel", "arcgis", "qgis", "prise en main"), "💻"),
    (("couche", "table attributaire", "symbologie"), "🗂️"),
    (("acquisition", "collecte", "shapefile", "gps"), "🛰️"),
    (("analyse spatiale", "analyse", "requête", "sélection", "tampon", "buffer", "jointure"), "🔎"),
    (("restitution", "mise en page", "impression", "export", "légende", "carte finale"), "🖨️"),
    (("information géographique", "donnée géographique"), "📍"),
    (("généralité", "introduction", "vue d'ensemble", "qu'est-ce", "définition"), "🗺️"),
    (("quiz", "évaluation", "test"), "📝"),
    (("devoir", "exercice", "pratique", "atelier", "cas pratique"), "📎"),
    (("vidéo",), "🎬"),
]
DEFAULT_LESSON_ICON = "📘"


def _icone_lecon(titre):
    t = (titre or "").lower()
    for mots, icone in LESSON_ICON_RULES:
        if any(m in t for m in mots):
            return icone
    return DEFAULT_LESSON_ICON


_YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)
_VIMEO_ID_RE = re.compile(r"vimeo\.com/(?:video/)?(\d+)")
# Google Drive propose plusieurs formats de lien de partage selon la façon
# dont on le copie (bouton « Partager » vs barre d'adresse vs lien
# « Télécharger ») : /file/d/ID/view, /open?id=ID, /uc?id=ID,
# /uc?export=download&id=ID... Les reconnaître tous est important : un lien
# non reconnu tombe sur un simple <a href> vers le fichier, ce qui le rend
# trivialement téléchargeable (au lieu d'être lu dans l'iframe /preview
# ci-dessous).
_DRIVE_FILE_ID_RE = re.compile(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)")
_DRIVE_QUERY_ID_RE = re.compile(r"drive\.google\.com/(?:open|uc)\?(?:[^#\s]*&)?id=([A-Za-z0-9_-]+)")


def _video_embed_url(video_url):
    """Convertit un lien vidéo (YouTube, Vimeo ou Google Drive) en URL
    d'intégration (embed) pour lecture directe sur le site, sans bouton de
    téléchargement ni renvoi vers le site d'origine : l'étudiant reste sur
    la page de la leçon et lit la vidéo dans un lecteur intégré (iframe).

    IMPORTANT — limite technique réelle, à bien comprendre avant de compter
    dessus : cet embed masque le bouton de téléchargement évident de chaque
    plateforme, mais ne constitue PAS une protection étanche contre des
    outils comme IDM (Internet Download Manager), qui surveillent le trafic
    réseau du navigateur plutôt que l'interface du lecteur. Le niveau de
    résistance dépend beaucoup de la plateforme :
      - YouTube (non répertoriée/privée) : le flux est découpé en segments
        avec des URLs signées et changeantes (DASH) — c'est actuellement le
        choix le plus résistant à ce type d'outil parmi les trois.
      - Vimeo : résistance correcte si le téléchargement est désactivé côté
        Vimeo (réglages de la vidéo → Confidentialité).
      - Google Drive : c'est le maillon faible. Même via l'iframe /preview
        ci-dessous, Drive diffuse la vidéo via une URL directe
        (googlevideo.com/videoplayback...) que des outils comme IDM
        détectent et proposent de télécharger — retirer la permission de
        téléchargement dans le partage du fichier (⋮ → Restreindre le
        téléchargement, impression et copie) empêche le bouton de
        téléchargement de Drive lui-même, mais n'empêche PAS IDM de capter
        le flux réseau. Pour des vidéos où le « lecture seule, jamais
        téléchargeable » est vraiment critique, préférer YouTube (non
        répertoriée) à Google Drive.

    Retourne None si l'URL n'est reconnue sur aucune de ces trois
    plateformes : le gabarit affiche alors un simple lien externe (donc
    directement téléchargeable, sans aucune protection)."""
    if not video_url:
        return None
    m = _YOUTUBE_ID_RE.search(video_url)
    if m:
        return f"https://www.youtube-nocookie.com/embed/{m.group(1)}?modestbranding=1&rel=0"
    m = _VIMEO_ID_RE.search(video_url)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    m = _DRIVE_FILE_ID_RE.search(video_url) or _DRIVE_QUERY_ID_RE.search(video_url)
    if m:
        return f"https://drive.google.com/file/d/{m.group(1)}/preview"
    return None


# Alias conservé pour compatibilité (ancien nom, ne gérait que YouTube).
_youtube_embed_url = _video_embed_url


def _database_uri():
    """Lit DATABASE_URL (fourni par la plupart des hébergeurs pour Postgres).
    À défaut, utilise un fichier SQLite local. Convertit 'postgres://' /
    'postgresql://' vers 'postgresql+psycopg://' pour utiliser psycopg (v3,
    voir requirements-postgres.txt)."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return "sqlite:///agroeco_formation.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


IS_PRODUCTION = os.environ.get("DATABASE_URL") is not None

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "agroeco-formation-secret-key-change-en-production")
app.config["SQLALCHEMY_DATABASE_URI"] = _database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
app.config["MAX_CONTENT_LENGTH"] = DOCUMENT_MAX_SIZE_MO * 1024 * 1024

if IS_PRODUCTION and app.config["SECRET_KEY"] == "agroeco-formation-secret-key-change-en-production":
    raise RuntimeError(
        "SECRET_KEY par défaut détectée en production ! "
        "Définissez la variable d'environnement SECRET_KEY avant de déployer "
        "(voir DEPLOIEMENT.md)."
    )

db.init_app(app)


def _ensure_column(inspector, table, column, ddl_type="TEXT"):
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
    db.create_all() (compatible SQLite en local et PostgreSQL en production) :
    ajoute les colonnes permettant de stocker un PDF uploadé directement sur
    une leçon (en plus du lien externe déjà existant), ainsi que le mode
    d'évaluation d'une leçon (QCM ou devoir à rendre)."""
    inspector = inspect(db.engine)
    _ensure_column(inspector, "lessons", "document_data", "BYTEA" if IS_PRODUCTION else "BLOB")
    _ensure_column(inspector, "lessons", "document_filename", "VARCHAR(255)")
    _ensure_column(inspector, "lessons", "document_mimetype", "VARCHAR(100)")
    _ensure_column(inspector, "lessons", "evaluation_mode", "VARCHAR(20)")
    _ensure_column(inspector, "lessons", "devoir_consignes", "TEXT")
    _ensure_column(inspector, "courses", "resource1_label", "VARCHAR(160)")
    _ensure_column(inspector, "courses", "resource1_url", "VARCHAR(500)")
    _ensure_column(inspector, "courses", "resource2_label", "VARCHAR(160)")
    _ensure_column(inspector, "courses", "resource2_url", "VARCHAR(500)")
    _ensure_column(inspector, "courses", "resource3_label", "VARCHAR(160)")
    _ensure_column(inspector, "courses", "resource3_url", "VARCHAR(500)")
    _ensure_column(inspector, "courses", "announcement", "TEXT")
    _ensure_column(inspector, "enrollments", "payment_source", "VARCHAR(10) DEFAULT 'manuel'")
    _ensure_column(inspector, "enrollments", "paydunya_token", "VARCHAR(80)")
    _ensure_column(inspector, "enrollments", "chariow_sale_id", "VARCHAR(60)")
    _ensure_column(inspector, "courses", "chariow_product_id", "VARCHAR(60)")
    _ensure_column(inspector, "courses", "date_fin", "DATE")
    # Les leçons déjà créées n'ont pas de mode d'évaluation défini : on les
    # fait pointer vers 'qcm' si elles ont déjà un quiz, sinon 'aucune'.
    with db.engine.begin() as conn:
        conn.execute(text(
            "UPDATE lessons SET evaluation_mode = 'qcm' "
            "WHERE evaluation_mode IS NULL AND id IN (SELECT lesson_id FROM quizzes)"
        ))
        conn.execute(text(
            "UPDATE lessons SET evaluation_mode = 'aucune' WHERE evaluation_mode IS NULL"
        ))


def _migrer_ressources_formations():
    """Reprend les anciennes ressources fixes (resource1/2/3, remplacées par
    la table dynamique course_resources) pour chaque formation qui n'a pas
    encore de ligne dans course_resources, afin de ne rien perdre de ce qui
    était déjà configuré. Sûr à ré-exécuter : ne fait rien pour une formation
    qui a déjà au moins une ressource migrée."""
    for course in Course.query.all():
        if course.resources.count() > 0:
            continue
        anciennes = [
            (course.resource1_label, course.resource1_url),
            (course.resource2_label, course.resource2_url),
            (course.resource3_label, course.resource3_url),
        ]
        position = 1
        for label, url in anciennes:
            url = (url or "").strip()
            if not url:
                continue
            db.session.add(CourseResource(
                course_id=course.id, label=(label or "").strip(), url=url, position=position,
            ))
            position += 1
    db.session.commit()


with app.app_context():
    db.create_all()
    _ensure_schema_upgrades()
    _migrer_ressources_formations()
    from seed import ensure_seed_data
    ensure_seed_data(verbose=False)


@app.errorhandler(413)
def _fichier_trop_volumineux(e):
    flash(f"Le fichier est trop volumineux (maximum {DOCUMENT_MAX_SIZE_MO} Mo).", "danger")
    return redirect(request.referrer or url_for("admin_formations")), 302

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
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    return {
        "app_name": APP_NAME, "cabinet_name": CABINET_NAME, "now": datetime.utcnow(),
        "wave_payment_number": WAVE_PAYMENT_NUMBER,
        "document_max_size_mo": DOCUMENT_MAX_SIZE_MO,
    }


app.jinja_env.filters["icone_lecon"] = _icone_lecon
app.jinja_env.filters["youtube_embed_url"] = _youtube_embed_url
app.jinja_env.filters["video_embed_url"] = _video_embed_url


# ---------- Pages publiques ----------

@app.route("/")
def accueil():
    """Page d'accueil vitrine du cabinet Agro Eco Consulting."""
    formations = Course.query.filter_by(published=True).order_by(Course.created_at.desc()).limit(3).all()
    return render_template("accueil.html", formations_apercu=formations)


@app.route("/formations")
def index():
    formations = Course.query.filter_by(published=True).order_by(Course.created_at.desc()).all()
    mes_statuts = {}
    if current_user.is_authenticated and not current_user.is_admin:
        for e in Enrollment.query.filter_by(student_id=current_user.id).all():
            mes_statuts[e.course_id] = e.status
    return render_template("index.html", formations=formations, mes_statuts=mes_statuts)


@app.route("/formations/<int:cid>")
def formation_detail(cid):
    formation = db.session.get(Course, cid)
    if not formation or (not formation.published and not (current_user.is_authenticated and current_user.is_admin)):
        abort(404)
    lecons = formation.lessons.order_by(Lesson.position, Lesson.id).all()
    mon_inscription = None
    if current_user.is_authenticated and not current_user.is_admin:
        mon_inscription = Enrollment.query.filter_by(
            student_id=current_user.id, course_id=cid
        ).order_by(Enrollment.created_at.desc()).first()
    return render_template(
        "formation_detail.html", formation=formation, lecons=lecons,
        mon_inscription=mon_inscription, payment_methods=PAYMENT_METHODS,
        paydunya_enabled=PAYDUNYA_ENABLED,
        chariow_enabled=bool(formation.chariow_product_id),
    )


# ---------- Authentification ----------

@app.route("/inscription", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        if not full_name or not email or not password:
            flash("Nom, email et mot de passe sont obligatoires.", "danger")
        elif password != password2:
            flash("Les mots de passe ne correspondent pas.", "danger")
        elif len(password) < 6:
            flash("Le mot de passe doit contenir au moins 6 caractères.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("Un compte existe déjà avec cet email.", "danger")
        else:
            u = User(full_name=full_name, email=email, phone=phone, role="etudiant", active=True)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            login_user(u)
            flash(f"Bienvenue, {u.full_name} ! Votre compte a été créé.", "success")
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.active and user.check_password(password):
            login_user(user)
            flash(f"Bienvenue, {user.full_name}.", "success")
            return redirect(url_for("admin_formations") if user.is_admin else url_for("index"))
        flash("Email ou mot de passe incorrect.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Vous êtes déconnecté(e).", "info")
    return redirect(url_for("index"))


# ---------- Inscription à une formation (paiement) ----------

@app.route("/formations/<int:cid>/inscription", methods=["POST"])
@login_required
def inscrire(cid):
    if current_user.is_admin:
        flash("Un compte administrateur ne peut pas s'inscrire à une formation.", "danger")
        return redirect(url_for("formation_detail", cid=cid))

    formation = db.session.get(Course, cid)
    if not formation or not formation.published:
        abort(404)

    existante = Enrollment.query.filter(
        Enrollment.student_id == current_user.id,
        Enrollment.course_id == cid,
        Enrollment.status.in_(["en_attente", "validee"]),
    ).first()
    if existante:
        flash("Vous avez déjà une inscription en cours ou validée pour cette formation.", "info")
        return redirect(url_for("formation_detail", cid=cid))

    payment_method = request.form.get("payment_method", "")
    payment_reference = request.form.get("payment_reference", "").strip()
    payment_phone = request.form.get("payment_phone", "").strip()

    if payment_method not in dict(PAYMENT_METHODS):
        flash("Mode de paiement invalide.", "danger")
        return redirect(url_for("formation_detail", cid=cid))
    if not payment_reference:
        flash("Merci d'indiquer la référence de votre paiement.", "danger")
        return redirect(url_for("formation_detail", cid=cid))

    e = Enrollment(
        student_id=current_user.id,
        course_id=formation.id,
        amount=formation.price,
        payment_method=payment_method,
        payment_reference=payment_reference,
        payment_phone=payment_phone,
        payment_source="manuel",
        status="en_attente",
    )
    db.session.add(e)
    db.session.commit()
    flash(
        "Votre inscription a été enregistrée. L'accès à la formation sera débloqué "
        "dès que votre paiement aura été vérifié par notre équipe.",
        "success",
    )
    return redirect(url_for("mes_formations"))


# ---------- Paiement automatique (PayDunya) ----------
#
# Circuit qui remplace la vérification manuelle par une confirmation
# automatique côté serveur : /formations/<cid>/payer-paydunya crée une
# facture PayDunya et redirige l'étudiant vers la page de paiement hébergée ;
# PayDunya rappelle ensuite /paiement/paydunya/notify (callback IPN) pour
# confirmer, ce qui valide l'inscription sans action d'un administrateur.
# /paiement/paydunya/retour affiche un retour immédiat à l'étudiant après son
# passage sur la page de paiement (au cas où le callback mettrait quelques
# secondes à arriver).

def _paydunya_check_status(token):
    """Interroge l'API PayDunya pour connaître le statut réel d'une facture,
    par son token. On ne fait jamais confiance au seul contenu du callback
    IPN entrant (qui peut être falsifié) : on revérifie systématiquement ici.
    Retourne 'completed', 'cancelled', 'failed', ou None si le statut n'a pas
    pu être déterminé (ex. erreur réseau)."""
    try:
        resp = requests.get(
            f"{PAYDUNYA_API_BASE}/checkout-invoice/confirm/{token}",
            headers=_paydunya_headers(),
            timeout=15,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        app.logger.error("PayDunya: échec de vérification de %s : %s", token, exc)
        return None
    if data.get("response_code") != "00":
        return None
    status = data.get("status")
    return status.lower() if isinstance(status, str) else status


def _paydunya_apply_status(enrollment, pd_status):
    """Met à jour une inscription selon le statut renvoyé par PayDunya.
    Idempotent : ne fait rien si l'inscription a déjà été traitée (statut
    différent de 'en_attente'), pour supporter sans risque un callback reçu
    plusieurs fois et un appel depuis /retour en plus de /notify."""
    if enrollment.status != "en_attente":
        return
    if pd_status == "completed":
        enrollment.status = "validee"
        enrollment.validated_at = datetime.utcnow()
        enrollment.note_admin = "Paiement confirmé automatiquement via PayDunya."
        db.session.commit()
    elif pd_status in ("cancelled", "failed"):
        enrollment.status = "rejetee"
        enrollment.note_admin = "Paiement refusé ou annulé (PayDunya)."
        db.session.commit()
    # 'pending' ou statut inconnu : on ne touche à rien, un prochain appel
    # (callback suivant ou nouveau /retour) retentera.


@app.route("/formations/<int:cid>/payer-paydunya", methods=["POST"])
@login_required
def payer_paydunya(cid):
    if current_user.is_admin:
        flash("Un compte administrateur ne peut pas s'inscrire à une formation.", "danger")
        return redirect(url_for("formation_detail", cid=cid))
    if not PAYDUNYA_ENABLED:
        flash("Le paiement en ligne automatique n'est pas encore activé sur ce site.", "danger")
        return redirect(url_for("formation_detail", cid=cid))

    formation = db.session.get(Course, cid)
    if not formation or not formation.published:
        abort(404)

    existante = Enrollment.query.filter(
        Enrollment.student_id == current_user.id,
        Enrollment.course_id == cid,
        Enrollment.status.in_(["en_attente", "validee"]),
    ).first()
    if existante:
        flash("Vous avez déjà une inscription en cours ou validée pour cette formation.", "info")
        return redirect(url_for("formation_detail", cid=cid))

    e = Enrollment(
        student_id=current_user.id,
        course_id=formation.id,
        amount=formation.price,
        payment_method="paydunya",
        payment_source="paydunya",
        status="en_attente",
    )
    db.session.add(e)
    db.session.flush()  # attribue e.id sans clôturer la transaction

    try:
        resp = requests.post(
            f"{PAYDUNYA_API_BASE}/checkout-invoice/create",
            headers=_paydunya_headers(),
            json={
                "invoice": {
                    "total_amount": int(round(formation.price)),
                    "description": f"Formation : {formation.title}"[:255],
                    "customer": {
                        "name": current_user.full_name,
                        "email": current_user.email,
                        "phone": current_user.phone or "",
                    },
                },
                "store": {"name": CABINET_NAME},
                "custom_data": {"enrollment_id": e.id},
                "actions": {
                    "cancel_url": url_for("formation_detail", cid=cid, _external=True),
                    "return_url": url_for("paydunya_retour", eid=e.id, _external=True),
                    "callback_url": url_for("paydunya_notify", _external=True),
                },
            },
            timeout=20,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        db.session.rollback()
        app.logger.error("PayDunya: échec d'initialisation du paiement : %s", exc)
        flash(
            "Le paiement en ligne n'a pas pu être initié pour le moment. "
            "Vous pouvez réessayer, ou utiliser le paiement manuel ci-dessous.",
            "danger",
        )
        return redirect(url_for("formation_detail", cid=cid))

    payment_url = data.get("response_text")
    token = data.get("token")
    if data.get("response_code") != "00" or not payment_url or not token:
        db.session.rollback()
        app.logger.error("PayDunya: réponse inattendue à l'initialisation : %s", data)
        flash(
            "Le paiement en ligne n'a pas pu être initié. "
            "Vous pouvez réessayer, ou utiliser le paiement manuel ci-dessous.",
            "danger",
        )
        return redirect(url_for("formation_detail", cid=cid))

    e.paydunya_token = token
    e.payment_reference = token
    db.session.commit()
    return redirect(payment_url)


@app.route("/paiement/paydunya/notify", methods=["GET", "POST"])
def paydunya_notify():
    """Callback IPN appelé par PayDunya pour confirmer un paiement. Doit
    répondre 200 (avec un corps "OK", comme demandé par la documentation
    PayDunya) dans tous les cas pour accuser réception. On ne fait jamais
    confiance au contenu du callback lui-même : on revérifie systématiquement
    via l'API /checkout-invoice/confirm, afin d'éviter toute notification
    falsifiée."""
    token = (
        request.values.get("data[invoice][token]")
        or request.values.get("token")
        or request.values.get("invoice_token")
    )
    if not token:
        return "OK", 200
    enrollment = Enrollment.query.filter_by(paydunya_token=token).first()
    if not enrollment:
        return "OK", 200
    pd_status = _paydunya_check_status(token)
    if pd_status:
        _paydunya_apply_status(enrollment, pd_status)
    return "OK", 200


@app.route("/paiement/paydunya/retour/<int:eid>")
@login_required
def paydunya_retour(eid):
    """Page de retour après le passage de l'étudiant sur la page de paiement
    PayDunya (succès, échec ou abandon). Revérifie une fois le statut tout de
    suite pour un retour immédiat, au cas où le callback mettrait quelques
    secondes à arriver — sans effet si /notify a déjà traité l'inscription."""
    enrollment = db.session.get(Enrollment, eid)
    if not enrollment or enrollment.student_id != current_user.id:
        abort(404)
    if enrollment.status == "en_attente" and enrollment.paydunya_token:
        pd_status = _paydunya_check_status(enrollment.paydunya_token)
        if pd_status:
            _paydunya_apply_status(enrollment, pd_status)

    if enrollment.status == "validee":
        flash("Paiement confirmé, bienvenue dans la formation !", "success")
    elif enrollment.status == "rejetee":
        flash(
            "Le paiement n'a pas abouti (refusé ou annulé). Vous pouvez réessayer.",
            "danger",
        )
    else:
        flash(
            "Paiement en cours de confirmation. L'accès à la formation sera débloqué "
            "automatiquement dès sa validation (généralement en quelques instants).",
            "info",
        )
    return redirect(url_for("mes_formations"))


# ---------- Paiement automatique (Chariow) ----------
#
# Même principe que PayDunya (voir plus haut) : /formations/<cid>/payer-chariow
# crée une session de paiement auprès de l'API Chariow et redirige l'étudiant
# vers la page de paiement hébergée (checkout_url). Chariow rappelle ensuite
# /paiement/chariow/pulse (webhook "Pulse") pour confirmer, ce qui valide
# l'inscription sans action d'un administrateur. /paiement/chariow/retour
# affiche un retour immédiat à l'étudiant après son passage sur la page de
# paiement (au cas où le webhook mettrait quelques secondes à arriver).
#
# Sécurité : le webhook /paiement/chariow/pulse vérifie la signature HMAC-SHA256
# envoyée par Chariow (header X-Chariow-Signature, calculée sur le corps brut
# de la requête avec CHARIOW_PULSE_SECRET), PUIS revérifie systématiquement le
# statut réel de la vente via l'API (/sales/<id>) avant de débloquer un accès
# — on ne fait jamais confiance directement au contenu du webhook, comme pour
# PayDunya.

def _chariow_check_sale(sale_id):
    """Interroge l'API Chariow pour connaître le statut réel d'une vente, par
    son id. Retourne le dict JSON de la vente, ou None si indisponible."""
    try:
        resp = requests.get(
            f"{CHARIOW_API_BASE}/sales/{sale_id}",
            headers=_chariow_headers(),
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        return (resp.json() or {}).get("data")
    except (requests.RequestException, ValueError) as exc:
        app.logger.error("Chariow: échec de vérification de la vente %s : %s", sale_id, exc)
        return None


def _chariow_apply_sale(enrollment, sale_data):
    """Met à jour une inscription selon le statut renvoyé par Chariow pour sa
    vente. Idempotent : ne fait rien si l'inscription a déjà été traitée."""
    if enrollment.status != "en_attente" or not sale_data:
        return
    statut_vente = sale_data.get("status")
    statut_paiement = (sale_data.get("payment") or {}).get("status")
    if statut_vente == "completed" or statut_paiement == "success":
        enrollment.status = "validee"
        enrollment.validated_at = datetime.utcnow()
        enrollment.note_admin = "Paiement confirmé automatiquement via Chariow."
        db.session.commit()
    elif statut_vente in ("failed", "cancelled", "abandoned") or statut_paiement == "failed":
        enrollment.status = "rejetee"
        enrollment.note_admin = "Paiement refusé ou annulé (Chariow)."
        db.session.commit()
    # Statut encore en attente ('awaiting_payment', 'initiated'...) : on ne
    # touche à rien, un prochain appel (webhook suivant ou /retour) retentera.


@app.route("/formations/<int:cid>/payer-chariow", methods=["POST"])
@login_required
def payer_chariow(cid):
    if current_user.is_admin:
        flash("Un compte administrateur ne peut pas s'inscrire à une formation.", "danger")
        return redirect(url_for("formation_detail", cid=cid))

    formation = db.session.get(Course, cid)
    if not formation or not formation.published:
        abort(404)
    if not CHARIOW_ENABLED or not formation.chariow_product_id:
        flash("Le paiement en ligne via Chariow n'est pas encore activé pour cette formation.", "danger")
        return redirect(url_for("formation_detail", cid=cid))

    telephone = request.form.get("phone", "").strip()
    if not telephone:
        flash("Merci d'indiquer un numéro de téléphone pour payer avec Chariow.", "danger")
        return redirect(url_for("formation_detail", cid=cid))

    existante = Enrollment.query.filter(
        Enrollment.student_id == current_user.id,
        Enrollment.course_id == cid,
        Enrollment.status.in_(["en_attente", "validee"]),
    ).first()
    if existante:
        flash("Vous avez déjà une inscription en cours ou validée pour cette formation.", "info")
        return redirect(url_for("formation_detail", cid=cid))

    prenom, _, nom = current_user.full_name.partition(" ")
    e = Enrollment(
        student_id=current_user.id,
        course_id=formation.id,
        amount=formation.price,
        payment_method="chariow",
        payment_source="chariow",
        status="en_attente",
    )
    db.session.add(e)
    db.session.flush()  # attribue e.id sans clôturer la transaction

    try:
        resp = requests.post(
            f"{CHARIOW_API_BASE}/checkout",
            headers=_chariow_headers(),
            json={
                "product_id": formation.chariow_product_id,
                "email": current_user.email,
                "first_name": prenom or current_user.full_name,
                "last_name": nom or "-",
                "phone": {"number": telephone, "country_code": "SN"},
                "custom_metadata": {"enrollment_id": e.id},
                "redirect_url": url_for("chariow_retour", eid=e.id, _external=True),
            },
            timeout=20,
        )
        data = (resp.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError) as exc:
        db.session.rollback()
        app.logger.error("Chariow: échec d'initialisation du paiement : %s", exc)
        flash(
            "Le paiement en ligne n'a pas pu être initié pour le moment. "
            "Vous pouvez réessayer, ou utiliser le paiement manuel ci-dessous.",
            "danger",
        )
        return redirect(url_for("formation_detail", cid=cid))

    checkout_url = ((data.get("payment") or {}).get("checkout_url"))
    sale_id = ((data.get("purchase") or {}).get("id"))
    if data.get("step") != "payment" or not checkout_url or not sale_id:
        db.session.rollback()
        app.logger.error("Chariow: réponse inattendue à l'initialisation : %s", data)
        flash(
            "Le paiement en ligne n'a pas pu être initié. "
            "Vous pouvez réessayer, ou utiliser le paiement manuel ci-dessous.",
            "danger",
        )
        return redirect(url_for("formation_detail", cid=cid))

    e.chariow_sale_id = sale_id
    e.payment_reference = sale_id
    e.payment_phone = telephone
    db.session.commit()
    return redirect(checkout_url)


@app.route("/paiement/chariow/pulse", methods=["POST"])
def chariow_pulse():
    """Webhook ('Pulse') appelé par Chariow pour confirmer un paiement. La
    signature est vérifiée AVANT tout traitement (HMAC-SHA256 sur le corps
    brut, jamais sur une version ré-encodée). On revérifie ensuite
    systématiquement le statut réel via l'API /sales/<id>, sans jamais faire
    confiance directement au contenu du webhook."""
    corps_brut = request.get_data()
    signature_recue = request.headers.get("X-Chariow-Signature", "")
    if not CHARIOW_PULSE_SECRET or not signature_recue:
        return "OK", 200
    signature_attendue = "sha256=" + hmac.new(
        CHARIOW_PULSE_SECRET.encode("utf-8"), corps_brut, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature_recue, signature_attendue):
        app.logger.warning("Chariow: signature de webhook invalide.")
        return "invalid signature", 401

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except ValueError:
        payload = {}
    donnees = payload.get("data") or {}
    sale_id = (
        donnees.get("id")
        or (donnees.get("purchase") or {}).get("id")
        or (donnees.get("sale") or {}).get("id")
    )
    if not sale_id:
        return "OK", 200

    enrollment = Enrollment.query.filter_by(chariow_sale_id=sale_id).first()
    if not enrollment:
        return "OK", 200
    sale_data = _chariow_check_sale(sale_id)
    if sale_data:
        _chariow_apply_sale(enrollment, sale_data)
    return "OK", 200


@app.route("/paiement/chariow/retour/<int:eid>")
@login_required
def chariow_retour(eid):
    """Page de retour après le passage de l'étudiant sur la page de paiement
    Chariow (succès, échec ou abandon) — revérifie une fois le statut tout de
    suite, au cas où le webhook mettrait quelques secondes à arriver."""
    enrollment = db.session.get(Enrollment, eid)
    if not enrollment or enrollment.student_id != current_user.id:
        abort(404)
    if enrollment.status == "en_attente" and enrollment.chariow_sale_id:
        sale_data = _chariow_check_sale(enrollment.chariow_sale_id)
        if sale_data:
            _chariow_apply_sale(enrollment, sale_data)

    if enrollment.status == "validee":
        flash("Paiement confirmé, bienvenue dans la formation !", "success")
    elif enrollment.status == "rejetee":
        flash(
            "Le paiement n'a pas abouti (refusé ou annulé). Vous pouvez réessayer.",
            "danger",
        )
    else:
        flash(
            "Paiement en cours de confirmation. L'accès à la formation sera débloqué "
            "automatiquement dès sa validation (généralement en quelques instants).",
            "info",
        )
    return redirect(url_for("mes_formations"))


# ---------- Espace étudiant ----------

@app.route("/mes-formations")
@login_required
def mes_formations():
    if current_user.is_admin:
        return redirect(url_for("admin_formations"))
    inscriptions = (
        Enrollment.query.filter_by(student_id=current_user.id)
        .order_by(Enrollment.created_at.desc())
        .all()
    )

    quiz_stats = {}  # course_id -> {"reussis": int, "total": int}
    for e in inscriptions:
        if e.status != "validee" or not e.course:
            continue
        total = 0
        reussis = 0
        for l in e.course.lessons:
            if l.quiz:
                total += 1
                meilleure = (
                    QuizAttempt.query.filter_by(quiz_id=l.quiz.id, student_id=current_user.id)
                    .order_by(QuizAttempt.score.desc()).first()
                )
                if meilleure and meilleure.passed:
                    reussis += 1
        if total:
            quiz_stats[e.course_id] = {"reussis": reussis, "total": total}

    return render_template("mes_formations.html", inscriptions=inscriptions, quiz_stats=quiz_stats)


def _lesson_validee(lesson, student):
    """Détermine si une leçon est considérée comme validée pour un étudiant
    donné, selon son mode d'évaluation :
    - QCM : la meilleure tentative doit avoir atteint le seuil de réussite
      (si le mode QCM est choisi mais qu'aucun quiz n'a encore été créé,
      la leçon est considérée NON validée — pas de passe-droit) ;
    - devoir : la dernière soumission doit avoir été validée par un admin ;
    - aucune évaluation : la leçon est considérée acquise dès qu'accessible."""
    mode = lesson.evaluation_mode or "aucune"
    if mode == "qcm":
        if not lesson.quiz:
            return False
        meilleure = (
            QuizAttempt.query.filter_by(quiz_id=lesson.quiz.id, student_id=student.id)
            .order_by(QuizAttempt.score.desc()).first()
        )
        return bool(meilleure and meilleure.passed)
    if mode == "devoir":
        derniere = (
            Submission.query.filter_by(lesson_id=lesson.id, student_id=student.id)
            .order_by(Submission.created_at.desc()).first()
        )
        return bool(derniere and derniere.status == "validee")
    return True


def _renumeroter_lecons(course_id):
    """Renumérote séquentiellement (1, 2, 3...) les leçons d'une formation,
    dans leur ordre d'affichage actuel (position, puis id en cas d'égalité).

    Corrige les positions dupliquées ou avec des trous qui ont pu s'accumuler
    au fil du temps (par ex. suppressions de leçons, ou anciennes leçons dont
    la position n'a jamais été mise à jour) — sans ce nettoyage, deux leçons
    qui partagent déjà la même valeur de position ne changent visiblement pas
    d'ordre quand on les permute (échanger deux valeurs identiques ne change
    rien), et la comparaison stricte « position < position » utilisée par
    _lecon_deverrouillee() peut aussi mal classer des leçons à égalité.

    Ne fait pas de commit : à committer par l'appelant (typiquement avec
    d'autres changements, comme la permutation dans admin_deplacer_lecon)."""
    lecons = (
        Lesson.query.filter_by(course_id=course_id)
        .order_by(Lesson.position, Lesson.id)
        .all()
    )
    for i, l in enumerate(lecons, start=1):
        if l.position != i:
            l.position = i
    return lecons


def _migrer_acces_lecons_existants():
    """Migration ponctuelle (exécutée au tout premier démarrage suivant
    l'introduction de LessonAccess) : convertit l'ancien déverrouillage
    automatique par évaluation (« toutes les leçons précédentes validées »)
    en autorisations manuelles explicites, pour ne priver aucun étudiant déjà
    en cours de formation de l'accès qu'il avait déjà au moment du passage au
    nouveau système.

    Ne s'exécute que si la table lesson_access est entièrement vide : dès
    qu'elle contient au moins une ligne (créée par cette migration ou
    directement par l'administrateur depuis la matrice d'accès), elle ne se
    relance plus jamais — sinon elle annulerait une révocation d'accès
    volontaire de l'administrateur à chaque redémarrage de l'application."""
    if LessonAccess.query.first() is not None:
        return

    for enrollment in Enrollment.query.filter_by(status="validee").all():
        lecons = (
            Lesson.query.filter_by(course_id=enrollment.course_id)
            .order_by(Lesson.position, Lesson.id)
            .all()
        )
        precedentes_validees = True  # la 1re leçon était toujours accessible
        for lesson in lecons:
            if precedentes_validees:
                db.session.add(LessonAccess(lesson_id=lesson.id, student_id=enrollment.student_id))
            precedentes_validees = precedentes_validees and _lesson_validee(lesson, enrollment.student)
    db.session.commit()


with app.app_context():
    _migrer_acces_lecons_existants()


def _lecon_deverrouillee(lesson):
    """Accès manuel : une leçon n'est accessible à un étudiant que si
    l'administrateur le lui a explicitement autorisé pour cette leçon
    précise (voir LessonAccess et admin_modifier_acces_lecons) — remplace
    l'ancien déverrouillage automatique basé sur la réussite des évaluations
    des leçons précédentes. Un administrateur a toujours accès à tout."""
    if current_user.is_admin:
        return True
    return LessonAccess.query.filter_by(
        lesson_id=lesson.id, student_id=current_user.id
    ).first() is not None


@app.route("/formations/<int:cid>/apprendre")
@login_required
def apprendre(cid):
    formation = db.session.get(Course, cid)
    if not formation:
        abort(404)
    if not current_user.is_admin:
        acces = Enrollment.query.filter_by(
            student_id=current_user.id, course_id=cid, status="validee"
        ).first()
        if not acces:
            flash("Vous n'avez pas (encore) accès à cette formation.", "danger")
            return redirect(url_for("formation_detail", cid=cid))
        if _formation_expiree(formation):
            flash(
                "Cette formation est terminée depuis le "
                f"{formation.date_fin.strftime('%d/%m/%Y')}. "
                "L'accès au contenu (leçons, quiz, devoirs) n'est plus disponible.",
                "warning",
            )
            return redirect(url_for("formation_detail", cid=cid))
    lecons = formation.lessons.order_by(Lesson.position, Lesson.id).all()

    meilleurs_scores = {}
    lecons_validees = {}
    lecons_deverrouillees = {}
    dernieres_soumissions = {}
    for l in lecons:
        if not current_user.is_admin and l.quiz:
            meilleure = (
                QuizAttempt.query.filter_by(quiz_id=l.quiz.id, student_id=current_user.id)
                .order_by(QuizAttempt.score.desc()).first()
            )
            if meilleure:
                meilleurs_scores[l.id] = meilleure
        if not current_user.is_admin and (l.evaluation_mode or "aucune") == "devoir":
            derniere = (
                Submission.query.filter_by(lesson_id=l.id, student_id=current_user.id)
                .order_by(Submission.created_at.desc()).first()
            )
            if derniere:
                dernieres_soumissions[l.id] = derniere
        lecons_validees[l.id] = current_user.is_admin or _lesson_validee(l, current_user)
        lecons_deverrouillees[l.id] = _lecon_deverrouillee(l)

    return render_template(
        "apprendre.html", formation=formation, lecons=lecons,
        meilleurs_scores=meilleurs_scores, lecons_validees=lecons_validees,
        lecons_deverrouillees=lecons_deverrouillees, dernieres_soumissions=dernieres_soumissions,
    )


# ---------- Espace étudiant : quiz d'évaluation ----------

def _formation_expiree(course):
    """Une formation dont la date de fin (Course.date_fin) est dépassée
    devient inaccessible aux étudiants — même avec une inscription déjà
    validée — jusqu'à ce que l'admin la republie avec une nouvelle date ou la
    laisse vide. L'administrateur garde toujours accès (voir apprendre() et
    _acces_lecon_ok()). Le jour de la date de fin elle-même reste accessible
    (comparaison stricte) ; le blocage démarre le lendemain.
    Aucune date de fin renseignée = formation en libre accès, sans limite
    dans le temps (comportement historique, inchangé)."""
    return bool(course.date_fin) and datetime.utcnow().date() > course.date_fin


def _acces_lecon_ok(lesson):
    """Vérifie que l'utilisateur courant a accès à la leçon (admin, ou
    inscription validée ET formation pas encore terminée ET accès progressif
    respecté)."""
    if current_user.is_admin:
        return True
    inscrit = Enrollment.query.filter_by(
        student_id=current_user.id, course_id=lesson.course_id, status="validee"
    ).first() is not None
    if not inscrit or _formation_expiree(lesson.course):
        return False
    return _lecon_deverrouillee(lesson)


@app.route("/lecons/<int:lid>/quiz", methods=["GET", "POST"])
@login_required
def passer_quiz(lid):
    lesson = db.session.get(Lesson, lid)
    if not lesson or not lesson.quiz:
        abort(404)
    if not _acces_lecon_ok(lesson):
        flash("Vous n'avez pas (encore) accès à cette leçon : terminez d'abord les leçons précédentes.", "danger")
        return redirect(url_for("apprendre", cid=lesson.course_id))

    quiz = lesson.quiz
    questions = quiz.questions.order_by(Question.position).all()

    if request.method == "POST" and not current_user.is_admin:
        correct_count = 0
        for q in questions:
            reponse = request.form.get(f"question_{q.id}")
            if reponse is not None:
                choix = db.session.get(Choice, int(reponse)) if reponse.isdigit() else None
                if choix and choix.question_id == q.id and choix.is_correct:
                    correct_count += 1
        total = len(questions)
        score = round((correct_count / total) * 100) if total else 0
        attempt = QuizAttempt(
            quiz_id=quiz.id, student_id=current_user.id, score=score,
            correct_count=correct_count, total_count=total,
            passed=score >= quiz.pass_score,
        )
        db.session.add(attempt)
        db.session.commit()
        return render_template(
            "quiz_resultat.html", formation=lesson.course, lesson=lesson,
            quiz=quiz, attempt=attempt,
        )

    dernieres_tentatives = []
    if not current_user.is_admin:
        dernieres_tentatives = (
            QuizAttempt.query.filter_by(quiz_id=quiz.id, student_id=current_user.id)
            .order_by(QuizAttempt.created_at.desc()).all()
        )
    return render_template(
        "quiz.html", formation=lesson.course, lesson=lesson,
        quiz=quiz, questions=questions, tentatives=dernieres_tentatives,
    )


# ---------- Espace étudiant : devoir à rendre ----------

@app.route("/lecons/<int:lid>/devoir", methods=["GET", "POST"])
@login_required
def rendre_devoir(lid):
    lesson = db.session.get(Lesson, lid)
    if not lesson or (lesson.evaluation_mode or "aucune") != "devoir":
        abort(404)
    if not _acces_lecon_ok(lesson):
        flash("Vous n'avez pas (encore) accès à cette leçon : terminez d'abord les leçons précédentes.", "danger")
        return redirect(url_for("apprendre", cid=lesson.course_id))

    if request.method == "POST" and not current_user.is_admin:
        fichier = request.files.get("devoir_file")
        if not fichier or not fichier.filename:
            flash("Merci de choisir un fichier à rendre.", "danger")
            return redirect(url_for("rendre_devoir", lid=lid))
        soumission = Submission(
            lesson_id=lesson.id, student_id=current_user.id,
            file_data=fichier.read(), file_filename=fichier.filename,
            file_mimetype=fichier.mimetype or "application/octet-stream",
            comment=request.form.get("comment", "").strip(),
        )
        db.session.add(soumission)
        db.session.commit()
        flash("Votre travail a été envoyé, il sera corrigé par le professeur.", "success")
        return redirect(url_for("rendre_devoir", lid=lid))

    mes_soumissions = []
    if not current_user.is_admin:
        mes_soumissions = (
            Submission.query.filter_by(lesson_id=lesson.id, student_id=current_user.id)
            .order_by(Submission.created_at.desc()).all()
        )
    peut_soumettre = not mes_soumissions or mes_soumissions[0].status == "rejetee"
    return render_template(
        "devoir.html", formation=lesson.course, lesson=lesson,
        soumissions=mes_soumissions, peut_soumettre=peut_soumettre,
    )


@app.route("/soumissions/<int:sid>/fichier")
@login_required
def telecharger_soumission(sid):
    soumission = db.session.get(Submission, sid)
    if not soumission:
        abort(404)
    if not current_user.is_admin and soumission.student_id != current_user.id:
        abort(403)
    return Response(
        soumission.file_data,
        mimetype=soumission.file_mimetype or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{soumission.file_filename}"'},
    )


# ---------- Administration : formations et leçons ----------

@app.route("/admin/formations", methods=["GET", "POST"])
@login_required
@admin_required
def admin_formations():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        image_url = request.form.get("image_url", "").strip()
        try:
            price = float(request.form.get("price") or 0)
        except ValueError:
            flash("Prix invalide.", "danger")
            return redirect(url_for("admin_formations"))
        if not title:
            flash("Le titre est obligatoire.", "danger")
        else:
            c = Course(title=title, description=description, price=price, image_url=image_url, published=True)
            db.session.add(c)
            db.session.commit()
            flash(f"Formation « {title} » créée.", "success")
        return redirect(url_for("admin_formations"))

    liste = Course.query.order_by(Course.created_at.desc()).all()
    return render_template("admin_formations.html", liste=liste)


DOCUMENT_MIMES_ZIP = (
    "application/zip",
    "application/x-zip-compressed",
    "application/x-zip",
    "multipart/x-zip",
)


def _appliquer_document_upload(lesson):
    """Lit le fichier (PDF ou archive ZIP) envoyé (champ 'document_file') et le
    stocke sur la leçon. Le ZIP sert aussi bien pour des jeux de données
    d'exercice (shapefiles .shp/.dbf/.shx/.prj) que pour un logiciel à
    installer (setup .exe/.msi compressé). Retourne un message d'erreur (str)
    si le fichier n'est ni un PDF ni un ZIP, sinon None. N'a aucun effet si
    aucun fichier n'a été sélectionné."""
    fichier = request.files.get("document_file")
    if not fichier or not fichier.filename:
        return None
    nom = fichier.filename
    type_mime = (fichier.mimetype or "").lower()
    nom_minuscule = nom.lower()
    est_pdf = type_mime == "application/pdf" or nom_minuscule.endswith(".pdf")
    est_zip = type_mime in DOCUMENT_MIMES_ZIP or nom_minuscule.endswith(".zip")
    if not est_pdf and not est_zip:
        return "Seuls les fichiers PDF ou les archives ZIP sont acceptés pour le document d'une leçon."
    lesson.document_data = fichier.read()
    lesson.document_filename = nom
    lesson.document_mimetype = "application/zip" if est_zip else "application/pdf"
    return None


EVALUATION_MODES = ("aucune", "qcm", "devoir")


@app.route("/admin/formations/<int:cid>", methods=["GET", "POST"])
@login_required
@admin_required
def admin_formation_detail(cid):
    formation = db.session.get(Course, cid)
    if not formation:
        abort(404)

    if request.method == "POST":
        # Ajout d'une nouvelle leçon (et, optionnellement, de son quiz avec questions)
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        video_url = request.form.get("video_url", "").strip()
        document_url = request.form.get("document_url", "").strip()
        document_label = request.form.get("document_label", "").strip()

        evaluation_mode = request.form.get("evaluation_mode", "aucune")
        if evaluation_mode not in EVALUATION_MODES:
            evaluation_mode = "aucune"
        devoir_consignes = request.form.get("devoir_consignes", "").strip()

        add_quiz = evaluation_mode == "qcm"
        quiz_title = request.form.get("quiz_title", "").strip() or "Quiz"
        try:
            quiz_pass_score = max(0, min(100, int(request.form.get("quiz_pass_score") or 70)))
        except ValueError:
            quiz_pass_score = 70

        if not title:
            flash("Le titre de la leçon est obligatoire.", "danger")
            return redirect(url_for("admin_formation_detail", cid=cid))

        # Validation préalable des questions du quiz, avant toute écriture en base
        questions_data = []
        if add_quiz:
            # Les champs des questions arrivent sous forme de listes parallèles :
            # q_text[], q_choice_0[], q_choice_1[], q_choice_2[], q_choice_3[], q_correct[]
            q_texts = request.form.getlist("q_text")
            q_corrects = request.form.getlist("q_correct")
            choice_lists = [request.form.getlist(f"q_choice_{i}") for i in range(4)]

            for idx, q_text in enumerate(q_texts):
                q_text = q_text.strip()
                if not q_text:
                    continue
                choix_valides = []
                for i in range(4):
                    if idx < len(choice_lists[i]):
                        texte_choix = choice_lists[i][idx].strip()
                        if texte_choix:
                            choix_valides.append((i, texte_choix))
                bonne_reponse = q_corrects[idx] if idx < len(q_corrects) else None
                if len(choix_valides) < 2:
                    flash(f"Question « {q_text} » : il faut au moins deux choix de réponse.", "danger")
                    return redirect(url_for("admin_formation_detail", cid=cid))
                if bonne_reponse is None or not any(str(i) == bonne_reponse for i, _ in choix_valides):
                    flash(f"Question « {q_text} » : merci d'indiquer quelle réponse est correcte.", "danger")
                    return redirect(url_for("admin_formation_detail", cid=cid))
                questions_data.append((q_text, choix_valides, bonne_reponse))

            if not questions_data:
                flash("Ajoutez au moins une question valide pour créer le quiz, ou changez le mode d'évaluation.", "danger")
                return redirect(url_for("admin_formation_detail", cid=cid))

        position = (formation.lessons.count() or 0) + 1
        lesson = Lesson(
            course_id=formation.id, title=title, content=content,
            video_url=video_url, document_url=document_url,
            document_label=document_label, position=position,
            evaluation_mode=evaluation_mode,
            devoir_consignes=devoir_consignes if evaluation_mode == "devoir" else "",
        )
        erreur = _appliquer_document_upload(lesson)
        if erreur:
            flash(erreur, "danger")
            return redirect(url_for("admin_formation_detail", cid=cid))

        db.session.add(lesson)
        db.session.flush()  # pour obtenir lesson.id

        if add_quiz:
            quiz = Quiz(lesson_id=lesson.id, title=quiz_title, pass_score=quiz_pass_score)
            db.session.add(quiz)
            db.session.flush()

            for q_position, (q_text, choix_valides, bonne_reponse) in enumerate(questions_data, start=1):
                question = Question(quiz_id=quiz.id, text=q_text, position=q_position)
                db.session.add(question)
                db.session.flush()
                for c_position, (i, choice_text) in enumerate(choix_valides, start=1):
                    db.session.add(Choice(
                        question_id=question.id, text=choice_text,
                        is_correct=(str(i) == bonne_reponse), position=c_position,
                    ))

        db.session.commit()
        if add_quiz:
            flash(f"Leçon ajoutée avec un quiz de {len(questions_data)} question(s).", "success")
        else:
            flash("Leçon ajoutée.", "success")
        return redirect(url_for("admin_formation_detail", cid=cid))

    # Auto-corrige au passage d'éventuelles positions dupliquées/désordonnées
    # héritées d'avant l'ajout du réordonnancement (⋮ voir _renumeroter_lecons)
    # — sans ça, la page peut afficher un ordre correct mais figé : les
    # boutons ▲▼ n'auraient alors visiblement aucun effet tant que les
    # positions sous-jacentes ne sont pas nettoyées.
    lecons = _renumeroter_lecons(cid)
    db.session.commit()

    etudiants_valides = [
        e.student for e in formation.enrollments.filter_by(status="validee").all()
    ]
    acces_accordes = set()
    if lecons:
        acces_accordes = {
            (a.lesson_id, a.student_id)
            for a in LessonAccess.query.filter(
                LessonAccess.lesson_id.in_([l.id for l in lecons])
            ).all()
        }

    return render_template(
        "admin_formation_detail.html", formation=formation, lecons=lecons,
        etudiants_valides=etudiants_valides, acces_accordes=acces_accordes,
    )


@app.route("/admin/formations/<int:cid>/acces", methods=["POST"])
@login_required
@admin_required
def admin_modifier_acces_lecons(cid):
    """Enregistre en une fois la matrice d'autorisations d'accès manuel aux
    leçons de cette formation (case cochée = étudiant autorisé pour cette
    leçon) : remplace entièrement l'ensemble des autorisations existantes de
    cette formation par celles cochées dans le formulaire — plus simple et
    plus sûr qu'un ajout/retrait au coup par coup, sans risque d'état
    intermédiaire incohérent."""
    formation = db.session.get(Course, cid)
    if not formation:
        abort(404)

    couples_coches = set()
    for valeur in request.form.getlist("acces"):
        try:
            lesson_id_str, student_id_str = valeur.split(":")
            couples_coches.add((int(lesson_id_str), int(student_id_str)))
        except ValueError:
            continue

    lesson_ids = {l.id for l in formation.lessons}
    student_ids = {
        e.student_id for e in formation.enrollments.filter_by(status="validee").all()
    }
    # Ne garde que des couples valides (leçon de cette formation, étudiant
    # réellement inscrit et validé) — ignore silencieusement toute valeur
    # trafiquée ou devenue obsolète (étudiant désinscrit entre-temps, etc.).
    couples_coches = {
        (lid, sid) for (lid, sid) in couples_coches
        if lid in lesson_ids and sid in student_ids
    }

    existants = {
        (a.lesson_id, a.student_id): a
        for a in LessonAccess.query.filter(LessonAccess.lesson_id.in_(lesson_ids)).all()
    } if lesson_ids else {}

    for (lid, sid) in couples_coches - set(existants.keys()):
        db.session.add(LessonAccess(lesson_id=lid, student_id=sid, granted_by_id=current_user.id))

    for (lid, sid), acces in existants.items():
        if (lid, sid) not in couples_coches:
            db.session.delete(acces)

    db.session.commit()
    flash("Accès aux leçons mis à jour.", "success")
    return redirect(url_for("admin_formation_detail", cid=cid))


@app.route("/admin/formations/<int:cid>/modifier", methods=["POST"])
@login_required
@admin_required
def admin_modifier_formation(cid):
    c = db.session.get(Course, cid)
    if not c:
        abort(404)
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    image_url = request.form.get("image_url", "").strip()
    try:
        price = float(request.form.get("price") or 0)
    except ValueError:
        flash("Prix invalide.", "danger")
        return redirect(url_for("admin_formation_detail", cid=cid))

    date_fin_str = request.form.get("date_fin", "").strip()
    date_fin = None
    if date_fin_str:
        try:
            date_fin = datetime.strptime(date_fin_str, "%Y-%m-%d").date()
        except ValueError:
            flash("Date de fin invalide.", "danger")
            return redirect(url_for("admin_formation_detail", cid=cid))

    if title:
        c.title = title
    c.description = description
    c.image_url = image_url
    c.price = price
    c.published = request.form.get("published") == "on"
    c.announcement = request.form.get("announcement", "").strip()
    c.chariow_product_id = request.form.get("chariow_product_id", "").strip()
    c.date_fin = date_fin
    db.session.commit()
    flash("Formation mise à jour.", "success")
    return redirect(url_for("admin_formation_detail", cid=cid))


@app.route("/admin/formations/<int:cid>/ressources/ajouter", methods=["POST"])
@login_required
@admin_required
def admin_ajouter_ressource(cid):
    formation = db.session.get(Course, cid)
    if not formation:
        abort(404)
    label = request.form.get("label", "").strip()
    url = request.form.get("url", "").strip()
    if not url:
        flash("Le lien de la ressource est obligatoire.", "danger")
        return redirect(url_for("admin_formation_detail", cid=cid))
    position = (formation.resources.count() or 0) + 1
    db.session.add(CourseResource(course_id=formation.id, label=label, url=url, position=position))
    db.session.commit()
    flash("Ressource ajoutée.", "success")
    return redirect(url_for("admin_formation_detail", cid=cid))


@app.route("/admin/ressources/<int:rid>/modifier", methods=["POST"])
@login_required
@admin_required
def admin_modifier_ressource(rid):
    ressource = db.session.get(CourseResource, rid)
    if not ressource:
        abort(404)
    url = request.form.get("url", "").strip()
    if not url:
        flash("Le lien de la ressource est obligatoire.", "danger")
        return redirect(url_for("admin_formation_detail", cid=ressource.course_id))
    ressource.label = request.form.get("label", "").strip()
    ressource.url = url
    db.session.commit()
    flash("Ressource mise à jour.", "success")
    return redirect(url_for("admin_formation_detail", cid=ressource.course_id))


@app.route("/admin/ressources/<int:rid>/supprimer", methods=["POST"])
@login_required
@admin_required
def admin_supprimer_ressource(rid):
    ressource = db.session.get(CourseResource, rid)
    if ressource:
        cid = ressource.course_id
        db.session.delete(ressource)
        db.session.commit()
        flash("Ressource supprimée.", "info")
        return redirect(url_for("admin_formation_detail", cid=cid))
    return redirect(url_for("admin_formations"))


@app.route("/admin/lecons/<int:lid>/ressources/ajouter", methods=["POST"])
@login_required
@admin_required
def admin_ajouter_ressource_lecon(lid):
    lecon = db.session.get(Lesson, lid)
    if not lecon:
        abort(404)
    label = request.form.get("label", "").strip()
    url = request.form.get("url", "").strip()
    if not url:
        flash("Le lien de la ressource est obligatoire.", "danger")
        return redirect(url_for("admin_formation_detail", cid=lecon.course_id))
    position = (lecon.resources.count() or 0) + 1
    db.session.add(LessonResource(lesson_id=lecon.id, label=label, url=url, position=position))
    db.session.commit()
    flash("Ressource de la leçon ajoutée.", "success")
    return redirect(url_for("admin_formation_detail", cid=lecon.course_id))


@app.route("/admin/lecons/ressources/<int:rid>/modifier", methods=["POST"])
@login_required
@admin_required
def admin_modifier_ressource_lecon(rid):
    ressource = db.session.get(LessonResource, rid)
    if not ressource:
        abort(404)
    url = request.form.get("url", "").strip()
    if not url:
        flash("Le lien de la ressource est obligatoire.", "danger")
        return redirect(url_for("admin_formation_detail", cid=ressource.lesson.course_id))
    ressource.label = request.form.get("label", "").strip()
    ressource.url = url
    db.session.commit()
    flash("Ressource de la leçon mise à jour.", "success")
    return redirect(url_for("admin_formation_detail", cid=ressource.lesson.course_id))


@app.route("/admin/lecons/ressources/<int:rid>/supprimer", methods=["POST"])
@login_required
@admin_required
def admin_supprimer_ressource_lecon(rid):
    ressource = db.session.get(LessonResource, rid)
    if ressource:
        cid = ressource.lesson.course_id
        db.session.delete(ressource)
        db.session.commit()
        flash("Ressource de la leçon supprimée.", "info")
        return redirect(url_for("admin_formation_detail", cid=cid))
    return redirect(url_for("admin_formations"))


@app.route("/admin/formations/<int:cid>/supprimer", methods=["POST"])
@login_required
@admin_required
def admin_supprimer_formation(cid):
    c = db.session.get(Course, cid)
    if c:
        if c.enrollments.count() > 0:
            flash("Impossible de supprimer : des étudiants sont inscrits à cette formation.", "danger")
        else:
            db.session.delete(c)
            db.session.commit()
            flash("Formation supprimée.", "info")
    return redirect(url_for("admin_formations"))


@app.route("/admin/lecons/<int:lid>/modifier", methods=["POST"])
@login_required
@admin_required
def admin_modifier_lecon(lid):
    lesson = db.session.get(Lesson, lid)
    if not lesson:
        abort(404)
    title = request.form.get("title", "").strip()
    if title:
        lesson.title = title
    lesson.content = request.form.get("content", "").strip()
    lesson.video_url = request.form.get("video_url", "").strip()
    lesson.document_url = request.form.get("document_url", "").strip()
    lesson.document_label = request.form.get("document_label", "").strip()

    evaluation_mode = request.form.get("evaluation_mode", "aucune")
    if evaluation_mode not in EVALUATION_MODES:
        evaluation_mode = "aucune"
    lesson.evaluation_mode = evaluation_mode
    lesson.devoir_consignes = (
        request.form.get("devoir_consignes", "").strip() if evaluation_mode == "devoir" else ""
    )

    if request.form.get("remove_document") == "on":
        lesson.document_data = None
        lesson.document_filename = None
        lesson.document_mimetype = None
    erreur = _appliquer_document_upload(lesson)
    if erreur:
        flash(erreur, "danger")
        return redirect(url_for("admin_formation_detail", cid=lesson.course_id))
    db.session.commit()
    flash("Leçon mise à jour.", "success")
    return redirect(url_for("admin_formation_detail", cid=lesson.course_id))


@app.route("/admin/lecons/<int:lid>/deplacer/<direction>", methods=["POST"])
@login_required
@admin_required
def admin_deplacer_lecon(lid, direction):
    """Change l'ordre des leçons d'une formation en permutant la position
    (le numéro) d'une leçon avec celle de sa voisine immédiate — précédente
    (direction="haut") ou suivante (direction="bas") — plutôt que d'obliger
    l'admin à retaper manuellement le numéro de chaque leçon.

    On détermine la voisine par sa place dans la liste triée par position,
    pas par arithmétique sur les valeurs de position elles-mêmes : ça reste
    correct même s'il existe des trous dans la numérotation (par exemple
    après la suppression d'une leçon)."""
    lesson = db.session.get(Lesson, lid)
    if not lesson:
        abort(404)
    if direction not in ("haut", "bas"):
        abort(404)

    # Renumérote d'abord (1, 2, 3...) : sur des formations anciennes où
    # plusieurs leçons peuvent avoir la même valeur de position (positions
    # jamais nettoyées, suppressions passées...), permuter deux positions
    # identiques ne changerait rien. Après renumérotation, deux leçons
    # voisines ont toujours des positions distinctes, donc la permutation a
    # bien un effet visible.
    lecons = _renumeroter_lecons(lesson.course_id)
    idx = next((i for i, l in enumerate(lecons) if l.id == lesson.id), None)

    voisine = None
    if idx is not None:
        if direction == "haut" and idx > 0:
            voisine = lecons[idx - 1]
        elif direction == "bas" and idx < len(lecons) - 1:
            voisine = lecons[idx + 1]

    if voisine:
        lesson.position, voisine.position = voisine.position, lesson.position
        flash("Ordre des leçons mis à jour.", "success")

    db.session.commit()
    return redirect(url_for("admin_formation_detail", cid=lesson.course_id))


@app.route("/lecons/<int:lid>/document")
@login_required
def telecharger_document_lecon(lid):
    lesson = db.session.get(Lesson, lid)
    if not lesson or not lesson.document_data:
        abort(404)
    if not _acces_lecon_ok(lesson):
        flash("Vous n'avez pas (encore) accès à cette leçon : terminez d'abord les leçons précédentes.", "danger")
        return redirect(url_for("apprendre", cid=lesson.course_id))
    nom = lesson.document_filename or "document.pdf"
    return Response(
        lesson.document_data,
        mimetype=lesson.document_mimetype or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{nom}"'},
    )


@app.route("/admin/lecons/<int:lid>/supprimer", methods=["POST"])
@login_required
@admin_required
def admin_supprimer_lecon(lid):
    lesson = db.session.get(Lesson, lid)
    if lesson:
        cid = lesson.course_id
        db.session.delete(lesson)
        db.session.commit()
        flash("Leçon supprimée.", "info")
        return redirect(url_for("admin_formation_detail", cid=cid))
    return redirect(url_for("admin_formations"))


# ---------- Administration : quiz d'évaluation ----------

@app.route("/admin/lecons/<int:lid>/quiz", methods=["GET", "POST"])
@login_required
@admin_required
def admin_quiz(lid):
    lesson = db.session.get(Lesson, lid)
    if not lesson:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip() or "Quiz"
        try:
            pass_score = int(request.form.get("pass_score") or 70)
        except ValueError:
            pass_score = 70
        pass_score = max(0, min(100, pass_score))

        if lesson.quiz:
            lesson.quiz.title = title
            lesson.quiz.pass_score = pass_score
        else:
            db.session.add(Quiz(lesson_id=lesson.id, title=title, pass_score=pass_score))
        db.session.commit()
        flash("Quiz enregistré.", "success")
        return redirect(url_for("admin_quiz", lid=lid))

    questions = lesson.quiz.questions.order_by(Question.position).all() if lesson.quiz else []
    return render_template("admin_quiz.html", lesson=lesson, formation=lesson.course, questions=questions)


@app.route("/admin/lecons/<int:lid>/quiz/supprimer", methods=["POST"])
@login_required
@admin_required
def admin_supprimer_quiz(lid):
    lesson = db.session.get(Lesson, lid)
    if not lesson:
        abort(404)
    if lesson.quiz:
        db.session.delete(lesson.quiz)
        db.session.commit()
        flash("Quiz supprimé.", "info")
    return redirect(url_for("admin_formation_detail", cid=lesson.course_id))


@app.route("/admin/quiz/<int:qid>/questions", methods=["POST"])
@login_required
@admin_required
def admin_ajouter_question(qid):
    quiz = db.session.get(Quiz, qid)
    if not quiz:
        abort(404)
    text = request.form.get("text", "").strip()
    if not text:
        flash("L'énoncé de la question est obligatoire.", "danger")
        return redirect(url_for("admin_quiz", lid=quiz.lesson_id))

    choix_textes = request.form.getlist("choice_text")
    bonne_reponse = request.form.get("correct_choice")  # index (str) du choix correct

    choix_valides = [(i, t.strip()) for i, t in enumerate(choix_textes) if t.strip()]
    if len(choix_valides) < 2:
        flash("Il faut au moins deux choix de réponse.", "danger")
        return redirect(url_for("admin_quiz", lid=quiz.lesson_id))
    if bonne_reponse is None or not any(str(i) == bonne_reponse for i, _ in choix_valides):
        flash("Merci d'indiquer quelle réponse est correcte.", "danger")
        return redirect(url_for("admin_quiz", lid=quiz.lesson_id))

    position = (quiz.questions.count() or 0) + 1
    question = Question(quiz_id=quiz.id, text=text, position=position)
    db.session.add(question)
    db.session.flush()  # pour obtenir question.id

    for pos, (i, choice_text) in enumerate(choix_valides, start=1):
        db.session.add(Choice(
            question_id=question.id, text=choice_text,
            is_correct=(str(i) == bonne_reponse), position=pos,
        ))
    db.session.commit()
    flash("Question ajoutée.", "success")
    return redirect(url_for("admin_quiz", lid=quiz.lesson_id))


@app.route("/admin/questions/<int:qid>/supprimer", methods=["POST"])
@login_required
@admin_required
def admin_supprimer_question(qid):
    question = db.session.get(Question, qid)
    if question:
        lid = question.quiz.lesson_id
        db.session.delete(question)
        db.session.commit()
        flash("Question supprimée.", "info")
        return redirect(url_for("admin_quiz", lid=lid))
    return redirect(url_for("admin_formations"))


# ---------- Administration : devoirs à corriger ----------

@app.route("/admin/devoirs")
@login_required
@admin_required
def admin_devoirs():
    statut_filtre = request.args.get("statut", "en_attente")
    q = Submission.query
    if statut_filtre in ("en_attente", "validee", "rejetee"):
        q = q.filter_by(status=statut_filtre)
    liste = q.order_by(Submission.created_at.desc()).all()
    return render_template("admin_devoirs.html", liste=liste, statut_filtre=statut_filtre)


@app.route("/admin/devoirs/<int:sid>/valider", methods=["POST"])
@login_required
@admin_required
def admin_valider_devoir(sid):
    soumission = db.session.get(Submission, sid)
    if not soumission or soumission.status != "en_attente":
        flash("Devoir introuvable ou déjà traité.", "danger")
        return redirect(url_for("admin_devoirs"))
    soumission.status = "validee"
    soumission.note_admin = request.form.get("note_admin", "").strip()
    soumission.corrected_at = datetime.utcnow()
    soumission.corrected_by_id = current_user.id
    db.session.commit()
    flash(f"Devoir validé : {soumission.student.full_name} → {soumission.lesson.title}.", "success")
    return redirect(url_for("admin_devoirs"))


@app.route("/admin/devoirs/<int:sid>/rejeter", methods=["POST"])
@login_required
@admin_required
def admin_rejeter_devoir(sid):
    soumission = db.session.get(Submission, sid)
    if not soumission or soumission.status != "en_attente":
        flash("Devoir introuvable ou déjà traité.", "danger")
        return redirect(url_for("admin_devoirs"))
    soumission.status = "rejetee"
    soumission.note_admin = request.form.get("note_admin", "").strip()
    soumission.corrected_at = datetime.utcnow()
    soumission.corrected_by_id = current_user.id
    db.session.commit()
    flash("Devoir rejeté — l'étudiant peut soumettre un nouveau travail.", "info")
    return redirect(url_for("admin_devoirs"))


# ---------- Administration : inscriptions / paiements ----------

@app.route("/admin/inscriptions")
@login_required
@admin_required
def admin_inscriptions():
    statut_filtre = request.args.get("statut", "en_attente")
    q = Enrollment.query
    if statut_filtre in ("en_attente", "validee", "rejetee"):
        q = q.filter_by(status=statut_filtre)
    liste = q.order_by(Enrollment.created_at.desc()).all()
    payment_methods_display = dict(PAYMENT_METHODS)
    payment_methods_display["paydunya"] = "Paiement en ligne (PayDunya)"
    payment_methods_display["chariow"] = "Paiement en ligne (Chariow)"
    return render_template(
        "admin_inscriptions.html", liste=liste, statut_filtre=statut_filtre,
        payment_methods=payment_methods_display,
    )


@app.route("/admin/inscriptions/<int:eid>/valider", methods=["POST"])
@login_required
@admin_required
def admin_valider_inscription(eid):
    e = db.session.get(Enrollment, eid)
    if not e or e.status != "en_attente":
        flash("Inscription introuvable ou déjà traitée.", "danger")
        return redirect(url_for("admin_inscriptions"))
    e.status = "validee"
    e.validated_at = datetime.utcnow()
    e.validated_by_id = current_user.id
    db.session.commit()
    flash(f"Inscription validée : {e.student.full_name} → {e.course.title}.", "success")
    return redirect(url_for("admin_inscriptions"))


@app.route("/admin/inscriptions/<int:eid>/rejeter", methods=["POST"])
@login_required
@admin_required
def admin_rejeter_inscription(eid):
    e = db.session.get(Enrollment, eid)
    if not e or e.status != "en_attente":
        flash("Inscription introuvable ou déjà traitée.", "danger")
        return redirect(url_for("admin_inscriptions"))
    e.status = "rejetee"
    e.note_admin = request.form.get("note_admin", "").strip()
    e.validated_at = datetime.utcnow()
    e.validated_by_id = current_user.id
    db.session.commit()
    flash("Inscription rejetée.", "info")
    return redirect(url_for("admin_inscriptions"))


@app.route("/admin/etudiants")
@login_required
@admin_required
def admin_etudiants():
    """Étudiants inscrits, regroupés par formation plutôt que par étudiant :
    pour chaque formation, la liste des étudiants qui s'y sont inscrits (avec
    le statut de leur inscription) — plus pratique que l'ancienne liste à plat
    pour, par exemple, constituer un groupe WhatsApp par promotion ou vérifier
    qui est réellement inscrit à quelle formation. Un même étudiant inscrit à
    plusieurs formations apparaît donc sous chacune d'elles."""
    # Ordre d'affichage des inscriptions au sein d'une formation : validées
    # d'abord (ce sont les étudiants ayant réellement accès au contenu),
    # puis en attente, puis rejetées — plutôt que l'ordre alphabétique du
    # champ status ("en_attente" < "rejetee" < "validee") qui n'a pas de sens
    # pour l'admin.
    ordre_statut = {"validee": 0, "en_attente": 1, "rejetee": 2}

    formations = Course.query.order_by(Course.created_at.desc()).all()
    par_formation = []
    for c in formations:
        inscriptions = sorted(
            c.enrollments.all(),
            key=lambda e: (ordre_statut.get(e.status, 9), e.created_at or datetime.min),
        )
        par_formation.append({
            "course": c,
            "inscriptions": inscriptions,
            "nb_validees": sum(1 for e in inscriptions if e.status == "validee"),
        })

    # Comptes étudiant sans aucune inscription (cas rare, ex. compte créé
    # sans jamais passer par une formation) : affichés à part pour ne pas
    # les faire disparaître de la page.
    etudiants_sans_formation = (
        User.query.filter_by(role="etudiant")
        .filter(~User.enrollments.any())
        .order_by(User.created_at.desc())
        .all()
    )

    return render_template(
        "admin_etudiants.html",
        par_formation=par_formation,
        etudiants_sans_formation=etudiants_sans_formation,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
