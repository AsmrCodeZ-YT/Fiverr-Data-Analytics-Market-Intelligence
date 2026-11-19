#!/usr/bin/env python3
import argparse
import asyncio
import csv
import hashlib
import json
import math
import random
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

DATA_MARKER = 'id="perseus-initial-props"'
BLOCK_MARKERS = (
    "px-captcha",
    "Press & Hold",
    "Access to this page has been denied",
    "Please verify you are a human",
)
LEVELS = {
    "LEVEL_1": "Level 1",
    "LEVEL_2": "Level 2",
    "LEVEL_TRS": "Top Rated Seller",
    "NEW_SELLER": "New Seller",
    "NO_LEVEL": "No level",
}
TIERS = {1: "basic", 2: "standard", 3: "premium"}
REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


class ScrapeError(Exception):
    pass


def get(data, *path, default=None):
    cur = data
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def cents_to_usd(value):
    if value is None:
        return None
    return round(value / 100, 2)


def iso_from_seconds(value):
    if not value:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def iso_from_millis(value):
    if not value:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def html_to_text(html):
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for ol in soup.find_all("ol"):
        for index, li in enumerate(ol.find_all("li", recursive=False), 1):
            li.insert(0, f"{index}. ")
    for ul in soup.find_all("ul"):
        for li in ul.find_all("li", recursive=False):
            li.insert(0, "- ")
    for el in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"]):
        el.append("\n")
    text = soup.get_text("")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def validate_url(url):
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if parsed.scheme not in ("http", "https") or not (host == "fiverr.com" or host.endswith(".fiverr.com")):
        raise ScrapeError(f"Not a Fiverr URL: {url}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ScrapeError("URL must look like https://www.fiverr.com/<username>/<gig-slug>")
    return f"https://www.fiverr.com{parsed.path}"


def has_data(html):
    return DATA_MARKER in html


def looks_blocked(html):
    return any(marker in html for marker in BLOCK_MARKERS)


def system_chrome_available():
    return any(
        shutil.which(name)
        for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser")
    )


def first_error_line(exc):
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else type(exc).__name__


class RetryableError(ScrapeError):
    pass


class BlockedError(RetryableError):
    pass


BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
DATA_READY_JS = """
() => {
    const el = document.getElementById('perseus-initial-props');
    if (!el || !el.textContent) return false;
    try { JSON.parse(el.textContent); return true; } catch (e) { return false; }
}
"""
SLIM_JS = """
() => Array.from(
    document.querySelectorAll('script#perseus-initial-props, script[type="application/ld+json"]')
).map(el => el.outerHTML).join('\\n')
"""
SLIM_PATTERNS = (
    re.compile(r'<script[^>]*\bid="perseus-initial-props"[^>]*>.*?</script>', re.S),
    re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>.*?</script>', re.S),
)
LISTING_ROOTS = ("search", "categories")


def slim_html(html):
    parts = []
    for pattern in SLIM_PATTERNS:
        parts.extend(pattern.findall(html))
    return "<html><body>" + "\n".join(parts) + "</body></html>"


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_listing_url(url):
    parts = [p for p in urlparse(url.strip()).path.split("/") if p]
    return bool(parts) and parts[0] in LISTING_ROOTS


def validate_listing_url(url):
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if parsed.scheme not in ("http", "https") or not (host == "fiverr.com" or host.endswith(".fiverr.com")):
        raise ScrapeError(f"Not a Fiverr URL: {url}")
    return urlunparse(("https", "www.fiverr.com", parsed.path, "", parsed.query, ""))


def page_url(listing_url, page):
    parsed = urlparse(listing_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in ("page", "offset")
    ]
    if page > 1:
        query.append(("page", str(page)))
    return urlunparse(parsed._replace(query=urlencode(query)))


async def launch_browser_async(pw, profile_dir, headed):
    options = {
        "headless": not headed,
        "locale": "en-US",
        "viewport": {"width": 1366, "height": 900},
    }
    try:
        return await pw.chromium.launch_persistent_context(str(profile_dir), **options)
    except Exception:
        if not system_chrome_available():
            raise
        options["channel"] = "chrome"
        return await pw.chromium.launch_persistent_context(str(profile_dir), **options)


class CurlFetcher:
    name = "curl_cffi"

    def __init__(self, args):
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            raise ScrapeError("curl_cffi is not installed (pip install curl_cffi)")
        self._requests = curl_requests
        self._local = threading.local()
        self._executor = ThreadPoolExecutor(max_workers=args.concurrency)
        self._timeout = args.timeout

    async def start(self):
        return None

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._requests.Session(impersonate="chrome")
            self._local.session = session
        return session

    def _request_sync(self, url, headers=None):
        try:
            response = self._session().get(
                url,
                headers=headers or REQUEST_HEADERS,
                timeout=self._timeout,
            )
        except Exception as exc:
            raise RetryableError(f"curl_cffi request failed: {first_error_line(exc)}")
        status = response.status_code
        if status in (403, 429):
            raise BlockedError(f"HTTP {status} from Fiverr")
        if status in (404, 410):
            raise ScrapeError(f"HTTP {status} from Fiverr")
        if status != 200:
            raise RetryableError(f"HTTP {status} from Fiverr")
        return response.text

    def _fetch_sync(self, url):
        text = self._request_sync(url)
        slim = slim_html(text)
        if not has_data(slim):
            if looks_blocked(text):
                raise BlockedError("Received bot protection page")
            raise RetryableError("Received page without gig data")
        return slim

    async def fetch(self, url, patience=None):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._fetch_sync, url)

    async def fetch_json(self, url):
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(
            self._executor,
            lambda: self._request_sync(url, headers={**REQUEST_HEADERS, "Accept": "application/json,*/*"}),
        )
        if looks_blocked(text):
            raise BlockedError("Received bot protection page")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RetryableError(f"Received non-JSON response: {first_error_line(exc)}")

    async def close(self):
        self._executor.shutdown(wait=False)


