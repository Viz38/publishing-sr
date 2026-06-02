## [2026-06-02] Fix: Prevent Cloudflare Tunnel Drop in Ubuntu
Files changed:
- control.sh
Reason:
Fix an issue where the Cloudflare tunnel (`cloudflared`) was constantly disconnecting on Ubuntu servers (e.g., 599) due to aggressive uninstall/install cycles during engine restarts.
Changes:
1. **Persistent Service Config**: Changed `stop_all` to cleanly `systemctl disable --now cloudflared` instead of `sudo cloudflared service uninstall`. This preserves the systemd service file and token configuration.
2. **Stable Start**: `start_standard` and `start_service_mode` now check if the service file exists instead of checking if it is merely active, preventing redundant and failing install attempts.
3. **Deep Clean Updates**: Added the `cloudflared service uninstall` command specifically to the Deep Clean (Option 10) menu for proper teardown when intended.

## [2026-06-02] LLM Resilience: Robust Token Fallback and Exception Handling
Files changed:
- sr_common/utils.py
Reason:
Ensure that caching failures (e.g. min token requirements not met, or transient networking exceptions) gracefully fallback to standard non-cached requests without dropping rows.
Changes:
1. **Min Token Fallback**: Added explicit 400 error catch in `get_or_create` for min token requirements to gracefully skip caching and fallback to standard requests.
2. **Exception Handling in Cache**: Wrapped the `session.post` for caching in a `try...except` block so that DNS/network errors do not propagate and crash the worker, but rather fallback to normal generation.
3. **Generate API Token Error Retry**: Broadened the caching error fallback inside `call_gemini_api` to include "token" string matches, stripping caching and cleanly retrying without dropping the request.

## [2026-06-02] Architectural Refactoring: Unified Dependencies and Memory Optimization
Files changed:
- pyproject.toml (New)
- control.sh
- TypeA/api.py, TypeB/api.py, TypeC/api.py
- sr_common/config.py
- TypeA/requirements.txt, TypeB/requirements.txt, TypeC/requirements.txt (Deleted)
Reason:
Implement a modern, unified architectural structure based on `uv` for robust dependency management, and resolve memory leaks in the API middleware.
Changes:
1. **Unified Dependencies**: Replaced individual `requirements.txt` files with a single, global `pyproject.toml` file at the root.
2. **`uv sync` Integration**: Updated `control.sh` to use `uv sync` for creating virtual environments and installing dependencies, significantly speeding up bootstrapping.
3. **Memory Leak Fix**: Refactored the `log_requests` HTTP middleware in TypeA, TypeB, and TypeC APIs. It no longer unconditionally buffers the entire request payload into memory, preventing Out-Of-Memory (OOM) errors during heavy traffic.
4. **Concurrency Safety**: Introduced `asyncio.Lock()` to synchronize state mutations in the FastAPI engines, resolving race conditions.
5. **Config Cleanup**: Refactored `sr_common/config.py` to correctly utilize Pydantic V2 `BaseSettings` out of the box, removing redundant `os.environ` sync hacks.
6. **LLM Resilience**: Fixed the `call_gemini_api` logic in `sr_common/utils.py` to properly execute the 3-attempt exponential backoff retry loop for transient non-200/non-429 API errors (e.g., 500/503), instead of immediately aborting.
7. **UV Build Fix**: Explicitly disabled package building in `pyproject.toml` (`[tool.uv] package = false`) and removed the `hatchling` build system. This resolves the `packages = ["src/foo"]` error during OS-Native Service installation, as the publishing engine is a collection of scripts, not a distributable wheel package.

