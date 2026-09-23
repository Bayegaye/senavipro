# Créer votre propre publicité Facebook, de A à Z, avec DaVinci Resolve

Comme vous n'avez pas encore de photos/vidéos précises sous la main, ce guide couvre les deux cas : utiliser vos propres images quand vous en aurez, et démarrer dès maintenant avec des ressources gratuites en attendant. L'objectif : une pub qui vous ressemble vraiment, plutôt que des images génériques trouvées sur internet.

---

## Étape 0 — Préparer un petit script (5 minutes, sur papier)

Une pub efficace de 20 à 30 secondes suit en général cette structure. Notez une phrase pour chaque partie avant même d'ouvrir DaVinci Resolve :

1. **Accroche (3 s)** — une question ou un constat qui parle à votre cible. Ex : *"Vous voulez travailler dans la géomatique mais ne savez pas par où commencer ?"*
2. **Problème (5 s)** — le manque que vous comblez. Ex : *"Les formations en SIG sont souvent trop théoriques, loin du terrain."*
3. **Solution (8 s)** — votre formation, ce qui la rend différente. Ex : *"Chez AgroEcoConsult, vous apprenez avec des cas concrets, encadrés par des ingénieurs de terrain."*
4. **Preuve/crédibilité (5 s)** — un chiffre, un témoignage, une image de vos étudiants ou de vos réalisations.
5. **Appel à l'action (4 s)** — ce qu'on a fait ensemble : *"Visitez agroecoconsulting.com pour créer un compte et suivre la formation en ligne pendant un mois."*

Gardez ces 5 phrases sous les yeux : elles deviennent le squelette de votre timeline dans Resolve.

---

## Étape 1 — Rassembler les images

