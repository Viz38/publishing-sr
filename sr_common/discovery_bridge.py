"""
Autonomous Discovery-to-SR Ingestion Bridge
============================================
Facilitates autonomous domain handoff from Discovery Engine's daily output
into Publishing SR's Type A, Type B, and Type C engines.

Categorization Rule (Feed Curation Type):
- Types 'A' and 'E' -> Type A
- Type 'B' -> Type B
- All others -> Type C

Operational State:
- Dormant / Feature-Flagged (ENABLE_AUTONOMOUS_INGESTION = False by default).
- Zero code changes required to activate: flip ENABLE_AUTONOMOUS_INGESTION=True in .env.
"""

import asyncio
import os
import sys
import json
import logging
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any

from sr_common.config import settings
from sr_common.clients import GoogleSheetsClient

logger = logging.getLogger("sr_common.discovery_bridge")

# Logging directory setup
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Logs")
os.makedirs(LOGS_DIR, exist_ok=True)
DEFAULT_STAGING_FILE = os.path.join(LOGS_DIR, "staged_autonomous_domains.json")


def categorize_by_curation_type(curation_type: str) -> str:
    """
    Categorizes a feed curation type string into Publishing SR engine target:
    - Type A & E -> TypeA
    - Type B -> TypeB
    - All others -> TypeC
    """
    if not curation_type or not str(curation_type).strip():
        return "TypeC"

    ct = str(curation_type).strip().upper()

    # Rule: Type A and E are considered Type A
    if ct.startswith("A") or ct.startswith("E") or ct.startswith("TYPE A") or ct.startswith("TYPE E"):
        return "TypeA"
    # Rule: Type B is Type B
    elif ct.startswith("B") or ct.startswith("TYPE B"):
        return "TypeB"
    # Rule: All others are Type C
    else:
        return "TypeC"


