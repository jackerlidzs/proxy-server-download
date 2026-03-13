#!/bin/bash
# ============================================
# Server Speed Optimization Script
# Run on VPS: sudo bash optimize.sh
# ============================================

echo "🚀 Optimizing server for maximum download speed..."

# === 1. TCP BBR (if not already enabled) ===
echo ">>> Enabling TCP BBR..."
modprobe tcp_bbr 2>/dev/null
if ! grep -q "tcp_bbr" /etc/modules-load.d/modules.conf 2>/dev/null; then
    echo "tcp_bbr" >> /etc/modules-load.d/modules.conf
fi

# === 2. Kernel TCP/Network Tuning ===
echo ">>> Applying TCP tuning..."
cat > /etc/sysctl.d/99-speed-optimize.conf << 'EOF'
# TCP BBR congestion control
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Increase TCP buffer sizes (important for long-distance transfers)
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.core.rmem_default = 1048576
net.core.wmem_default = 1048576
net.ipv4.tcp_rmem = 4096 1048576 33554432
net.ipv4.tcp_wmem = 4096 1048576 33554432

# Network connection tuning
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.tcp_max_tw_buckets = 2000000
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 10

# Enable TCP window scaling
net.ipv4.tcp_window_scaling = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_sack = 1

# Fast Open
net.ipv4.tcp_fastopen = 3

# Keep alive
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 5

# File descriptors
fs.file-max = 2097152
fs.nr_open = 2097152

# Increase ARP cache
net.ipv4.neigh.default.gc_thresh1 = 1024
net.ipv4.neigh.default.gc_thresh2 = 4096
net.ipv4.neigh.default.gc_thresh3 = 8192
EOF

/usr/sbin/sysctl -p /etc/sysctl.d/99-speed-optimize.conf

# === 3. System Limits ===
echo ">>> Setting system limits..."
cat > /etc/security/limits.d/99-speed.conf << 'EOF'
*       soft    nofile  65535
*       hard    nofile  65535
root    soft    nofile  65535
root    hard    nofile  65535
EOF

# === 4. Swap (if not exists) ===
if [ ! -f /swapfile ]; then
    echo ">>> Creating 2GB swap..."
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    if ! grep -q "/swapfile" /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    echo "   Swap created ✓"
else
    echo "   Swap already exists ✓"
fi

# === 5. Verify ===
echo ""
echo "============================================"
echo "✅ Optimization complete!"
echo "============================================"
echo "TCP Congestion: $(cat /proc/sys/net/ipv4/tcp_congestion_control)"
echo "TCP Fastopen:   $(cat /proc/sys/net/ipv4/tcp_fastopen)"
echo "Max files:      $(cat /proc/sys/fs/file-max)"
echo "Swap:           $(free -h | grep Swap | awk '{print $2}')"
echo "TCP rmem max:   $(cat /proc/sys/net/core/rmem_max)"
echo "TCP wmem max:   $(cat /proc/sys/net/core/wmem_max)"
echo ""
echo "🔄 Now restart Docker containers:"
echo "   cd proxy-server-download && docker compose up -d --build"