class BrowserFetcher:
    name = "playwright"

    def __init__(self, args):
        self._headed = args.headed
        self._profile = args.profile
        self._timeout = args.timeout
        self._block_assets = not args.keep_assets
        self._pw = None
        self._context = None

    async def start(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ScrapeError("playwright is not installed (pip install playwright && playwright install chromium)")
        self._pw = await async_playwright().start()
        try:
            self._context = await launch_browser_async(self._pw, self._profile, self._headed)
        except Exception as exc:
            await self._pw.stop()
            raise ScrapeError(
                "Playwright could not launch a browser "
                f"({first_error_line(exc)}). "
                "Run: playwright install chromium  (or install Google Chrome)"
            )
        if self._block_assets:
            await self._context.route("**/*", self._route)

    async def _route(self, route):
        try:
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass

    async def _page_is_blocked(self, page):
        try:
            return looks_blocked(await page.content())
        except Exception:
            return False

    async def fetch(self, url, patience=None):
        patience = patience or self._timeout
        page = await self._context.new_page()
        try:
            response = await page.goto(url, wait_until="commit", timeout=patience * 1000)
            status = response.status if response else None
            if status in (404, 410):
                raise ScrapeError(f"HTTP {status} from Fiverr")
            if status in (403, 429) and not self._headed:
                raise BlockedError(f"HTTP {status} from Fiverr")
            if self._headed and status in (403, 429):
                print("Verification challenge detected: solve it in the browser window.", file=sys.stderr, flush=True)
            await page.wait_for_function(DATA_READY_JS, timeout=patience * 1000, polling=100)
            return slim_html(await page.evaluate(SLIM_JS))
        except ScrapeError:
            raise
        except Exception as exc:
            if await self._page_is_blocked(page):
                raise BlockedError("Browser received bot protection page")
            raise RetryableError(f"Browser could not load the page: {first_error_line(exc)}")
        finally:
            try:
                await page.evaluate("window.stop()")
            except Exception:
                pass
            try:
                await asyncio.wait_for(page.close(), 10)
            except Exception:
                pass

    async def fetch_json(self, url):
        page = await self._context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout * 1000)
            status = response.status if response else None
            if status in (403, 429):
                raise BlockedError(f"HTTP {status} from Fiverr")
            if status in (404, 410):
                raise ScrapeError(f"HTTP {status} from Fiverr")
            if status and status != 200:
                raise RetryableError(f"HTTP {status} from Fiverr")
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if not text.strip():
                text = await page.content()
            if looks_blocked(text):
                raise BlockedError("Received bot protection page")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RetryableError(f"Received non-JSON response: {first_error_line(exc)}")
        except ScrapeError:
            raise
        except Exception as exc:
            raise RetryableError(f"Browser could not load JSON: {first_error_line(exc)}")
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def close(self):
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass


class BlockMonitor:
    def __init__(self):
        self.aborted = False

    def blocked(self):
        self.aborted = True


class RateLimiter:
    def __init__(self, interval):
        self._interval = interval
        self._next = 0.0

    async def wait(self):
        if self._interval <= 0:
            return
        now = time.monotonic()
        slot = max(now, self._next)
        self._next = slot + self._interval * random.uniform(0.6, 1.4)
        if slot > now:
            await asyncio.sleep(slot - now)


class Runtime:
    def __init__(self, args):
        self.retries = args.retries
        self.sem = asyncio.Semaphore(args.concurrency)
        self.limiter = RateLimiter(args.delay)
        self.monitor = BlockMonitor()
        self.first_patience = args.wait if args.headed else None

    def take_first_patience(self):
        value, self.first_patience = self.first_patience, None
        return value


async def fetch_with_retries(fetcher, url, runtime, patience=None, json_mode=False):
    last = None
    attempts = runtime.retries + 1
    for attempt in range(attempts):
        if runtime.monitor.aborted:
            raise BlockedError("Stopped after Fiverr blocked access")
        await runtime.limiter.wait()
        if runtime.monitor.aborted:
            raise BlockedError("Stopped after Fiverr blocked access")
        try:
            result = await (fetcher.fetch_json(url) if json_mode else fetcher.fetch(url, patience))
            return result
        except BlockedError:
            runtime.monitor.blocked()
            raise
        except RetryableError as exc:
            last = exc
            delay = 1 + attempt * 2 + random.random()
        if attempt < runtime.retries:
            await asyncio.sleep(delay)
    raise last


def load_props(soup):
    tag = soup.find("script", id="perseus-initial-props")
    if tag is None:
        raise ScrapeError("Gig data block (perseus-initial-props) not found in the HTML")
    try:
        return json.loads(tag.get_text())
    except json.JSONDecodeError as exc:
        raise ScrapeError(f"Gig data block is not valid JSON: {exc}")


def load_ld_json(soup):
    result = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.get_text())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            result[data.get("@type", "unknown")] = data
    return result


