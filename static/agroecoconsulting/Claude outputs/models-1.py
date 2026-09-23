from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(128), nullable=False)
    email = db.Column(db.String(128), unique=True, nullable=False)
    phone = db.Column(db.String(32))
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="etudiant")  # admin | etudiant
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    enrollments = db.relationship(
        "Enrollment", foreign_keys="Enrollment.student_id", backref="student", lazy="dynamic"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    def get_id(self):
        return str(self.id)


class Course(db.Model):
    """Une formation payante proposée par le cabinet."""
    __tablename__ = "courses"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Float, nullable=False, default=0)
    image_url = db.Column(db.String(500))  # lien vers une image de couverture (optionnel)
    published = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Ressources générales de la formation (setup logiciels, jeux de données,
    # dossier Drive...) affichées sur la page principale de la formation —
    # distinctes des documents/vidéos propres à chaque leçon.
    resource1_label = db.Column(db.String(160))
    resource1_url = db.Column(db.String(500))
    resource2_label = db.Column(db.String(160))
    resource2_url = db.Column(db.String(500))
    resource3_label = db.Column(db.String(160))
    resource3_url = db.Column(db.String(500))

    # Identifiant du produit correspondant à cette formation côté Chariow
    # (créé manuellement dans le tableau de bord Chariow, puis collé ici) —
    # nécessaire pour initier un paiement en ligne via Chariow pour cette
    # formation (voir CHARIOW.md et payer_chariow() dans app.py). Laissé vide
    # tant que le paiement Chariow n'est pas configuré pour la formation.
    chariow_product_id = db.Column(db.String(60))

    # Annonce publique décrivant le déroulement de la formation (dates,
    # horaires, format des séances, attestation...), affichée sur la page
    # publique de la formation pour informer les candidats avant inscription.
    announcement = db.Column(db.Text)

    # Date de fin de la formation (optionnelle) : au-delà de cette date,
    # l'accès au contenu (leçons, quiz, devoirs, documents) est bloqué pour
    # les étudiants, même avec une inscription déjà validée — l'administrateur
    # garde toujours accès. Laissée vide, la formation reste accessible sans
    # limite dans le temps (comportement historique, inchangé).
    date_fin = db.Column(db.Date)

    lessons = db.relationship(
        "Lesson", backref="course", lazy="dynamic",
        order_by="Lesson.position", cascade="all, delete-orphan"
    )
    enrollments = db.relationship("Enrollment", backref="course", lazy="dynamic")
    resources = db.relationship(
        "CourseResource", backref="course", lazy="dynamic",
        order_by="CourseResource.position", cascade="all, delete-orphan"
    )


class CourseResource(db.Model):
    """Une ressource libre de la formation (lien vers un setup logiciel, un
    jeu de données, un dossier Drive...), affichée sur la page principale de
    la formation. Remplace les anciens champs fixes resource1/2/3 du modèle
    Course (conservés pour compatibilité/migration) par une liste dynamique
    : l'administrateur peut désormais en ajouter ou en supprimer autant
    qu'il le souhaite."""
    __tablename__ = "course_resources"
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=False)
    label = db.Column(db.String(160))
    url = db.Column(db.String(500), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Lesson(db.Model):
    """Une leçon d'une formation : vidéo (lien, intégrée en lecture directe
    sans bouton de téléchargement pour YouTube/Vimeo/Google Drive) et/ou
    document (lien ou upload) et/ou texte, plus une liste libre de
    ressources complémentaires (voir LessonResource)."""
    __tablename__ = "lessons"
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    content = db.Column(db.Text)  # texte / instructions (optionnel)
    video_url = db.Column(db.String(500))  # ex: lien YouTube non répertorié / Vimeo (optionnel)
    document_url = db.Column(db.String(500))  # ex: lien Google Drive / Dropbox vers un PDF (optionnel)
    document_label = db.Column(db.String(160))  # ex: "Support de cours (PDF)"
    document_data = db.Column(db.LargeBinary)  # contenu binaire du PDF uploadé depuis l'admin (optionnel)
    document_filename = db.Column(db.String(255))  # nom original du fichier PDF uploadé
    document_mimetype = db.Column(db.String(100))  # type MIME du fichier uploadé (ex: application/pdf)
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Mode d'évaluation de la leçon : "aucune" | "qcm" | "devoir". Détermine
    # comment un étudiant valide la leçon (badge "Validée" affiché à
    # l'étudiant et à l'admin, voir _lesson_validee dans app.py) — sert
    # uniquement au suivi de progression : depuis l'introduction de
    # LessonAccess, la réussite d'une évaluation ne débloque plus
    # automatiquement l'accès à la leçon suivante, l'administrateur autorise
    # désormais lui-même l'accès de chaque étudiant, leçon par leçon.
    evaluation_mode = db.Column(db.String(20), nullable=False, default="aucune")
    devoir_consignes = db.Column(db.Text)  # consignes du devoir à rendre (si evaluation_mode == "devoir")

    # Ressources complémentaires de la leçon (fichiers Drive, Dropbox...),
    # en plus de l'unique document_url/document_data ci-dessus : l'admin
    # peut désormais joindre autant de liens qu'il le souhaite.
    resources = db.relationship(
        "LessonResource", backref="lesson", lazy="dynamic",
        order_by="LessonResource.position", cascade="all, delete-orphan"
    )


class LessonResource(db.Model):
    """Une ressource complémentaire d'une leçon (lien Drive, Dropbox...),
    affichée sous le contenu de la leçon. Fonctionne comme CourseResource,
    mais au niveau de la leçon plutôt que de la formation entière :
    l'administrateur peut en ajouter ou en supprimer autant qu'il le
    souhaite pour chaque leçon."""
    __tablename__ = "lesson_resources"
    id = db.Column(db.Integer, primary_key=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=False)
    label = db.Column(db.String(160))
    url = db.Column(db.String(500), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class LessonAccess(db.Model):
    """Autorisation manuelle, accordée par l'administrateur, permettant à un
    étudiant précis d'accéder à une leçon précise. Remplace l'ancien
    déverrouillage automatique basé sur la réussite des évaluations des
    leçons précédentes (voir _lecon_deverrouillee dans app.py) : désormais,
    l'administrateur décide lui-même, pour chaque leçon d'une formation,
    quels étudiants inscrits y ont accès — via la matrice d'accès de la page
    de gestion de la formation.

    L'absence de ligne pour un couple (leçon, étudiant) signifie que l'accès
    n'est pas autorisé ; il n'y a pas de valeur "par défaut" implicite,
    même pour la toute première leçon d'une formation."""
    __tablename__ = "lesson_access"
    id = db.Column(db.Integer, primary_key=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    granted_at = db.Column(db.DateTime, default=datetime.utcnow)
    granted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    lesson = db.relationship(
        "Lesson", backref=db.backref("acces_accordes", lazy="dynamic", cascade="all, delete-orphan")
    )
    student = db.relationship("User", foreign_keys=[student_id])
    granted_by = db.relationship("User", foreign_keys=[granted_by_id])

    __table_args__ = (
        db.UniqueConstraint("lesson_id", "student_id", name="uq_lesson_access_lesson_student"),
    )


class Quiz(db.Model):
    """Quiz d'évaluation associé à une leçon (QCM)."""
    __tablename__ = "quizzes"
    id = db.Column(db.Integer, primary_key=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=False, unique=True)
    title = db.Column(db.String(160), nullable=False, default="Quiz")
    pass_score = db.Column(db.Integer, nullable=False, default=70)  # % requis pour "réussi"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    lesson = db.relationship("Lesson", backref=db.backref("quiz", uselist=False, cascade="all, delete-orphan"))
    questions = db.relationship(
        "Question", backref="quiz", lazy="dynamic",
        order_by="Question.position", cascade="all, delete-orphan"
    )
    attempts = db.relationship("QuizAttempt", backref="quiz", lazy="dynamic", cascade="all, delete-orphan")


class Question(db.Model):
    """Une question à choix multiples d'un quiz."""
    __tablename__ = "questions"
    id = db.Column(db.Integer, primary_key=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey("quizzes.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    choices = db.relationship(
        "Choice", backref="question", lazy="dynamic",
        order_by="Choice.position", cascade="all, delete-orphan"
    )


class Choice(db.Model):
    """Un choix de réponse pour une question (une seule bonne réponse par question)."""
    __tablename__ = "choices"
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    text = db.Column(db.String(300), nullable=False)
    is_correct = db.Column(db.Boolean, default=False)
    position = db.Column(db.Integer, nullable=False, default=0)


class QuizAttempt(db.Model):
    """Une tentative d'un étudiant sur un quiz (essais illimités)."""
    __tablename__ = "quiz_attempts"
    id = db.Column(db.Integer, primary_key=True)
    quiz_id = db.Column(db.Integer, db.ForeignKey("quizzes.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    score = db.Column(db.Integer, nullable=False)  # % (0-100)
    correct_count = db.Column(db.Integer, nullable=False, default=0)
    total_count = db.Column(db.Integer, nullable=False, default=0)
    passed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship("User", backref=db.backref("quiz_attempts", lazy="dynamic"))


class Enrollment(db.Model):
    """Inscription d'un étudiant à une formation, avec suivi du paiement.

    Trois circuits de paiement coexistent :

    - Automatique PayDunya (payment_source='paydunya') : l'étudiant paie via
      PayDunya (Wave, Orange Money, Free Money, carte bancaire). Le paiement
      est confirmé côté serveur (voir /paiement/paydunya/notify dans app.py),
      sans intervention humaine : le statut passe directement à 'validee' dès
      confirmation par l'API PayDunya (voir PAYDUNYA.md).
    - Automatique Chariow (payment_source='chariow') : même principe que
      PayDunya, mais via l'API Chariow (voir /paiement/chariow/pulse dans
      app.py et CHARIOW.md). Nécessite qu'un produit Chariow correspondant à
      la formation ait été créé et son identifiant renseigné sur la
      formation (Course.chariow_product_id).
    - Manuel (payment_source='manuel', comportement historique) : l'étudiant
      indique comment et avec quelle référence il a payé (Wave, Orange Money,
      Free Money, virement...), puis un administrateur vérifie et valide (ou
      rejette) l'inscription depuis l'espace admin. Conservé en secours (ex:
      virement bancaire, que ni PayDunya ni Chariow ne couvrent).

    Dans les trois cas, l'accès au contenu de la formation n'est débloqué
    qu'une fois le statut passé à 'validee'."""
    __tablename__ = "enrollments"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)  # prix au moment de l'inscription
    payment_method = db.Column(db.String(30), nullable=False)  # wave | orange_money | free_money | virement | autre
    payment_reference = db.Column(db.String(120))  # référence / n° de transaction (manuel) ou token PayDunya (auto)
    payment_phone = db.Column(db.String(32))  # numéro utilisé pour le paiement
    status = db.Column(db.String(20), nullable=False, default="en_attente")  # en_attente | validee | rejetee
    note_admin = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    validated_at = db.Column(db.DateTime)
    validated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    # Champs liés au paiement automatique PayDunya (laissés à None pour les
    # inscriptions au circuit manuel historique).
    payment_source = db.Column(db.String(10), nullable=False, default="manuel")  # manuel | paydunya | chariow
    paydunya_token = db.Column(db.String(80), unique=True)  # token de facture renvoyé par PayDunya
    chariow_sale_id = db.Column(db.String(60), unique=True)  # id de vente ("sal_...") renvoyé par Chariow

    validated_by = db.relationship("User", foreign_keys=[validated_by_id])


class Submission(db.Model):
    """Un devoir rendu par un étudiant pour une leçon en mode 'devoir', à
    corriger manuellement par un administrateur (comme les inscriptions).
    Un étudiant peut soumettre plusieurs fois (ex: après un rejet)."""
    __tablename__ = "submissions"
    id = db.Column(db.Integer, primary_key=True)
    lesson_id = db.Column(db.Integer, db.ForeignKey("lessons.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    file_data = db.Column(db.LargeBinary, nullable=False)
    file_filename = db.Column(db.String(255), nullable=False)
    file_mimetype = db.Column(db.String(100))
    comment = db.Column(db.Text)  # commentaire optionnel laissé par l'étudiant
    status = db.Column(db.String(20), nullable=False, default="en_attente")  # en_attente | validee | rejetee
    note_admin = db.Column(db.String(500))  # retour du professeur
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    corrected_at = db.Column(db.DateTime)
    corrected_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    lesson = db.relationship("Lesson", backref=db.backref("submissions", lazy="dynamic", cascade="all, delete-orphan"))
    student = db.relationship("User", foreign_keys=[student_id], backref=db.backref("submissions", lazy="dynamic"))
    corrected_by = db.relationship("User", foreign_keys=[corrected_by_id])
