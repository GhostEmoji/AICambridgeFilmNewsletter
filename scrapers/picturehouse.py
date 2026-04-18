"""Scraper for Arts Picturehouse Cambridge listings."""

import re
import requests
from datetime import datetime, timedelta

from scrapers import make_session


API_URL = "https://www.picturehouses.com/api/scheduled-movies-ajax"
CINEMA_ID = "002"
CINEMA_NAME = "Arts Picturehouse"


def scrape():
    """Return a list of films showing at Arts Picturehouse Cambridge this week."""
    response = make_session().post(
        API_URL,
        data={"cinema_id": CINEMA_ID},
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; CambridgeFilmNewsletter/1.0)",
            "Referer": "https://www.picturehouses.com/whats-on",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("response") != "success":
        raise RuntimeError(f"Picturehouse API error: {data.get('response')}")

    today = datetime.now().date()
    end_date = today + timedelta(days=7)

    films = []
    for movie in data["movies"]:
        # Filter showtimes to the upcoming week
        week_showtimes = []
        for st in movie.get("show_times", []):
            show_date_str = st.get("date_f", "")
            try:
                show_date = datetime.strptime(show_date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if today <= show_date < end_date:
                week_showtimes.append({
                    "date_iso": show_date_str,
                    "date": st["date"],
                    "time": st["time_format"],
                    "screen": st.get("ScreenName", ""),
                    "booking_url": f"https://web.picturehouses.com/order/showtimes/{CINEMA_ID}-{st['SessionId']}/seats",
                    "sold_out": st.get("SoldoutStatus") == 1,
                    "attributes": st.get("SessionAttributesNames", []),
                })

        if not week_showtimes:
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", movie["Title"].lower()).strip("-")
        films.append({
            "title": movie["Title"],
            "cinema": CINEMA_NAME,
            "image_url": movie.get("image_url", ""),
            "url": f"https://www.picturehouses.com/movie-details/{CINEMA_ID}/{movie['ScheduledFilmId']}/{slug}",
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
