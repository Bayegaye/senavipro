@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0requirements.txt" (
    echo ============================================================
    echo  ERREUR : fichiers du projet introuvables a cet endroit.
    echo.
    echo  Vous avez probablement lance start.bat depuis l'INTERIEUR
    echo  du fichier zip ^(apercu Windows^), qui n'extrait que ce seul
    echo  fichier dans un dossier temporaire.
    echo.
    echo  Solution :
    echo   1. Clic droit sur senavipro.zip -^> "Extraire tout..."
    echo   2. Ouvrez le dossier extrait
    echo   3. Relancez start.bat depuis ce dossier extrait
    echo ============================================================
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo ============================================================
    echo  ERREUR : Python n'est pas installe ou pas dans le PATH.
    echo  Installez-le depuis https://www.python.org/downloads/
    echo  et cochez "Add Python to PATH" pendant l'installation.
    echo ============================================================
    pause
    exit /b 1
)

if not exist venv (
    echo Creation de l'environnement Python...
    python -m venv venv
    if errorlevel 1 (
        echo ERREUR lors de la creation de l'environnement virtuel.
        pause
        exit /b 1
    )
)

call venv\Scripts\activate.bat

echo Installation des dependances ^(peut prendre une minute au premier lancement^)...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo ERREUR lors de l'installation des dependances. Verifiez votre connexion internet.
    pause
    exit /b 1
)

if not exist instance\senavipro.db (
    echo Initialisation de la base de donnees...
    python init_db.py
)

echo.
echo ==========================================================
echo  SENAVIPRO va demarrer sur http://127.0.0.1:5000
echo  Identifiant : admin   /   Mot de passe : senavipro2026
echo.
echo  Laissez cette fenetre ouverte tant que vous utilisez l'app.
echo  Pour arreter : fermez cette fenetre ou faites Ctrl+C.
echo  Si le navigateur affiche une erreur de connexion, attendez
echo  quelques secondes puis rechargez la page (F5) : le serveur
echo  est pret des que la ligne "Running on http://127.0.0.1:5000"
echo  apparait ci-dessous.
echo ==========================================================
echo.

start "" /min cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:5000"

python app.py

pause
