# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Get static ffmpeg (lightweight, no X11 dependencies)
COPY --from=mwader/static-ffmpeg:6.0 /ffmpeg /usr/local/bin/
COPY --from=mwader/static-ffmpeg:6.0 /ffprobe /usr/local/bin/

# Set the working directory in the container
WORKDIR /app

# Install system dependencies (libsodium for voice, git for yt-dlp, nodejs for signature solving)
# We use --no-install-recommends to avoid pulling in X11/GUI libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsodium23 \
    libopus0 \
    git \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first to leverage cache
COPY requirements.txt /app/requirements.txt

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy the rest of the application
COPY . /app

# Run main.py when the container launches
CMD ["sh", "-c", "cp /tmp/cookies-ro/cookies.txt /app/cookies.txt && python main.py"]
