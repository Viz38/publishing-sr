"""
AI Fast Crawler with Local Semantic Link Classifier for TechCrawler.

Architecture:
- Semantic Link Classifier: Fast zero-cost local NLP/fuzzy token embedding matching for 10 subpage categories.
- Producer: Non-stop parallel domain processing (20 domains for local discovery, 50 for job creation) with INSTANT real-time push to Google Sheets.
- Cyclical Poller: Scheduled batch tracking sweeps with max job age safeguard.
- Database: Supabase PostgreSQL as primary state engine.
- Sheet Syncer: Real-time asynchronous streaming updater for Google Sheets.
"""

import argparse
import asyncio
import difflib
import inspect
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import aiohttp
import asyncpg
import backoff
from bs4 import BeautifulSoup
import gspread_asyncio
import httpx
from oauth2client.service_account import ServiceAccountCredentials

# Add project root to sys.path for sr_common imports
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sr_common.config import settings
from content_extractor import clean_html

# Configure logging
LOGS_DIR = os.path.join(BASE_DIR, 'Logs')
PROGRESS_FILE = os.path.join(BASE_DIR, ".progress.json")
STOP_FILE = os.path.join(BASE_DIR, ".stop_requested")
os.makedirs(LOGS_DIR, exist_ok=True)
log_path = os.path.join(LOGS_DIR, 'techcrwler.log')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_path, mode="a"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("techcrwler")

# ----------------------------- CONFIG ---------------------------------
def get_credentials_path() -> str:
    creds_file = os.path.join(BASE_DIR, "techcrwler.json")
    if not os.path.exists(creds_file) and settings.TECHCRWLER_CREDENTIALS_B64:
        try:
            import base64
            b64_val = settings.TECHCRWLER_CREDENTIALS_B64
            decoded = base64.b64decode(b64_val).decode('utf-8')
            with open(creds_file, "w") as f:
                f.write(decoded)
            os.chmod(creds_file, 0o600)
            logger.info("Restored techcrwler.json from Base64 env variable.")
        except Exception as e:
            logger.error(f"Failed to decode TECHCRWLER_CREDENTIALS_B64: {e}")
    return creds_file

def parse_access_keys() -> List[str]:
    keys_str = os.getenv("TECHCRWLER_ACCESS_KEYS", getattr(settings, "TECHCRWLER_ACCESS_KEYS", ""))
    if keys_str:
        return [k.strip() for k in keys_str.split(",") if k.strip()]
    return []

CONFIG = {
    "SHEET_ID": os.getenv("TECHCRWLER_SHEET_ID", getattr(settings, "TECHCRWLER_SHEET_ID", "")),
    "CREDENTIALS_FILE": get_credentials_path(),
    "ACCESS_KEYS": parse_access_keys(),
    "BASE_URL": os.getenv("TRACXN_BASE_URL", "https://platform.tracxn.com"),
    "MAX_CONCURRENT_REQUESTS": 500,
    "LOCAL_FETCH_CONCURRENCY": 200,
    "DOMAIN_CONCURRENCY": 20,
    "LOCAL_TIMEOUT": 5.0,
    "DOMAIN_LOCAL_DISCOVERY_TIMEOUT": 12.0,
    "GENERATE_WINDOW_MINUTES": 60,
    "POLL_INTERVAL_SECONDS": 180,
    "MAX_BATCH_TRACKING_MINUTES": 60,
    "MAX_JOB_AGE_SECONDS": 7200,
    "SUPABASE": {
        "USER": os.getenv("SUPABASE_USER", getattr(settings, "SUPABASE_USER", "")),
        "PASSWORD": os.getenv("SUPABASE_PASSWORD", getattr(settings, "SUPABASE_PASSWORD", "")),
        "HOST": os.getenv("SUPABASE_HOST", getattr(settings, "SUPABASE_HOST", "")),
        "PORT": int(os.getenv("SUPABASE_PORT", getattr(settings, "SUPABASE_PORT", "5432")) or "5432"),
        "DATABASE": os.getenv("SUPABASE_DB", getattr(settings, "SUPABASE_DB", ""))
    }
}

LOCAL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

OTHER_PAGES = ["Product", "Pricing", "Solutions", "Offerings", "Features", "Technology", "Mission", "Impact", "Team"]
ALL_CATEGORIES = ["About"] + OTHER_PAGES

DEFAULT_CATEGORY_PATHS = {
    "About": [
        "about", "about-us", "company", "who-we-are", "overview", "our-practice", "our-clinic", "about.html", "about-us.html",
        "a-propos", "apropos", "qui-sommes-nous", "ueber-uns", "ueberuns", "wer-wir-sind", "portrait", "sobre-nosotros", "quienes-somos", "chi-siamo", "over-ons"
    ],
    "Product": [
        "services", "service", "treatments", "treatment", "programs", "program", "care", "product", "products", "platform", "software", "products.html", "services.html",
        "prestations", "produits", "leistungen", "produkte", "angebot", "dienstleistungen", "servicios", "productos", "servizi", "diensten"
    ],
    "Pricing": [
        "pricing", "plans", "price", "rates", "fees", "cost",
        "tarifs", "prix", "preise", "tarife", "precios", "tarifas", "prezzi", "prijzen"
    ],
    "Solutions": [
        "solutions", "solution", "use-cases", "industries", "conditions", "specialties", "what-we-treat",
        "specialites", "loesungen", "lösungen", "soluciones", "especialidades", "soluzioni"
    ],
    "Offerings": ["offerings", "offering", "portfolio", "catalog", "services-offered", "offres"],
    "Features": ["features", "feature", "capabilities", "highlights", "funktionen"],
    "Technology": ["technology", "tech", "architecture", "approach", "technologie"],
    "Mission": ["mission", "vision", "values", "culture", "why-us", "nos-valeurs", "werte", "philosophie"],
    "Impact": ["impact", "social-impact", "sustainability", "outcomes", "engagements", "nachhaltigkeit"],
    "Team": [
        "team", "our-team", "providers", "practitioners", "doctors", "staff", "leadership", "people", "founders", "meet-the-team",
        "equipe", "notre-equipe", "unser-team", "mitarbeiter", "equipo", "nuestro-equipo"
    ]
}