class DiscoveryBridge:
    """
    Manages autonomous domain transfer and categorization between
    Discovery Engine and Publishing SR.
    """
    def __init__(
        self,
        discovery_sheet_id: Optional[str] = None,
        discovery_creds_file: Optional[str] = None,
        feed_owner_sheet_id: Optional[str] = None,
        sr_creds_file: Optional[str] = None,
        staging_file: Optional[str] = None
    ):
        self.discovery_sheet_id = discovery_sheet_id or settings.DISCOVERY_DAILY_PROCESSING_SHEET_ID
        self.discovery_creds_file = discovery_creds_file or settings.DISCOVERY_CREDENTIALS_FILE
        self.feed_owner_sheet_id = feed_owner_sheet_id or settings.FEED_OWNER_SHEET_ID
        self.sr_creds_file = sr_creds_file or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "TypeC", "TypeC.json")
        self.staging_file = staging_file or DEFAULT_STAGING_FILE
        self.feed_curation_map: Dict[str, str] = {}

    async def load_feed_curation_map(self) -> Dict[str, str]:
        """
        Loads the authoritative Feed Name -> Curation Type dictionary
        from the Feed Owner Details Google Sheet tab.
        """
        if self.feed_curation_map:
            return self.feed_curation_map

        if not self.feed_owner_sheet_id:
            logger.warning("DiscoveryBridge: FEED_OWNER_SHEET_ID is not configured.")
            return {}

        try:
            gc = await GoogleSheetsClient.get_manager(self.sr_creds_file).authorize()
            sh = await gc.open_by_key(self.feed_owner_sheet_id)
            ws = await sh.worksheet("Feed Owner Details")
            all_vals = await ws.get_all_values()

            # Row 0 is header: ['Feed Name', 'Feed Id', 'Status', 'Curation Type', ...]
            f_map = {}
            for r in all_vals[1:]:
                if len(r) > 3 and r[0].strip():
                    feed_name = r[0].strip().lower()
                    curation_type = r[3].strip()
                    f_map[feed_name] = curation_type

            self.feed_curation_map = f_map
            logger.info(f"DiscoveryBridge: Loaded {len(f_map)} feed curation mappings from Feed Owner Details.")
            return self.feed_curation_map
        except Exception as e:
            logger.error(f"DiscoveryBridge: Failed to load feed curation mappings: {e}")
            return {}

    async def fetch_discovery_candidates(self) -> List[Dict[str, str]]:
        """
        Fetches candidate domain records from Discovery Daily Processing sheet (B4:E).
        Returns list of dicts: domain, dp_id, funnel_name, funnel_id.
        """
        if not os.path.exists(self.discovery_creds_file):
            logger.warning(f"DiscoveryBridge: Discovery credentials file missing at {self.discovery_creds_file}")
            return []

        try:
            gc = await GoogleSheetsClient.get_manager(self.discovery_creds_file).authorize()
            sh = await gc.open_by_key(self.discovery_sheet_id)
            ws = await sh.worksheet(settings.DISCOVERY_DAILY_PROCESSING_TAB_NAME)
            
            # Read B4:E range
            rows = await ws.get_values("B4:E")
            candidates = []
            for r in rows:
                if not r or not r[0].strip():
                    continue
                domain = r[0].strip()
                dp_id = r[1].strip() if len(r) > 1 else ""
                funnel_name = r[2].strip() if len(r) > 2 else ""
                funnel_id = r[3].strip() if len(r) > 3 else ""
                
                candidates.append({
                    "domain": domain,
                    "dp_id": dp_id,
                    "funnel_name": funnel_name,
                    "funnel_id": funnel_id
                })

            logger.info(f"DiscoveryBridge: Fetched {len(candidates)} candidate domains from Discovery Daily Processing.")
            return candidates
        except Exception as e:
            logger.error(f"DiscoveryBridge: Failed to fetch candidates from Discovery sheet: {e}")
            return []

    def route_candidates(
        self,
        candidates: List[Dict[str, str]],
        feed_map: Optional[Dict[str, str]] = None
    ) -> Dict[str, List[Dict[str, str]]]:
        """
        Partitions candidate domain records into TypeA, TypeB, and TypeC.
        """
        f_map = feed_map if feed_map is not None else self.feed_curation_map
        partitioned: Dict[str, List[Dict[str, str]]] = {
            "TypeA": [],
            "TypeB": [],
            "TypeC": []
        }

        for cand in candidates:
            funnel_name = cand.get("funnel_name", "")
            # Extract raw feed name (handle "Publishing Funnel : <Feed Name>")
            if " : " in funnel_name:
                feed_clean = funnel_name.split(" : ")[-1].strip().lower()
            else:
                feed_clean = funnel_name.strip().lower()

            curation_type = f_map.get(feed_clean, "")
            target_engine = categorize_by_curation_type(curation_type)

            enriched = dict(cand)
            enriched["feed_name"] = feed_clean
            enriched["curation_type"] = curation_type
            enriched["target_engine"] = target_engine

            partitioned[target_engine].append(enriched)

        logger.info(
            f"DiscoveryBridge: Partitioned {len(candidates)} domains -> "
            f"TypeA: {len(partitioned['TypeA'])}, "
            f"TypeB: {len(partitioned['TypeB'])}, "
            f"TypeC: {len(partitioned['TypeC'])}"
        )
        return partitioned

    def stage_to_local_cache(
        self,
        partitioned: Dict[str, List[Dict[str, str]]]
    ) -> str:
        """
        Persists partitioned candidates to a local staging JSON file.
        Provides zero-loss auditing while autonomous ingestion remains dormant.
        """
        payload = {
            "timestamp": datetime.now().isoformat(),
            "enabled": bool(settings.ENABLE_AUTONOMOUS_INGESTION),
            "summary": {k: len(v) for k, v in partitioned.items()},
            "records": partitioned
        }
        
        try:
            with open(self.staging_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"DiscoveryBridge: Successfully staged partitioned records to {self.staging_file}")
            return self.staging_file
        except Exception as e:
            logger.error(f"DiscoveryBridge: Failed staging to local file: {e}")
            return ""

    async def stage_to_sr_sheets(
        self,
        partitioned: Dict[str, List[Dict[str, str]]],
        force: bool = False
    ) -> Dict[str, int]:
        """
        Pushes partitioned records to the respective Publishing SR Google Sheets (DB tab).
        Respects the ENABLE_AUTONOMOUS_INGESTION feature flag.
        If dormant (ENABLE_AUTONOMOUS_INGESTION=False and not force), aborts write and retains safe staging.
        """
        is_active = bool(settings.ENABLE_AUTONOMOUS_INGESTION) or force
        if not is_active:
            logger.warning(
                "DiscoveryBridge: AUTONOMOUS INGESTION IS DORMANT (ENABLE_AUTONOMOUS_INGESTION=False). "
                "Sheet updates skipped. Data preserved in staging cache."
            )
            return {"TypeA": 0, "TypeB": 0, "TypeC": 0}

        results = {}
        sheet_targets = {
            "TypeA": (settings.TYPEA_SHEET_ID, "TypeA/TypeA.json"),
            "TypeB": (settings.TYPEB_SHEET_ID, "TypeB/TypeB.json"),
            "TypeC": (settings.TYPEC_SHEET_ID, "TypeC/TypeC.json"),
        }

        date_str = datetime.now().strftime("%d-%b-%Y")

        for engine, items in partitioned.items():
            if not items:
                results[engine] = 0
                continue

            target_id, creds_rel = sheet_targets.get(engine, ("", ""))
            creds_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), creds_rel)
            if not target_id or not os.path.exists(creds_path):
                logger.error(f"DiscoveryBridge: Target sheet or creds missing for {engine}")
                results[engine] = 0
                continue

            try:
                gc = await GoogleSheetsClient.get_manager(creds_path).authorize()
                sh = await gc.open_by_key(target_id)
                ws = await sh.worksheet("DB")

                # Format rows for SR DB: [Date, Domain, DP_ID, Funnel_Name, Funnel_ID]
                rows_to_append = [
                    [
                        date_str,
                        item.get("domain", ""),
                        item.get("dp_id", ""),
                        item.get("funnel_name", ""),
                        item.get("funnel_id", "")
                    ]
                    for item in items
                ]

                await ws.append_rows(rows_to_append)
                results[engine] = len(rows_to_append)
                logger.info(f"DiscoveryBridge: Appended {len(rows_to_append)} rows to {engine} DB sheet.")
            except Exception as e:
                logger.error(f"DiscoveryBridge: Error pushing to {engine} sheet: {e}")
                results[engine] = 0

        return results

    async def execute_bridge_run(self, dry_run: bool = True, force_push: bool = False) -> Dict[str, Any]:
        """
        Executes end-to-end bridge sequence:
        1. Load feed curation map
        2. Fetch discovery candidates
        3. Partition candidates
        4. Stage to local cache
        5. Push to sheets (if active/forced)
        """
        logger.info(f"DiscoveryBridge: Starting bridge run (dry_run={dry_run}, force_push={force_push})...")
        await self.load_feed_curation_map()
        candidates = await self.fetch_discovery_candidates()
        if not candidates:
            logger.warning("DiscoveryBridge: No candidates fetched. Run concluded.")
            return {"status": "no_candidates", "summary": {}}

        partitioned = self.route_candidates(candidates)
        staging_path = self.stage_to_local_cache(partitioned)

        pushed = {}
        if not dry_run and (settings.ENABLE_AUTONOMOUS_INGESTION or force_push):
            pushed = await self.stage_to_sr_sheets(partitioned, force=force_push)

        return {
            "status": "success",
            "total_candidates": len(candidates),
            "summary": {k: len(v) for k, v in partitioned.items()},
            "pushed": pushed,
            "staging_path": staging_path,
            "dormant": not (settings.ENABLE_AUTONOMOUS_INGESTION or force_push)
        }


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Autonomous Discovery-to-SR Ingestion Bridge")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate routing and cache staging without pushing to sheets")
    parser.add_argument("--force-push", action="store_true", help="Force push staged records to live SR sheets")
    parser.add_argument("--status", action="store_true", help="Display current bridge configuration and staging state")
    args = parser.parse_args()

    bridge = DiscoveryBridge()

    if args.status:
        print("=== Autonomous Discovery Bridge Status ===")
        print(f"Autonomous Ingestion Active: {settings.ENABLE_AUTONOMOUS_INGESTION}")
        print(f"Discovery Processing Sheet:  {settings.DISCOVERY_DAILY_PROCESSING_SHEET_ID}")
        print(f"Discovery Credentials:       {settings.DISCOVERY_CREDENTIALS_FILE}")
        print(f"Staging Cache Path:          {DEFAULT_STAGING_FILE}")
        if os.path.exists(DEFAULT_STAGING_FILE):
            with open(DEFAULT_STAGING_FILE, "r") as f:
                data = json.load(f)
                print(f"Last Staged At:              {data.get('timestamp')}")
                print(f"Staged Counts:               {data.get('summary')}")
        else:
            print("Staging Cache:               No staged records yet")
        return

    result = await bridge.execute_bridge_run(dry_run=not args.force_push, force_push=args.force_push)
    print("\nBridge Execution Result:")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
