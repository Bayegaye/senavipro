#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "Création de l'environnement Python..."
    python3 -m venv venv
fi

source venv/bin/activate

echo "Installation des dépendances..."
pip install -q -r requirements.txt

if [ ! -f instance/senavipro.db ]; then
    echo "Initialisation de la base de données..."
    python3 init_db.py
fi

echo ""
echo "=========================================================="
echo " SENAVIPRO va démarrer sur http://127.0.0.1:5000"
echo " Identifiant : admin   /   Mot de passe : senavipro2026"
echo " Laissez ce terminal ouvert tant que vous utilisez l'app."
echo "=========================================================="
echo ""

( sleep 3 && (open http://127.0.0.1:5000 2>/dev/null || xdg-open http://127.0.0.1:5000 2>/dev/null) ) &
python3 app.py