PATH_HEADER_TO_CAT = {
    "About Keywords": "About",
    "Product Keywords": "Product",
    "Pricing Keywords": "Pricing",
    "Solutions Keywords": "Solutions",
    "Offerings Keywords": "Offerings",
    "Features Keywords": "Features",
    "Technology": "Technology",
    "Mission": "Mission",
    "Impact/Social Imapct": "Impact",
    "Team Page": "Team"
}

JUNK_TOKENS = {"keyword", "keywords", "heuristic", "heuristics", "header",
               "label", "column", "example", "list", "path", "paths", "term", "terms"}

def sanitize_category_keywords(raw_keywords: List[str]) -> List[Dict]:
    cleaned = []
    seen = set()
    for raw in raw_keywords:
        if not raw or not raw.strip():
            continue
        val = raw.strip().lower().strip("/#. ")
        if not val:
            continue
        words = val.split()
        if any(tok in words for tok in JUNK_TOKENS):
            continue
        slug = "-".join(words).strip("-")
        if slug in seen:
            continue
        seen.add(slug)
        variants = {val, slug, val.replace(" ", ""), val.replace("-", "")}
        cleaned.append({"raw": val, "slug": slug, "variants": variants})
    return cleaned

# ----------------- LOCAL SEMANTIC LINK CLASSIFIER --------------------

class SemanticLinkClassifier:
    JUNK_PATHS = {
        "privacy", "terms", "login", "signin", "signup", "register", "contact",
        "contact-us", "cart", "checkout", "search", "tag", "tags", "category",
        "categories", "archive", "feed", "rss", "policy", "legal", "cookies",
        "events", "event", "webinar", "webinars", "news", "press", "blog",
        "blogs", "help", "support", "faq", "faqs"
    }

    SEMANTIC_CLUSTERS = {
        "About": [
            "about", "story", "who-we-are", "company", "overview", "who", "history", "profile", "manifesto", "about-us", "our-practice", "our-clinic", "practice", "aboutme",
            "a-propos", "apropos", "propos", "qui-sommes-nous", "ueber-uns", "ueberuns", "wer-wir-sind", "portrait", "sobre-nosotros", "quienes-somos", "chi-siamo", "over-ons"
        ],
        "Product": [
            "service", "services", "treatment", "treatments", "program", "programs", "care", "product", "products", "platform", "software", "tool", "app", "what-we-do",
            "prestations", "produits", "leistungen", "produkte", "angebot", "dienstleistungen", "servicios", "productos", "servizi", "diensten"
        ],
        "Pricing": [
            "pricing", "price", "plan", "plans", "cost", "tier", "subscription", "rates", "fees", "what-we-charge", "insurance", "payment",
            "tarifs", "prix", "preise", "tarife", "precios", "tarifas", "prezzi", "prijzen"
        ],
        "Solutions": [
            "solution", "solutions", "use-cases", "industries", "enterprise", "business", "cases", "condition", "conditions", "specialty", "specialties",
            "specialites", "loesungen", "lösungen", "soluciones", "especialidades", "soluzioni"
        ],
        "Offerings": ["offering", "offerings", "catalog", "capabilities", "portfolio", "what-we-do", "modalities", "offres"],
        "Features": ["feature", "features", "specs", "functionality", "highlights", "benefits", "methodology", "funktionen"],
        "Technology": ["tech", "technology", "stack", "architecture", "engine", "ai", "security", "developer", "equipment", "technologie"],
        "Mission": ["mission", "vision", "purpose", "values", "culture", "why-us", "our-why", "philosophy", "nos-valeurs", "werte"],
        "Impact": ["impact", "social-impact", "sustainability", "esg", "giving", "outcomes", "community", "results", "engagements", "nachhaltigkeit"],
        "Team": [
            "team", "people", "leadership", "crew", "founders", "executives", "staff", "our-team", "doctors", "providers", "practitioners", "meet-our-team",
            "equipe", "notre-equipe", "unser-team", "mitarbeiter", "equipo", "nuestro-equipo"
        ]
    }

    @classmethod
    def classify_link(cls, href: str, text: str, category_heuristics: Dict[str, List[Dict]]) -> Dict[str, float]:
        scores = {cat: 0.0 for cat in ALL_CATEGORIES}
        parsed = urlparse(href)
        path = parsed.path.lower().strip("/")
        text_l = (text or "").lower()
        
        path_segments = set(p for p in path.split("/") if p)
        path_tokens = set(re.split(r"[-_/\s.]", path))
        text_tokens = set(re.split(r"[-_/\s.]", text_l))
        all_tokens = path_tokens | text_tokens

        if any(junk in path_segments or junk in path_tokens for junk in cls.JUNK_PATHS):
            return scores

        for cat, cluster in cls.SEMANTIC_CLUSTERS.items():
            best_score = 0.0
            heuristics_for_cat = category_heuristics.get(cat, [])
            for h in heuristics_for_cat:
                for var in h.get("variants", []):
                    if not var:
                        continue
                    if var in path_segments or var == path or var in path_tokens or var in text_l:
                        best_score = 0.98
                        break
                if best_score >= 0.98:
                    break

            if best_score < 0.98:
                for term in cluster:
                    if "-" in term or " " in term:
                        if term in path_segments or term == path or term in text_l:
                            best_score = max(best_score, 0.95)
                    else:
                        if term in path_segments or term in path_tokens:
                            best_score = max(best_score, 0.95)
                        elif term in text_tokens:
                            best_score = max(best_score, 0.85)

                    for token in all_tokens:
                        if len(token) >= 4 and len(term) >= 4 and abs(len(token) - len(term)) <= 2:
                            ratio = difflib.SequenceMatcher(None, term, token).ratio()
                            if ratio >= 0.85:
                                best_score = max(best_score, ratio * 0.90)

            scores[cat] = best_score

        return scores

