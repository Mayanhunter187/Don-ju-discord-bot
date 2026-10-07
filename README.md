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

1. A push to `main` that touches code runs ruff and the tests, builds an
   image, smoke-tests it, and pushes it to
   `ghcr.io/mayanhunter187/don-ju-discord-bot` with the tag `<date>-<short sha>`.
2. The workflow then commits that tag to `deploy/k8s/base/statefulset.yaml`.
3. Argo CD keeps `deploy/k8s/base` applied to the cluster and rolls out the
   new image.

Pull requests run the lint and tests only. Dependabot opens PRs every week for
Python packages (yt-dlp separately, since it has to keep up with YouTube) and
for the base images.

The Argo CD Application and both Secrets are created by the
[homelab](https://github.com/Mayanhunter187/homelab) Ansible roles. External
Secrets keeps the Secrets in sync with AWS SSM Parameter Store every hour, and
the bot reads the cookie file fresh for every yt-dlp call, so updated cookies
take effect without a restart:

| Secret | Key | Parameter |
|---|---|---|
| `discord-bot-secret` | `token` | `/homelab/don-ju/discord-token` |
| `youtube-cookies` | `cookies.txt` | `/homelab/don-ju/youtube-cookies` |

### High availability

Two replicas run on different nodes. Only the one holding the `don-ju-leader`
Lease connects to Discord; the other waits as a hot standby.

- On a rollout or node drain, the leader saves the queue, disconnects and
  releases the lease. The standby takes over within a couple of seconds and
  resumes the current song where it left off.
- If the leader's node dies, the standby takes over once the lease expires
  (30 seconds).

The queue is saved to the `don-ju-state` ConfigMap, so either replica can
resume it. Each replica keeps its own song cache on a `local-path` volume. The
least recently played songs are evicted once a cache passes `CACHE_MAX_BYTES`
(default 4 GiB).

The bot is in too few servers to need sharding; Discord requires it only from
2,500 guilds, and a guild is always served by a single shard either way.

### Metrics

Each replica serves Prometheus metrics on port 8000 at `/metrics` (leader
status, songs played, yt-dlp calls and timings, queue length, cache size,
command counts). The cluster's Prometheus scrapes them through the PodMonitor in
`deploy/k8s/base`, and the "Don'Ju" Grafana dashboard ships alongside it.

To see which replica is leading:

```bash
kubectl -n don-ju get lease don-ju-leader -o jsonpath='{.spec.holderIdentity}'
```

## Running locally

Requires Python 3.11+, ffmpeg, and Node.js 22 (yt-dlp uses it to solve
YouTube's JS challenges).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
echo "DISCORD_TOKEN=..." > .env
COOKIES_FILE_PATH=./cookies.txt python main.py
```

Outside Kubernetes the bot skips leader election and saves the queue to
`songs/state.json`. `LOG_LEVEL=DEBUG` turns on verbose logging.

To run the lint and tests:

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

`cookies.txt` is a Netscape-format cookie export from a logged-in YouTube
session.
