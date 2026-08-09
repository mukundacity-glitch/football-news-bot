import asyncio
import re
import html
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
from src.constants import (
    JOURNALISTS, 
    NITTER_INSTANCES, 
    FOOTBALL_KW, 
    STAFF_BLOCK_KW, 
    OFFICIAL_ACCOUNTS, 
    ELITE_TRUSTED, 
    TRUSTED_MEDIA
)

# ── TWIKIT CLIENT TRANSACTION PATCH ─────────────────────────────────────
# Workaround for responsive-web client state tracking (Issue #408)
try:
    _tx_mod = __import__(
        "twikit.x_client_transaction.transaction", fromlist=["ClientTransaction"]
    )
except Exception:
    _tx_mod = None

if _tx_mod is not None:
    _tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
        r""",(\d+):["']ondemand\.s["']""", flags=(re.VERBOSE | re.MULTILINE)
    )
    _tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'
    _tx_mod.INDICES_REGEX = re.compile(
        r"""(\(\w{1,2}\[(\d{1,2})\],\s*16\))+""", flags=(re.VERBOSE | re.MULTILINE)
    )

    async def _patched_get_indices(self, home_page_response, session, headers):
        key_byte_indices = []
        response = self.validate_response(home_page_response) or self.home_page_response
        response_str = str(response)

        on_demand_file = _tx_mod.ON_DEMAND_FILE_REGEX.search(response_str)
        if on_demand_file:
            on_demand_file_index = on_demand_file.group(1)
            hash_regex = re.compile(_tx_mod.ON_DEMAND_HASH_PATTERN.format(on_demand_file_index))
            hash_match = hash_regex.search(response_str)
            if hash_match:
                filename = hash_match.group(1)
                on_demand_file_url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{filename}a.js"
                on_demand_file_response = await session.request(method="GET", url=on_demand_file_url, headers=headers)
                key_byte_indices_match = _tx_mod.INDICES_REGEX.finditer(str(on_demand_file_response.text))
                for item in key_byte_indices_match:
                    key_byte_indices.append(item.group(2))

        if not key_byte_indices:
            raise Exception("Couldn't get KEY_BYTE indices")
        key_byte_indices = list(map(int, key_byte_indices))
        return key_byte_indices[0], key_byte_indices[1:]

    _tx_mod.ClientTransaction.get_indices = _patched_get_indices


# ── SOURCE VERIFICATION ENGINE ──────────────────────────────────────────
def source_tier(handle: str) -> int:
    """Evaluates the verification tier of an incoming news source dynamically."""
    h = (handle or "").lower().lstrip("@")
    if h in OFFICIAL_ACCOUNTS: 
        return 1
    if h in ELITE_TRUSTED: 
        return 2
    if h in TRUSTED_MEDIA: 
        return 3
    return 0


