"""Prometheus metrics, served on METRICS_PORT (default 8000) at /metrics."""
from prometheus_client import Counter, Gauge, Histogram

LEADER = Gauge('donju_leader', '1 if this replica holds the leader lease and is connected to Discord')
DISCORD_LATENCY = Gauge('donju_discord_latency_seconds', 'Discord gateway heartbeat latency')
VOICE_CONNECTIONS = Gauge('donju_voice_connections', 'Voice channels the bot is connected to')
QUEUE_LENGTH = Gauge('donju_queue_length', 'Songs waiting in all queues, not counting the ones playing')

SONGS_PLAYED = Counter('donju_songs_played_total', 'Songs started', ['source'])  # cache | download
COMMANDS = Counter('donju_commands_total', 'Slash commands completed', ['command'])
COMMAND_ERRORS = Counter('donju_command_errors_total', 'Slash commands that raised an error', ['command'])

YTDL_CALLS = Counter('donju_ytdl_calls_total', 'yt-dlp calls', ['kind', 'result'])
YTDL_SECONDS = Histogram('donju_ytdl_seconds', 'Time spent in yt-dlp calls', ['kind'],
                         buckets=(0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60))

CACHE_BYTES = Gauge('donju_cache_bytes', 'Size of the song cache on this replica')
CACHE_SONGS = Gauge('donju_cache_songs', 'Songs in the cache on this replica')
CACHE_EVICTIONS = Counter('donju_cache_evictions_total', 'Songs evicted from the cache')

STATE_SAVE_ERRORS = Counter('donju_state_save_errors_total', 'Failed writes of the saved queue')
LEASE_ERRORS = Counter('donju_lease_errors_total', 'Failed requests to the leader lease')