def parse_gig(props, ld):
    general = props.get("general") or {}
    overview_gig = get(props, "overview", "gig", default={})
    category = get(props, "overview", "categories", default={})
    ai_summary = props.get("aiSummary") or {}
    offers = get(ld, "Product", "offers", default={})
    return {
        "id": general.get("gigId"),
        "encrypted_id": general.get("encryptedGigId"),
        "title": general.get("gigTitle") or overview_gig.get("title"),
        "status": general.get("gigStatus"),
        "slug": get(props, "portfolio", "slug"),
        "category": {
            "id": general.get("categoryId"),
            "name": general.get("categoryName"),
            "slug": general.get("categorySlug"),
        },
        "sub_category": {
            "id": general.get("subCategoryId"),
            "name": general.get("subCategoryName"),
            "slug": general.get("subCategorySlug"),
        },
        "nested_sub_category": get(category, "nestedSubCategory"),
        "rating": overview_gig.get("rating"),
        "ratings_count": overview_gig.get("ratingsCount"),
        "orders_in_queue": overview_gig.get("ordersInQueue"),
        "saved_by_count": get(props, "topNav", "gigCollectedCount"),
        "is_pro": general.get("isPro"),
        "is_handpicked": general.get("isHandpicked"),
        "is_studio": general.get("isStudio"),
        "seller_on_vacation": general.get("isOnVacation"),
        "description_text": html_to_text(get(props, "description", "content", default="")),
        "description_html": get(props, "description", "content"),
        "metadata": {
            attr.get("name"): [opt.get("label") for opt in attr.get("options") or []]
            for attr in get(props, "description", "metadataAttributes", default=[])
        },
        "tags": [tag.get("name") for tag in get(props, "tags", "tagsGigList", default=[])],
        "price_range_usd": {
            "low": to_float(offers.get("lowPrice")),
            "high": to_float(offers.get("highPrice")),
            "currency": offers.get("priceCurrency"),
            "offer_count": offers.get("offerCount"),
            "availability": offers.get("availability"),
        },
        "ai_summary": ai_summary.get("summary") or [],
        "allow_custom_orders": get(props, "customOrder", "allowCustomOrders"),
        "faq": [
            {"question": item.get("question"), "answer": item.get("answer")}
            for item in get(props, "faq", "questionsAndAnswers", default=[])
        ],
    }


def parse_packages(props):
    packages = []
    for pk in get(props, "packages", "packageList", default=[]):
        features = []
        for f in pk.get("features") or []:
            is_boolean = f.get("type") == "BOOLEAN"
            features.append({
                "name": f.get("name"),
                "label": f.get("label"),
                "type": f.get("type"),
                "included": f.get("included"),
                "value": f.get("included") if is_boolean else f.get("value"),
                "extra_price_usd": cents_to_usd(f.get("price")),
                "extra_delivery_hours": f.get("duration"),
                "is_popular": f.get("isPopular"),
            })
        hours = pk.get("duration")
        packages.append({
            "tier": TIERS.get(pk.get("id")),
            "title": pk.get("title"),
            "description": pk.get("description"),
            "price_usd": cents_to_usd(pk.get("price")),
            "delivery_hours": hours,
            "delivery_days": round(hours / 24, 2) if hours else None,
            "revisions": get(pk, "revisions", "value"),
            "extra_revision_price_usd": cents_to_usd(get(pk, "revisions", "price")),
            "extra_fast_delivery": pk.get("extraFast"),
            "features": features,
            "custom_extras": [
                {
                    "label": x.get("label"),
                    "description": x.get("buyerTooltip"),
                    "price_usd": cents_to_usd(x.get("price")),
                    "delivery_hours": x.get("duration"),
                }
                for x in pk.get("customExtras") or []
            ],
        })
    return packages


def parse_recurring(props):
    return [
        {
            "discount_percent": item.get("discountPercentage"),
            "duration": item.get("duration"),
            "unit": str(item.get("durationTimeUnit", "")).replace("time_unit_", ""),
        }
        for item in get(props, "packages", "recurringOptions", default=[])
    ]


def parse_consultation(props):
    data = props.get("consultationData") or {}
    if not data:
        return None
    return {
        "description": data.get("description"),
        "pricing": [
            {
                "session_minutes": p.get("session_minutes"),
                "price": p.get("price"),
                "status": p.get("status"),
            }
            for p in data.get("pricing") or []
        ],
    }


def parse_gallery(props):
    images, videos, documents = [], [], []
    for item in get(props, "gallery", "slides", default=[]):
        slide = item.get("slide", item)
        common = {
            "name": slide.get("name"),
            "is_work_sample": slide.get("isWorkSample"),
            "thumbnail": slide.get("thumbnail"),
        }
        if slide.get("typeVideo"):
            videos.append({
                **common,
                "url": slide.get("videoSrc") or slide.get("src"),
                "poster": get(slide, "media", "original"),
                "encrypted_order_id": slide.get("encryptedOrderId"),
            })
        elif slide.get("typePdf"):
            documents.append({
                **common,
                "url": slide.get("src") or slide.get("pdfSrc"),
                "filename": slide.get("filename"),
            })
        else:
            images.append({
                **common,
                "url": get(slide, "media", "original") or slide.get("src"),
                "medium": get(slide, "media", "medium"),
                "filename": slide.get("filename"),
            })
    return {"images": images, "videos": videos, "documents": documents}


def parse_seller(props):
    seller = props.get("seller") or {}
    card = props.get("sellerCard") or {}
    user = seller.get("user") or {}
    username = user.get("name") or get(props, "overview", "seller", "username")
    level_raw = seller.get("sellerLevel")
    return {
        "id": seller.get("id") or user.get("id"),
        "username": username,
        "profile_url": f"https://www.fiverr.com/{username}" if username else None,
        "display_name": get(user, "profile", "displayName"),
        "full_name": user.get("fullName"),
        "level": LEVELS.get(level_raw, level_raw),
        "level_raw": level_raw,
        "is_pro": seller.get("isPro"),
        "is_verified": seller.get("isVerified"),
        "is_highly_responsive": seller.get("isHighlyResponsive"),
        "is_on_vacation": seller.get("isOnVacation"),
        "one_liner": seller.get("oneLinerTitle") or card.get("oneLiner"),
        "description": seller.get("description") or card.get("description"),
        "country_code": get(user, "address", "countryCode") or card.get("countryCode"),
        "languages": [
            {"code": lang.get("code"), "level": lang.get("level")}
            for lang in user.get("languages") or []
        ],
        "member_since": iso_from_seconds(card.get("memberSince") or user.get("joinedAt")),
        "last_delivery": iso_from_millis(card.get("recentDelivery")),
        "response_time_hours": get(seller, "responseTime", "inHours") or card.get("responseTime"),
        "rating": get(seller, "rating", "score") or card.get("rating"),
        "ratings_count": get(seller, "rating", "count") or card.get("ratingsCount"),
        "completed_orders": seller.get("completedOrdersCount"),
        "hourly_rate_usd": cents_to_usd(get(seller, "hourlyRate", "priceInCents")),
        "profile_image": get(user, "profileImage", "previewUrl", "url") or card.get("profilePhoto"),
        "intro_video": get(seller, "introVideo", "url"),
        "agency": seller.get("agency"),
        "skills": [
            {"name": s.get("name"), "level": s.get("level")}
            for s in seller.get("activeStructuredSkills") or []
        ],
        "certifications": [
            {
                "name": c.get("certificationName"),
                "issuer": (c.get("receivedFrom") or "").strip(),
                "year": c.get("year"),
            }
            for c in seller.get("certifications") or []
        ],
        "education": [
            {
                "degree": e.get("degreeTitle"),
                "field": e.get("degree"),
                "school": (e.get("school") or "").strip(),
                "year": e.get("toYear"),
            }
            for e in seller.get("activeEducations") or []
        ],
        "languages_proficiency_raw": card.get("proficientLanguages") or [],
    }


