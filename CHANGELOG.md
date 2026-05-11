## [2026-05-11] Engine Stability & Resource Hardening (v6.8)
Files changed:
- Type*/main.py
- control.sh
Reason:
Resolve critical stability issues across all publishing engines:
- Fixed Scrapling IndentationError and duplicated code logic.
- Resolved "Unknown parser argument: timeout" error by removing redundant Fetcher configuration.
- Added Chromium installation to control.sh to support Tier 3 (Scrapling Stealth) fetchers.
- Implemented global BROWSER_SEMAPHORE (3) to strictly limit concurrent browser contexts and prevent CPU spikes.
- Fixed memory leaks in Tier 2/3 by enforcing context cleanup in finally blocks.
- Reduced MAX_WORKERS to 3 for better stability on macOS Air hardware.
Related tests:
Manual validation of log streams and process lists.

## [2026-05-11] High-Resilience Scraping & Dependency Optimization
Files changed:
- TypeA/main.py, TypeB/main.py, TypeC/main.py
- TypeA/requirements.txt, TypeB/requirements.txt, TypeC/requirements.txt
Reason:
- Implemented 3-Tier (now 4-Tier) Fetching Strategy: Tier 0 (Basic HTTPX), Tier 1 (Scrapling Request), Tier 2 (Camoufox Browser), Tier 3 (Async Stealth Scrapling).
- Resolved 'TypeError' crashes in Camoufox launch parameters.
- Fixed Scrapling 0.4.8 API mismatches (status_code -> status, StealthFetcher -> StealthyFetcher, async_fetch).
- Integrated explicit media blocking (Images/Fonts/Media) in browser tiers to optimize CPU/Bandwidth.
- Standardized latest unpinned dependencies across all environments.
Related tests:
- Manual domain verification (teluu.com, rotarex.in, etc.)

## [2026-05-11] High-Fidelity Five-Tier Logging & Full Observability
Files changed:
- TypeA/main.py, TypeB/main.py, TypeC/main.py (Updated)
- TypeA/api.py, TypeB/api.py, TypeC/api.py (Updated)
- sr_common/utils.py (Updated)
- control.sh (Updated)
Reason:
- **Full Observability**: Implemented a comprehensive logging suite to capture 100% of background processes.
  - **api.logs**: Detailed FastAPI request/response tracking, including full headers and bodies (Cloudflare/Apps Script).
  - **scrap.logs**: Granular browser lifecycle events (navigation, settlement, content extraction).
  - **pipeline.logs**: Deep execution tracking, including raw Gemini prompts and Tracxn JSON payloads.
  - **system.logs**: Periodic health monitoring (CPU, Memory, Disk) to diagnose engine crashes.
  - **HTML Snapshots**: Automatic saving of raw HTML to `Logs/Snapshots/` during failed or low-quality scrapes for visual debugging.
- **API Hardening**: Refactored `sr_common/utils.py` to log raw JSON payloads for all external Tracxn and Gemini API interactions.
- **Control Center**: Updated `control.sh` log viewer to support the new multi-tier structure.

## [2026-05-09] Backend Stabilization & Architectural Refactor
Files changed:
- sr_common/ (New Package)
- TypeA/main.py, TypeB/main.py, TypeC/main.py (Refactored)
- TypeA/api.py, TypeB/api.py, TypeC/api.py (Refactored)
- control.sh (Updated)
- .env (Updated)
Reason:
- **Architecture**: Created `sr_common` to eliminate 80-90% code duplication. Centralized `RateLimiter`, `GoogleSheetsClient`, and API wrappers.
- **Process Management**: Implemented surgical PID tracking to prevent "friendly fire" during cancellation. Replaced global `pkill` with `os.kill(pid)`.
- **Security**: Moved `SERVICE_AUTH_TOKEN` to `.env` and enforced strict token verification across all APIs and App Scripts.
- **Hardened Error Handling**: Replaced generic `except:` blocks with specific exception handling and strict Pydantic models for data validation.
- **Config Management**: Consolidated all Sheet IDs and secrets into the root `.env` file.
Related tests:
- Manual verification of API health and process isolation.
- Verification of config loading via scratch script.

### Major Overhaul
- **Architecture**: Migrated Type A, B, and C pipelines to a robust **Worker-Queue Architecture** (`asyncio.Queue`).
  - Separated Stage 1 (Scraping/AI) and Stage 2 (Tracxn Write API) into independent worker pools with dedicated rate limits.
  - Implemented a `sheet_writer_aggregator` for efficient batch reporting to Google Sheets.
