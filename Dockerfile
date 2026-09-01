# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Get static ffmpeg and node 22 binaries
COPY --from=mwader/static-ffmpeg:6.0 /ffmpeg /usr/local/bin/
COPY --from=mwader/static-ffmpeg:6.0 /ffprobe /usr/local/bin/
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/

# Set the working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsodium23 \
    libopus0 \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install python packages
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy the app
COPY . /app

# Run main.py
CMD ["sh", "-c", "cp /tmp/cookies-ro/cookies.txt /app/cookies.txt && python main.py"]