def parse_portfolio(props):
    counts = {
        p.get("id"): p.get("totalCount")
        for p in get(props, "seller", "portfolios", "portfolioProjects", default=[])
    }
    return {
        "total_projects": get(props, "portfolio", "projectsCount"),
        "projects": [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "cover_image": p.get("item"),
                "items_count": counts.get(p.get("id")),
            }
            for p in get(props, "portfolio", "portfolioProjects", default=[])
        ],
    }


def parse_review_item(r):
    return {
        "id": r.get("id"),
        "reviewer": r.get("username"),
        "reviewer_country": r.get("reviewer_country"),
        "reviewer_country_code": r.get("reviewer_country_code"),
        "rating": r.get("value"),
        "date": r.get("created_at"),
        "comment": r.get("comment"),
        "comment_language": r.get("comment_language"),
        "order_duration_days": r.get("order_duration_in_days"),
        "order_price_range_usd": r.get("order_price_range_usd"),
        "repeat_buyer": r.get("repeat_buyer"),
        "is_business": r.get("is_business"),
        "seller_response": r.get("seller_response"),
        "encrypted_order_id": r.get("encrypted_order_id"),
        "score": r.get("score"),
    }


def _review_cursor(review):
    rid = review.get("id") or review.get("review_id")
    if not rid:
        return None
    star = review.get("cluster_root_review_id") or review.get("star_rating_id") or rid
    score = review.get("score")
    if score is None:
        score = 0
    return str(star), str(rid), score


def _reviews_api_url(gig_id, cursor=None, page_size=5):
    params = [("gig_id", str(gig_id))]
    if cursor:
        star, rid, score = cursor
        params.extend((
            ("last_star_rating_id", str(star)),
            ("last_review_id", str(rid)),
            ("last_score", str(score)),
        ))
    params.extend((("sort_by", "relevant"), ("page_size", str(page_size))))
    return f"https://www.fiverr.com/gig_page/api/fetch_reviews/{gig_id}?{urlencode(params)}"


def _reviews_source(props, url=None):
    block = props.get("reviews")
    if isinstance(block, dict) and (block.get("reviews") or block.get("reviews_snippet")):
        return block, block.get("gig_id") or get(props, "general", "gigId")
    reviews_data = props.get("reviewsData") or {}
    block = reviews_data.get("selling_reviews") or reviews_data.get("buying_reviews")
    gigs = props.get("gigsData") or []
    slug = urlparse(url).path.rstrip("/").split("/")[-1] if url else None
    gig_id = None
    for gig in gigs:
        cached = gig.get("cached_slug") or ""
        if slug and (cached == slug or cached.startswith(slug)):
            gig_id = gig.get("gig_id")
            break
    if gig_id is None and len(gigs) == 1:
        gig_id = gigs[0].get("gig_id")
    return (block if isinstance(block, dict) else {}), gig_id


async def fetch_all_reviews(fetcher, runtime, gig_id, props, url=None):
    block, source_gig_id = _reviews_source(props, url)
    gig_id = gig_id or source_gig_id
    filters = block.get("filters_counters") or {}
    breakdown = block.get("breakdown") or []
    sub_ratings = block.get("star_summary") or {}
    avg_rating = block.get("average_valuation")
    total_count = block.get("total_count")
    parsed = []
    seen = set()
    cursor = None
    has_next = True
    full_block = props.get("reviews")
    if isinstance(full_block, dict) and full_block is block:
        initial_items = full_block.get("reviews") or full_block.get("reviews_snippet") or []
        fallback = [parse_review_item(review) for review in initial_items]
    else:
        fallback = []

    if gig_id:
        for _ in range(500):
            page_url = _reviews_api_url(gig_id, cursor)
            try:
                async with runtime.sem:
                    page_data = await fetch_with_retries(fetcher, page_url, runtime, json_mode=True)
            except BlockedError:
                print(f"[warning] stopped fetching reviews for gig {gig_id}: Fiverr blocked access",
                      file=sys.stderr, flush=True)
                break
            except Exception as exc:
                print(f"[warning] could not fetch all reviews for gig {gig_id}: {exc}", file=sys.stderr, flush=True)
                break

            batch = page_data.get("reviews") or page_data.get("reviews_snippet") or []
            if not batch:
                has_next = False
                break
            added = 0
            for review in batch:
                rid = str(review.get("id") or "")
                if rid and rid in seen:
                    continue
                if rid:
                    seen.add(rid)
                parsed.append(parse_review_item(review))
                added += 1

            if page_data.get("filters_counters"):
                filters = page_data["filters_counters"] or filters
            if page_data.get("breakdown"):
                breakdown = page_data["breakdown"] or breakdown
            if page_data.get("star_summary"):
                sub_ratings = page_data["star_summary"] or sub_ratings
            if page_data.get("average_valuation") is not None:
                avg_rating = page_data["average_valuation"]
            if page_data.get("total_count") is not None:
                total_count = page_data["total_count"]

            has_next = page_data.get("has_next")
            if has_next is None:
                has_next = bool(total_count and len(parsed) < total_count)
            if not has_next:
                break
            cursor = _review_cursor(batch[-1])
            if not cursor or added == 0:
                break
    else:
        has_next = False

    if not parsed:
        parsed = fallback

    return {
        "gig_id": gig_id,
        "average_rating": avg_rating,
        "total_count": total_count,
        "loaded_count": len(parsed),
        "has_more_on_site": bool(has_next),
        "star_breakdown": {
            str(b.get("average_valuation_value")): b.get("count")
            for b in breakdown
        },
        "sub_ratings": sub_ratings,
        "business_reviews_count": filters.get("business"),
        "work_sample_reviews_count": filters.get("work_sample"),
        "top_industries": filters.get("most_common_industries") or {},
        "items": parsed,
    }


