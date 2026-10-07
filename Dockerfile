FROM python:3.11-slim

# Static ffmpeg and the Node.js runtime yt-dlp uses for YouTube's JS challenges
COPY --from=mwader/static-ffmpeg:6.0 /ffmpeg /usr/local/bin/
COPY --from=mwader/static-ffmpeg:6.0 /ffprobe /usr/local/bin/
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsodium23 \
    libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin bot

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Writable scratch space; the root filesystem can be mounted read-only
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    # Where the cluster mounts the youtube-cookies Secret
    COOKIES_FILE_PATH=/tmp/cookies-ro/cookies.txt

USER 10001
EXPOSE 8000
CMD ["python", "main.py"]
