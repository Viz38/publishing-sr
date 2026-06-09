# SR Publishing Engine: Technical Documentation v6.7

## 1. System Overview
The SR Publishing Engine (v6.7) is a high-performance, Python-native automation suite designed to stabilize and scale the Tracxn publishing pipeline. It features a stealthy web scraping tier, Gemini-backed LLM data extraction, real-time Google Sheets syncing, and Tracxn API integrations. It supports cross-platform execution (macOS LaunchAgents & Ubuntu systemd) with robust environment management via `uv`.

## 2. Core Architecture

### 2.1 Control Center (`control.sh`)
- Manages the entire lifecycle of the 3 Engines (Type A, B, and C).
- **Environment Management:** Uses `uv` for ultra-fast dependency resolution and virtual environments. Auto-installs browsers (Patchright, Camoufox).
- **Identity & Secrets:** Matches local machine users to specific identities (`vishnu`, `tracxn-ds-499`, etc.) and automatically parses/creates base64 credentials from `.env`.
- **Persistence:**
  - **macOS:** Installs `LaunchAgents` via `launchctl` for per-user background processing.
  - **Ubuntu:** Installs `systemd` services for server-grade uptime.
- **Tunneling:** Manages `cloudflared` to expose internal FastAPI endpoints globally.

### 2.2 Shared Libraries (`sr_common/`)
- **`config.py`**: Centralized configuration via `pydantic_settings`. Loads API keys, Google Sheet IDs, and operational limits.
- **`stealth.py`**: Generates human-like interaction patterns. Uses quadratic Bézier curves for mouse movements, variable scroll delays, and hardware-coherent browser profiles to bypass bot detection.
- **`fetcher.py`**: Orchestrates a resilient, multi-tier scraping strategy:
  1. *Tier 0:* `curl_cffi` for TLS/HTTP2 fingerprint impersonation (fastest).
  2. *Tier 2:* `Camoufox` (headless browser) with human movement simulation when Tier 0 fails.
  3. *Tier 3:* `Scrapling Stealth` for aggressive anti-bot evasion.
- **`utils.py`**: Contains core API logic. Features `call_gemini_api` (with native context caching) and `call_tracxn_api` with exponential backoff. Includes `SystemHealthMonitor` to dynamically scale workers based on CPU/RAM limits, and sophisticated heuristics for parked domain classification.
- **`clients.py`**: Implements a `MultiTierRateLimiter` utilizing `collections.deque` for high-throughput, in-memory sliding windows (replacing legacy SQLite methods for a ~30x speed boost).

## 3. Pipeline Engine Workflows (Types A, B, C)

Each engine operates via a two-phase architecture to isolate scraping/LLM processing from Tracxn API mutation.

### 3.1 Type A (High-Fidelity / Multi-Page)
**Target:** High-priority domains requiring deep business model mapping and Special Flags.
- **Phase 1: Extraction & Scraping**
  - **Scraping**: Fetches the homepage and traverses multiple sub-pages (e.g., /about, /product) based on mapped feed paths. Combines HTML into a unified text document.
  - **LLM Call 1 (`prompts[0]`)**: Extracts Short Description (SD) and Long Description Part 1 (LD1).
  - **LLM Call 2 (`prompts[1]`)**: Extracts Long Description Part 2 (LD2) using LD1 context. Combined with LD1 to form the final Long Description.
  - **LLM Call 3 (`prompts[2]`)**: 1st Level Business Model (BM) prediction.
  - **LLM Call 4 (`prompts[7]`)**: 2nd Level BM Prediction (if 1st Level BM maps to deeper taxonomies).
  - **LLM Call 5 (`prompts[8]`)**: Executes only on full success (valid BM). Predicts Special Flags, parses them as a JSON array, and merges them with Pre-Filled Special Flags (PFSF) from Column G.
  - **Output**: Writes SD, LD, BM data, and the merged Special Flags JSON array back to the Google Sheet.
- **Phase 2: Tracxn API Push**
  - **Domain Profile (`update_dp`)**: Pushes SD, LD, and parsed Special Flags (`specialFlags: {"value": sfarray}`). Appends `bu_llm_sd_ld` to the Hashtags. If successful, strictly adds `bu_Internal_SRprocess_TypeA`.
  - **Business Model (`update_bm`)**: Maps the domain to the identified Theme ID and Business Model ID.
  - **Funnel Assignment (`update_funnel`)**: Moves domain out of the user's queue and into specific feed funnels.