def parse_reviews(props):
    reviews = props.get("reviews") or {}
    items = reviews.get("reviews") or reviews.get("reviews_snippet") or []
    parsed = [parse_review_item(r) for r in items]
    filters = reviews.get("filters_counters") or {}
    return {
        "average_rating": reviews.get("average_valuation"),
        "total_count": reviews.get("total_count"),
        "loaded_count": len(parsed),
        "has_more_on_site": reviews.get("has_next"),
        "star_breakdown": {
            str(b.get("average_valuation_value")): b.get("count")
            for b in reviews.get("breakdown") or []
        },
        "sub_ratings": reviews.get("star_summary") or {},
        "business_reviews_count": filters.get("business"),
        "work_sample_reviews_count": filters.get("work_sample"),
        "top_industries": filters.get("most_common_industries") or {},
        "items": parsed,
    }


def parse_misc(props):
    currency = props.get("currency") or {}
    og = props.get("openGraph") or {}
    return {
        "display_currency": {
            "name": currency.get("name"),
            "symbol": currency.get("symbol"),
            "rate_from_usd": currency.get("rate"),
        },
        "open_graph": {
            "title": og.get("title"),
            "image": og.get("image"),
            "video": og.get("video"),
            "starting_price_usd": cents_to_usd(og.get("price")),
        },
        "related_gig_ids": get(props, "otherGigs", "gigIds", default=[]),
        "service_fee_rules": get(props, "priceUpfrontConfiguration", "service_fee_rules"),
        "region_code": props.get("regionCode"),
    }


def extract(html, source_url=None):
    soup = BeautifulSoup(html, "lxml")
    props = load_props(soup)
    ld = load_ld_json(soup)
    gig = parse_gig(props, ld)
    seller = parse_seller(props)
    url = source_url
    if not url and seller["username"] and gig["slug"]:
        url = f"https://www.fiverr.com/{seller['username']}/{gig['slug']}"
    return {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "gig": gig,
        "packages": parse_packages(props),
        "recurring_discounts": parse_recurring(props),
        "consultation": parse_consultation(props),
        "media": parse_gallery(props),
        "seller": seller,
        "portfolio": parse_portfolio(props),
        "reviews": parse_reviews(props),
        "misc": parse_misc(props),
        "breadcrumbs": [
            {"name": i.get("name"), "url": i.get("item")}
            for i in get(ld, "BreadcrumbList", "itemListElement", default=[])
        ],
    }


SUMMARY_COLUMNS = [
    "gig_id", "url", "title", "category", "sub_category",
    "seller_username", "seller_name", "seller_level", "seller_country",
    "rating", "ratings_count", "seller_completed_orders", "orders_in_queue",
    "basic_price_usd", "basic_days", "standard_price_usd", "standard_days",
    "premium_price_usd", "premium_days",
    "tags", "is_promoted", "listing_page", "listing_position",
]


def gig_path_parts(url):
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0], parts[1]


def gig_output_path(url, directory):
    username, slug = gig_path_parts(url)
    name = re.sub(r"[^\w\-]+", "_", f"{username}__{slug}")
    return Path(directory) / f"{name}.json"


def default_output_path(result, directory):
    if result.get("url"):
        return gig_output_path(result["url"], directory)
    username = get(result, "seller", "username", default="unknown")
    slug = get(result, "gig", "slug", default="gig")
    name = re.sub(r"[^\w\-]+", "_", f"{username}__{slug}")
    return Path(directory) / f"{name}.json"


def print_summary(result, path):
    gig, seller = result["gig"], result["seller"]
    print(f"\nTitle      : {gig['title']}")
    print(f"Category   : {gig['category']['name']} > {gig['sub_category']['name']}")
    print(f"Seller     : {seller['display_name']} (@{seller['username']}) - {seller['level']} - {seller['country_code']}")
    print(f"Rating     : {gig['rating']} ({gig['ratings_count']} ratings)")
    for pk in result["packages"]:
        print(f"  [{pk['tier']}] {pk['title']}: ${pk['price_usd']} / {pk['delivery_days']} days / {pk['revisions']} revisions")
    media = result["media"]
    print(f"Media      : {len(media['images'])} images, {len(media['videos'])} videos, {len(media['documents'])} documents")
    print(f"Reviews    : {result['reviews']['loaded_count']} of {result['reviews']['total_count']} loaded")
    print(f"Saved to   : {path}")


def summary_row(result):
    gig = result.get("gig") or {}
    seller = result.get("seller") or {}
    listing = result.get("listing") or {}
    tiers = {p.get("tier"): p for p in result.get("packages") or []}
    row = {
        "gig_id": gig.get("id"),
        "url": result.get("url"),
        "title": gig.get("title"),
        "category": get(gig, "category", "name"),
        "sub_category": get(gig, "sub_category", "name"),
        "seller_username": seller.get("username"),
        "seller_name": seller.get("display_name"),
        "seller_level": seller.get("level"),
        "seller_country": seller.get("country_code"),
        "rating": gig.get("rating"),
        "ratings_count": gig.get("ratings_count"),
        "seller_completed_orders": seller.get("completed_orders"),
        "orders_in_queue": gig.get("orders_in_queue"),
        "tags": "; ".join(t for t in gig.get("tags") or [] if t),
        "is_promoted": listing.get("is_promoted"),
        "listing_page": listing.get("page"),
        "listing_position": listing.get("position"),
    }
    for tier in ("basic", "standard", "premium"):
        package = tiers.get(tier) or {}
        row[f"{tier}_price_usd"] = package.get("price_usd")
        row[f"{tier}_days"] = package.get("delivery_days")
    return row