sheet_lock = asyncio.Lock()
registry_lock = asyncio.Lock()

# Progress tracking helper
progress_tracker = {"current": 0, "total": 0, "success": 0, "fail": 0}

def update_progress_file(current: int, total: int, success: int, fail: int):
    progress_tracker["current"] = current
    progress_tracker["total"] = total
    progress_tracker["success"] = success
    progress_tracker["fail"] = fail
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump(progress_tracker, f)
    except Exception:
        pass

# ---------------------------- UTILITIES -------------------------------

class AsyncGoogleSheetsClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            scope = ['https://spreadsheets.google.com/feeds',
                     'https://www.googleapis.com/auth/drive']
            creds_file = CONFIG["CREDENTIALS_FILE"]
            creds = ServiceAccountCredentials.from_json_keyfile_name(creds_file, scope)
            cls._instance = gspread_asyncio.AsyncioGspreadClientManager(lambda: creds)
        return cls._instance

def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")

def get_clean_domain(netloc: str) -> str:
    netloc = netloc.lower().strip()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc

async def is_soft_404(response: httpx.Response, base_url: str) -> bool:
    final_url = str(response.url).rstrip("/")
    root = base_url.rstrip("/")
    if final_url == root:
        return True
    try:
        text_lower = response.text[:3000].lower()
    except Exception:
        return False
    error_signals = ["page not found", "404 error", "404 - ", "doesn't exist",
                     "does not exist", "oops", "we can't find", "cannot be found"]
    return any(sig in text_lower for sig in error_signals)

async def find_links_with_semantic_ai(
    client: httpx.AsyncClient,
    base_url: str,
    category_heuristics: Dict[str, List[Dict]],
    categories_to_find: List[str]
) -> Dict[str, Optional[str]]:
    results = {cat: None for cat in categories_to_find}
    try:
        r = await client.get(base_url, headers=LOCAL_HEADERS, timeout=CONFIG["LOCAL_TIMEOUT"], follow_redirects=True)
        if r.status_code >= 400:
            return results
        html = r.text
    except Exception:
        return results

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    parsed_base = urlparse(base_url)
    base_domain = get_clean_domain(parsed_base.netloc)
    homepage_clean = base_url.rstrip("/")
    resp_url_clean = str(r.url).rstrip("/")

    candidates: List[Tuple[str, Dict[str, float]]] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(str(r.url), href)
        parsed_abs = urlparse(full)
        if get_clean_domain(parsed_abs.netloc) != base_domain:
            continue
        
        full_no_frag = parsed_abs._replace(fragment="").geturl().rstrip("/")
        if full_no_frag == homepage_clean or full_no_frag == resp_url_clean:
            continue

        if full in seen:
            continue
        seen.add(full)

        text = a.get_text(strip=True) or ""
        cat_scores = SemanticLinkClassifier.classify_link(href, text, category_heuristics)
        candidates.append((full, cat_scores))

    assigned_urls = set()

    for cat in categories_to_find:
        best_url = None
        best_score = 0.75
        for full, scores in candidates:
            if full in assigned_urls:
                continue
            sc = scores.get(cat, 0.0)
            if sc > best_score:
                best_score = sc
                best_url = full
        
        if best_url:
            results[cat] = best_url
            assigned_urls.add(best_url)

    return results

async def guess_single_candidate(client: httpx.AsyncClient, candidate: str, base_url: str) -> Optional[str]:
    try:
        r = await client.get(candidate, headers=LOCAL_HEADERS, timeout=CONFIG["LOCAL_TIMEOUT"], follow_redirects=True)
        if r.status_code == 200 and not await is_soft_404(r, base_url):
            return str(r.url)
    except Exception:
        pass
    return None

async def guess_category_path_parallel(client: httpx.AsyncClient, base_url: str, heuristics: List[Dict], cat_name: str = "") -> Optional[str]:
    candidates = [f"{base_url}/{h['slug']}" for h in heuristics[:3]]
    defaults = DEFAULT_CATEGORY_PATHS.get(cat_name, [])
    for d in defaults:
        cand = f"{base_url}/{d}"
        if cand not in candidates:
            candidates.append(cand)

    if not candidates:
        return None
    for cand in candidates:
        res = await guess_single_candidate(client, cand, base_url)
        if res:
            return res
    return None