## [2026-05-28] Fix BM Prompt 2 Skipping Issue (Type A)
Files changed:
- TypeA/main.py
Reason:
Fixed an issue in Type A where BM Prompt 2 was silently skipped for all domains. The Gemini API output for BM Prompt 1 often included Markdown asterisks (e.g., `**1. E-Commerce** - Explanation:`), which caused the regex matching for `bm_name_1` to fail or incorrectly include `**`. Additionally, exact case-sensitive matching prevented some 2nd level BM lookups.
Changes:
1. **Markdown Stripping**: Stripped `*` and `#` characters from LLM responses before regex processing to handle Markdown formatting correctly.
2. **Regex Lenience**: Updated the regex to be case-insensitive, support leading whitespace, and handle optional delimiters around "Explanation".
3. **Case-Insensitive Matching**: Changed `s[2].startswith(bm_name_1)` to be completely case-insensitive.
4. **Whitespace Cleaning**: Added `.strip()` to `f_lvl` and `s_lvl` data when populating `bm_mapping` to prevent trailing space lookup failures.

## [2026-05-27] Fix Token Tracking for Gemini Flash Lite
Files changed:
- sr_common/utils.py
Reason:
Stop subtracting cached tokens from promptTokenCount. Since Gemini Flash Lite prices cached tokens the same as standard tokens, the full `promptTokenCount` must be logged to accurately reflect billed tokens.

## [2026-05-20] Full Ecosystem Migration to `uv` (Ultra-Fast Package Manager)
Files changed:
- control.sh
Reason:
Perform a 100% migration from `pip` and standard `venv` to `uv` to maximize dependency resolution speed and ensure extremely reliable, deterministic environment bootstraps across macOS and Ubuntu.
Changes:
1. **Interactive Auto-Installer**:
   - Upgraded `control.sh` to universally detect the absence of `uv` and automatically fetch it from `astral.sh`.
   - Injected absolute `uv` binary paths dynamically into the `$PATH` to allow immediate use without shell restarts.
2. **Absolute Runner Execution**:
   - Modified `create_runner()` to dynamically resolve the absolute path to `uv` (`$UV_PATH`) and hardcode it into the generated `runner.sh` daemon scripts. This guarantees bulletproof execution inside strict `launchd` and `systemd` environments where `$PATH` is highly isolated.
3. **Legacy Pip Deprecation**:
   - Entirely removed standard `python3 -m venv` and `pip install` fallbacks.
   - Refactored `patchright` and `camoufox` bootstrapping to execute purely under `uv run python -m`.

## [2026-05-20] Implement Force-Assign Funnel Step in Type A Pipeline
Files changed:
- TypeA/main.py
- CHANGELOG.md
Reason:
Address common HTTP 400 "Funnel State Conflicts" failures by integrating the `/force-assign` prerequisite step into the Type A pipeline before performing domain profile funnel movements.
Changes:
1. **Integrated force-assign prior to move**:
   - Added `/force-assign` PUT call to `TypeA/main.py` mimicking the validated, robust operational workflow of the Type B engine.
   - Updated the move status writing to conditionally invoke `/move` only upon a successful `/force-assign` assignment response (HTTP 200/201), otherwise outputting "Assign Failed".

