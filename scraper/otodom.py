"""
Otodom scraper — czyta aktywne scrape_jobs z Supabase, scrapuje listę wyników
+ pojedyncze oferty, wysyła do RPC leads_ingest_offer. Odpalane przez GitHub Actions
co 3h; ingest idempotentny (ON CONFLICT).
"""
import os
import re
import json
import time
import random
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("otodom")

MOBILE_UA = ("Mozilla/5.0 (Linux; Android 10; Pixel 3) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/88.0.4324.181 Mobile Safari/537.36")
HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept-Language": "pl-PL,pl;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 20
MAX_PAGES = 30
PER_REQUEST_DELAY_RANGE = (0.5, 1.5)

sb: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
session = requests.Session()
session.headers.update(HEADERS)


def jitter_sleep():
    time.sleep(random.uniform(*PER_REQUEST_DELAY_RANGE))


def normalize_phone(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0048"):
        digits = digits[4:]
    if digits.startswith("48") and len(digits) == 11:
        digits = digits[2:]
    if len(digits) == 9:
        return f"+48{digits}"
    log.warning(f"Cannot normalize phone: {raw!r}")
    return None


def fetch(url: str) -> Optional[str]:
    for attempt in range(3):
        try:
            r = session.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            log.warning(f"HTTP {r.status_code} on {url} (attempt {attempt + 1})")
        except Exception as e:
            log.warning(f"Fetch error {url} (attempt {attempt + 1}): {e}")
        time.sleep(2 ** attempt)
    return None


def extract_listing_urls(html: str) -> set:
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/pl/oferta/" in href:
            if href.startswith("/"):
                href = "https://www.otodom.pl" + href
            urls.add(href.split("?")[0].split("#")[0])
    return urls


def extract_next_data(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return {}
    try:
        return json.loads(tag.string)
    except Exception as e:
        log.warning(f"NEXT_DATA parse error: {e}")
        return {}


def extract_pagination(html: str) -> dict:
    # 2026-10-14: strona wyników mówi, którą stronę faktycznie zwróciła i ile ich jest.
    # Prośba o stronę za ostatnią (np. ?page=6 przy 5 stronach) NIE daje pustej strony:
    # Otodom gubi wtedy wszystkie filtry (prywatni, ostatnia doba) i zwraca ostatnią stronę
    # całej bazy — ok. 36 ogłoszeń biur i deweloperów na każdy przebieg każdego wyszukiwania.
    data = extract_next_data(html)
    return dig(data, "props", "pageProps", "data", "searchAds", "pagination") or {}


# Typy ogłoszeniodawcy, którym nie wysyłamy SMS-ów. Celowo lista zakazanych, a nie „tylko PRIVATE”:
# ogłoszenie bez oznaczenia typu (zdarza się 1–4 razy w miesiącu) przechodzi jak dotąd.
NOT_PRIVATE_OWNER_TYPES = {"AGENCY", "DEVELOPER_UNIT", "DEVELOPER"}

# 2026-10-15: ogłoszeniodawcy, których nie zbieramy wcale — także gdy wystawią ogłoszenie z nowym
# numerem, bo czarna lista działa tylko po numerze. Skarga z 13.10.2026 (ogłoszenie 68492827).
# Sprawdzamy konto i nazwisko w owner i contactDetails; nazwisko porównywane w locie,
# nie trafia ani do bazy, ani do logów.
BLOCKED_OWNER_IDS = {"11218569"}
BLOCKED_OWNER_NAME_PARTS = ("richter",)


def blocked_owner_match(ad: dict) -> Optional[dict]:
    """None albo {"match": "konto"|"nazwisko", "owner_id": ...} — samo nazwisko nie wychodzi z funkcji."""
    for key in ("owner", "contactDetails"):
        person = ad.get(key) or {}
        owner_id = str(person.get("id")) if person.get("id") is not None else None
        if owner_id in BLOCKED_OWNER_IDS:
            return {"match": "konto", "owner_id": owner_id}
        name = (person.get("name") or "").lower()
        if any(part in name for part in BLOCKED_OWNER_NAME_PARTS):
            return {"match": "nazwisko", "owner_id": owner_id}
    return None


def is_blocked_owner(ad: dict) -> bool:
    return blocked_owner_match(ad) is not None


def dig(d, *keys, default=None):
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int) and k < len(cur):
            cur = cur[k]
        else:
            return default
    return cur if cur is not None else default


def parse_offer(url: str, property_type: str, transaction_type: str) -> Optional[dict]:
    html = fetch(url)
    if not html:
        return None

    data = extract_next_data(html)
    ad = dig(data, "props", "pageProps", "ad") or {}

    # phone: 2026-05-26 — Otodom przeniósł phones z ad.phones na
    # ad.contactDetails.phones (i duplikat w ad.owner.phones). Stary ad.phones
    # zwraca teraz None → scraper graceful-failował na phone=None od 25.05 ~17:00.
    # Sprawdzamy wszystkie 3 lokalizacje (contactDetails najpierw — kanoniczna).
    phone = None
    for path in (("contactDetails", "phones"), ("owner", "phones"), ("phones",)):
        phones_list = dig(ad, *path)
        if isinstance(phones_list, list) and phones_list:
            phone = normalize_phone(str(phones_list[0]))
            if phone:
                break
    if not phone:
        # Fallback: tel: link w DOM (rzadko renderowane, ale defensive)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if a["href"].lower().startswith("tel:"):
                phone = normalize_phone(a["href"][4:])
                if phone:
                    break

    price = dig(ad, "target", "Price")
    try:
        price = float(price) if price not in (None, "") else None
    except Exception:
        price = None

    area = dig(ad, "target", "Area")
    try:
        area = float(str(area).replace(",", ".")) if area not in (None, "") else None
    except Exception:
        area = None

    rooms_raw = dig(ad, "target", "Rooms_num")
    rooms = None
    try:
        if isinstance(rooms_raw, list) and rooms_raw:
            rooms = int(rooms_raw[0])
        elif rooms_raw not in (None, ""):
            rooms = int(rooms_raw)
    except Exception:
        pass

    return {
        "url": url,
        "portal": "otodom",
        "portal_listing_id": str(dig(ad, "id") or "") or None,
        "property_type": property_type,
        "transaction_type": transaction_type,
        "phone": phone,
        "title": dig(ad, "title"),
        "price": price,
        "currency": "PLN",
        "area_m2": area,
        "rooms": rooms,
        "city": dig(ad, "location", "address", "city", "name"),
        "district": dig(ad, "location", "address", "district", "name"),
        "voivodeship": dig(ad, "location", "address", "province", "name"),
        "owner_type": dig(ad, "advertType"),
        # zdejmowane w scrape_job przed wysłaniem do bazy
        "blocked_owner": blocked_owner_match(ad),
        "posted_at": dig(ad, "createdAt") or dig(ad, "dateCreated"),
        "raw": {
            "ad_id": dig(ad, "id"),
            "category": dig(ad, "adCategory"),
            "subcategory": dig(ad, "subcategory"),
        },
    }


def scrape_job(job: dict) -> dict:
    run_id = sb.rpc("leads_start_run", {"p_job_id": job["id"]}).execute().data
    stats = {"pages": 0, "listings_found": 0, "listings_new": 0, "phones_new": 0, "skipped_not_private": 0,
             "skipped_blocked_owner": 0}
    try:
        base_url = job["search_url"]
        page = 1
        seen_urls = set()
        while page <= MAX_PAGES:
            page_url = base_url + (f"&page={page}" if page > 1 else "")
            log.info(f"[{job['name']}] page {page}: {page_url}")
            html = fetch(page_url)
            if not html:
                break

            pagination = extract_pagination(html)
            total_pages = pagination.get("totalPages")
            current_page = pagination.get("currentPage")
            if current_page is not None and current_page != page:
                log.info(f"[{job['name']}] Otodom zwrócił stronę {current_page} zamiast {page} (koniec wyników) — stop")
                break
            if total_pages is not None and page > total_pages:
                log.info(f"[{job['name']}] strona {page} > ostatnia ({total_pages}) — stop")
                break

            urls = extract_listing_urls(html) - seen_urls
            if not urls:
                log.info(f"[{job['name']}] no new URLs on page {page}, stopping")
                break
            seen_urls |= urls
            stats["pages"] += 1

            for url in urls:
                stats["listings_found"] += 1
                offer = parse_offer(url, job["property_type"], job["transaction_type"])
                jitter_sleep()
                if not offer:
                    continue
                blocked = offer.pop("blocked_owner", None)
                if blocked:
                    stats["skipped_blocked_owner"] += 1
                    log.info(f"[{job['name']}] {url} pominięte: zablokowany ogłoszeniodawca ({blocked['match']})")
                    # ślad dla strażnika (zadanie co 2 h); błąd zapisu nie może puścić ogłoszenia dalej
                    try:
                        sb.rpc("leads_log_blocked_owner_hit", {"p_hit": {
                            "portal": "otodom",
                            "portal_listing_id": offer.get("portal_listing_id") or url,
                            "url": url,
                            "owner_id": blocked["owner_id"],
                            "match": blocked["match"],
                            "phone": offer.get("phone"),
                            "job_name": job["name"],
                        }}).execute()
                    except Exception as e:
                        log.error(f"Blocked-owner log error {url}: {e}")
                    continue
                if offer.get("owner_type") in NOT_PRIVATE_OWNER_TYPES:
                    stats["skipped_not_private"] += 1
                    log.info(f"[{job['name']}] {url} pominięte: {offer['owner_type']}")
                    continue
                try:
                    res = sb.rpc("leads_ingest_offer", {"p_offer": offer}).execute()
                    result = res.data if isinstance(res.data, dict) else (res.data or {})
                    if result.get("phone_is_new"):
                        stats["phones_new"] += 1
                    if result.get("listing_is_new"):
                        stats["listings_new"] += 1
                    log.info(
                        f"[{job['name']}] {url} phone={offer['phone']} "
                        f"sms={result.get('sms_status')}"
                    )
                except Exception as e:
                    log.error(f"Ingest error {url}: {e}")

            page += 1
            jitter_sleep()

        sb.rpc("leads_finalize_run", {
            "p_run_id": run_id,
            "p_job_id": job["id"],
            "p_status": "success",
            "p_pages": stats["pages"],
            "p_listings_found": stats["listings_found"],
            "p_listings_new": stats["listings_new"],
            "p_phones_new": stats["phones_new"],
            "p_error": None,
        }).execute()
        log.info(f"[{job['name']}] done: {stats}")
        return stats

    except Exception as e:
        log.exception(f"Job {job['name']} failed")
        try:
            sb.rpc("leads_finalize_run", {
                "p_run_id": run_id,
                "p_job_id": job["id"],
                "p_status": "error",
                "p_pages": stats["pages"],
                "p_listings_found": stats["listings_found"],
                "p_listings_new": stats["listings_new"],
                "p_phones_new": stats["phones_new"],
                "p_error": str(e)[:1000],
            }).execute()
        except Exception:
            pass
        raise


def main():
    jobs = sb.rpc("leads_get_active_jobs", {"p_portal": "otodom"}).execute().data or []
    log.info(f"Found {len(jobs)} active jobs")
    for job in jobs:
        try:
            scrape_job(job)
        except Exception:
            continue


if __name__ == "__main__":
    main()
