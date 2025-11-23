#!/bin/bash

# Exit on error
set -e

echo "Starting K3s & ArgoCD Setup..."

# 1. Configure Firewall (Fedora/CentOS)
if command -v firewall-cmd &> /dev/null; then
    echo "Configuring Firewalld for K3s..."
    firewall-cmd --permanent --add-port=6443/tcp # API Server
    firewall-cmd --permanent --add-port=10250/tcp # Kubelet Metrics
    firewall-cmd --permanent --add-port=8472/udp # Flannel VXLAN
    firewall-cmd --permanent --add-port=30000-32767/tcp # NodePorts (ArgoCD)
    firewall-cmd --reload
    echo "Firewall configured."
fi

# 2. Install K3s
echo "Installing K3s..."
curl -sfL https://get.k3s.io | sh -

# Wait for K3s to be ready
echo "Waiting for K3s to start..."
sleep 15

# 2. Install ArgoCD
echo "Installing ArgoCD..."
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# 3. Patch ArgoCD Server to be accessible (NodePort)
# This exposes ArgoCD on a high port (e.g., 30080) so you can access it via IP
echo "Exposing ArgoCD Server..."
kubectl patch svc argocd-server -n argocd -p '{"spec": {"type": "NodePort"}}'

# 4. Get Initial Password
echo "Waiting for ArgoCD pods to be ready..."
kubectl wait --for=condition=Ready pods --all -n argocd --timeout=300s

PASSWORD=$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d)
PORT=$(kubectl -n argocd get svc argocd-server -o jsonpath='{.spec.ports[?(@.name=="https")].nodePort}')
IP=$(curl -s ifconfig.me)

echo "--------------------------------------------------"
echo "Setup Complete!"
echo "K3s and ArgoCD are installed."
echo ""
echo "Access ArgoCD UI at: https://$IP:$PORT"
echo "Username: admin"
echo "Password: $PASSWORD"
echo "--------------------------------------------------"
echo "IMPORTANT: Save this password!"
