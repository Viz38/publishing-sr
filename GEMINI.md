# SR Publishing Engine: Technical Documentation v5.0

## Overview
The SR Publishing Engine is a high-performance, Python-native automation suite designed to stabilize and scale the Tracxn publishing pipeline. Version 5.0 introduces a production-ready architecture with robust persistence and cross-platform support for both **macOS** and **Ubuntu**.

## Core Architecture

### 1. Unified Control Center (`control.sh`)
The system is managed by a centralized, production-grade shell script that handles:
- **Persistence**: 
    - **macOS**: Installs and manages `LaunchAgents` (`~/Library/LaunchAgents`) for background persistence.
    - **Ubuntu**: Installs and manages `systemd` services (`/etc/systemd/system`) for server-level persistence.
- **Environment Management**: Automatically detects the best available Python runtime (3.10+) and manages isolated virtual environments (`.venv`).
- **Identity Detection**: Tailors tunnel tokens and configurations based on the executing user/device.
- **Unified Logging**: Standardizes all service output to `api.logs` for centralized monitoring.

### 2. Robust API Orchestration (`api.py`)
Each engine type (A, B, C) exposes a standardized FastAPI interface:
- **State Tracking**: Uses an explicit `status` flag (`idle`, `running`, `succeeded`, `failed`) to ensure UI consistency.
- **Background Execution**: Monitors sub-processes in real-time and synchronizes row-level progress from `.progress.json`.
- **Automatic Recovery**: The state management ensures that even if a process crashes, the status is correctly reported as `failed` rather than staying in a "Running" loop.

### 3. Pipeline Engines
- **Type A (High-Fidelity)**: Multi-page scraping, dual-level BM prediction, and Special Flag detection.
- **Type B (Scale)**: High-concurrency scraping with stealth mechanisms.
- **Type C (Operations)**: Dynamic sheet processing, data cleanup, and Feed/Funnel automation.

## Operational Workflow

### Service Management
```bash
./control.sh
```
- **Option 1**: Initialize workspace (venv, dependencies, browsers).
- **Option 2**: Start Engines in **Persistent Mode** (installs system services).
- **Option 3**: Deep Clean (stops all processes and uninstalls services).
- **Option 4**: Live Log stream across all engines.

### Log Locations
- **API Status Logs**: `Type[A/B/C]/Logs/api.logs` (Standardized)
- **Engine Logic Logs**: `Type[A/B/C]/Logs/Type[A/B/C]Publishing.log` (Logic-specific)

## Cross-Platform Compatibility
- **macOS**: Uses `launchctl` for per-user background tasks. Includes `/opt/homebrew/bin` in PATH for Silicon compatibility.
- **Ubuntu**: Uses `systemd` with `Restart=always` for server-grade uptime.

## Troubleshooting
If the UI shows a "Running" state incorrectly, use **Option 3** in `control.sh` to stop all services and then **Option 2** to restart them cleanly. This resets the internal status flags and re-syncs the system.
