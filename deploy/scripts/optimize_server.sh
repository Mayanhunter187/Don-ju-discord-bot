#!/bin/bash
set -e

echo "Optimizing Server for Low Memory (2GB)..."

# 1. Add 2GB Swap
# K3s + ArgoCD on 2GB RAM is tight. Swap prevents OOM kills.
if [ ! -f /swapfile ]; then
    echo "Adding 2GB Swap file..."
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' | tee -a /etc/fstab
    echo "Swap enabled."
else
    echo "Swap already exists."
fi

# 2. Adjust Swappiness
# 10 means "try to avoid swapping unless necessary", which is good for performance
sysctl vm.swappiness=10
echo 'vm.swappiness=10' | tee -a /etc/sysctl.conf

echo "--------------------------------------------------"
echo "Optimization Complete!"
echo "Run 'free -h' to verify you now have 2GB of Swap."
echo "Your pods should stop crashing now."
echo "--------------------------------------------------"
