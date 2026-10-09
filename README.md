# Asterisk ARI + Speech Recognition

Docker-based Asterisk setup with ARI integration and Whisper speech recognition.

## Features

- 📞 Asterisk PBX with SIP.TG integration
- 🎙️ Call recording functionality
- 🤖 Speech-to-text using OpenAI Whisper (via Hugging Face)
- 🚀 Dockerized for easy deployment
- 📝 TypeScript/Bun for modern development

## Quick Start

### 1. Build and run with Docker Compose

```bash
# Start Asterisk service
docker-compose up -d asterisk

# Or run with app service
docker-compose --profile app-only up -d
```

### 2. Run scripts manually

```bash
# Inside container
docker exec -it asterisk-ari bun scripts/call-and-record.ts

# Or from host (if you have bun installed)
bun scripts/call-and-record.ts
```

## Directory Structure

```
├── asterisk.d/          # Asterisk configuration files
├── scripts/             # Node.js/TypeScript scripts
├── recordings/          # Recorded audio files (mounted volume)
├── logs/                # Asterisk logs (mounted volume)
├── Dockerfile           # Container build instructions
└── docker-compose.yml   # Service orchestration
```

## Configuration

### Environment Variables

- `HF_TOKEN` - Hugging Face API token for Whisper

### SIP Configuration

Edit `asterisk.d/sip.conf` to update your SIP credentials:

```ini
[my-sip-trunk]
username=YOUR_USERNAME
secret=YOUR_PASSWORD
```

## Ports

- `5060` - SIP (UDP/TCP)
- `8088` - ARI HTTP interface
- `10000-20000` - RTP media (UDP)

## Development

```bash
# Install dependencies
bun install

# Format code
bun run format

# Deploy to server (Asterisk + App)
bun run deploy

# Deploy only Asterisk (faster)
bun run deploy:asterisk

# Watch and auto-deploy
bun run deploy:watch

# Update SIP.TG IP whitelist
bun run update-acl
```

## Deployment

The project uses Docker for containerization and rsync for deployment:

1. **Local development**: `docker-compose up -d`
2. **Deploy to server**: `bun run deploy`
3. **Auto-deployment**: `bun run deploy:watch`

Files are synced to the server and Docker containers are automatically rebuilt and restarted.

## Security Configuration

The Asterisk setup is secured with multiple layers:

### 1. Network Access Control (ACL)

- **SIP.TG whitelist**: Dynamically fetched from <https://files.sip.tg/fw.txt>
- **Admin access**: Configured for your IP address only
- **ARI interface**: Bound to localhost only

**Update IP whitelist:**

```bash
# Update SIP.TG IP addresses and redeploy
bun run update-acl
bun run deploy
```

### 2. Firewall Rules

Run the firewall setup script on your server (fetches IPs dynamically):

```bash
# Copy and run firewall setup with current IPs
scp firewall-setup.sh root@104.248.84.190:/tmp/
ssh root@104.248.84.190 'bash /tmp/firewall-setup.sh'
```

### 3. Configure Admin IP Addresses

Add your IP addresses to `.env` file (supports multiple IPs):

```bash
# Single IP
ADMIN_IPS=192.168.1.100/32

# Multiple IPs (comma-separated)
ADMIN_IPS=192.168.1.100/32,10.0.0.50/32,203.0.113.0/24

# IP ranges (CIDR notation supported)
ADMIN_IPS=192.168.1.0/24,10.0.0.0/16
```

Then update ACL and deploy:

```bash
bun run update-acl
bun run deploy
```

### 4. Optional: Fail2ban Protection

```bash
# Install fail2ban configuration
scp fail2ban-asterisk.conf root@104.248.84.190:/etc/fail2ban/jail.d/asterisk.conf
ssh root@104.248.84.190 'systemctl restart fail2ban'
```

This configuration ensures that:

- ✅ Only SIP.TG servers can connect for SIP traffic
- ✅ Only your IP can access Asterisk management
- ✅ ARI interface is not exposed externally
- ✅ Automatic blocking of suspicious activity
