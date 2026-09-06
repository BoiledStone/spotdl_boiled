# SpotDL Bot Resolver

Télécharge les pistes manquantes d'une playlist Spotify en reprenant la logique de recherche du bot Discord.

## Arborescence

- `download_missing_autonomous_v2.py` : résolveur principal
- `download_playlist.ps1` : lanceur PowerShell
- `requirements.txt` : dépendances Python
- `.env.example` : modèle de configuration

## Installation

Prérequis :

- Python 3.10 ou plus récent
- FFmpeg disponible dans le `PATH`
- Firefox si tu veux utiliser les cookies du navigateur avec yt-dlp

```powershell
python -m pip install -r requirements.txt
```

## Configuration

Copie `.env.example` en `.env` et renseigne au minimum l'URL de la playlist.

Variables utiles :

- `SPOTDL_PLAYLIST_URL` : playlist Spotify à traiter
- `SPOTDL_OUTPUT_DIR` : dossier de sortie
- `BOT_YTDLP_COOKIES_BROWSER` : source des cookies yt-dlp, `firefox` par défaut
- `BOT_ENV_FILE` : chemin vers un autre fichier `.env`
- `SPOTDL_PYTHON` : exécutable Python utilisé par le lanceur PowerShell
- `SPOTIFY_API_MAX_RETRY_AFTER_SECONDS` : attente maximale acceptée quand Spotify répond `429`
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` : optionnels

## Lancement

PowerShell :

```powershell
Set-Location .\spotdl_github
.\download_playlist.ps1 -Playlist "https://open.spotify.com/playlist/..."
```

Si tu copies un lien depuis un chat et qu'il arrive sous la forme `[texte](https://...)`, le script extrait automatiquement l'URL Spotify.

Ou directement :

```powershell
python .\download_missing_autonomous_v2.py --playlist "https://open.spotify.com/playlist/..." --output .\downloads
```

## Comportement

- Les fichiers déjà présents sont ignorés.
- Les doublons internes à la playlist sont ignorés.
- Le cache Spotify est stocké dans `.spotify_cache`.
- Les échecs sont écrits dans `_FAILED_BOT_RESOLVER.txt`.
- Le script ne remplace pas un fichier audio existant.
- Les longues limites Spotify sont plafonnées à 30 secondes par défaut.
- Si Spotify ne renvoie qu'un aperçu partiel d'une grande playlist, le script s'arrête au lieu de télécharger seulement les 100 premiers titres.

## Notes GitHub

Le dépôt ne doit pas contenir les fichiers audio téléchargés ni le cache. Le dossier `downloads/` est créé à l'exécution et reste ignoré par Git.

Respecte les conditions d'utilisation des services appelés et les droits des contenus que tu télécharges.
