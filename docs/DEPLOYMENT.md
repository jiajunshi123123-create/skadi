# Deployment Guide

This guide covers three deployment scenarios for **AI Data Analysis Assistant**:

1. [Docker Deployment](#1-docker-deployment-recommended) (recommended)
2. [Bare-Metal Deployment](#2-bare-metal-deployment) (for development or custom environments)
3. [Cloud Server Deployment](#3-cloud-server-deployment) (production-grade with systemd)

---

## 1. Docker Deployment (Recommended)

### Prerequisites

- Docker 20.10+
- Docker Compose v2+
- 2GB+ free RAM
- DeepSeek API key (or any OpenAI-compatible endpoint)
- Database credentials (read-only user recommended)

### Step 1 — Clone the Repository

```bash
git clone https://github.com/jiajunshi123123-create/skadi.git
cd skadi
```

### Step 2 — Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your real values:

```bash
# Required
DEEPSEEK_API_KEY=sk-your-key-here

DB_TYPE=mysql                # mysql / postgresql / starrocks
DB_HOST=your-db-host
DB_PORT=3306
DB_USER=readonly_user
DB_PASSWORD=your_password
DB_NAME=your_database

PG_HOST=postgres             # Use the docker-compose service name
PG_DATABASE=agent_experience
PG_USER=agent_user
PG_PASSWORD=change_me

DINGTALK_BOT_APP_KEY=your_key
DINGTALK_BOT_APP_SECRET=your_secret
```

### Step 3 — Launch Services

```bash
docker-compose up -d
```

This starts:
- `bot` — the agent + DingTalk listener
- `postgres` — experience store (sessions, audit logs, learned patterns)
- `chromadb` — vector knowledge base

### Step 4 — Verify

Check container status:

```bash
docker-compose ps
```

Expected: all services in `Up` state.

Tail logs:

```bash
docker-compose logs -f bot
```

You should see:

```
[bot] DingTalk Stream connected
[bot] Orchestrator ready
[bot] Listening for messages...
```

### Step 5 — Smoke Test

Send a message in your DingTalk group (with the bot @-mentioned):

```
@bot 昨天的活跃用户数是多少？
```

You should receive a 3-part reply (data + analysis + recommendation) within 10–15 seconds.

### Updating

```bash
git pull
docker-compose build
docker-compose up -d
```

### Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `bot` container restarting | Wrong API key or DB credentials | Check `docker-compose logs bot` |
| `Connection refused` on PG | PG not ready yet | Wait 10s; check `docker-compose logs postgres` |
| Bot online but no replies | DingTalk credentials wrong | Re-check `DINGTALK_BOT_APP_KEY/SECRET` |

---

## 2. Bare-Metal Deployment

### Prerequisites

- Python 3.10+
- PostgreSQL 12+ (running locally or remote)
- 1GB+ free RAM

### Step 1 — Clone & Set Up Python

```bash
git clone https://github.com/jiajunshi123123-create/skadi.git
cd skadi

python -m venv venv
source venv/bin/activate          # Linux/Mac
# venv\Scripts\activate           # Windows

pip install -r requirements.txt
```

### Step 2 — Configure PostgreSQL

Create the experience-store database and user:

```sql
CREATE DATABASE agent_experience;
CREATE USER agent_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE agent_experience TO agent_user;
```

Initialize tables:

```bash
psql -U agent_user -d agent_experience -f scripts/init_db.sql
```

### Step 3 — Configure ChromaDB

ChromaDB runs embedded by default — it persists to the `chroma_db/` directory.
No separate server needed.

To pre-populate the knowledge base with your data dictionary:

```bash
python knowledge/init_knowledge_base.py --reset
```

### Step 4 — Configure Environment

```bash
cp .env.example .env
# Edit .env (use localhost for PG_HOST)
```

### Step 5 — Run

```bash
python dingtalk_bot.py
```

Logs are written to `./logs/dingtalk_bot.log` by default (configurable via `LOG_DIR`).

### Running in the Background

Use `nohup`, `screen`, or `tmux` for short-term background use:

```bash
nohup python dingtalk_bot.py > logs/bot.log 2>&1 &
```

For production, use **systemd** (see Section 3 below).

---

## 3. Cloud Server Deployment

### Recommended Server Specs

| Tier | vCPU | RAM | Disk | Monthly Cost | Suitable For |
|------|------|-----|------|--------------|--------------|
| **Minimum** | 2 | 4 GB | 40 GB SSD | ~$8 | <100 queries/day |
| **Recommended** | 4 | 8 GB | 80 GB SSD | ~$25 | <1000 queries/day |
| **High-volume** | 8 | 16 GB | 200 GB SSD | ~$80 | Multi-tenant / SaaS |

Cloud providers tested: **AWS EC2**, **Alibaba Cloud ECS**, **Tencent Cloud CVM**, **DigitalOcean Droplet**.

### Step 1 — Prepare the Server

```bash
# Ubuntu 22.04 / 24.04
sudo apt update
sudo apt install -y python3.10 python3.10-venv postgresql postgresql-contrib git

# Optional: 4GB swap (helps for 2C4G servers)
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Step 2 — Deploy the Application

Follow [Bare-Metal Deployment](#2-bare-metal-deployment) Steps 1–4 above.
Place the project under `/opt/ai-data-assistant/` (recommended).

### Step 3 — Configure systemd Service

Create `/etc/systemd/system/ai-data-assistant.service`:

```ini
[Unit]
Description=AI Data Analysis Assistant - DingTalk Bot
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=app
WorkingDirectory=/opt/ai-data-assistant
EnvironmentFile=/opt/ai-data-assistant/.env
ExecStart=/opt/ai-data-assistant/venv/bin/python dingtalk_bot.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/ai-data-assistant/bot.log
StandardError=append:/var/log/ai-data-assistant/bot.err.log

# Resource limits (tune as needed)
LimitNOFILE=65536
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

Create the log directory and a non-root user:

```bash
sudo useradd -r -s /bin/false app
sudo mkdir -p /var/log/ai-data-assistant
sudo chown -R app:app /opt/ai-data-assistant /var/log/ai-data-assistant
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable ai-data-assistant
sudo systemctl start ai-data-assistant
sudo systemctl status ai-data-assistant
```

### Step 4 — Log Management

#### View live logs

```bash
sudo journalctl -u ai-data-assistant -f
# or
tail -f /var/log/ai-data-assistant/bot.log
```

#### Rotate logs with logrotate

Create `/etc/logrotate.d/ai-data-assistant`:

```
/var/log/ai-data-assistant/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

### Step 5 — Monitoring Recommendations

#### Health check (cron + script)

```bash
# /opt/ai-data-assistant/scripts/healthcheck.sh
#!/bin/bash
if ! systemctl is-active --quiet ai-data-assistant; then
  echo "Service down!" | mail -s "[ALERT] AI Bot Down" admin@example.com
  systemctl restart ai-data-assistant
fi
```

Add to crontab:

```
*/5 * * * * /opt/ai-data-assistant/scripts/healthcheck.sh
```

#### Recommended metrics to track

| Metric | Threshold | Why |
|--------|-----------|-----|
| Process memory | < 80% of `MemoryMax` | Catch leaks |
| PG connection count | < 50 | Catch connection leaks |
| LLM API error rate | < 1% | Catch API key / quota issues |
| Avg response time | < 15s | Catch DB slowness |
| Daily token spend | budget | Cost control |

Tooling options: **Prometheus + Grafana**, **Datadog**, **Uptime Kuma** (lightweight), or simple log-based alerts.

#### Database backups

```bash
# Daily backup of experience store
pg_dump -U agent_user agent_experience | gzip > /backup/agent_$(date +%F).sql.gz
```

Add to crontab and retain 7–30 days.

---

## Common Operations

### Restart after config change

```bash
sudo systemctl restart ai-data-assistant
```

### Update to latest version

```bash
cd /opt/ai-data-assistant
sudo -u app git pull
sudo -u app venv/bin/pip install -r requirements.txt
sudo systemctl restart ai-data-assistant
```

### Reset the knowledge base

```bash
sudo -u app venv/bin/python knowledge/init_knowledge_base.py --reset
sudo systemctl restart ai-data-assistant
```

### Inspect recent queries (audit log)

```sql
-- Connect to experience store
psql -U agent_user -d agent_experience

SELECT created_at, user_id, intent, sql_query, status
FROM audit_logs
ORDER BY created_at DESC
LIMIT 20;
```

---

## Security Checklist

Before going to production:

- [ ] Use a **read-only** database user for `DB_USER`
- [ ] Whitelist only the bot server IP at the database firewall
- [ ] Store `.env` with `chmod 600` (do not commit to git)
- [ ] Rotate `DEEPSEEK_API_KEY` if leaked
- [ ] Enable PG `pg_hba.conf` with `scram-sha-256` auth
- [ ] Set up automated `agent_experience` backups
- [ ] Configure log rotation
- [ ] Set up health check + alerting
- [ ] Review `permission_manager.py` allow/deny lists
- [ ] Test SQL injection defense with malicious inputs

---

## Need Help?

- Open an issue on [GitHub](https://github.com/jiajunshi123123-create/skadi/issues)
- See [ARCHITECTURE.md](../ARCHITECTURE.md) for internals
- See [CONTRIBUTING.md](../CONTRIBUTING.md) to contribute fixes
