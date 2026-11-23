# WaveBox Studio 4.0 — Windows

Application audio locale en Python, avec interface sombre. Code source lisible : `app.py`.

## Ce qui change depuis la version 3

- **Refonte graphique complète** : navigation latérale, bannière illustrée, cartes de synthèse, interface plus lisible, journal technique dans une fenêtre séparée.
- **Fonction de réparation supprimée intégralement**. Il reste deux espaces : « Télécharger des pistes » et « Organiser le vinyle ».
- **Affichage adaptatif** : les longues listes de pistes défilent dans leur panneau ; les paramètres et la destination restent accessibles.
- **Numérotation automatique** maintenue : `01 - Artiste - Titre.mp3`, `02 - Artiste - Titre.mp3`, etc., selon l'ordre exact des URL collées.
- **Réorganisation sans recompression** : le mode « Organiser le vinyle » recopie les MP3 et vérifie l'identité de leur contenu audio sans modifier les originaux.
- **Icône Windows personnalisée**, conservée dans `assets/wavebox.ico`.

## Installation

1. Installer Python 3.11 ou plus depuis <https://www.python.org/downloads/windows/> si nécessaire.
2. Extraire entièrement l'archive dans un dossier stable (ne pas lancer le programme à l'intérieur du ZIP).
3. Double-cliquer sur **`LANCER_WINDOWS.bat`**.
4. Au premier lancement, un environnement isolé `.venv` est créé, puis les dépendances officielles sont téléchargées via `pip` depuis PyPI. Les versions et empreintes SHA-256 sont verrouillées dans `requirements.txt`.
5. Optionnel : double-cliquer sur `CREER_RACCOURCI_BUREAU.bat` pour créer le raccourci « WaveBox Studio » avec l'icône du vinyle. Ne pas déplacer le dossier après la création du raccourci.

La nouvelle version n'efface ni ne remplace les MP3 créés par l'ancienne.

## Retrouver mes fichiers

L'emplacement par défaut est :

`C:\Users\<utilisateur>\Music\MP3 YouTube`

Dans l'application, la destination est affichée ; cliquer sur **Choisir** pour la changer, ou sur **↗** juste à côté pour ouvrir le dossier. Le réglage est mémorisé dans `%APPDATA%\WaveBoxMP3\preferences.json`.

## Télécharger des pistes

- Coller un à cinquante liens YouTube directs, **un par ligne**. Les vidéos doivent être accessibles et leur téléchargement autorisé.
- Le numéro est celui de l'ordre des lignes, même si l'une échoue.
- Optionnel : coller un titre personnalisé pour chaque URL dans la zone « Noms des pistes ». Le bouton « Charger mes 12 titres » préremplit les titres de l'exemple fourni ; il ne remplit pas les liens.
- Choisir un dossier et lancer l'opération. Le résultat est **uniquement un fichier audio MP3**, et non une vidéo.

## Organiser le vinyle

- Choisir les fichiers MP3 présents sur le PC.
- Utiliser **Monter**, **Descendre** et **Retirer** pour les ordonner.
- Cliquer sur **Créer la sélection ordonnée** : l'application crée des **copies numérotées**, après vérification complète du décodage audio ; les originaux restent inchangés.
- Si la liste est longue, **faire défiler le panneau** avec la barre verticale.
- Pour préparer deux faces, créer deux dossiers « Face A » et « Face B » et lancer deux lots distincts. Chaque lot recommence à `01`.

## Qualité et compatibilité

La conversion utilise FFmpeg / LAME en MP3 stéréo à 44,1 kHz, ID3v2.3, débit constant **320 kb/s** par défaut ou 192 kb/s en option. L'application décode intégralement chaque sortie pour en détecter les problèmes de format. Elle ne peut pas améliorer un son source de faible qualité ; pour un pressage ou une gravure de vinyle, une source WAV/FLAC légalement obtenue est préférable.

## Sécurité

- `app.py` est fourni en clair, sans code obscurci.
- Aucun mot de passe ni connexion utilisateur n'est demandé par WaveBox.
- Le lanceur ne réclame aucun accès administrateur et utilise un environnement Python isolé.
- `yt-dlp` et `imageio-ffmpeg` sont deux projets open source tiers. Leurs archives PyPI sont vérifiées par hash lors de l'installation ; aucune mise à jour automatique non vérifiée.
- L'outil n'envoie aucune télémétrie, mais le téléchargement accède nécessairement à YouTube et PyPI est contacté lors de l'installation.
- Un logiciel ne peut pas être garanti « sans virus » à 100 %. Analyser le ZIP avec Microsoft Defender, puis consulter les fichiers `.bat` et `.py` avant de le lancer.

**Limites des tests effectués :** interface sous affichage virtuel Linux, contrôles de la numérotation, de l'organisation, réencodage et décodage d'un signal audio synthétique, copie identique. **Aucun téléchargement réel depuis YouTube ni test d'exécution final sous Windows** n'a été effectué dans cet environnement.

**Sources officielles :** <https://github.com/yt-dlp/yt-dlp> et <https://github.com/imageio/imageio-ffmpeg>.
