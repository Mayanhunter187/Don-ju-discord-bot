#!/bin/bash

# Docker Maintenance Script
# Removes unused data to prevent disk pressure

echo "Starting Docker Cleanup: $(date)"

# Prune unused images (dangling and unreferenced) older than 24h
# -a: Remove all unused images, not just dangling ones
# -f: Force (no prompt)
# --filter "until=24h": Keep recent stuff to avoid re-downloading constantly
echo "Pruning images..."
docker system prune -a -f --filter "until=24h"

# Prune builder cache (can get huge)
echo "Pruning builder cache..."
docker builder prune -a -f --filter "until=24h"

echo "Cleanup Complete. Current Disk Usage:"
df -h /var/lib/docker