### 3.2 Type B (Scalable / High-Concurrency)
**Target:** Bulk processing pipelines utilizing a single-page approach.
- **Phase 1: Extraction & Scraping**
  - **Scraping**: Fetches the homepage only.
  - **LLM Call 1 (`prompts[0]`)**: Extracts Short Description and Long Description.
  - **LLM Call 2 (`prompts[3]`)**: Determines "FeedCheck" (Yes/No) and predicts a single Business Model mapping against the entire feed taxonomy.
  - **Output**: Writes SD, LD, FeedCheck, and BM data to Google Sheets.
- **Phase 2: Tracxn API Push**
  - **Domain Profile (`update_dp`)**: Pushes SD and LD. Appends `bu_llm_sd_ld` and `llmbasedpublishing`. If FeedCheck is 'Yes', adds `bu_llm_businessmodel_prediction`.
  - **Business Model (`update_bm`)**: If fully successful, updates the Theme Company Association.
  - **Funnel Assignment (`update_funnel`)**: Handles funnel lifecycle movements based on whether the BM prediction succeeded or failed.

### 3.3 Type C (Operations & Lightweight)
**Target:** Basic extraction without explicit Business Model taxonomy predictions.
- **Phase 1: Extraction & Scraping**
  - **Scraping**: Fetches the homepage only.
  - **LLM Call 1 (`prompts[0]`)**: Extracts Short Description and Long Description.
  - **Output**: Writes SD, LD, and tokens to Google Sheets.
- **Phase 2: Tracxn API Push**
  - **Domain Profile (`update_dp`)**: Pushes SD and LD. Appends `bu_llm_sd_ld` and `llmbasedpublishing` to Hashtags. No special flags or deep BMs are updated.
  - **Funnel Assignment (`update_funnel`)**: Executes standard funnel assignments.

## 4. Edge Cases & Resilience Mechanisms (All Types)
1. **Parked Domains**: Pre-LLM heuristic scanning (`is_parked_domain`) checks for GoDaddy, Hugedomains, Sedo, or keyword markers (e.g., "buy this domain"). Fails early to save LLM tokens.
2. **Low Content / JS-walls**: If HTML body is `< 300` characters, or cleaned text is `< 100` characters, the domain is rejected immediately.
3. **LLM Failsafe Identifiers**: If Gemini explicitly detects parking or insufficient data mid-prompt, it returns `PARKED_LLM` or `NO_DATA` for the Short Description. Pipeline interprets this as a definitive failure.
4. **Column Mapping Shift**: Engine detects dynamically if the Domain is at Index 1 (Standard Layout) or Index 2 (Shifted Type A layout) via header scanning, adjusting Google Sheet bounds (`r1, r2, r3`) automatically.
5. **Bot Cleanup**: During Phase 2, `update_dp` queries the Tracxn `edithistory` API. If `foundedYear` or `companyLocation` were improperly created by `publish.edits@tracxn.com`, the engine forces them to `null` to self-heal bot artifacts.
6. **Resource Saturation (OOM/CPU Lock)**: The `SystemHealthMonitor` evaluates CPU (>90%) and Memory (>90%) across workers. If saturated, the engine pauses consumer loops (`await monitor.wait_for_resources()`) and gracefully re-queues domains to prevent browser crashes.

## 5. Troubleshooting & Logging
- **Log Locations:**
  - Standard API requests: `Type[A/B/C]/Logs/api.logs`
  - Engine logic tracing: `Type[A/B/C]/Logs/pipeline.logs`
  - Scraping & Browser telemetry: `Type[A/B/C]/Logs/scrap.logs`
  - Hardware & Resource usage: `Type[A/B/C]/Logs/system.logs`
- If an engine hangs due to syntax or OOM errors, check `pipeline.logs` or `api.logs`. Use `./control.sh` Option 4 (Stop & Clean All) to wipe state across LaunchAgents, Systemd, and Screen, followed by Option 2 to re-initialize cleanly.
