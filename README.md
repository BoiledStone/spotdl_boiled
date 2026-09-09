# Spotify Downloader Bot Resolver

Résolveur local qui récupère les pistes d’une playlist Spotify, recherche une source audio YouTube fiable, puis enregistre les fichiers dans un dossier local. Les fichiers déjà présents sont conservés.

## Démarrage rapide

Prérequis :

- Python 3.10 ou plus récent
- FFmpeg accessible depuis le `PATH`
- Firefox connecté à YouTube pour fournir des cookies navigateur à yt-dlp
- Deno ou Node recommandé pour l’extraction YouTube récente de yt-dlp; Deno est détecté automatiquement

`requirements.txt` installe aussi `yt-dlp-ejs`, utilisé avec le runtime JavaScript pour les signatures YouTube récentes.

Depuis PowerShell, dans le dossier du projet :

```powershell
python -m pip install -r .\requirements.txt
Copy-Item .env.example .env
.\download_playlist.ps1 -Playlist "https://open.spotify.com/playlist/0rFIvkUL9MgfkyVU50zC42?si=f372b182b612473c"
```

Le dossier `downloads` est créé automatiquement. Pour une première vérification sans playlist configurée, utilise :

```powershell
python .\download_missing_autonomous_v2.py --help
```

Pour exécuter les tests locaux, sans appel réseau ni modification des téléchargements :

```powershell
python -m unittest -v .\test_resolver.py
```

Pour utiliser l’interface Windows, double-clique sur `start_spotdl_gui.cmd` (recommandé) ou `spotdl_gui.pyw`. Elle permet de choisir la playlist, le dossier de sortie et le nombre de workers, puis affiche le journal du resolver en direct. Python doit être installé avec Tkinter, ce qui est inclus dans l’installation Windows standard de Python. Le lanceur `.cmd` utilise `pythonw.exe` trouvé dans le `PATH` et évite le launcher `py` s’il est mal configuré.

Si PowerShell bloque les scripts dans la session courante :

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Configuration

Le fichier `.env` est optionnel. Il permet d’éviter de répéter les paramètres entre deux exécutions :

```dotenv
SPOTDL_PLAYLIST_URL=https://open.spotify.com/playlist/...
SPOTDL_OUTPUT_DIR=.\downloads
SPOTDL_WORKERS=4
SPOTDL_MAX_CANDIDATE_ATTEMPTS=6
# Facultatif: Firefox est détecté automatiquement si un profil local existe.
# Définis une valeur vide pour désactiver cette détection.
# BOT_YTDLP_COOKIES_BROWSER=firefox
# BOT_YTDLP_JS_RUNTIME=deno:C:\path\to\deno.exe
```

Variables disponibles :

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `SPOTDL_PLAYLIST_URL` | URL, URI ou identifiant de playlist Spotify | aucun |
| `SPOTDL_OUTPUT_DIR` | dossier de sortie | `downloads` |
| `SPOTDL_WORKERS` | pistes traitées en parallèle, de 1 à 5 | `4` |
| `SPOTDL_MAX_CANDIDATE_ATTEMPTS` | candidats YouTube essayés après un téléchargement invalide, de 1 à 8 | `6` |
| `SPOTDL_MIN_FALLBACK_SCORE` | score minimal du fallback YouTube | `130` |
| `BOT_YTDLP_COOKIES_BROWSER` | navigateur utilisé pour les cookies yt-dlp | `firefox` si un profil local est détecté, sinon désactivé |
| `BOT_YTDLP_JS_RUNTIME` | runtime JavaScript yt-dlp forcé, au format `deno:chemin` ou `node:chemin` | Deno puis Node détecté automatiquement |
| `BOT_YTDLP_REMOTE_EJS` | autorise le composant EJS distant yt-dlp si le paquet local manque | désactivé si `yt-dlp-ejs` est installé |
| `BOT_ENV_FILE` | chemin vers un autre fichier `.env` | `.env` |
| `SPOTDL_PYTHON` | exécutable Python utilisé par PowerShell | `python` |
| `SPOTIFY_API_MAX_RETRY_AFTER_SECONDS` | attente maximale après un `429` Spotify | `30` s |
| `SPOTIFY_PATHFINDER_PAGE_SIZE` | taille des pages Spotify internes | `200` |
| `SPOTIFY_PATHFINDER_PAGE_DELAY_SECONDS` | pause entre les pages Spotify | `0.2` s |
| `SPOTIFY_SP_DC` | cookie Spotify `sp_dc`, si nécessaire | aucun |
| `SPOTIFY_TOTP_SECRETS_URL` | source des secrets TOTP Spotify | source intégrée |
| `SPOTIFY_TOTP_TIMEOUT_SECONDS` | timeout des appels TOTP | `10` s |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | identifiants API Spotify optionnels | aucun |

Ne partage jamais `.env` : il peut contenir des cookies ou des identifiants.
Les valeurs numériques non valides sont remplacées par leurs valeurs par défaut; les valeurs hors limites sont normalisées.

## Utilisation

### Lanceur PowerShell recommandé

Le lanceur vérifie Python, affiche les paramètres actifs et relaie le code retour du resolver. Les options omises sont lues depuis `.env` par le resolver; une option fournie sur la ligne de commande la remplace.

