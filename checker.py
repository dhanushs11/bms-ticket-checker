import asyncio
import json
import os
import re
import smtplib
import ssl
import subprocess
from datetime import datetime, timedelta
from email.mime.text import MIMEText

RETRY_MAX = 3
RETRY_DELAY = 5

REGION_MAP = {
    "hyderabad": "hyd",
    "mumbai": "mumbai",
    "bangalore": "blr",
    "bengaluru": "blr",
    "chennai": "chen",
    "delhi": "del",
    "pune": "pune",
    "kolkata": "kol",
    "ahmedabad": "ahd",
    "kochi": "koch",
}

EMAIL_CONFIG = None


def load_email_config():
    global EMAIL_CONFIG
    smtp_host = os.getenv("BMS_SMTP_HOST")
    smtp_port = os.getenv("BMS_SMTP_PORT", "587")
    smtp_user = os.getenv("BMS_SMTP_USER")
    smtp_pass = os.getenv("BMS_SMTP_PASS")
    to_email = os.getenv("BMS_TO_EMAIL")
    from_email = os.getenv("BMS_FROM_EMAIL") or smtp_user
    if smtp_host and smtp_user and smtp_pass and to_email:
        EMAIL_CONFIG = {
            "host": smtp_host,
            "port": int(smtp_port),
            "user": smtp_user,
            "pass": smtp_pass,
            "to": to_email,
            "from": from_email,
        }


def send_email(subject, body):
    if not EMAIL_CONFIG:
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_CONFIG["from"]
        msg["To"] = EMAIL_CONFIG["to"]
        ctx = ssl.create_default_context()
        with smtplib.SMTP(EMAIL_CONFIG["host"], EMAIL_CONFIG["port"]) as s:
            s.starttls(context=ctx)
            s.login(EMAIL_CONFIG["user"], EMAIL_CONFIG["pass"])
            s.send_message(msg)
    except Exception as e:
        print(f"  [email error] {e}")


def notify_all(title, message):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}" sound name "default"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass
    send_email(title, message)


def browser_context_args():
    return {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "viewport": {"width": 1280, "height": 720},
        "locale": "en-IN",
        "timezone_id": "Asia/Kolkata",
    }


async def retry_playwright(fn, *args, **kwargs):
    last_err = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < RETRY_MAX:
                print(f"  [retry {attempt}/{RETRY_MAX}] {e}")
                await asyncio.sleep(RETRY_DELAY)
    raise last_err


