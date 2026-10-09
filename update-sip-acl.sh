#!/bin/bash

# Script to update Asterisk ACL with current SIP.TG IP addresses
echo "🔄 Updating Asterisk ACL configuration..."

# Load environment variables
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
    echo "✅ Loaded environment variables from .env"
else
    echo "⚠️  No .env file found, using defaults"
fi

# Fetch current SIP.TG IP addresses
echo "📡 Fetching current SIP.TG IP addresses..."
SIP_TG_IPS=$(curl -s https://files.sip.tg/fw.txt | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+')

if [ -z "$SIP_TG_IPS" ]; then
    echo "❌ Failed to fetch SIP.TG IP addresses. Check internet connection."
    exit 1
fi

echo "✅ Found $(echo "$SIP_TG_IPS" | wc -l) SIP.TG IP addresses"

# Update acl.conf file
ACL_FILE="asterisk.d/acl.conf"

# Create new ACL configuration
cat > "$ACL_FILE" << 'EOF'
;
; Named Access Control Lists (ACLs)
;
; This configuration file defines named access control lists
; that can be referenced from other configuration files.
;

[general]

; SIP.TG allowed IP addresses (auto-generated from https://files.sip.tg/fw.txt)
[sip_tg_hosts]
type=acl
EOF

# Add current SIP.TG IPs
echo "$SIP_TG_IPS" | while read -r ip; do
    if [ ! -z "$ip" ]; then
        echo "permit=$ip" >> "$ACL_FILE"
        echo "✅ Added to ACL: $ip"
    fi
done

# Add the rest of the configuration
cat >> "$ACL_FILE" << 'EOF'

; Allow localhost for ARI
[localhost]
type=acl
permit=127.0.0.1/32
permit=::1/128

; Admin IP addresses (from ADMIN_IPS environment variable)
[admin_access]
type=acl
EOF

# Add admin IPs from environment variable
ADMIN_IPS=${ADMIN_IPS:-"0.0.0.0/0"}
echo "🔧 Adding admin IPs: $ADMIN_IPS"

# Split by comma and add each IP
echo "$ADMIN_IPS" | tr ',' '\n' | while read -r ip; do
    ip=$(echo "$ip" | xargs)  # trim whitespace
    if [ ! -z "$ip" ]; then
        echo "permit=$ip" >> "$ACL_FILE"
        echo "✅ Added admin IP: $ip"
    fi
done

echo "🎉 ACL configuration updated successfully!"
echo "📁 File updated: $ACL_FILE"
echo "💡 To update admin IPs, edit ADMIN_IPS in .env file"