def parse_listing(html):
    soup = BeautifulSoup(html, "lxml")
    props = load_props(soup)
    raw = get(props, "listings", 0, "gigs") or props.get("items") or get(props, "rawListingData", "gigs") or []
    pagination = get(props, "appData", "pagination", default={}) or {}
    raw_data = props.get("rawListingData") or {}
    entries = []
    for position, item in enumerate(raw):
        path = item.get("gig_url")
        if not path and item.get("seller_name") and item.get("cached_slug"):
            path = f"/{item['seller_name']}/{item['cached_slug']}"
        if not path:
            continue
        entries.append({
            "gig_id": item.get("gig_id") or item.get("gigId"),
            "url": "https://www.fiverr.com" + path.split("?")[0],
            "title": item.get("title"),
            "position": position,
            "is_promoted": item.get("type") == "promoted_gigs",
            "is_pro": item.get("is_pro"),
            "is_fiverr_choice": item.get("is_fiverr_choice"),
            "seller_username": item.get("seller_name"),
            "seller_display_name": item.get("seller_display_name"),
            "seller_country": item.get("seller_country"),
            "seller_level": item.get("seller_level"),
            "seller_online": item.get("seller_online"),
            "seller_rating": get(item, "seller_rating", "score"),
            "seller_ratings_count": get(item, "seller_rating", "count"),
            "gig_rating": item.get("buying_review_rating"),
            "gig_ratings_count": item.get("buying_review_rating_count"),
            "starting_price": item.get("price_i"),
            "packages_count": item.get("num_of_packages"),
            "offers_consultation": item.get("offer_consultation"),
            "hourly_rate_usd": cents_to_usd(item.get("hourly_rate")),
            "thumbnail": get(item.get("assets") or [], 0, "cloud_img_main_gig"),
            "metadata": {m.get("type"): m.get("value") for m in item.get("metadata") or []},
        })
    return {
        "entries": entries,
        "page": pagination.get("page"),
        "page_size": pagination.get("page_size"),
        "total": pagination.get("total") or raw_data.get("num_found"),
        "has_more": raw_data.get("has_more"),
        "query": props.get("listingQuery"),
        "title": get(props, "seoData", "seo", "meta", "meta_title"),
    }


def listing_url_from_html(html):
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", id="CONTEXT_MANAGER_STORAGE_SCRIPT")
    if tag is None:
        return None
    try:
        data = json.loads(tag.get_text())
    except json.JSONDecodeError:
        return None
    url = get(data, "fiverr_context", "url")
    return validate_listing_url(url) if url else None


def last_page_for(parsed, start, pages):
    size, total = parsed.get("page_size"), parsed.get("total")
    total_pages = math.ceil(total / size) if size and total else None
    if pages == 0:
        return total_pages or start
    last = start + pages - 1
    return min(last, total_pages) if total_pages else last


async def collect_listing(fetcher, listing_url, args, runtime, preloaded_html=None):
    start = 1 if preloaded_html is not None else args.start_page
    if preloaded_html is not None:
        html = preloaded_html
    else:
        async with runtime.sem:
            html = await fetch_with_retries(
                fetcher, page_url(listing_url, start), runtime, runtime.take_first_patience()
            )
    first = parse_listing(html)
    if first["page"] not in (None, start):
        raise ScrapeError(f"Listing returned page {first['page']} instead of page {start}")
    pages = {start: first}
    last = last_page_for(first, start, args.pages)
    total_pages = f"/{last}" if last > start else ""
    print(f"Listing page {start}{total_pages}: {len(first['entries'])} gigs "
          f"(query: {first['query']!r}, total results: {first['total']})", flush=True)

    async def load_page(number):
        async with runtime.sem:
            page_html = await fetch_with_retries(fetcher, page_url(listing_url, number), runtime)
        return number, parse_listing(page_html)

    if last > start:
        if listing_url is None:
            print("Cannot paginate: the saved listing page does not contain its own URL", file=sys.stderr)
        else:
            outcomes = await asyncio.gather(
                *(load_page(n) for n in range(start + 1, last + 1)), return_exceptions=True
            )
            for outcome in outcomes:
                if isinstance(outcome, Exception):
                    print(f"[warning] listing page failed: {outcome}", file=sys.stderr)
                    continue
                number, parsed = outcome
                if parsed["page"] not in (None, number):
                    print(f"[warning] page {number} was not honored by Fiverr (got page {parsed['page']}); skipped",
                          file=sys.stderr)
                    continue
                pages[number] = parsed
                print(f"Listing page {number}/{last}: {len(parsed['entries'])} gigs", flush=True)

    entries = []
    for number in sorted(pages):
        for entry in pages[number]["entries"]:
            entries.append({**entry, "page": number})
    unique = {}
    for entry in entries:
        existing = unique.get(entry["url"])
        if existing is None:
            unique[entry["url"]] = entry
        elif entry["is_promoted"]:
            existing["is_promoted"] = True
    meta = {
        "listing_url": listing_url,
        "query": first["query"],
        "title": first["title"],
        "total_results": first["total"],
        "pages_fetched": sorted(pages),
        "gigs_found": len(entries),
        "unique_gigs": len(unique),
    }
    return list(unique.values()), meta