```powershell
.\download_playlist.ps1 `
  -Playlist "https://open.spotify.com/playlist/..." `
  -Output ".\downloads" `
  -Workers 4
```

`-Workers` accepte une valeur de `1` à `5`. Avec `-Workers 1`, le traitement est réellement séquentiel: aucun processus enfant n’est créé et le détail de la recherche reste visible en direct. Une valeur explicite hors de cette plage est refusée avec le code `2`.

### Exécution directe

```powershell
python .\download_missing_autonomous_v2.py `
  --playlist "https://open.spotify.com/playlist/..." `
  --output ".\downloads" `
  --workers 4
```

Le script accepte aussi les liens copiés sous forme Markdown, par exemple `[ma playlist](https://open.spotify.com/playlist/...)`.

### Reprendre une exécution

Relance simplement la même commande. Les fichiers audio existants et les doublons internes de playlist sont ignorés. Les échecs sont listés dans `_FAILED_BOT_RESOLVER.txt`.

## Fonctionnement

- Spotify est lu par plusieurs chemins de secours, avec détection des réponses partielles; un cache de secours n’est utilisé que s’il couvre le nombre de pistes annoncé.
- Les candidats YouTube sont classés selon le titre, l’artiste, la durée et les signaux de qualité.
- Comme le bot Discord, plusieurs candidats classés sont essayés jusqu’à ce qu’un téléchargement valide soit écrit; le nombre maximal est réglable avec `SPOTDL_MAX_CANDIDATE_ATTEMPTS`.
- Les titres Unicode, dont le japonais, le coréen et le cyrillique, sont conservés pour l’indexation et le matching.
- Les téléchargements sont traités en parallèle, sans remplacer un fichier audio déjà présent.
- Un profil Firefox local est utilisé automatiquement pour les cookies YouTube; définis `BOT_YTDLP_COOKIES_BROWSER=` pour désactiver ce comportement.
- Si YouTube exige une authentification, le resolver arrête la série de recherches et les tâches encore en attente plutôt que de poursuivre des échecs répétitifs.
- Le rapport `_FAILED_BOT_RESOLVER.txt` conserve la piste et la cause technique de chaque échec.
- Le cache Spotify est conservé dans `.spotify_cache`.
- Les caches sont écrits atomiquement pour éviter un JSON incomplet après une interruption.
- Les recherches et téléchargements yt-dlp ont des retries et un timeout réseau.
- L’extraction utilise le runtime JavaScript détecté et retente le profil YouTube de secours utilisé par le bot.

Extensions audio reconnues : `.opus`, `.mp3`, `.m4a`, `.flac`, `.wav`, `.ogg`, `.aac` et `.webm`.

## Codes de sortie

- `0` : traitement terminé, aucun échec
- `1` : erreur de récupération Spotify
- `2` : configuration invalide ou au moins une piste non résolue/téléchargée
- `3` : YouTube a exigé une session authentifiée; le traitement a été arrêté volontairement

## Dépannage

`Python introuvable` : installe Python 3.10+ ou définis `SPOTDL_PYTHON` vers l’exécutable voulu.

`FFmpeg` absent : installe FFmpeg et vérifie que `ffmpeg.exe` est accessible avec `Get-Command ffmpeg`.

`Aucun candidat suffisamment fiable` : vérifie le titre, l’artiste et l’accès réseau; consulte ensuite `_FAILED_BOT_RESOLVER.txt`.

`Téléchargement invalide` : le resolver essaie automatiquement les candidats suivants et les profils yt-dlp disponibles. Si plusieurs pistes échouent de suite, vérifie d’abord l’accès YouTube et les cookies avant d’augmenter `SPOTDL_MAX_CANDIDATE_ATTEMPTS`.

`Playlist partielle` : relance plus tard. Le resolver refuse volontairement de traiter une liste incomplète.

`Workers invalide` : utilise une valeur entre `1` et `5`, avec `-Workers` ou `--workers`.

`Sign in to confirm you’re not a bot` : arrête une ancienne exécution avec `Ctrl+C`, ouvre YouTube dans Firefox sur la même connexion, termine toute vérification demandée, puis relance le resolver. Le profil Firefox est détecté automatiquement; pour le forcer, ajoute `BOT_YTDLP_COOKIES_BROWSER=firefox` dans `.env`. Une erreur d’authentification arrête volontairement les essais, car changer de candidat ne contourne pas ce blocage. Pour diagnostiquer clairement une piste, commence avec `-Workers 1`.

## Fichiers du projet

- `download_missing_autonomous_v2.py` : resolver principal
- `download_playlist.ps1` : lanceur PowerShell recommandé
- `spotdl_gui.pyw` : interface Windows lançable par double-clic
- `spotdl_gui.py` : logique de l’interface et lancement du resolver
- `start_spotdl_gui.cmd` : lanceur Windows recommandé
- `.env.example` : modèle de configuration
- `requirements.txt` : dépendances Python
- `test_resolver.py` : tests rapides du parsing, du matching et des noms de fichiers

Les téléchargements, caches et fichiers d’échec sont ignorés par Git. Respecte les conditions d’utilisation des services appelés et les droits applicables aux contenus téléchargés.
