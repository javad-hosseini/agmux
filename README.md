# agmux — Remote Antigravity Telegram Agent

`agmux` is a pragmatic, resilient, single-user Telegram interface for Google Antigravity (`agy`). It gives you full remote access to Antigravity sessions on your Linux server through Telegram without needing webhooks, inbound ports, Celery, Redis, or fragile persistent PTY processes.

---

## Key Features

- **Process-per-turn CLI Execution:** Uses `agy -p ... --output-format stream-json` in headless mode.
- **Two-Stage State Machine (`PLAN` → `EXECUTE`):** Sessions start in read-only investigation mode (`--mode plan`). Mutating code requires clicking **[✅ Approve & Execute Plan]** in Telegram.
- **Turn Queuing & `/btw` Support:** Automatically enqueues prompts if a task is already executing, with out-of-band `/btw <note>` context injection.
- **Artifact Discovery & Secret Scrubbing:** Diff-based artifact collector for generated walkthroughs, plans, and charts, with automated secret redaction.
- **Single-User Security Middleware:** Strict numeric Telegram user ID filtering drops unauthorized updates silently.
- **SQLite WAL Mode Persistence:** Lightweight state tracking across 3 tables (`user_context`, `sessions`, `audit_log`).
- **Interactive Inline Keyboards:** One-tap navigation for projects, sessions, approval gates, and desktop Antigravity brain sessions (`/brain`).

---

## Directory Structure

```
/home/javad/projects/agmux/
├── .env.example            # Environment variable template
├── projects.json           # Project allowlist mapping slugs to repo directories
├── requirements.txt        # Python dependencies
├── main.py                 # Bot entrypoint, command handlers, and polling loop
├── db.py                   # SQLite schema and async state queries
├── runner.py               # agy CLI subprocess runner and NDJSON stream parser
├── artifacts.py            # Pre/post snapshot diffing and secret scrubbing
├── queue_manager.py        # Per-session async lock and turn queue
├── service/
│   └── agmux.service       # Production systemd unit template
└── tests/                  # Automated pytest suite
```

---

## Quickstart

### 1. Set Up Python Virtual Environment

```bash
cd /home/javad/projects/agmux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env`:
```ini
BOT_TOKEN=123456789:ABCDEF_your_telegram_bot_token
ALLOWED_USER_ID=987654321
AGY_BIN=/home/javad/.local/bin/agy
DATA_DIR=./data
PROJECTS_FILE=./projects.json
```

- Get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather) on Telegram.
- Get your numeric `ALLOWED_USER_ID` from [@userinfobot](https://t.me/userinfobot).

### 3. Configure Projects

Edit `projects.json` to allowlist your server repositories:

```json
{
  "agmux": "/home/javad/projects/agmux",
  "dentino": "/home/javad/projects/dentino"
}
```

### 4. Run Tests

```bash
pytest -v tests/
```

### 5. Start in Development

```bash
python main.py
```

---

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome overview, active project, and active session status |
| `/projects` | List projects with tap-to-switch inline buttons |
| `/project <slug>` | Switch active project |
| `/new [title]` | Create a new session in read-only `PLAN` mode |
| `/sessions` | List recent sessions with tap-to-switch buttons |
| `/resume <id>` | Switch to a session by ID |
| `/mode [plan\|execute]` | Manually view or toggle session mode |
| `/brain` | Browse 5 most recent native Antigravity desktop sessions |
| `/btw <note>` | Append an out-of-band note to current context |
| `/cancel` | Send SIGTERM to the running task and clear queued items |
| `/status` | View detailed runtime state and queue metrics |
| `/help` | Display command reference |

---

## Systemd Production Daemon

To run `agmux` continuously as a background systemd service:

```bash
sudo cp /home/javad/projects/agmux/service/agmux.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agmux.service
sudo journalctl -u agmux.service -f
```