def _parse_tweet_date(raw) -> Optional[datetime]:
    if not raw: 
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    
    s = str(raw).strip()
    s_norm = re.sub(r'\b(GMT|UTC)\b', '+0000', s)
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z", 
        "%a %b %d %H:%M:%S %z %Y", 
        "%Y-%m-%dT%H:%M:%S%z", 
        "%Y-%m-%d %H:%M:%S%z"
    ):
        try:
            dt = datetime.strptime(s_norm, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def tweet_too_old(created_at, max_days=3) -> bool:
    """Prevents stale timeline data from entering the queue."""
    dt = _parse_tweet_date(created_at)
    if dt is None: 
        return False
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() > max_days * 86400


def clean_source_text(raw_text: str) -> str:
    """Sanitizes scraped HTML entities while preserving complete punctuation and context."""
    if not raw_text:
        return ""
    # Unescape HTML entities (&amp; -> &, &gt; -> >, etc.)
    text = html.unescape(raw_text)
    # Strip HTML tags but preserve internal text
    text = re.sub(r'<[^>]+>', ' ', text)
    # Collapse multiple whitespaces without altering case or punctuation
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def sanitize_payload(
    tweet_id: str, 
    raw_text: str, 
    author_handle: str, 
    created_at: Any, 
    media_url: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    FAIL CLOSED VALIDATOR:
    Ensures every scraped item possesses the required lineage and contextual evidence 
    before passing into processing pipelines. Drops incomplete payloads.
    """
    clean_txt = clean_source_text(raw_text)
    clean_handle = (author_handle or "").lower().lstrip("@")
    
    # Fail closed on missing text or missing ID
    if not tweet_id or not clean_txt or len(clean_txt) < 10:
        return None

    dt = _parse_tweet_date(created_at)
    tier = source_tier(clean_handle)
    
    return {
        "id": str(tweet_id),
        "text": clean_txt,
        "author": clean_handle,
        "source_tier": tier,
        "source_url": f"https://x.com/{clean_handle}/status/{tweet_id}" if clean_handle else "",
        "media_url": media_url,
        "created_at": dt.isoformat() if dt else str(created_at or "")
    }


# ── ASYNC INGESTION LOOPS ───────────────────────────────────────────────
def get_nitter_tweets(username: str) -> List[Dict[str, Any]]:
    """Fallback RSS parsing loop over active Nitter nodes with contextual payload validation."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RSS reader)"}
    clean_user = username.lower().lstrip("@")
    
    for inst in NITTER_INSTANCES:
        try:
            r = requests.get(f"{inst}/{clean_user}/rss", headers=headers, timeout=10)
            if r.status_code != 200: 
                continue
            root = ET.fromstring(r.content)
            out = []
            for it in root.findall(".//item")[:8]:
                link = it.find("link")
                desc = it.find("description")
                if link is None or link.text is None: 
                    continue
                    
                tid = link.text.strip().split("/")[-1].split("#")[0]
                desc_text = desc.text if desc is not None and desc.text else ""
                pub = it.find("pubDate")
                created_at = pub.text.strip() if pub is not None and pub.text else None

                media_url = None
                img_match = re.search(r'<img[^>]+src="([^">]+)"', desc_text)
                if img_match:
                    media_url = img_match.group(1)
                    if media_url.startswith("/"):
                        media_url = f"{inst}{media_url}"

                payload = sanitize_payload(
                    tweet_id=tid,
                    raw_text=desc_text,
                    author_handle=clean_user,
                    created_at=created_at,
                    media_url=media_url
                )
                
                if payload and not tweet_too_old(payload["created_at"]):
                    out.append(payload)

            if out: 
                return out
        except Exception: 
            continue
    return []


async def get_twikit_tweets(read_client, username: str, count=20, retries=2) -> List[Dict[str, Any]]:
    """Primary asynchronous extraction thread utilizing Twikit session with payload verification."""
    if read_client is None: 
        return []
    
    clean_user = username.lower().lstrip("@")
    
    for attempt in range(retries):
        try:
            user = await read_client.get_user_by_screen_name(clean_user)
            tweets = await read_client.get_user_tweets(user.id, "Tweets", count=count)
            out = []
            for t in tweets:
                txt = getattr(t, "full_text", None) or getattr(t, "text", "") or ""
                tid = str(getattr(t, "id", "") or "")
                created_at = getattr(t, "created_at", None) or getattr(t, "created_at_datetime", None)

                media_url = None
                if hasattr(t, "media") and t.media:
                    for m in t.media:
                        m_type = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
                        if m_type == "photo":
                            media_url = getattr(m, "media_url_https", None) or (m.get("media_url_https") if isinstance(m, dict) else None)
                            if media_url: 
                                break

                payload = sanitize_payload(
                    tweet_id=tid,
                    raw_text=txt,
                    author_handle=clean_user,
                    created_at=created_at,
                    media_url=media_url
                )

                if payload and not tweet_too_old(payload["created_at"]):
                    out.append(payload)

            return out
        except Exception as e:
            if attempt + 1 < retries: 
                await asyncio.sleep(3 * (attempt + 1))
            else: 
                print(f"  [READ ERROR] twikit failed for @{clean_user}: {e}")
    return []


async def fetch_tweets(read_client, username: str) -> Tuple[List[Dict[str, Any]], str]:
    """Orchestrates primary Twikit retrieval with a transparent failover to Nitter RSS feeds."""
    tweets = await get_twikit_tweets(read_client, username)
    if tweets: 
        return tweets, "twikit"
    
    nit = get_nitter_tweets(username)
    return nit, ("nitter" if nit else "none")
