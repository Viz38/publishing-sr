# SR Publishing Engine V6.8

A high-performance, Python-native automation suite designed to stabilize and scale the Tracxn publishing pipeline. Supports multi-engine types (A, B, C) with shared core logic and centralized configuration.

## 🏗️ Repository Structure

```text
.
├── sr_common/              # 📦 Shared Core Package
│   ├── config.py           # Centralized Pydantic Settings
│   ├── clients.py          # Shared RateLimiter & GoogleSheetsClient
│   ├── utils.py            # Shared API wrappers & Scraping logic
│   └── models.py           # Strict Pydantic models
├── TypeA/                  # 🚀 Engine Type A (High-Fidelity)
├── TypeB/                  # 🚀 Engine Type B (Scale)
├── TypeC/                  # 🚀 Engine Type C (Operations)
├── control.sh              # 🛠️ Central Control Center (CLI)
├── control.logs            # 📑 Centralized Operation Logs (Auto-generated)
└── .env                    # 🔐 Local Environment Secrets (Ignored)
```

## 🛠️ Prerequisites

- **Python 3.10+** (3.12+ recommended)
- **Git**
- **Browsers**: **Firefox** (Tier 2) and **Chromium** (Tier 3) installed automatically via `control.sh`.
- **Cloudflared** (For tunnel connectivity)
- **Screen & Lsof** (Auto-installed on Ubuntu via `control.sh`)

## ⚡ Resource & Stability Hardening

- **Optimized Workers**: Default `MAX_WORKERS` is tuned to **3** for stable operation on standard hardware (Mac Air / 4-core servers).

## 🛡️ Advanced Stealth Architecture (v6.8+)
Version 6.8+ introduces a production-grade stealth layer in `sr_common/fetcher.py`:
- **Unified Fetcher**: A multi-tier fallback system (curl-cffi -> Scrapling -> Camoufox) shared across all engines.
- **TLS/HTTP2 Impersonation**: Uses `curl-cffi` to mimic Chrome 130 fingerprints at the transport layer.
- **Behavioral Mimicry**: Implements non-linear Bézier mouse paths and Gamma-distributed timing delays to simulate human interaction.
- **Environmental Coherence**: Ensures OS, hardware concurrency, and screen metrics are synchronized between the request headers and browser environment.

## 🚀 Setup & Installation

### 1. Clone the Repository
```bash
git clone https://github.com/Viz38/publishing-sr.git
cd publishing-sr
```

### 2. Configure Environment
Create a `.env` file in the root directory. You must provide valid API keys for:
- `GEMINI_API_KEY` (Verify key is active and not leaked)
- `SERVICE_AUTH_TOKEN` (For inter-service security)

### 3. Initialize Workspace
Run the control script and select **Option 1**:
```bash
./control.sh
```
This will:
- Create isolated virtual environments (`.venv`) for each engine.
- Install all dependencies from `requirements.txt`.
- Install **Firefox** and **Chromium** binaries.

## 🚦 Operational Guide

### Execution Modes
The engine supports three distinct execution modes selectable in the UI:
*   **Full Run**: Scrapes the domain, runs LLM predictions, and pushes to Tracxn APIs.
*   **Phase 1 Only**: Performs scraping and LLM predictions (SD, LD, Business Model) only. Saves results to the sheet for review.
*   **Phase 2 Only**: Skips scraping entirely. Reads existing Phase 1 data from the sheet and performs Tracxn API updates.

### Engine Ports
- **Type A**: `http://localhost:8767`
- **Type B**: `http://localhost:8765`
- **Type C**: `http://localhost:8766`

### Logging & Debugging
- **Operation Logs**: `control.logs` (Root directory) tracks all CLI actions.
- **API Logs**: `Type[A/B/C]/Logs/api.logs` tracks FastAPI requests.
- **Engine Logs**: `Type[A/B/C]/Logs/Type[A/B/C]Publishing.log` tracks scraping and API logic.

## 🔐 Security & Persistence
- **Auth**: All API requests require a `Bearer <SERVICE_AUTH_TOKEN>` header.
- **Auto-Persistence**: On Ubuntu, use **Option 3** to install as a systemd service. The script automatically manages user permissions and service lifecycle.
