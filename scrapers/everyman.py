"""Scraper for Everyman Cambridge listings."""

import json
import re
import urllib.parse
import requests
from datetime import datetime, timedelta


SCHEDULE_URL = "https://www.everymancinema.com/api/gatsby-source-boxofficeapi/schedule"
MOVIES_URL = "https://www.everymancinema.com/api/gatsby-source-boxofficeapi/movies"
THEATER_ID = "G02AM"
TIMEZONE = "Europe/London"
CINEMA_NAME = "Everyman"


def scrape():
    """Return a list of films showing at Everyman Cambridge this week."""
    today = datetime.now().date()
    end_date = today + timedelta(days=7)

    # Step 1: Get schedule
    theaters_param = json.dumps({"id": THEATER_ID, "timeZone": TIMEZONE}, separators=(",", ":"))
    schedule_resp = requests.get(
        SCHEDULE_URL,
        params={
            "theaters": theaters_param,
            "from": today.isoformat(),
            "to": end_date.isoformat(),
        },
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; CambridgeFilmNewsletter/1.0)",
        },
        timeout=30,
    )
    schedule_resp.raise_for_status()
    schedule_data = schedule_resp.json()

    theater_data = schedule_data.get(THEATER_ID, {})
    schedule = theater_data.get("schedule", {})

    if not schedule:
        return []

    # Step 2: Get movie metadata
    movie_ids = list(schedule.keys())
    movies_resp = requests.get(
        MOVIES_URL,
        params=[("ids", mid) for mid in movie_ids],
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; CambridgeFilmNewsletter/1.0)",
        },
        timeout=30,
    )
    movies_resp.raise_for_status()
    movies_list = movies_resp.json()
    movies_by_id = {str(m["id"]): m for m in movies_list}

    # Step 3: Combine schedule + metadata
    films = []
    for movie_id, dates in schedule.items():
        meta = movies_by_id.get(str(movie_id), {})
        title = meta.get("title", meta.get("originalTitle", f"Film {movie_id}"))
        # Clean up escaped quotes in titles
        title = title.strip('"').strip("'")

        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")

        week_showtimes = []
        for date_str, sessions in dates.items():
            try:
                show_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                display_date = show_date.strftime("%a %d %b")
            except ValueError:
                display_date = date_str

            for session in sessions:
                starts_at = session.get("startsAt", "")
                try:
                    dt = datetime.fromisoformat(starts_at)
                    time_str = dt.strftime("%-I:%M %p")
                except (ValueError, TypeError):
                    time_str = starts_at

                # Get booking URL from ticketing data
                booking_url = ""
                ticketing = session.get("data", {}).get("ticketing", [])
                for t in ticketing:
                    if t.get("type") == "DESKTOP" and t.get("provider") == "default":
                        urls = t.get("urls", [])
                        if urls:
                            booking_url = urls[0]
                        break

                week_showtimes.append({
                    "date_iso": date_str,
                    "date": display_date,
                    "time": time_str,
                    "screen": "",
                    "booking_url": booking_url,
                    "sold_out": session.get("isExpired", False),
                    "attributes": session.get("tags", []),
                })

        if not week_showtimes:
            continue

        runtime_mins = meta.get("runtime", 0) // 60 if meta.get("runtime") else None

        films.append({
            "title": title,
            "cinema": CINEMA_NAME,
            "image_url": meta.get("poster", ""),
            "url": f"https://www.everymancinema.com/film-listing/{movie_id}-{slug}",
            "showtimes": week_showtimes,
        })

    return films


if __name__ == "__main__":
    results = scrape()
    for film in results:
        print(f"\n{film['title']} ({len(film['showtimes'])} showings)")
        for st in film["showtimes"][:3]:
            print(f"  {st['date']} {st['time']}")
    print(f"\nTotal: {len(results)} films")