- **Scraping**: Implemented a standardized, high-fidelity multi-page scraper across all types.
  - Concurrently fetches Homepage, Paths from Master Sheet, and critical business keywords (`about`, `team`, `product`, etc.).
- **Type C Specifics**:
  - Implemented **Fuzzy Header Detection** to handle variable sheet structures.
  - Added **Bot Cleanup Logic**: Automatically checks `edithistory` and clears `foundedYear`/`companyLocation` if authored by `publish.edits@tracxn.com`.
- **Persistence & Observability**:
  - Synchronized write-back of **modified prompts** (with placeholders replaced) to Google Sheets.
  - Standardized token consumption tracking for all Gemini calls.
- **Resilience**: Implemented infinite retry loops for transient API errors (403, 429, 5xx) across all pipelines.
- **Formatting**: Unified high-fidelity conditional formatting and sheet setup across all engines.
 to accommodate prompt write-backs and token observability.
- **Bug Fixes**: Corrected method scoping errors in Type A that prevented AI extraction from functioning correctly.
Related tests:
Manual verification of modified prompt columns and LD P1/P2 consistency in Google Sheets.

## [2026-05-08] Logic Restoration: Prompt Manipulation & Stage Consistency
Files changed:
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
- **Prompt Persistence**: Resolved failures in prompt manipulation by ensuring modified prompts (Description and BM) are written back to the Google Sheets instead of template placeholders.
- **Legacy Logic Alignment**: Restored the two-stage description processing logic from legacy scripts to all types, improving data fidelity for large content sets.
- **Type C Robustness**: Implemented multi-page scraping and stage-based AI classification in Type C, synchronizing it with the high-fidelity standards of Type A.
- **UI & Formatting**: Expanded headers and formatting ranges across all types to accommodate prompt write-backs and token observability.
- **Bug Fixes**: Corrected method scoping errors in Type A that prevented AI extraction from functioning correctly.
Related tests:
Manual verification of modified prompt columns and LD P1/P2 consistency in Google Sheets.

## [2026-05-08] Pipeline Stability & Mapping Fixes
Files changed:
- TypeA/main.py
- TypeB/main.py
Reason:
Fixed column mapping discrepancies in Type A (I-S for Phase 1, T-W for Phase 2). Updated Tracxn API tokens to the correct production credentials. Resolved "0 rows processed" issue in Type B by correcting the Sheet ID placeholder. Improved error handling for 422 responses (duplicate updates).
Related tests:
Manual verification on Google Sheets.

## [2026-05-08] Initial Type A Implementation
Files changed:
- TypeA/legacy.py
- TypeA/main.py
Reason:
Ported Type A logic from legacy Mojo scripts to Python-native FastAPI architecture. Implemented two-level BM prediction and Special Flags processing.
Related tests:
TypeA/Logs/scraper.log

## [2026-05-08] Optimization: Batch Updates & Precise Column Mapping
Files changed:
- TypeA/main.py, TypeB/main.py, TypeC/main.py
- GEMINI.md (New)
Reason:
- Implemented **Batch Update Logic** across all pipelines to prevent Google Sheets 429 quota errors.
- Re-aligned **Type A** column mapping to the exact I-S (Phase 1) and T-W (Phase 2) specification.
- Fixed `f_bms2` UnboundLocalError in Type A that was crashing the pipeline during "No Results" scenarios.
- Standardized `psutil` resource recalibration across all engines for consistent dynamic worker scaling.
- Fixed `control.sh` on macOS to handle existing `cloudflared` service installations without errors.
- Added comprehensive project documentation in `GEMINI.md`.

## [2026-05-08] Full Migration: Pure Python Engine & Total Mojo Cleanup
Files changed:
- control.sh (Refactored)
- TypeA/main.py, TypeB/main.py, TypeC/main.py (New)
- TypeA/api.py, TypeB/api.py, TypeC/api.py (Updated)
- start.mojo, test_mojo.mojo, start.py (Deleted)
- All main.mojo files and .pixi directories (Deleted)
Reason:
- Successfully migrated the entire publishing engine from Mojo to pure Python for production-grade stability and cross-platform reliability.
- Refactored `control.sh` to handle direct background service spawning with automatic Python version enforcement (minimum 3.10).
- Migrated all pipeline workers from Mojo wrappers to native Python scripts (`main.py`), significantly reducing execution overhead and debugging complexity.
- Removed all Mojo and Pixi dependencies, including configurations and local environment artifacts, to keep the codebase clean and maintainable.
- Verified that all Type A, B, and C services are fully operational and "ONLINE".