def build_plan(args):
    targets = list(args.urls)
    if args.urls_file:
        lines = Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        targets += [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    listings, gigs = [], []
    for target in targets:
        if is_listing_url(target):
            listings.append({"url": validate_listing_url(target), "html": None})
        else:
            gigs.append(validate_url(target))
    if args.listing_file:
        html = Path(args.listing_file).read_text(encoding="utf-8", errors="replace")
        if not has_data(html):
            raise ScrapeError("The given listing file does not contain Fiverr listing data")
        listings.append({"url": listing_url_from_html(html), "html": html})
    return listings, gigs


async def discover_jobs(fetcher, listings, gigs, args, runtime):
    jobs = {}
    for url in gigs:
        jobs.setdefault(url, {"url": url, "listing": None})
    summaries = []
    for item in listings:
        entries, meta = await collect_listing(fetcher, item["url"], args, runtime, item["html"])
        summaries.append({**meta, "gigs": entries})
        for entry in entries:
            jobs.setdefault(entry["url"], {
                "url": entry["url"],
                "listing": {**entry, "listing_url": meta["listing_url"], "query": meta["query"]},
            })
    ordered = list(jobs.values())
    if args.limit:
        ordered = ordered[:args.limit]
    return ordered, summaries


async def scrape_gigs(fetcher, jobs, args, runtime, results_map):
    total = len(jobs)
    stats = {"ok": 0, "skipped": 0, "failed": 0}
    failures = []
    counter = 0
    loop = asyncio.get_running_loop()

    def log(status, url, detail=""):
        nonlocal counter
        counter += 1
        username, slug = gig_path_parts(url)
        print(f"[{counter:>{len(str(total))}}/{total}] {status:<7} {username}/{slug} {detail}".rstrip(), flush=True)

    async def ensure_reviews(result, props, job, url):
        if not getattr(args, "all_reviews", False):
            return False
        gig_id = get(result, "gig", "id") or get(props or {}, "general", "gigId")
        if not gig_id:
            return False
        reviews = result.get("reviews") or {}
        total = reviews.get("total_count") or 0
        loaded = reviews.get("loaded_count") or len(reviews.get("items") or [])
        if reviews.get("items") and loaded >= total and not reviews.get("has_more_on_site"):
            return False
        if props is None:
            async with runtime.sem:
                html = await fetch_with_retries(fetcher, url, runtime)
            soup = await loop.run_in_executor(None, BeautifulSoup, html, "lxml")
            props = await loop.run_in_executor(None, load_props, soup)
            refreshed = await loop.run_in_executor(None, extract, html, url)
            refreshed["reviews"] = await fetch_all_reviews(fetcher, runtime, gig_id, props, url=url)
            refreshed["listing"] = result.get("listing") or job.get("listing")
            result.clear()
            result.update(refreshed)
        else:
            result["reviews"] = await fetch_all_reviews(fetcher, runtime, gig_id, props, url=url)
        return True

    async def work(job):
        url = job["url"]
        if not args.force and url in results_map:
            try:
                result = results_map[url]
                updated = await ensure_reviews(result, None, job, url)
                if runtime.monitor.aborted:
                    error = "Fiverr blocked access; saved any reviews collected before the block"
                    stats["failed"] += 1
                    failures.append({"url": url, "error": error, "job": job})
                    log("failed", url, f"- {error}")
                    return
                if updated:
                    stats["ok"] += 1
                    log("ok", url, f"(updated reviews: {get(result, 'reviews', 'loaded_count', default='?')}/{get(result, 'reviews', 'total_count', default='?')})")
                else:
                    stats["skipped"] += 1
                    log("skipped", url)
            except Exception as exc:
                stats["failed"] += 1
                failures.append({"url": url, "error": str(exc), "job": job})
                log("failed", url, f"- {exc}")
            return
        started = time.monotonic()
        try:
            async with runtime.sem:
                html = await fetch_with_retries(fetcher, url, runtime)
            soup = await loop.run_in_executor(None, BeautifulSoup, html, "lxml")
            props = await loop.run_in_executor(None, load_props, soup)
            result = await loop.run_in_executor(None, extract, html, url)
            if job.get("listing"):
                result["listing"] = job["listing"]
            results_map[url] = result
            await ensure_reviews(result, props, job, url)
            if runtime.monitor.aborted:
                raise BlockedError("Fiverr blocked access; saved any reviews collected before the block")
            stats["ok"] += 1
            log("ok", url, f"({time.monotonic() - started:.1f}s, reviews: {get(result, 'reviews', 'loaded_count', default='?')}/{get(result, 'reviews', 'total_count', default='?')})")
        except ScrapeError as exc:
            stats["failed"] += 1
            failures.append({"url": url, "error": str(exc), "job": job})
            log("failed", url, f"- {exc}")
        except Exception as exc:
            stats["failed"] += 1
            failures.append({"url": url, "error": f"{type(exc).__name__}: {exc}", "job": job})
            log("failed", url, f"- {type(exc).__name__}: {exc}")

    await asyncio.gather(*(work(job) for job in jobs))
    return stats, failures


def load_existing_results(out_dir):
    path = out_dir / "all_gigs.json"
    if not path.exists():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, []
    if not isinstance(data, list):
        return {}, []
    results_map = {item["url"]: item for item in data if isinstance(item, dict) and item.get("url")}
    return results_map, data


def write_aggregates(out_dir, jobs, failures, listings, results_map):
    results = [results_map[j["url"]] for j in jobs if j["url"] in results_map]
    (out_dir / "all_gigs.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_dir / "summary.csv", "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        for result in results:
            writer.writerow(summary_row(result))
    if listings:
        (out_dir / "listing_gigs.json").write_text(json.dumps(listings, ensure_ascii=False, indent=2), encoding="utf-8")
    failed_path = out_dir / "failed.json"
    if failures:
        slim = [{"url": f["url"], "error": f["error"]} for f in failures]
        failed_path.write_text(json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8")
    elif failed_path.exists():
        failed_path.unlink()
    return len(results)


async def open_fetcher(engine, args):
    fetcher = CurlFetcher(args) if engine == "curl" else BrowserFetcher(args)
    await fetcher.start()
    return fetcher


def write_reviews_only(out_dir, jobs, results_map):
    written = []
    for job in jobs:
        result = results_map.get(job["url"])
        if not isinstance(result, dict):
            continue
        reviews = result.get("reviews") or {}
        gig_id = reviews.get("gig_id") or get(result, "gig", "id")
        if not gig_id:
            continue
        items = reviews.get("items") or []
        path = out_dir / f"reviews_{gig_id}.json"
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(old, list) and len(old) > len(items):
                    items = old
            except (json.JSONDecodeError, TypeError):
                pass
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append((gig_id, len(items), path))
    return written


async def run(args):
    listings_in, gigs_in = build_plan(args)
    if not listings_in and not gigs_in:
        raise ScrapeError("No URL given")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    engines = ["curl", "playwright"] if args.engine == "auto" else [args.engine]

    jobs = None
    summaries = []
    failures = []
    totals = {"ok": 0, "skipped": 0, "failed": 0}
    results_map = {}
    started = time.monotonic()
    try:
        for index, engine in enumerate(engines):
            is_last = index == len(engines) - 1
            runtime = Runtime(args)
            try:
                fetcher = await open_fetcher(engine, args)
            except ScrapeError as exc:
                print(f"[{engine}] unavailable: {exc}", file=sys.stderr)
                if is_last:
                    raise
                continue
            print(f"Engine: {fetcher.name} | concurrency: {args.concurrency}", flush=True)
            try:
                if jobs is None:
                    results_map, _ = load_existing_results(out_dir)
                    jobs, summaries = await discover_jobs(fetcher, listings_in, gigs_in, args, runtime)
                    if runtime.monitor.aborted:
                        raise BlockedError("Fiverr blocked access; stopping without trying another engine")
                    print(f"Gigs to process: {len(jobs)}", flush=True)
                pending = jobs if index == 0 or not failures else [f["job"] for f in failures]
                if index and failures:
                    print(f"Retrying {len(pending)} failed gigs with {fetcher.name}", flush=True)
                stats, failures = await scrape_gigs(fetcher, pending, args, runtime, results_map)
                for key in ("ok", "skipped"):
                    totals[key] += stats[key]
                totals["failed"] = len(failures)
                if args.reviews_only:
                    write_reviews_only(out_dir, jobs, results_map)
                else:
                    write_aggregates(out_dir, jobs, failures, summaries, results_map)
                if runtime.monitor.aborted:
                    break
            except ScrapeError as exc:
                print(f"[{engine}] {exc}", file=sys.stderr)
                if is_last or runtime.monitor.aborted:
                    raise
                continue
            finally:
                await fetcher.close()
            if not failures:
                break
    finally:
        if jobs is not None:
            if args.reviews_only:
                for gig_id, count, path in write_reviews_only(out_dir, jobs, results_map):
                    print(f"reviews: gig {gig_id} -> {count} saved in {path}", flush=True)
                output_summary = f"{len(results_map)} gigs with review data in {out_dir}/"
            else:
                saved = write_aggregates(out_dir, jobs, failures, summaries, results_map)
                output_summary = f"{saved} gigs in {out_dir}/all_gigs.json and {out_dir}/summary.csv"
            elapsed = time.monotonic() - started
            print(f"\nDone in {elapsed:.1f}s: {totals['ok']} scraped, {totals['skipped']} skipped, "
                  f"{totals['failed']} failed | {output_summary}", flush=True)
    return 1 if failures else 0


def scrape_file(args):
    html = Path(args.file).read_text(encoding="utf-8", errors="replace")
    if not has_data(html):
        raise ScrapeError("The given file does not contain Fiverr gig data")
    result = extract(html)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "all_gigs.json"
    path.write_text(json.dumps([result], ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(result, path)
    if args.print_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Scrape Fiverr gigs: give gig URLs and/or listing (search/category) URLs"
    )
    parser.add_argument("urls", nargs="*", help="Fiverr gig URL(s) and/or listing URL(s)")
    parser.add_argument("--urls-file", help="Text file with one URL per line")
    parser.add_argument("-f", "--file", help="Parse a saved gig HTML file instead of downloading")
    parser.add_argument("--listing-file", help="Use a saved listing HTML file as the first listing page")
    parser.add_argument("-o", "--output-dir", default="output", help="Output directory (default: output)")
    parser.add_argument("--pages", type=int, default=1, help="Listing pages to scan (0 = all, default: 1)")
    parser.add_argument("--start-page", type=int, default=1, help="First listing page (default: 1)")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of gigs to scrape (0 = no limit)")
    parser.add_argument("-c", "--concurrency", type=int, default=5, help="Parallel requests (default: 5)")
    parser.add_argument("--engine", choices=("auto", "curl", "playwright"), default="auto")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (playwright engine)")
    parser.add_argument("--profile", default=".browser_profile", help="Browser profile directory for session reuse")
    parser.add_argument("--keep-assets", action="store_true", help="Do not block images/fonts/media in the browser")
    parser.add_argument("--timeout", type=int, default=30, help="Per-request timeout in seconds")
    parser.add_argument("--wait", type=int, default=120, help="Seconds to wait for a manual challenge on the first page")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum seconds between request starts")
    parser.add_argument("--retries", type=int, default=2, help="Retries for transient network errors (not access blocks)")
    parser.add_argument("--max-blocks", type=int, default=1, help="Deprecated; any access block stops the run")
    parser.add_argument("--force", action="store_true", help="Re-scrape gigs that already have an output file")
    parser.add_argument("--all-reviews", action=argparse.BooleanOptionalAction, default=True, help="Paginate and scrape all reviews for each gig (default: on; use --no-all-reviews to disable)")
    parser.add_argument("--reviews-only", action="store_true", help="Save only reviews_<gig_id>.json files")
    parser.add_argument("--print", dest="print_json", action="store_true", help="Print full JSON (with --file)")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.pages < 0 or args.start_page < 1:
        parser.error("--pages must be >= 0 and --start-page >= 1")
    try:
        if args.file:
            scrape_file(args)
            sys.exit(0)
        if not args.urls and not args.urls_file and not args.listing_file:
            args.urls = [input("Fiverr gig or listing URL: ").strip()]
        code = asyncio.run(run(args))
    except ScrapeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("\nInterrupted. Finished gigs were saved; run again to resume.", file=sys.stderr)
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()