async def find_all_links_locally_for_categories(
    client: httpx.AsyncClient,
    domain: str,
    category_heuristics: Dict[str, List[Dict]],
    categories: List[str]
) -> Dict[str, Optional[str]]:
    base = normalize_url(domain)
    results = {cat: None for cat in categories}
    if not base or not categories:
        return results

    try:
        async def _discover():
            ai_results = await find_links_with_semantic_ai(client, base, category_heuristics, categories)
            for cat in categories:
                if ai_results[cat]:
                    results[cat] = ai_results[cat]

            missing_categories = [cat for cat in categories if not results[cat]]
            if missing_categories:
                for cat in missing_categories:
                    path_url = await guess_category_path_parallel(client, base, category_heuristics.get(cat, []), cat)
                    if path_url:
                        results[cat] = path_url

            return results

        return await asyncio.wait_for(_discover(), timeout=CONFIG["DOMAIN_LOCAL_DISCOVERY_TIMEOUT"])
    except Exception:
        return results

# ----------------- TRACXN SCRAPER API ENDPOINTS -----------------------

@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=15)
async def create_scraper_job(session: aiohttp.ClientSession, url: str, token: str) -> str:
    payload = {
        "url": url,
        "configuration": {
            "mode": "render",
            "output": {"html": True, "markdown": True}
        }
    }
    url_endpoint = f"{CONFIG['BASE_URL']}/utils/web-scraper/fetch/url/bu-sr"
    headers = {"accessToken": token, "Content-Type": "application/json"}
    async with session.post(url_endpoint, json=payload, headers=headers, timeout=30) as response:
        response.raise_for_status()
        data = await response.json()
        job_id = data.get("jobId")
        if not job_id:
            raise ValueError(f"No jobId returned from API for URL: {url}")
        return job_id

@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=15)
async def get_job_status(session: aiohttp.ClientSession, job_id: str, token: str) -> Dict:
    url_endpoint = f"{CONFIG['BASE_URL']}/utils/web-scraper/fetch/url/track"
    payload = {"jobId": job_id}
    headers = {"accessToken": token, "Content-Type": "application/json"}
    async with session.post(url_endpoint, json=payload, headers=headers, timeout=30) as response:
        response.raise_for_status()
        return await response.json()

@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=15)
async def get_signed_url(session: aiohttp.ClientSession, s3_url: str, token: str) -> str:
    url_endpoint = f"{CONFIG['BASE_URL']}/utils/s3/signed-url"
    payload = {"url": s3_url}
    headers = {"accessToken": token, "Content-Type": "application/json"}
    async with session.post(url_endpoint, json=payload, headers=headers, timeout=30) as response:
        response.raise_for_status()
        data = await response.json()
        signed_url = data.get("url") or data.get("signedUrl")
        if not signed_url:
            raise ValueError(f"No signedUrl returned from API for S3 path: {s3_url}")
        return signed_url

@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=15)
async def fetch_html_content(session: aiohttp.ClientSession, signed_url: str) -> str:
    async with session.get(signed_url, timeout=30) as response:
        response.raise_for_status()
        return await response.text()

# ----------------- DATABASE & SUPABASE STORAGE ------------------------

