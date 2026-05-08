# SR Publishing Engine v6.6

A high-performance, Python-native automation suite designed to stabilize and scale the Tracxn publishing pipeline. This engine supports multi-engine types (A, B, C) with shared core logic and centralized configuration.

## 🏗️ Repository Structure

```text
.
├── sr_common/              # 📦 Shared Core Package
│   ├── config.py           # Centralized Pydantic Settings
│   ├── clients.py          # Shared RateLimiter & GoogleSheetsClient
│   ├── utils.py            # Shared API wrappers & Scraping logic
│   └── models.py           # Strict Pydantic models
├── TypeA/                  # 🚀 Engine Type A (High-Fidelity)
│   ├── main.py             # Pipeline Logic
│   ├── api.py              # FastAPI Controller
│   └── requirements.txt    # Dependencies
├── TypeB/                  # 🚀 Engine Type B (Scale)
├── TypeC/                  # 🚀 Engine Type C (Operations)
├── control.sh              # 🛠️ Central Control Center (CLI)
└── .env                    # 🔐 Local Environment Secrets (Ignored)
```

## 🛠️ Prerequisites

- **Python 3.10+** (3.12 or 3.13 recommended)
- **Git**
- **Chromium** (Installed automatically via `patchright`)
- **Cloudflared** (For tunnel connectivity)

## 🚀 Setup & Installation

### 1. Clone the Repository
```bash
git clone https://github.com/Viz38/publishing-sr.git
cd publishing-sr
```

### 2. Configure Environment
Create a `.env` file in the root directory with the following variables:
```ini
# Gemini API
TYPEA_GEMINI_API_KEY=your_key
TYPEB_GEMINI_API_KEY=your_key
TYPEC_GEMINI_API_KEY=your_key

# Tracxn API
TYPEA_TRACXN_ACCESS_TOKEN=your_token
TYPEB_TRACXN_ACCESS_TOKEN=your_token
TYPEC_TRACXN_ACCESS_TOKEN=your_token

# Sheet IDs
TYPEA_SHEET_ID=...
TYPEB_SHEET_ID=...
TYPEC_SHEET_ID=...

# Security
SERVICE_AUTH_TOKEN=YOUR_SECRET_TOKEN

# Tunnel Tokens (Dynamic selection based on system user)
TUNNEL_TOKEN_VISHNU=...
TUNNEL_TOKEN_4990=...
TUNNEL_TOKEN_4230=...
TUNNEL_TOKEN_DEFAULT=...
```

### 3. Initialize Workspace
Run the control script and select **Option 1**:
```bash
./control.sh
```
This will:
- Create isolated virtual environments (`.venv`) for each engine.
- Install all dependencies.
- Install necessary browser binaries.

## 🚦 Operational Guide

### Starting Engines
Use **Option 2** in `./control.sh` to start all engines in the background. The engines will be reachable via:
- Type A: `http://localhost:8767`
- Type B: `http://localhost:8768`
- Type C: `http://localhost:8769`

### Fleet Management (Updates)
To keep the engine updated across multiple devices, use **Option 6 (Check for Updates)**. This will automatically pull the latest code from GitHub and restart the services.

## 🔐 Security & Persistence
- **Auth**: All API requests require a `Bearer <SERVICE_AUTH_TOKEN>` header.
- **PID Tracking**: The system uses surgical PID tracking to allow isolated engine cancellation without affecting sibling processes.
- **Auto-Restart**: On Linux/Ubuntu, use **Option 3** to install engines as systemd services for server-grade uptime.

## 📝 Author