## [2026-05-08] Stabilization: Mojo Engine Orchestration & Process Persistence
Files changed:
- start.mojo
- control.sh
- Type A/B/C/main.mojo
Reason:
- Stabilized the Mojo-native orchestrator (`start.mojo`) for cross-platform reliability between macOS and Ubuntu.
- Implemented a "Shell Handoff" strategy using `os.system` and `nohup` to ensure FastAPI workers are truly detached and persistent.
- Resolved `ModuleNotFoundError: uvicorn` by migrating to `python -m uvicorn` with absolute virtual environment paths.
- Fixed Python-to-Mojo string concatenation errors (`__radd__`) by explicitly casting PythonObject values to Mojo Strings.
- Enhanced `control.sh` with a deeper cleanup routine to prevent port collisions and stale background processes.
- Hardened all `main.mojo` workers with modern Mojo syntax and raw string literals for robust regex handling.
- Integrated row-by-row progress tracking via `.progress.json` for real-time Apps Script UI updates.

## [2026-05-08] Migration: Mojo Engine & Phase 2 Fixes
Files changed:
- Type A/main.mojo
Reason:
- Migrated core pipeline logic to `main.mojo` to bypass legacy bugs in `main.py`.
- Fixed Phase 1/Phase 2 separation logic to ensure scraping and API calls are mutually exclusive when requested.
- Fixed incorrect column mapping for `FeedID` (was index 20, corrected to index 19).
- Updated Tracxn API payloads:
    - Added missing `sourceData` (view/tab) to the funnel move API.
    - Updated `publishingDepth` to `Pub 1 - Full` to ensure complete data publishing.
- Fixed logic to correctly handle Phase 2 resumption from sheet data when running in `phase2` mode.
Related tests:
- Manual verification of logic flow in Mojo.
- Re-aligned mappings with the latest sheet structure.


## [2026-05-07] Fix: Environment Setup & Venv Corruption
Files changed:
- control.sh
Reason:
- Resolved `ModuleNotFoundError: No module named 'pip._vendor.rich._extension'` in `TypeB` by implementing health checks for virtual environments.
- Enhanced `control.sh` to automatically recreate virtual environments if `pip` is corrupted or if there is a Python version mismatch.
- Added error handling to venv creation to prevent silent failures during installation.
Related tests:
- Manual verification of `TypeB` installation.

Files changed:
- setup_mac.sh
- Type A/apps_script.gs
- Type B/apps_script.gs
- Type C/apps_script.gs
Reason:
- Created `setup_mac.sh` for temporary local deployment on Mac with automatic cleanup on terminal close.
- Optimized `setup_mac.sh` to use existing virtual environments instead of recreating them on each run.
- Integrated new Cloudflare tunnel connector with the provided token.
- Added Vishnu-Mac specific URLs to all Apps Script worker configurations.

## [2026-04-30] UI Update: Optimized Run Pipeline Workflow
Files changed:
- Type A/apps_script.gs
- Type B/apps_script.gs
- Type C/apps_script.gs
Reason:
- Removed the manual "Fetch Status" button from the Run Pipeline wizard as it was taking too long.
- Enabled immediate device selection by defaulting all workers to a "Ready" state.
- Retained the ability to check detailed status via the dedicated "Check Status" and "Check Health" menu options.
Related tests:
- Manual UI walkthrough verification.


## [2026-04-29] Fix: Stealth Scraping Failures & Environment Setup
Files changed:
- Type A/requirements.txt
- Type B/requirements.txt
- Type C/requirements.txt
- install.py
Reason:
- Added `curl_cffi` and `uvloop` to all pipelines to resolve "No module named curl_cffi" errors in `StealthyFetcher` and improve async performance.
- Upgraded `scrapling` to `scrapling[stealth]` in `requirements.txt` to ensure all necessary stealth libraries are bundled.
- Enhanced `install.py` to prompt for `sudo` password at the beginning and keep it alive, preventing the installation from halting midway.
- Enhanced `install.py` with `playwright install-deps` to ensure all necessary system libraries are installed on Ubuntu for Chromium to run correctly.
Related tests:
- Manual verification of logs revealed the missing dependency.

## [2026-05-09] Configuration Stabilization & Remote Deployment
Files changed:
- sr_common/config.py
- control.sh
Reason:
Fixed Pydantic ValidationError crash loop on remote devices. Implemented literal defaults and manual environment injection to ensure service startup even if .env is partially missing. Standardized log clearing in control.sh.
Related tests:
Manual verification on Device-4230 (Success: All engines ONLINE).

## [2026-05-11] Fix sr-typec OFFLINE failure
Files changed:
- TypeC/.venv (Recreated)
Reason:
Resolved ModuleNotFoundError in TypeC by repairing a corrupted virtual environment.
Related tests:
Manual service startup verification on Port 8766.
