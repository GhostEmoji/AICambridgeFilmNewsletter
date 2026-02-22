"""Scraper for The Light Cambridge listings."""

import json
import re
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup


NOW_SHOWING_URL = "https://cambridge.thelight.co.uk/cinema/nowshowing"
BASE_URL = "https://cambridge.thelight.co.uk"
CINEMA_NAME = "The Light"


def scrape():
    """Return a list of films showing at The Light Cambridge this week."""
    response = requests.get(
        NOW_SHOWING_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; CambridgeFilmNewsletter/1.0)",
        },
        timeout=30,
    )
    response.raise_for_status()
    html = response.text

    # Extract schedule JSON from inline ScheduleBrowser scripts
    schedule_pattern = re.compile(
        r"ScheduleBrowser\(\{[^}]*selector:\s*['\"]#prog-(\d+)['\"].*?data:\s*(\[.*?\])\s*\}\s*\)",
        re.DOTALL,
    )
    schedules = {}
    for match in schedule_pattern.finditer(html):
        programme_id = match.group(1)
        data_json = match.group(2)
        try:
            schedules[programme_id] = json.loads(data_json)
        except json.JSONDecodeError:
            continue

    # Parse HTML for film metadata
    soup = BeautifulSoup(html, "html.parser")

    today = datetime.now().date()
    end_date = today + timedelta(days=7)

    films = []

    # Iterate over div.prog containers — each wraps one film's info + schedule
    for card in soup.find_all("div", class_="prog"):
        programme_id = card.get("data-prog", "")
        schedule_data = schedules.get(programme_id, [])

        # Find title from h2 > a inside the card
        title_tag = card.find(["h2", "h3"])
        if not title_tag:
            continue
        title_link = title_tag.find("a")
        if title_link:
            title = title_link.get_text(strip=True)
            slug = title_link.get("href", "").strip("/")
            film_url = f"{BASE_URL}/{slug}" if slug else ""
        else:
            title = title_tag.get_text(strip=True)
            film_url = ""

        if not title:
            continue

        # Find poster image (prefer .poster class)
        img_tag = card.find("img", class_="poster") or card.find("img")
        image_url = img_tag.get("src", "") if img_tag else ""

        # Filter showtimes to this week
        week_showtimes = []
        for day in schedule_data:
            date_key = day.get("Key", "")  # "20260223"
            display_date = day.get("Display", "")  # "Mon 23 Feb"

            try:
                show_date = datetime.strptime(date_key, "%Y%m%d").date()
            except ValueError:
                continue

            if not (today <= show_date < end_date):
                continue

            for session in day.get("Sessions", []):
                time_display = session.get("Display", "")  # "14.00"
                # Convert dot format to colon
                time_display = time_display.replace(".", ":")

                format_display = session.get("FormatDisplay", "2D")

                week_showtimes.append({
                    "date_iso": show_date.strftime("%Y-%m-%d"),
                    "date": display_date,
                    "time": time_display,
                    "screen": format_display,
                    "booking_url": film_url,  # No direct session booking URL; link to film page
                    "sold_out": session.get("CssClass", "") != "availGreen",
                    "attributes": [c.get("Title", "") for c in session.get("Collections", [])],
                })

        if not week_showtimes:
            continue

        films.append({
            "title": title,
            "cinema": CINEMA_NAME,
            "image_url": image_url,
            "url": film_url,
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
