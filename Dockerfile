FROM python:3.11-slim

WORKDIR /app

# Dépendances système pour psycopg2 (client PostgreSQL)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-postgres.txt .
RUN pip install --no-cache-dir -r requirements.txt -r requirements-postgres.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

# Applique les migrations/données de démarrage puis lance le serveur de production.
CMD python init_db.py && gunicorn wsgi:app --bind 0.0.0.0:${PORT} --workers 3
