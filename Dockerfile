# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies (FFmpeg and libsodium for voice)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsodium23 \
    libopus0 \
    git \ 
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Run main.py when the container launches
CMD cp /tmp/cookies-ro/cookies.txt /app/cookies.txt && python main.py