**Si vous avez déjà des photos/vidéos** (bureau, équipe, étudiants, terrain) : sortez-les sur une clé ou un dossier, quitte à filmer quelques plans rapides au téléphone (tenez-le à l'horizontale, pas trop de mouvement, lumière naturelle si possible — 5 à 10 plans de 5-10 secondes suffisent largement).

**Si vous n'avez rien pour l'instant**, deux banques d'images/vidéos gratuites, sans attribution obligatoire, adaptées à un usage commercial :
- **pexels.com** — cherchez des mots-clés comme "agriculture", "gps survey", "training", "farming technology".
- **pixabay.com** — mêmes mots-clés, beaucoup de choix en vidéo et en photo.

Téléchargez 6 à 10 clips/photos qui correspondent à votre script, dans un seul dossier sur votre ordinateur.

**Pour la musique de fond**, gratuite et libre de droits :
- **pixabay.com/music**
- **YouTube Audio Library** (accessible depuis YouTube Studio, même sans être youtubeur)

Cherchez quelque chose d'énergique mais discret (ne doit jamais couvrir la voix).

---

## Étape 2 — Créer le projet dans DaVinci Resolve

1. **New Project**, donnez-lui un nom clair (ex: "Pub_Facebook_v2").
2. Page **Media** : importez tout votre dossier (images, vidéos, musique, logo) dans le **Media Pool**.
3. Choisissez d'abord le format : pour une pub Facebook qui doit bien fonctionner sur mobile, préférez un format **carré (1:1)** ou **vertical (4:5 ou 9:16)** plutôt que l'horizontal classique. Réglez ça via **File → Project Settings → Master Settings → Timeline Resolution**, par exemple `1080x1350` (format 4:5).
4. Faites un clic droit dans le Media Pool → **New Timeline**, donnez la résolution choisie ci-dessus.

---

## Étape 3 — Poser les images sur la timeline selon votre script

1. Passez sur la page **Edit**.
2. Faites glisser vos clips/photos sur la piste V1 dans l'ordre de votre script (accroche → problème → solution → preuve → CTA).
3. Ajustez la durée de chaque clip en étirant ses bords pour respecter à peu près le minutage prévu à l'étape 0.
4. Si une photo/vidéo est au mauvais format (horizontale alors que votre timeline est verticale), cliquez dessus, dans l'**Inspector** (Video) augmentez le **Zoom** pour qu'elle remplisse le cadre, quitte à recadrer sur la partie intéressante.
5. Ajoutez des transitions douces entre les clips : dans le panneau **Effects → Transitions**, faites glisser un **Cross Dissolve** sur chaque jonction entre deux clips (évitez les transitions trop "gadget", une pub sérieuse reste sobre).

---

## Étape 4 — Ajouter votre texte et vos couleurs de marque

1. Panneau **Effects → Titles**, choisissez un modèle **Text** simple.
2. Pour chaque partie du script, ajoutez le texte correspondant sur une piste au-dessus (V2), aligné avec le bon clip.
3. Dans l'**Inspector** de chaque titre, utilisez vos couleurs de marque : orange **#E2621F** pour les mots clés, blanc pour le reste, sur fond légèrement assombri si besoin pour la lisibilité (onglet **Settings → Background**).
4. Sur le tout dernier plan (l'appel à l'action), affichez clairement en gros : `agroecoconsulting.com`.

---

## Étape 5 — Enregistrer votre voix off

1. Page **Fairlight**. Ajoutez une piste audio (clic droit → **Add Track → Mono**).
2. Placez-vous dans un endroit calme, micro du téléphone ou d'un casque suffit largement pour commencer.
3. Enregistrez chaque phrase du script l'une après l'autre (plus facile à synchroniser que tout d'un coup) : bouton d'enregistrement rouge sur la piste, lisez, arrêtez, recommencez si besoin.
4. Faites glisser chaque phrase enregistrée pour qu'elle corresponde bien au bon passage de la vidéo.

---

## Étape 6 — Ajouter la musique de fond

1. Faites glisser votre fichier de musique sur une nouvelle piste audio, en dessous de la voix.
2. Étirez-la ou coupez-la (touche B) pour qu'elle dure exactement la longueur de la vidéo.
3. Baissez son volume : cliquez sur le clip, dans l'**Inspector → Audio**, réglez le **Volume** nettement plus bas que la voix (environ -18 à -22 dB), pour qu'elle reste en fond sans gêner la compréhension.
4. Ajoutez un fondu de sortie à la toute fin (clic droit sur le coin du clip dans la timeline).

---

## Étape 7 — Ajouter votre logo

1. Sur une piste au-dessus de tout le reste, ajoutez votre logo en filigrane discret dans un coin, présent du début à la fin (voir la méthode détaillée dans le premier guide que je vous ai envoyé : réduire le Zoom, ajuster la Position et l'Opacity dans l'Inspector).
2. Sur le tout premier plan, vous pouvez aussi faire apparaître le logo en grand pendant 1 à 2 secondes avant que la pub ne démarre vraiment (comme un générique d'ouverture).

---

## Étape 8 — Relire et ajuster

1. Repassez toute la vidéo une ou deux fois, casque sur les oreilles : est-ce que le rythme est bon ? Le texte a-t-il le temps d'être lu ? La voix est-elle claire ?
2. Ajustez les durées des clips si un passage semble trop rapide ou trop long.

---

## Étape 9 — Exporter pour Facebook

1. Page **Deliver**.
2. Format **MP4**, codec **H.264**.
3. Résolution : celle de votre timeline (1080x1350 si vous avez suivi l'étape 2).
4. Bitrate autour de 8 000 à 10 000 kb/s pour une bonne qualité sur ce format court.
5. **Add to Render Queue**, puis **Start Render**.
6. Regardez le résultat final avant de le mettre en ligne.

---

### Pour aller plus loin
Une fois cette première version en ligne, gardez un œil sur les statistiques Facebook (vues complètes vs. abandons) : c'est le meilleur moyen de savoir si l'accroche des 3 premières secondes fonctionne, et d'ajuster la prochaine version en conséquence.
