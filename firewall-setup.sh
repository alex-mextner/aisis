#!/bin/bash

# Script to setup firewall rules for Asterisk container
echo "🔥 Setting up firewall rules for Asterisk..."

# Fetch current SIP.TG IP addresses dynamically
echo "📡 Fetching current SIP.TG IP addresses..."
SIP_TG_IPS=$(curl -s https://files.sip.tg/fw.txt | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | sed 's|/32||')

if [ -z "$SIP_TG_IPS" ]; then
    echo "❌ Failed to fetch SIP.TG IP addresses. Check internet connection."
    exit 1
fi

echo "✅ Found $(echo "$SIP_TG_IPS" | wc -l) SIP.TG IP addresses"

# Load admin IPs from environment or detect current IP
if [ ! -z "$ADMIN_IPS" ]; then
    echo "🔧 Using admin IPs from environment: $ADMIN_IPS"
    ADMIN_IP_LIST=$(echo "$ADMIN_IPS" | tr ',' '\n' | sed 's|/.*||' | xargs)
else
    # Fallback: detect current IP
    MY_IP=$(curl -s https://ipinfo.io/ip)
    echo "🔍 Auto-detected your IP: $MY_IP"
    ADMIN_IP_LIST="$MY_IP"
fi

# Create custom iptables chain for Asterisk
iptables -N ASTERISK_FILTER 2>/dev/null || iptables -F ASTERISK_FILTER

# Allow localhost
iptables -A ASTERISK_FILTER -s 127.0.0.1 -j ACCEPT

# Allow admin IPs
for ip in $ADMIN_IP_LIST; do
    if [ ! -z "$ip" ]; then
        iptables -A ASTERISK_FILTER -s $ip -j ACCEPT
        echo "✅ Added admin IP: $ip"
    fi
done

# Allow SIP.TG IPs
echo "$SIP_TG_IPS" | while read -r ip; do
    if [ ! -z "$ip" ]; then
        iptables -A ASTERISK_FILTER -s $ip -j ACCEPT
        echo "✅ Added SIP.TG IP: $ip"
    fi
done

# Drop everything else
iptables -A ASTERISK_FILTER -j DROP

# Apply rules to SIP ports (5060)
iptables -I INPUT -p udp --dport 5060 -j ASTERISK_FILTER
iptables -I INPUT -p tcp --dport 5060 -j ASTERISK_FILTER

# Allow ARI port only from localhost (already secured in http.conf)
iptables -I INPUT -p tcp --dport 8088 -s 127.0.0.1 -j ACCEPT
iptables -I INPUT -p tcp --dport 8088 -j DROP

echo "🎉 Firewall rules applied successfully!"
echo "ℹ️  SIP ports (5060) are now restricted to SIP.TG IPs and admin IPs"
echo "ℹ️  ARI port (8088) is restricted to localhost only"
echo "💡 Admin IPs: $ADMIN_IP_LIST"