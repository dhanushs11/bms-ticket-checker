# BMS Ticket Checker

Monitor BookMyShow for movie ticket releases and get notified when your preferred show opens for booking.

## Features

- Search movies by name and city
- Check ticket availability for any movie
- Monitor a specific venue for a movie to appear
- Target a specific date
- Polling mode with macOS desktop notifications
- Email alerts (SMTP)
- Retry logic on failure
- Docker support
- GitHub Actions workflow for 24/7 cloud monitoring

## Quick Start

```bash
pip install -r requirements.txt
playwright install chromium

# Check a movie
python checker.py "Spider-Man" --city chennai

# Check at a specific venue + date
python checker.py "The Odyssey" --city chennai \
  --venue "/cinemas/CHEN/pvr-palazzo-the-nexus-vijaya-mall/buytickets/PVPZ/20260730"

# Poll every 5 minutes until found
python checker.py "Spider-Man" --city chennai \
  --venue "/cinemas/CHEN/pvr-palazzo-the-nexus-vijaya-mall/buytickets/PVPZ/20260730" \
  --poll 300
```

## Usage

```
usage: checker.py [-h] [--city CITY] [--poll POLL] [--venue VENUE] [--days DAYS] movie

positional arguments:
  movie          Movie name to search for

options:
  --city CITY    City (default: hyderabad)
  --poll POLL    Poll every N seconds (0 = single check)
  --venue VENUE  Venue URL or path to watch for this movie
  --days DAYS    Days ahead to scan (default: 7)
```

### Examples

```bash
# Single check across all venues in a city
python checker.py "Pushpa 2" --city hyderabad

# Monitor for tickets opening at a specific venue
python checker.py "Jana Nayagan" --city chennai \
  --venue "/cinemas/CHEN/pvr-palazzo-the-nexus-vijaya-mall/buytickets/PVPZ"

# Poll every 60 seconds with desktop notification
python checker.py "Spider-Man" --city chennai --poll 60

# Scan 14 days ahead
python checker.py "Spider-Man" --city chennai --days 14
```

Finding the venue URL: go to the venue's page on BookMyShow and copy the path after `in.bookmyshow.com` (e.g. `/cinemas/CHEN/pvr-palazzo-the-nexus-vijaya-mall/buytickets/PVPZ`).

## Email Alerts

Set these environment variables:

```bash
export BMS_SMTP_HOST=smtp.gmail.com
export BMS_SMTP_PORT=587
export BMS_SMTP_USER=your.email@gmail.com
export BMS_SMTP_PASS=your-app-password
export BMS_TO_EMAIL=your.email@gmail.com
export BMS_FROM_EMAIL=your.email@gmail.com
```

## Docker

```bash
docker compose up -d
```

Edit `docker-compose.yml` to set your movie, venue, and email config.

## GitHub Actions

The included workflow (`.github/workflows/check.yml`) runs on a cron schedule and can also be triggered manually via the GitHub UI.

1. Fork / push this repo
2. Add secrets in repo Settings → Secrets → Actions:
   - `BMS_SMTP_HOST`, `BMS_SMTP_PORT`, `BMS_SMTP_USER`, `BMS_SMTP_PASS`, `BMS_TO_EMAIL`
3. The workflow checks every 10 minutes and emails you on match
4. Or go to Actions → BMS Ticket Checker → Run workflow to run on demand

## Limitations

- Relies on BookMyShow's public web pages (may break if they change their UI)
- Cloudflare-protected pages are handled via headless Chromium through Playwright
- **Respect BookMyShow's ToS** — this is a personal tool, not for commercial use