## [2026-05-19] Implement Explicit Gemini Context Caching & Token Analytics
Files changed:
- sr_common/models.py
- sr_common/utils.py
- scratch/test_token_usage.py
- CHANGELOG.md
Reason:
Implement high-fidelity explicit context caching utilizing Gemini's native `cachedContents` API, reducing token billing by up to 96.8% and adding full tracking for cached token usage.
Changes:
1. **Net Billed Input Token Tracking**:
   - Updated `call_gemini_api` in [utils.py](file:///Users/vishnu/Documents/Tracxn/SR/Publishing-Caching/sr_common/utils.py) to set `prompt_tokens` to `totalTokenCount - cachedContentTokenCount`. This ensures that the Google Sheets and logs write out the **actual reduced billed input tokens** directly, making the massive token savings visible on the sheets.
2. **Add Caching Observability**:
   - Updated `LLMResult` model to track `cached_tokens` in [models.py](file:///Users/vishnu/Documents/Tracxn/SR/Publishing-Caching/sr_common/models.py).
   - Integrated `cachedContentTokenCount` parsing from Gemini response `usageMetadata` inside `call_gemini_api` in [utils.py](file:///Users/vishnu/Documents/Tracxn/SR/Publishing-Caching/sr_common/utils.py).
2. **Simplified Caching Heuristics**:
   - Utilized the modern 1,024-token cache threshold for Gemini 3.1 Flash, allowing direct context caching of raw system instructions without complex taxonomies or padding.
3. **Advanced Token Savings Aggregator**:
   - Upgraded [test_token_usage.py](file:///Users/vishnu/Documents/Tracxn/SR/Publishing-Caching/scratch/test_token_usage.py) to measure, log, and neatly display cached input tokens, net billed tokens, and billed token savings percentage.
Related tests:
- scratch/test_token_usage.py (9/9 domains completed successfully with verified 72% to 96% token savings).

## [2026-05-18] Remove Pre-Launch JSON Credentials Check in Standard Start
Files changed:
- control.sh
Reason:
Improve UX by removing blocking pre-launch JSON credentials checks in `start_standard()`, allowing engines to boot up and be configured post-launch.
Changes:
1. **Removed Credentials Lock**:
   - Deleted the blocking `if [ ! -f "$f_creds" ]` guard statement inside `start_standard()` in [control.sh](file:///Users/vishnu/Documents/Tracxn/SR/Publishing/control.sh).
   - This permits FastAPI API workers to boot up and serve requests without pre-configured local JSON service account key files, allowing them to be configured after service launch.
Related tests:
- Validated `control.sh` syntax successfully via `bash -n`.

## [2026-05-18] Add User TRACXN-LP-477
Files changed:
- .env
- control.sh
Reason:
Provision credentials and identity mapping for new operator user TRACXN-LP-477 to ensure persistent background tunnel connectivity.
Changes:
1. **Credentials Allocation**:
   - Added `TUNNEL_TOKEN_477` with the Cloudflare Tunnel credentials to `.env`.
2. **Identity Resolution & Case Insensitivity Mapping**:
   - Integrated user `tracxn-lp-477` case-insensitively into the standard environment loader inside `control.sh` as `Device-477`.
Related tests:
- Validated `control.sh` syntax successfully via `bash -n`.

## [2026-05-18] Credentials Auto-Creation & Interactive Configuration Center
Files changed:
- control.sh
Reason:
Implement automated workspace credentials decoding from `.env` during Option 1 ("Init Workspace") and introduce an interactive credentials configuration dashboard.
Changes:
1. **Automated Workspace Credentials Integration**:
   - Refactored option `1) Init Workspace (Venv + Browsers + Credentials)` to automatically recreate and decode credentials JSON files (`TypeA.json`, `TypeB.json`, `TypeC.json`) directly from the `.env` base64 variables without requiring interactive prompts.
   - Refactored `auto_create_credentials_explicit` to support a `force` parameter to allow automated non-interactive overwrites when setting up the workspace.
2. **Manual "Configure Credentials" Dashboard**:
   - Added option `8) Configure Credentials` as a manual diagnostic/configuration screen showing whether JSON files and `.env` variables exist for all engines.
   - Includes sub-menu options to trigger manual base64 recreation (with conflict prompt) or manually input raw service account JSON keys.
3. **Cross-Platform Compatibility Upgrade**:
   - Replaced Bash-4-specific `${var^^}` upper-case parameter expansions with POSIX-standard `tr` translation (`tr '[:lower:]' '[:upper:]'`). This fixes all syntax and "bad substitution" errors under standard macOS Bash 3.2 while maintaining absolute reliability on Linux/Ubuntu.
   - Enforced secure file permissions (`chmod 600`) on all auto-generated and manually pasted JSON files.
Related tests:
- Syntax checked `control.sh` successfully with `bash -n`.
- Verified decoding logic and JSON format validation of all `.env` credentials using a custom scratch test script.

## [2026-05-18] Phase 2 Fixes, Bot Cleanup, Column S Hashtags, Log Truncation, & Type C Column Mapping
Files changed:
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
- control.sh
- handoff.md
Reason:
Resolve silent Phase 2 failures by correcting Script Run Status index checks and variable scopes, and implement bot edit cleanup logic.
Changes:
1. **Critical Phase 2 Fixes**: Resolved major bugs where Phase 2 runs would silently fail or crash:
   - *Script Run Status & Relative Field Indexing*: Fixed the incorrect hardcoded offsets (which checked the wrong columns like `SD` and caused false `"Missing Phase 1 inputs"` failures). We resolved this by anchoring all Phase 2 field lookups relatively to `r1_idx = ord(h_map["r1"]) - ord('A')` (the static start column index of Stage 1 outputs). This guarantees that Phase 2 will always read the exact columns that Stage 1 wrote for both standard and shifted layouts, eliminating shifted layout off-by-one errors completely.
   - *Scope / UnboundLocalError Resolution*: Fixed scope crashes in `TypeC` (where `r1` was undefined during Phase 2 success write-back) and `TypeB` (where `hash_stat` was undefined during Phase 2). Both are now defined in global worker scope.
   - *Visible Failure Reporting*: Enforced failure status reporting back to the sheets under Phase 2 instead of skipping write-back silently.
2. **Type C Bot Cleanup & Status Updates**: Restored the legacy bot cleanup feature. It now dynamically checks a domain's edit history (`/data/edithistory/edits/DOMAIN_PROFILE/{DID}`) and clears `foundedYear`/`companyLocation` if they were edited by `publish.edits@tracxn.com`. It also updates the profile status to `"PUBLISHED"` as the final write step in Phase 2/Full processing.
3. **Type A Column S Filtering**: Filtered out input hashtags and printed only newly added hashtags (`bu_llm_sd_ld, bu_Internal_SRprocess_TypeA`) to Column S (Output SF).
4. **Log Truncation Fix**: Updated `clear_logs` in `control.sh` to truncate files (`>`) rather than using `rm -f`, preserving file descriptors and keeping logging active.
5. **Type C Column Realignment**: Configured standard layout results to start in Column H (contiguous to Q) and implemented relative mathematical offsets (`chr(ord(r1) + X)`) to dynamically adjust subsequent columns for both standard (H to Q) and shifted (I to R) layouts.
6. **Type C Phase 1 Feed ID Write-back**: Added logic to write the Feed ID to Column K (Standard) / Column L (Shifted) during Phase 1 processing, ensuring alignment with Types A and B behavior.
Related tests:
- Manual log clearing validation (Verified file truncation does not unlink files).
- Dry-run validation of Type A and Type C write-backs (Confirmed correct column offsets and Phase 1 Feed ID output).
- Compiled code checks (Zero syntax errors across all modified main scripts).

## [2026-05-15] Dynamic Column Mapping & Environment Repair
Files changed:
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
- .venv (Repaired)
Reason:
Resolved critical failures caused by virtual environment corruption and inconsistent Google Sheet structures (shifted columns).
Changes:
1. **Environment Restoration**: Forcibly re-installed `aiohttp` and `pip` in the virtual environment to resolve `AttributeError: module 'aiohttp' has no attribute 'ClientSession'`.
2. **Dynamic Mapping**: Implemented an automated mapping detector in all pipelines. It now detects if a sheet has an extra "Date/Engine Type" prefix (Shifted) or uses the Standard layout, adjusting indices for domain extraction and write-backs dynamically.
3. **Accurate Row Indexing**: Fixed a bug where filtered rows caused row index misalignment during Google Sheets write-backs. The engine now tracks the original row index for every data point.
4. **Resilient Write-Backs**: Updated `sheet_writer` logic to use dynamic column letters based on the detected mapping (e.g., shifting I-T to J-U for Type A sheets).
Related tests:
- Manual Sheet Inspection (A1:Z10 verification for A and B)
- Live Type A trigger (Confirmed "Detected SHIFTED column mapping" and successful initialization)

## [2026-05-15] Camoufox Pipeline Stabilization & Argument Fix
Files changed:
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
Fixed critical "unexpected keyword argument 'screen_resolution'" crash loop in AsyncCamoufox browser launch.
Changes:
1. **Argument Alignment**: Updated `AsyncCamoufox` calls to use the correct `screen` argument (expecting a `browserforge.fingerprints.Screen` object) instead of the unsupported `screen_resolution` keyword.
2. **Fingerprint Precision**: Imported `Screen` from `browserforge.fingerprints` to pass explicit resolution constraints to the camoufox fingerprint generator.
3. **Fingerprint Cleanup**: Removed `device_scale_factor` and `hardware_concurrency` from the `launch` arguments as they are not supported in the current `camoufox` version and were causing runtime errors.
4. **Domain Filter Expansion**: Updated the domain filter to include "Type A", "Type B", and "Type C" (with spaces) to prevent header rows from being processed as valid domains.
Related tests:
- scratch/test_camoufox.py (Verified successful browser launch with Screen object)
- Live Type A pipeline verification (Confirmed engine no longer crashes and enters scraping loop)

Files changed:
- sr_common/stealth.py [NEW]
- sr_common/fetcher.py [NEW]
- sr_common/utils.py
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
Implement modern stealth techniques beyond VPN and unify fetching logic for better maintainability (Clean Code).
Changes:
1.  **Unified StealthFetcher**: Centralized multi-tier fetching service in `sr_common/fetcher.py`.
2.  **TLS/HTTP2 Impersonation**: Replaced TIER 0 with `curl-cffi` for browser-grade handshakes.
3.  **Behavioral Entropy**: Added non-linear Bézier mouse paths and Gamma-distributed jitter in `sr_common/stealth.py`.
4.  **Hardware Coherence**: Synchronized hardware and OS profiles across all fetcher tiers.
5.  **Refactoring**: Eliminated ~500 lines of duplicated fetching logic across A/B/C engines.
6.  **Credential Portability**: Extracted Type A/B/C `.json` credentials to Base64 in `.env`.
7.  **Automation**: Upgraded `control.sh` to auto-decode `.env` and restore missing `.json` credentials.
Related tests:
- tests/test_stealth_utils.py (Passed)
- tests/test_fetcher.py (Passed)

## [2026-05-14] Type B and Type C Funnel Fixes
Files changed:
- TypeB/main.py
- TypeC/main.py
Reason:
- Implemented `force-assign` before `move` in Tracxn API updates to resolve "Funnel State Conflicts".
- Logic merged from manual testing in `manual.py`.
- Fixed `batch_calls` initialization in `sheet_writer` to prevent runtime crashes during token tracking.
Related tests:
- Manual logic validation via manual.py.

## [2026-05-12] Stability & Config: Worker Isolation, Safe Scaling, and Dynamic Identity
Files changed:
- TypeA/main.py, TypeB/main.py, TypeC/main.py
- TypeA/api.py, TypeB/api.py, TypeC/api.py
- sr_common/config.py, sr_common/utils.py
- control.sh
Reason:
Hardened the engines against mid-run stops caused by bad data or unexpected network errors, introduced configurable concurrency, and removed hardcoded identities.
Key Fixes:
- **Accurate Progress Reporting**: Fixed an issue where the UI progress bar advanced as soon as a row was queued. The progress count now only increments after a domain has been completely processed (success or fail).
- **Dynamic Worker Identity**: Removed the hardcoded `"Vishnu-TypeC-Pipeline"` in the status response. It now dynamically reads `WORKER_IDENTITY` from `control.sh`, reflecting the actual device or user running the script.
- **Configurable Workers**: Users can now set `CONFIGURED_MAX_WORKERS=X` and `CONFIGURED_MIN_WORKERS=Y` in the `.env` file to control concurrency.
- **Safe Scaling**: The dynamic worker logic now guarantees the engine will never exceed the configured workers, while still strictly enforcing the Available-RAM and CPU limits (90% Max CPU/Memory).
- **Worker Isolation**: Wrapped domain processing in a global `try/except` to ensure one failing row doesn't kill the entire orchestrator.
- **Data Safety**: Added null-checks for Funnel Names and hashtags to prevent `AttributeError` crashes.
- **Port Restoration**: Reverted ports to original project specifications (8767, 8765, 8766).
- **UI Progress Format**: Maintained the nested JSON structure to resolve "undefined" errors in the dashboard.

## [2026-05-12] Error Reporting: Distinguish between Fetch Failures and Low Content
Files changed:
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
Resolved a logic bug where actual fetch errors (e.g., DNS failures, Timeouts) were being masked by a generic "Low Content" status in the Google Sheets.
Key Fixes:
- **Refined Validation**: Separated `html is None` (fetch failure) from `len(html) < 300` (low content) to ensure the sheet correctly reflects why a domain failed.
- **Log Precision**: Added character counts to "Low Content" logs for better diagnostic visibility.

## [2026-05-12] API Stability: Fix 'NoneType' Decode Crash in Background Task
Files changed:
- TypeA/api.py
- TypeB/api.py
- TypeC/api.py
Reason:
Resolved a critical bug where the API service would crash if the underlying pipeline failed. The crash was caused by attempting to decode a `None` stderr object.
Key Fixes:
- **Subprocess Capture**: Updated `create_subprocess_exec` to properly use `asyncio.subprocess.PIPE` for capturing logs.
- **Safety Checks**: Added null-checks before decoding `stderr` to ensure the API service remains alive even if the pipeline fails.

## [2026-05-12] Tracxn API: Include Company Name in Domain Profile Update (Type C Only)
Files changed:
- TypeC/main.py
Reason:
Ensured that the company name from the sheet is written to the Tracxn `domain-profile` entity during the automated publishing process for Type C.
Key Fixes:
- **API Payload Update**: Added the `companyName` field to the `domain-profile` PUT request payload in the Type C pipeline.

## [2026-05-15] Pipeline Stabilization and Token Integration
Files changed:
- .env
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
Finalized stabilization of Type A, B, and C pipelines with actual production sheet IDs. Implemented dynamic column detection and relative indexing for Phase 2 robustness. Standardized token tracking columns and enforced numeric-only formatting for Thinking Tokens. Fixed environment-level corruption (uvicorn/typing_extensions).
Related tests:
N/A (Live sheet verification)

## [2026-05-12] Fetch Hardening: Minimum Content Length & Stricter Validation
Files changed:
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
Prevent "Success" false positives in Google Sheets when a site returns 200 OK but with empty/insufficient content (observed in 23 cases).
Key Fixes:
- **Tier 3 Length Check**: Enforced a 300-character minimum for the Scrapling Stealth (Tier 3) scraper before allowing a "Success" status.
- **Pipeline Validation**: Refined `process_domain_stage1` to explicitly fail with "Low Content" if the fetched HTML is under 300 characters, even if HTTP status is 200.

## [2026-05-12] Resilience Hardening: LLM Retry, Timeouts & Proxy Recovery
Files changed:
- sr_common/utils.py
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
Address production failures observed in api.logs (GEMINI ERR 429, GEMINI EXC, curl timeouts, Proxy None failures).
Key Fixes:
- **Gemini 429 Retry**: `call_gemini_api` now retries up to 3 times with jittered exponential backoff (2-6s, 4-12s, 8-24s) on rate-limit errors.
- **Gemini Timeout**: LLM calls now use a dedicated 60s `aiohttp.ClientTimeout` to prevent empty exception failures on large prompts.
- **HTTPX Timeout**: Tier 0 fetch timeout increased from 30s to 45s for slow-responding sites.
- **Browser Timeout**: Camoufox `page.goto` timeout increased from 60s to 90s to accommodate heavy DOM sites.
- **Proxy Recovery**: Tier 2 (Camoufox) now retries once on proxy initialization failures before falling through to Tier 3.
Related tests:
- py_compile validation across all engines.

## [2026-05-12] Improved Parked Domain Detection & False Positive Reduction
Files changed:
- sr_common/utils.py
Reason:
Major overhaul of the parked domain detection engine to reduce false positives:
- **Heuristic-Based Detection**: Replaced simple substring matching with a tiered confidence system.
- **Technical Signatures**: Added explicit detection for parking scripts (Sedo, Bodis, ParkingCrew) and meta-refresh redirects to domain marketplaces.
- **Word Boundary Enforcement**: Implemented regex `\b` matching for all keywords to prevent partial matches (e.g., matching "available" inside "unavailable").
- **Sparse Content Heuristics**: "Weak" keywords (like "available" or "construction") now only trigger if the page content is extremely sparse (< 400 chars) or if the keyword appears in the `<title>`.
- **False Positive Blacklist**: Explicitly blacklisted high-risk generic terms like "registrar", "hosting", and "related searches" from triggering parked status unless accompanied by stronger technical signals.
Related tests:
- Verified against 8 test scenarios covering false positives (legitimate business text) and true parked signatures.
- **Production Validation**: Successfully verified 6 specific false-positive domains (`cotesa.com.es`, `hocloop.eu`, `agwelldrilling.com`, `njmcdirect.shop`, `bolma.ng`, `alltagskompetenzen.bayern`) which now all correctly pass as CLEAN.

## [2026-05-12] Resource-Adaptive Concurrency & Health Monitoring
Files changed:
- sr_common/utils.py
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
Optimize engine performance for both high-end and low-end hardware while ensuring system stability.
Key Upgrades:
- **Dynamic Worker Scaling**: Implemented `get_dynamic_max_workers` using realistic 120MB/worker RAM assumptions and physical core counts. Scales up to 25 workers on high-end machines.
- **System Health Gate**: Integrated `SystemHealthMonitor` into the worker loops. Workers now automatically pause if CPU usage > 75% or RAM > 90%.
- **Automatic Backoff**: Implemented a 5-second backoff when resource thresholds are breached, preventing system lockups on low-end machines.
- **API Guard**: Maintained strict Tracxn (160 RPM) and Gemini (2000 RPM) rate limits across all concurrent workers.

Files changed:
- sr_common/utils.py
- sr_common/config.py
- TypeA/main.py
- TypeB/main.py
- TypeC/main.py
Reason:
Implement high-performance parsing and dynamic resource scaling.
Key Upgrades:
- Migrated HTML parsing from `html.parser` to `lxml` for 30%+ speedup.
- Hardened resource blocking in Playwright (added fonts, objects, manifests) while preserving CSS.
- Implemented dynamic worker/browser scaling based on physical CPU cores.
- Optimized Google Sheets batching (increased from 5 to 10).
Related tests:
scratch/test_parsing.py

## [2026-05-12] Critical Bug Fix: Deadlock & Timeout Resolution (v6.9)
Files changed:
- Type*/api.py
- Type*/main.py
- Type*/.pixi (Deleted)
Reason:
Resolve critical issues causing the engine to hang and fail:
- **Deadlock Fix**: Removed `asyncio.subprocess.PIPE` usage in `api.py` (all types). By setting `stdout=None` and `stderr=None`, we prevent the subprocess from hanging when the OS pipe buffer fills up.
- **Timeout Correction**: Fixed a typo in Tier 3 (Scrapling Stealth) scraper where `timeout=60` was interpreted as 60ms. It now correctly uses `CONFIG["REQUEST_TIMEOUT"] * 1000` (60,000ms).
- **Standardized Config**: Updated Type C to use the global `settings.REQUEST_TIMEOUT` for consistency.
- **Cleanup**: Removed legacy `.pixi` directories across all types as the project has fully migrated to pure Python virtual environments (`.venv`).
Related tests:
Manual code verification and log analysis.

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
