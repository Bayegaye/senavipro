"""Point d'entrée pour serveur de production (gunicorn, uWSGI...).

Utilisation typique :
    gunicorn wsgi:app
"""
from app import app

if __name__ == "__main__":
    app.run()
