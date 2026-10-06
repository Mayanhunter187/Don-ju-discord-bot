# Don'Ju

A Discord music bot that plays audio from YouTube in voice channels. Songs are
downloaded once and cached on disk, and the queue survives restarts.

## Commands

| Command | What it does |
|---|---|
| `/play <url or search>` | Play a URL, or pick from the top five search results. `random` plays a cached song. |
| `/playing` | Show the current song and its progress |
| `/skip` | Skip to the next song |
| `/stop` | Stop playback and clear the queue |
| `/queue` | Show the queue |
| `/cache` | Show cache size and the largest cached songs |
| `/sync` | Clear guild-specific commands (admin only) |
| `/help` | List commands |

Songs longer than 10 minutes are refused.

## How it runs

The bot runs on the homelab k3s cluster, in the `don-ju` namespace.

1. A push to `main` that touches code builds an image with GitHub Actions and
   pushes it to `ghcr.io/mayanhunter187/don-ju-discord-bot` with the tag
   `<date>-<short sha>`.
2. Update the image tag in `deploy/k8s/base/deployment.yaml` and push.
3. Argo CD keeps `deploy/k8s/base` applied to the cluster.

The Argo CD Application and both Secrets are created by the
[homelab](https://github.com/Mayanhunter187/homelab) Ansible roles. The Secret
values come from AWS SSM Parameter Store:

| Secret | Key | Parameter |
|---|---|---|
| `discord-bot-secret` | `token` | `/homelab/don-ju/discord-token` |
| `youtube-cookies` | `cookies.txt` | `/homelab/don-ju/youtube-cookies` |

The song cache and the saved queue (`songs/state.json`) live on the
`don-ju-songs` PersistentVolumeClaim. The least recently played songs are
evicted once the cache passes `CACHE_MAX_BYTES` (default 4 GiB).

## Running locally

Requires Python 3.11+, ffmpeg, and Node.js 22 (yt-dlp uses it to solve
YouTube's JS challenges).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
echo "DISCORD_TOKEN=..." > .env
COOKIES_FILE_PATH=./cookies.txt python main.py
```

`cookies.txt` is a Netscape-format cookie export from a logged-in YouTube
session.