async def find_movie_on_bms(movie_name, region="hyderabad"):
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(**browser_context_args())
            page = await context.new_page()

            url = f"https://in.bookmyshow.com/explore/movies-{region.lower()}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            movies = await page.evaluate("""
                () => {
                    const seen = new Set();
                    const results = [];
                    const links = document.querySelectorAll('a[href*="/movies/"]');
                    links.forEach(a => {
                        const text = a.textContent.trim();
                        const href = a.getAttribute('href');
                        if (text && href && text.length > 2 && !seen.has(text.toLowerCase())) {
                            seen.add(text.toLowerCase());
                            results.push({ name: text, url: href.startsWith('http') ? href : 'https://in.bookmyshow.com' + href });
                        }
                    });
                    return results;
                }
            """)
            await browser.close()
            return movies

    try:
        movies = await retry_playwright(_run)
    except Exception:
        return None

    if not movies:
        return None

    query = movie_name.lower()
    query_words = [w for w in query.split() if len(w) > 2] or query.split()
    matches = []
    for m in movies:
        name_lower = m["name"].lower()
        if query == name_lower or all(w in name_lower for w in query_words):
            matches.append((m["name"], m["url"]))
    if not matches:
        for m in movies:
            name_lower = m["name"].lower()
            if any(w in name_lower for w in query_words):
                matches.append((m["name"], m["url"]))
    return matches if matches else None


async def check_tickets_playwright(movie_url, region_code="hyd"):
    from playwright.async_api import async_playwright

    async def _run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(**browser_context_args())
            page = await context.new_page()

            event_code = None
            m = re.search(r"/ET\d+", movie_url)
            if m:
                event_code = m.group(0).lstrip("/")

            slug_match = re.search(r"/movies/([^/]+)", movie_url)
            movie_slug = slug_match.group(1) if slug_match else None

            if event_code and movie_slug:
                buytickets_url = f"https://in.bookmyshow.com/buytickets/{movie_slug}-{region_code}/movie-{region_code}-{event_code}"
            else:
                buytickets_url = movie_url

            await page.goto(buytickets_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            body = await page.text_content("body") or ""
            page_url = page.url
            page_title = await page.title()

            await browser.close()

            if "Cloudflare" in body or "Attention Required" in body:
                return {"status": "error", "message": "Blocked by Cloudflare"}

            body_lower = body.lower()
            venues_found = re.findall(r'"VenueName":"([^"]+)"', body)
            unique_venues = list(set(venues_found)) if venues_found else []

            has_show_timings = "show timing" in body_lower or "showtime" in body_lower
            has_venue_blocks = len(unique_venues) > 0
            is_buytickets_page = "/buytickets/" in page_url
            is_coming_soon = "coming soon" in body_lower or "notify me" in body_lower
            has_no_shows = "no shows" in body_lower

            if is_buytickets_page and (has_venue_blocks or has_show_timings) and not has_no_shows:
                status = "available"
                show_info = [{"venue": v} for v in unique_venues[:5]]
                date_match = re.search(r"/buytickets/[^/]+/(\d{8})", page_url)
                if date_match:
                    try:
                        formatted = datetime.strptime(date_match.group(1), "%Y%m%d").strftime("%d %b %Y")
                        show_info.append({"date": formatted})
                    except:
                        pass
                if "exceeded the cinema's booking cutoff" in body_lower:
                    show_info.append({"note": "Some shows past booking cutoff"})
            elif is_coming_soon:
                status = "unavailable"
                show_info = [{"message": "Coming soon - tickets not yet released"}]
            else:
                status = "unavailable"
                show_info = [{"message": "No shows available yet"}]

            return {"status": status, "title": page_title, "url": page_url, "shows": show_info}

    try:
        return await retry_playwright(_run)
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def check_venue_for_movie(page, venue_base_url, movie_keywords, days_ahead=7):
    date_in_url = re.search(r"/(\d{8})$", venue_base_url)
    if date_in_url:
        dates_to_check = [date_in_url.group(1)]
        specific_date_requested = True
    else:
        start = datetime.now()
        dates_to_check = [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(days_ahead)]
        specific_date_requested = False

    base_url = re.sub(r"/\d{8}$", "", venue_base_url)

    for date_str in dates_to_check:
        url = f"{base_url}/{date_str}"

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        except Exception:
            continue

        final_url = page.url
        loaded_target_date = final_url.endswith(date_str)

        if specific_date_requested and not loaded_target_date:
            return {
                "found": False,
                "redirected": True,
                "message": f"Shows not loaded yet for this date (redirected to {final_url.split('/')[-1]})",
            }

        if not specific_date_requested and not loaded_target_date:
            continue

        try:
            date_obj = datetime.strptime(date_str, "%Y%m%d")
        except:
            date_obj = datetime.now()

        body = (await page.text_content("body") or "").replace("\n", " ")

        movie_blocks = re.findall(
            r"([A-Z][A-Za-z0-9][A-Za-z0-9 .&:,()/-]{2,60})\((?:Tamil|English|Hindi|Japanese|Telugu|Malayalam|Kannada)\)",
            body,
        )

        for movie in movie_blocks:
            movie_clean = movie.strip().rstrip("(").strip()
            if any(kw.lower() in movie_clean.lower() for kw in movie_keywords):
                return {
                    "found": True,
                    "movie": movie_clean,
                    "date": date_str,
                    "url": url,
                    "formatted_date": date_obj.strftime("%d %b %Y"),
                }

        for kw in movie_keywords:
            idx = body.lower().find(kw.lower())
            if idx != -1:
                nearby = body[max(0, idx - 50) : idx + 200]
                if re.search(r"\d{1,2}:\d{2}\s*(?:AM|PM)", nearby):
                    return {
                        "found": True,
                        "movie": kw,
                        "date": date_str,
                        "url": url,
                        "formatted_date": date_obj.strftime("%d %b %Y"),
                    }

        if specific_date_requested:
            return {"found": False, "message": f"Movie not found at this venue on {date_obj.strftime('%d %b %Y')}"}
        else:
            return {"found": False, "message": "Movie not found in upcoming shows"}

    return {"found": False, "message": "No shows found at this venue"}


async def poll_tickets(movie_url, region_code, interval=60):
    print(f"Polling every {interval}s... Press Ctrl+C to stop")
    notified = False
    try:
        while True:
            result = await check_tickets_playwright(movie_url, region_code)
            now = datetime.now().strftime("%H:%M:%S")
            if result["status"] == "available":
                print(f"[{now}] TICKETS AVAILABLE!")
                print(f"  {result['url']}")
                if not notified:
                    notify_all("BMS Ticket Checker", f"Tickets are now available at {result['url']}")
                    notified = True
            elif result["status"] == "error":
                print(f"[{now}] Error: {result['message']}")
            else:
                print(f"[{now}] {result['shows'][0]['message']}")
            await asyncio.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped polling")


async def poll_venue(venue_url, movie_keywords, days_ahead=7, interval=300):
    from playwright.async_api import async_playwright

    print(f"Checking {venue_url}")
    print(f"Looking for: {movie_keywords}")
    print(f"Checking next {days_ahead} days, polling every {interval}s")
    print("Press Ctrl+C to stop\n")

    notified = False
    retries = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(**browser_context_args())
        page = await context.new_page()
        try:
            while True:
                try:
                    result = await check_venue_for_movie(page, venue_url, movie_keywords, days_ahead)
                    retries = 0
                except Exception as e:
                    retries += 1
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Error: {e} (retry {retries})")
                    if retries >= RETRY_MAX:
                        print("  Too many errors, re-creating page...")
                        await page.close()
                        page = await context.new_page()
                        retries = 0
                    await asyncio.sleep(RETRY_DELAY)
                    continue

                now = datetime.now().strftime("%H:%M:%S")
                if result["found"]:
                    msg = f'{result["movie"]} found on {result["formatted_date"]}!'
                    print(f"[{now}] {msg}")
                    print(f"  {result['url']}")
                    if not notified:
                        notify_all("BMS Ticket Checker", msg)
                        notified = True
                elif result.get("redirected"):
                    print(f"[{now}] {result['message']}")
                else:
                    print(f"[{now}] {result.get('message', 'Not found')}")

                await asyncio.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped polling")


async def main():
    import argparse

    load_email_config()

    parser = argparse.ArgumentParser(
        description="Check if movie tickets are released on BookMyShow"
    )
    parser.add_argument("movie", help="Movie name to search for")
    parser.add_argument("--city", default="hyderabad", help="City (default: hyderabad)")
    parser.add_argument("--poll", type=int, default=0, help="Poll every N seconds (0 = single check)")
    parser.add_argument("--venue", help="Venue URL or path to watch for this movie")
    parser.add_argument("--days", type=int, default=7, help="Days ahead to scan (default: 7)")
    args = parser.parse_args()

    region_code = REGION_MAP.get(args.city.lower(), args.city.lower()[:3])

    if args.venue:
        venue_url = args.venue
        if not venue_url.startswith("http"):
            venue_url = f"https://in.bookmyshow.com{venue_url}" if venue_url.startswith("/") else venue_url
        movie_keywords = [args.movie.lower()]
        if args.poll > 0:
            await poll_venue(venue_url, movie_keywords, args.days, args.poll)
        else:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(**browser_context_args())
                page = await context.new_page()
                try:
                    result = await retry_playwright(check_venue_for_movie, page, venue_url, movie_keywords, args.days)
                except Exception as e:
                    print(f"Error: {e}")
                    return
                finally:
                    await browser.close()

                if result["found"]:
                    print(f"Found! {result['movie']} on {result['formatted_date']}")
                    print(f"  {result['url']}")
                elif result.get("redirected"):
                    print(f"Not loaded yet: {result['message']}")
                else:
                    print(result.get("message", "Not found at this venue"))
        return

    print(f"Searching for '{args.movie}' in {args.city}...")
    matches = await find_movie_on_bms(args.movie, args.city)
    if not matches:
        print(f"No movie found matching '{args.movie}' on BookMyShow")
        return

    print("Found matches:")
    for name, url in matches:
        print(f"  - {name}")

    exact = [m for m in matches if args.movie.lower() == m[0].lower()]
    selected = (exact or matches)[0]
    if selected != matches[0]:
        print(f"Auto-selected: {selected[0]}")
    else:
        print(f"Using: {selected[0]}")

    movie_name, movie_url = selected
    print(f"\nChecking tickets for: {movie_name}")

    if args.poll > 0:
        await poll_tickets(movie_url, region_code, interval=args.poll)
    else:
        result = await check_tickets_playwright(movie_url, region_code)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
