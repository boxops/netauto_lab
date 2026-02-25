#!/bin/bash
#
# Docker Installation Script
# Installs the latest Docker Engine and Docker Compose on Ubuntu/Debian
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_status() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Docker Installation Script${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Check if running on Ubuntu/Debian
if [ ! -f /etc/os-release ]; then
    print_error "Cannot detect OS. This script is for Ubuntu/Debian."
    exit 1
fi

source /etc/os-release
if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
    print_error "This script is designed for Ubuntu/Debian. Detected: $ID"
    exit 1
fi

print_status "Detected OS: $PRETTY_NAME"

# Check if Docker is already installed
if command -v docker &> /dev/null; then
    print_warning "Docker is already installed: $(docker --version)"
    read -p "Do you want to reinstall? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_status "Keeping existing Docker installation"
        exit 0
    fi
fi

echo ""
echo "Installing Docker Engine..."
echo ""

# Update package index
print_status "Updating package index..."
sudo apt-get update -qq

# Install prerequisites
print_status "Installing prerequisites..."
sudo apt-get install -y -qq \
    ca-certificates \
    curl \
    gnupg \
    lsb-release

# Add Docker's official GPG key
print_status "Adding Docker GPG key..."
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/$ID/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Set up the repository
print_status "Setting up Docker repository..."
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$ID \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Update package index again
print_status "Updating package index with Docker repository..."
sudo apt-get update -qq

# Install Docker Engine
print_status "Installing Docker Engine..."
sudo apt-get install -y -qq \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

# Add current user to docker group
print_status "Adding $USER to docker group..."
sudo usermod -aG docker $USER

# Start and enable Docker
print_status "Starting Docker service..."
sudo systemctl start docker
sudo systemctl enable docker

# Verify installation
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Installation Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

docker --version
docker compose version

echo ""
print_status "Docker has been successfully installed!"
echo ""
print_warning "IMPORTANT: You need to log out and log back in for group changes to take effect."
print_warning "Alternatively, run: newgrp docker"
echo ""
print_status "To test Docker, run: docker run hello-world"
echo ""