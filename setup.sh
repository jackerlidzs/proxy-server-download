#!/bin/bash
set -e

echo "========================================="
echo "  Download Proxy Server - Quick Setup"
echo "========================================="
echo ""

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found. Please install Docker first."
    exit 1
fi

echo "✅ Docker found"

# Check Docker Compose
if docker compose version &> /dev/null; then
    COMPOSE="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE="docker-compose"
else
    echo "❌ Docker Compose not found. Please install Docker Compose."
    exit 1
fi

echo "✅ Docker Compose found ($COMPOSE)"

# Create .env if not exists
if [ ! -f .env ]; then
    # Generate random API key
    API_KEY=$(openssl rand -hex 16 2>/dev/null || head -c 32 /dev/urandom | xxd -p | head -c 32)
    
    # Get server IP
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s icanhazip.com 2>/dev/null || echo "localhost")
    
    echo ""
    echo "📝 Creating .env configuration..."
    echo ""
    
    # Ask for port
    read -p "Port to use [8080]: " PORT
    PORT=${PORT:-8080}
    
    cat > .env << EOF
PORT=${PORT}
API_KEY=${API_KEY}
SERVER_URL=http://${SERVER_IP}:${PORT}
MAX_CONNECTIONS=16
CLEANUP_HOURS=48
MAX_CONCURRENT_DOWNLOADS=3
EOF
    
    echo ""
    echo "✅ .env created"
else
    echo "✅ .env already exists"
    source .env
    API_KEY=${API_KEY:-changeme}
    PORT=${PORT:-8080}
    SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo "localhost")
fi

# Build and start
echo ""
echo "🔨 Building containers..."
$COMPOSE build --quiet

echo "🚀 Starting services..."
$COMPOSE up -d

echo ""
echo "========================================="
echo "  ✅ Server is running!"
echo "========================================="
echo ""
echo "  📡 API URL:      http://${SERVER_IP}:${PORT}"
echo "  📖 API Docs:     http://${SERVER_IP}:${PORT}/docs"
echo "  🔑 API Key:      ${API_KEY}"
echo "  📁 Files URL:    http://${SERVER_IP}:${PORT}/files/"
echo ""
echo "========================================="
echo "  Quick Usage Examples"
echo "========================================="
echo ""
echo "  1️⃣  Submit download:"
echo "  curl -X POST http://${SERVER_IP}:${PORT}/api/download \\"
echo "    -H 'Authorization: Bearer ${API_KEY}' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"url\": \"https://example.com/file.zip\", \"headers\": {\"referer\": \"https://example.com\"}}'"
echo ""
echo "  2️⃣  Check status:"
echo "  curl http://${SERVER_IP}:${PORT}/api/status/{task_id} \\"
echo "    -H 'Authorization: Bearer ${API_KEY}'"
echo ""
echo "  3️⃣  List files:"
echo "  curl http://${SERVER_IP}:${PORT}/api/files \\"
echo "    -H 'Authorization: Bearer ${API_KEY}'"
echo ""
echo "  4️⃣  Download file:"
echo "  wget http://${SERVER_IP}:${PORT}/files/filename.zip"
echo ""