@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=10)
async def init_supabase_table():
    try:
        sb = CONFIG["SUPABASE"]
        conn = await asyncpg.connect(
            user=sb["USER"], password=sb["PASSWORD"],
            host=sb["HOST"], port=sb["PORT"], database=sb["DATABASE"],
            timeout=10
        )
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS scraped_data (
                domain TEXT PRIMARY KEY,
                home_about_content TEXT,
                rest_content TEXT,
                pages TEXT
            );
        ''')
        await conn.execute('ALTER TABLE scraped_data ADD COLUMN IF NOT EXISTS rest_content TEXT;')
        await conn.execute('ALTER TABLE scraped_data ADD COLUMN IF NOT EXISTS pages TEXT;')
        await conn.close()
        logger.info("Supabase PostgreSQL table verified.")
    except Exception as e:
        logger.error(f"Failed to initialize Supabase table: {e}")
        raise

@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=10)
async def check_domain_in_supabase(domain: str) -> bool:
    try:
        sb = CONFIG["SUPABASE"]
        conn = await asyncpg.connect(
            user=sb["USER"], password=sb["PASSWORD"],
            host=sb["HOST"], port=sb["PORT"], database=sb["DATABASE"],
            timeout=5
        )
        escaped_domain = domain.replace("'", "''")
        query = f"""
            SELECT domain FROM scraped_data 
            WHERE domain = '{escaped_domain}' 
              AND (
                  (home_about_content IS NOT NULL AND length(trim(home_about_content)) > 20)
                  OR 
                  (rest_content IS NOT NULL AND length(trim(rest_content)) > 20)
              )
        """
        row = await conn.fetchrow(query)
        await conn.close()
        return row is not None
    except Exception as e:
        logger.error(f"Error checking domain {domain} in Supabase: {e}")
        return False

@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=10)
async def save_to_supabase(domain: str, home_about_content: str, rest_content: Optional[str], pages: str):
    clean_home = (home_about_content or "").strip()
    clean_rest = (rest_content or "").strip()

    if not clean_home and not clean_rest:
        logger.info(f"SUPABASE: Skipping save for {domain} because no content was scraped.")
        return

    try:
        sb = CONFIG["SUPABASE"]
        conn = await asyncpg.connect(
            user=sb["USER"], password=sb["PASSWORD"],
            host=sb["HOST"], port=sb["PORT"], database=sb["DATABASE"],
            timeout=10
        )
        escaped_domain = domain.replace("'", "''")
        escaped_home_about = clean_home.replace("'", "''")
        escaped_rest = f"'{clean_rest.replace('\'', '\'\'')}'" if clean_rest else "NULL"
        escaped_pages = (pages or "").replace("'", "''")
        
        query = f"""
            INSERT INTO scraped_data (domain, home_about_content, rest_content, pages)
            VALUES ('{escaped_domain}', '{escaped_home_about}', {escaped_rest}, '{escaped_pages}')
            ON CONFLICT (domain) 
            DO UPDATE SET 
                home_about_content = '{escaped_home_about}', 
                rest_content = {escaped_rest}, 
                pages = '{escaped_pages}';
        """
        await conn.execute(query)
        await conn.close()
        logger.info(f"Saved valid scraped data for {domain} to Supabase DB.")
    except Exception as e:
        logger.error(f"Failed to save {domain} to Supabase: {e}")

def is_valid_job_id(job_id: str) -> bool:
    if not job_id:
        return False
    val = job_id.strip()
    if val.startswith("bu-daily"):
        return True
    placeholders = {
        "creation failed", "exists in db", "failed", "timeout", "threshold reached", "reached threshold",
        "success", "n/a", "no about link", "no html output", "processing"
    }
    if val.lower() in placeholders:
        return False
    return len(val) >= 8 and " " not in val

# ----------------- DECOUPLED PIPELINE ARCHITECTURE --------------------

@dataclass
class DomainTask:
    row_idx: int
    domain: str
    token: str
    homepage_job_id: str = ""
    category_job_ids: Dict[str, str] = field(default_factory=dict)
    category_links: Dict[str, Optional[str]] = field(default_factory=dict)
    home_status: str = "PENDING"
    category_statuses: Dict[str, str] = field(default_factory=dict)
    home_s3: str = ""
    category_s3s: Dict[str, str] = field(default_factory=dict)
    home_content: str = ""
    category_contents: Dict[str, str] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)

created_tasks_registry: Dict[str, DomainTask] = {}
sheet_update_queue: asyncio.Queue = asyncio.Queue()

def task_needs_polling(task: DomainTask) -> bool:
    if task.home_status == "PROCESSING":
        return True
    for cat in ALL_CATEGORIES:
        if task.category_statuses.get(cat) == "PROCESSING":
            return True
    return False

TERMINAL_STATUSES = {
    "SUCCESS", "FAILED", "EXISTS IN DB", "TIMEOUT", "THRESHOLD REACHED",
    "REACHED THRESHOLD", "N/A", "NO ABOUT LINK", "CREATION FAILED"
}

def is_terminal_status(st: str) -> bool:
    if not st:
        return False
    return st.strip().upper() in TERMINAL_STATUSES

# 1. PARALLEL PRODUCER WITH LOCAL SEMANTIC AI CLASSIFIER
async def process_single_domain_producer(
    session: aiohttp.ClientSession,
    httpx_client: httpx.AsyncClient,
    domain_sem: asyncio.Semaphore,
    tracxn_sem: asyncio.Semaphore,
    local_sem: asyncio.Semaphore,
    category_heuristics: Dict[str, List[Dict]],
    row: List[str],
    idx: int
):
    domain = row[0].strip()
    if not domain:
        return None

    async with domain_sem:
        access_keys = CONFIG["ACCESS_KEYS"]
        token = access_keys[idx % len(access_keys)]
        
        existing_home_job_id = row[2].strip() if len(row) > 2 else ""
        existing_about_job_id = row[3].strip() if len(row) > 3 else ""
        existing_about_link = row[4].strip() if len(row) > 4 else ""
        existing_home_s3 = row[5].strip() if len(row) > 5 else ""
        existing_home_content = row[6].strip() if len(row) > 6 else ""
        existing_about_s3 = row[7].strip() if len(row) > 7 else ""
        existing_about_content = row[8].strip() if len(row) > 8 else ""
        existing_home_status = row[9].strip() if len(row) > 9 else ""
        existing_about_status = row[10].strip() if len(row) > 10 else ""

        existing_cat_job_ids = {}
        existing_cat_links = {}
        existing_cat_s3s = {}
        existing_cat_contents = {}
        existing_cat_statuses = {}

        existing_cat_job_ids["About"] = existing_about_job_id
        existing_cat_links["About"] = existing_about_link if existing_about_link else None
        existing_cat_s3s["About"] = existing_about_s3
        existing_cat_contents["About"] = existing_about_content
        existing_cat_statuses["About"] = existing_about_status

        curr = 13
        for cat in OTHER_PAGES:
            j_id = row[curr].strip() if len(row) > curr else ""
            lnk = row[curr+1].strip() if len(row) > curr+1 else ""
            s3 = row[curr+2].strip() if len(row) > curr+2 else ""
            cnt = row[curr+3].strip() if len(row) > curr+3 else ""
            st = row[curr+4].strip() if len(row) > curr+4 else ""

            existing_cat_job_ids[cat] = j_id
            existing_cat_links[cat] = lnk if lnk else None
            existing_cat_s3s[cat] = s3
            existing_cat_contents[cat] = cnt
            existing_cat_statuses[cat] = st
            curr += 5

        home_is_terminal = is_terminal_status(existing_home_status)
        all_cats_terminal = all(is_terminal_status(existing_cat_statuses.get(cat, "")) for cat in ALL_CATEGORIES)

        if home_is_terminal and all_cats_terminal:
            logger.info(f"PRODUCER: Domain {domain} is already fully processed in sheet ({existing_home_status}). Skipping.")
            return None

        if await check_domain_in_supabase(domain):
            logger.info(f"PRODUCER: Domain {domain} already exists in DB with valid content. Skipping.")
            task = DomainTask(
                row_idx=idx, domain=domain, token=token,
                home_status="EXISTS IN DB",
                category_statuses={cat: "N/A" for cat in ALL_CATEGORIES}
            )
            await sheet_update_queue.put(task)
            return task

        task = DomainTask(
            row_idx=idx,
            domain=domain,
            token=token,
            homepage_job_id=existing_home_job_id if is_valid_job_id(existing_home_job_id) else "",
            category_job_ids={cat: j for cat, j in existing_cat_job_ids.items() if is_valid_job_id(j)},
            category_links=existing_cat_links,
            home_status=existing_home_status if existing_home_status else "PENDING",
            category_statuses=existing_cat_statuses,
            home_s3=existing_home_s3,
            category_s3s=existing_cat_s3s,
            home_content=existing_home_content,
            category_contents=existing_cat_contents
        )

        need_discovery = any(not task.category_links.get(cat) and not is_terminal_status(task.category_statuses.get(cat, "")) for cat in ALL_CATEGORIES)

        if need_discovery:
            logger.info(f"PRODUCER (Semantic AI): Classifying subpage links for {domain}...")
            async with local_sem:
                detected_links = await find_all_links_locally_for_categories(
                    httpx_client, domain, category_heuristics, ALL_CATEGORIES
                )
            for cat, lnk in detected_links.items():
                if not task.category_links.get(cat):
                    task.category_links[cat] = lnk

        creation_tasks = []

        if not task.homepage_job_id and not is_terminal_status(task.home_status):
            async def submit_home():
                async with tracxn_sem:
                    try:
                        task.homepage_job_id = await create_scraper_job(session, normalize_url(domain), token)
                        task.home_status = "PROCESSING"
                        logger.info(f"PRODUCER: Generated homepage Job ID ({task.homepage_job_id}) for {domain}")
                    except Exception as e:
                        logger.error(f"PRODUCER: Failed homepage job for {domain}: {e}")
                        task.homepage_job_id = "CREATION FAILED"
                        task.home_status = "FAILED"
            creation_tasks.append(submit_home())

        for cat in ALL_CATEGORIES:
            cat_st = task.category_statuses.get(cat, "")
            cat_j_id = task.category_job_ids.get(cat, "")
            cat_url = task.category_links.get(cat)

            if not cat_j_id and not is_terminal_status(cat_st):
                if cat_url and cat_url.startswith("http"):
                    async def submit_cat(c=cat, u=cat_url):
                        async with tracxn_sem:
                            try:
                                j_id = await create_scraper_job(session, u, token)
                                task.category_job_ids[c] = j_id
                                task.category_statuses[c] = "PROCESSING"
                                logger.info(f"PRODUCER: Generated {c} Job ID ({j_id}) for {domain}")
                            except Exception as e:
                                logger.error(f"PRODUCER: Failed {c} job for {domain}: {e}")
                                task.category_job_ids[c] = "FAILED"
                                task.category_statuses[c] = "FAILED"
                    creation_tasks.append(submit_cat(cat, cat_url))
                else:
                    task.category_job_ids[cat] = "No About Link" if cat == "About" else "N/A"
                    task.category_statuses[cat] = "N/A"

        if creation_tasks:
            await asyncio.gather(*creation_tasks)

        if task_needs_polling(task):
            async with registry_lock:
                created_tasks_registry[domain] = task
            await sheet_update_queue.put(task)
            logger.info(f"REAL-TIME PUSH: Streamed Job IDs / status for {domain} to Google Sheet queue.")

        return task

async def run_batch_pipeline(
    session: aiohttp.ClientSession,
    httpx_client: httpx.AsyncClient,
    rows: List[List[str]],
    category_heuristics: Dict[str, List[Dict]],
    tracxn_sem: asyncio.Semaphore,
    local_sem: asyncio.Semaphore,
    start_row: int = 2
):
    total_rows = len(rows) - 1
    if total_rows <= 0:
        logger.info("No domain rows to process.")
        return

    domain_sem = asyncio.Semaphore(CONFIG["DOMAIN_CONCURRENCY"])
    next_row_idx = max(2, start_row)
    batch_num = 1
    processed_count = 0
    success_count = 0
    fail_count = 0

    update_progress_file(processed_count, total_rows, success_count, fail_count)

    while next_row_idx <= len(rows):
        if os.path.exists(STOP_FILE):
            logger.info("Stop requested (.stop_requested found). Terminating pipeline loop.")
            break

        batch_tasks: Dict[str, DomainTask] = {}
        gen_start_time = time.time()
        gen_window_sec = CONFIG["GENERATE_WINDOW_MINUTES"] * 60

        logger.info("\n=======================================================")
        logger.info(f"BATCH #{batch_num}: Starting Job ID Generation Phase (Max {CONFIG['GENERATE_WINDOW_MINUTES']} mins)...")
        logger.info(f"Starting at sheet row {next_row_idx} of {len(rows)}")
        logger.info("=======================================================\n")

        active_gen_tasks = set()

        while next_row_idx <= len(rows):
            if os.path.exists(STOP_FILE):
                break

            elapsed_gen = time.time() - gen_start_time
            if elapsed_gen >= gen_window_sec:
                logger.info(f"Generation window limit ({CONFIG['GENERATE_WINDOW_MINUTES']} mins) reached for Batch #{batch_num}.")
                break

            row = rows[next_row_idx - 1]
            idx = next_row_idx
            next_row_idx += 1

            domain_name = row[0].strip() if row else ""
            if not domain_name:
                continue

            async def _process_and_register(r=row, i=idx):
                nonlocal processed_count
                task = await process_single_domain_producer(
                    session, httpx_client, domain_sem, tracxn_sem, local_sem,
                    category_heuristics, r, i
                )
                processed_count += 1
                update_progress_file(processed_count, total_rows, success_count, fail_count)

                if task and task_needs_polling(task):
                    batch_tasks[task.domain] = task

            coro = asyncio.create_task(_process_and_register())
            active_gen_tasks.add(coro)
            coro.add_done_callback(active_gen_tasks.discard)

            if len(active_gen_tasks) >= CONFIG["DOMAIN_CONCURRENCY"] * 2:
                await asyncio.sleep(0.1)

        if active_gen_tasks:
            await asyncio.gather(*active_gen_tasks)

        logger.info(f"BATCH #{batch_num}: Generation Phase complete! Generated/queued {len(batch_tasks)} active domains.")

        if not batch_tasks:
            if next_row_idx > len(rows):
                logger.info("No more domains remaining to process.")
                break
            continue

        logger.info("\n-------------------------------------------------------")
        logger.info(f"BATCH #{batch_num}: Starting Tracking Phase for {len(batch_tasks)} generated domains...")
        logger.info(f"Polling status every {CONFIG['POLL_INTERVAL_SECONDS']}s for up to {CONFIG['MAX_BATCH_TRACKING_MINUTES']} mins...")
        logger.info("-------------------------------------------------------\n")

        track_start_time = time.time()
        max_track_sec = CONFIG["MAX_BATCH_TRACKING_MINUTES"] * 60
        sweep_count = 0

        while True:
            if os.path.exists(STOP_FILE):
                break

            active_polling_tasks = [t for t in batch_tasks.values() if task_needs_polling(t)]
            if not active_polling_tasks:
                logger.info(f"BATCH #{batch_num}: All {len(batch_tasks)} domain jobs in this batch reached terminal status!")
                break

            elapsed_track = time.time() - track_start_time
            if elapsed_track >= max_track_sec:
                logger.info(f"BATCH #{batch_num}: Tracking window limit ({CONFIG['MAX_BATCH_TRACKING_MINUTES']} mins) reached.")
                break

            sweep_count += 1
            logger.info(f"BATCH #{batch_num} Sweep #{sweep_count}: Polling {len(active_polling_tasks)} active domains...")

            sweep_sem = asyncio.Semaphore(50)

            async def _sweep_domain(task: DomainTask):
                nonlocal success_count, fail_count
                async with sweep_sem:
                    was_processing = task_needs_polling(task)
                    await check_single_task_jobs(session, task)
                    still_processing = task_needs_polling(task)

                    if was_processing and not still_processing:
                        if task.home_status == "SUCCESS" or any(s == "SUCCESS" for s in task.category_statuses.values()):
                            success_count += 1
                        else:
                            fail_count += 1
                        update_progress_file(processed_count, total_rows, success_count, fail_count)

                        home_about_text = (task.home_content or "") + "\n\n" + (task.category_contents.get("About") or "")
                        rest_parts = [task.category_contents[c] for c in OTHER_PAGES if task.category_contents.get(c)]
                        rest_text = "\n\n".join(rest_parts) if rest_parts else None
                        pages_val = "home,about," + ",".join([c.lower() for c in OTHER_PAGES if task.category_contents.get(c)])

                        await save_to_supabase(task.domain, home_about_text.strip(), rest_text, pages_val)
                        await sheet_update_queue.put(task)
                        logger.info(f"REAL-TIME PUSH: Streamed SUCCESS update for {task.domain} to Google Sheet queue.")

            await asyncio.gather(*[_sweep_domain(t) for t in active_polling_tasks])
            await asyncio.sleep(CONFIG["POLL_INTERVAL_SECONDS"])

        logger.info(f"BATCH #{batch_num} complete! Moving to next batch...\n")
        batch_num += 1

    await sheet_update_queue.put(None)

# 2. CYCLICAL BATCH POLLER WITH MAX JOB AGE SAFEGUARD
async def check_single_task_jobs(session: aiohttp.ClientSession, task: DomainTask):
    job_age = time.time() - task.start_time
    if job_age > CONFIG["MAX_JOB_AGE_SECONDS"]:
        logger.warning(f"POLLER SAFEGUARD: Job for {task.domain} exceeded max age ({round(job_age/60)} mins). Marking THRESHOLD REACHED.")
        if task.home_status == "PROCESSING":
            task.home_status = "THRESHOLD REACHED"
        for cat in ALL_CATEGORIES:
            if task.category_statuses.get(cat) == "PROCESSING":
                task.category_statuses[cat] = "THRESHOLD REACHED"
        return

    if is_valid_job_id(task.homepage_job_id) and task.home_status == "PROCESSING":
        try:
            res = await get_job_status(session, task.homepage_job_id, task.token)
            st = res.get("status", "").upper()
            if st in ["SUCCESS", "FAILED", "TIMEOUT"]:
                task.home_status = st
                if st == "SUCCESS" and res.get("output", {}).get("html"):
                    task.home_s3 = res["output"]["html"]
                    signed = await get_signed_url(session, task.home_s3, task.token)
                    html = await fetch_html_content(session, signed)
                    task.home_content = clean_html(html)
        except Exception as e:
            logger.warning(f"POLLER SWEEP: Error tracking homepage for {task.domain}: {e}")

    async def _check_cat(cat: str):
        j_id = task.category_job_ids.get(cat, "")
        if is_valid_job_id(j_id) and task.category_statuses.get(cat) == "PROCESSING":
            try:
                res = await get_job_status(session, j_id, task.token)
                st = res.get("status", "").upper()
                if st in ["SUCCESS", "FAILED", "TIMEOUT"]:
                    task.category_statuses[cat] = st
                    if st == "SUCCESS" and res.get("output", {}).get("html"):
                        task.category_s3s[cat] = res["output"]["html"]
                        signed = await get_signed_url(session, task.category_s3s[cat], task.token)
                        html = await fetch_html_content(session, signed)
                        task.category_contents[cat] = clean_html(html)
            except Exception as e:
                logger.warning(f"POLLER SWEEP: Error tracking {cat} for {task.domain}: {e}")

    cat_tasks = [_check_cat(cat) for cat in ALL_CATEGORIES if is_valid_job_id(task.category_job_ids.get(cat, ""))]
    if cat_tasks:
        await asyncio.gather(*cat_tasks)

# 3. REAL-TIME SHEET STREAMING SYNCER
async def sheet_syncer(db_ws):
    logger.info("REAL-TIME SHEET SYNCER: Starting live streaming spreadsheet updater...")

    while True:
        task: Optional[DomainTask] = await sheet_update_queue.get()
        if task is None:
            sheet_update_queue.task_done()
            break

        async with sheet_lock:
            row_vals = [""] * 58
            row_vals[0] = task.domain
            row_vals[1] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row_vals[2] = task.homepage_job_id
            row_vals[3] = task.category_job_ids.get("About", "")
            row_vals[4] = task.category_links.get("About") or "No About Link"
            row_vals[5] = task.home_s3
            row_vals[6] = task.home_content[:20000] if task.home_content else ""
            row_vals[7] = task.category_s3s.get("About", "")
            row_vals[8] = task.category_contents.get("About", "")[:20000] if task.category_contents.get("About") else ""
            row_vals[9] = task.home_status
            row_vals[10] = task.category_statuses.get("About", "N/A")
            row_vals[11] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row_vals[12] = str(round(time.time() - task.start_time, 2))

            curr = 13
            for cat in OTHER_PAGES:
                row_vals[curr] = task.category_job_ids.get(cat, "")
                row_vals[curr+1] = task.category_links.get(cat) or "N/A"
                row_vals[curr+2] = task.category_s3s.get(cat, "")
                row_vals[curr+3] = task.category_contents.get(cat, "")[:20000] if task.category_contents.get(cat) else ""
                row_vals[curr+4] = task.category_statuses.get(cat, "N/A")
                curr += 5

            try:
                await db_ws.update(range_name=f"B{task.row_idx}:BF{task.row_idx}", values=[row_vals[1:]])
                logger.info(f"REAL-TIME SHEET SYNC: Row {task.row_idx} ({task.domain}) written to Google Sheet.")
            except Exception as e:
                logger.error(f"SHEET SYNCER: Failed row update for {task.domain}: {e}")

        sheet_update_queue.task_done()
        await asyncio.sleep(0.1)

    logger.info("REAL-TIME SHEET SYNCER: All live updates written to Google Sheet.")

async def safe_get_all_values(ws):
    if hasattr(ws, "get_all_values"):
        res = ws.get_all_values()
        if inspect.isawaitable(res):
            return await res
        return res
    elif hasattr(ws, "get_all_records"):
        res = ws.get_all_records()
        if inspect.isawaitable(res):
            records = await res
        else:
            records = res
        return [list(r.values()) for r in records]
    raise AttributeError("'Worksheet' object has no attribute 'get_all_values'")

# --------------------------- MAIN ENTRY -------------------------------

async def main():
    parser = argparse.ArgumentParser(description="AI Fast Crawler for TechCrawler")
    parser.add_argument("start_row", nargs="?", type=int, default=2, help="Row index to start processing from")
    parser.add_argument("mode", nargs="?", type=str, default="full", help="Processing mode")
    parser.add_argument("--sheet_id", type=str, default=None, help="Google Sheet ID to override default")
    args = parser.parse_args()

    sheet_id = args.sheet_id or CONFIG["SHEET_ID"]
    start_row = args.start_row if args.start_row >= 2 else 2

    logger.info(f"Initializing TechCrawler with Sheet ID: {sheet_id}, Start Row: {start_row}")

    client_manager = AsyncGoogleSheetsClient()
    gc = await client_manager.authorize()
    sheet = await gc.open_by_key(sheet_id)

    await init_supabase_table()

    paths_ws = await sheet.worksheet("paths")
    all_values = await safe_get_all_values(paths_ws)
    category_heuristics = {}
    for row in all_values:
        if not row:
            continue
        header_clean = row[0].strip()
        if header_clean in PATH_HEADER_TO_CAT:
            cat_name = PATH_HEADER_TO_CAT[header_clean]
            keywords = [val for val in row[1:] if val.strip()]
            category_heuristics[cat_name] = sanitize_category_keywords(keywords)

    for cat in PATH_HEADER_TO_CAT.values():
        if cat not in category_heuristics:
            category_heuristics[cat] = []

    db_ws = await sheet.worksheet("DB")
    rows = await safe_get_all_values(db_ws)
    if not rows:
        logger.error("DB Worksheet is empty.")
        return

    tracxn_sem = asyncio.Semaphore(CONFIG["MAX_CONCURRENT_REQUESTS"])
    local_sem = asyncio.Semaphore(CONFIG["LOCAL_FETCH_CONCURRENCY"])

    limits = httpx.Limits(
        max_connections=CONFIG["LOCAL_FETCH_CONCURRENCY"] * 2,
        max_keepalive_connections=CONFIG["LOCAL_FETCH_CONCURRENCY"]
    )

    async with aiohttp.ClientSession() as session:
        async with httpx.AsyncClient(limits=limits, verify=False) as httpx_client:
            logger.info("Launching AI Fast Crawler pipeline...")
            await asyncio.gather(
                run_batch_pipeline(session, httpx_client, rows, category_heuristics, tracxn_sem, local_sem, start_row=start_row),
                sheet_syncer(db_ws)
            )

    logger.info("Entire AI Fast Crawler pipeline completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
