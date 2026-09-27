"""Scraper for Arts Picturehouse Cambridge listings."""

import re
import requests
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from scrapers import make_session


API_URL = "https://www.picturehouses.com/api/scheduled-movies-ajax"
CINEMA_ID = "002"
CINEMA_NAME = "Arts Picturehouse"


def scrape():
    """Return a list of films showing at Arts Picturehouse Cambridge this week."""
    session = make_session()
    response = session.post(
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
        film_url = f"https://www.picturehouses.com/movie-details/{CINEMA_ID}/{movie['ScheduledFilmId']}/{slug}"
        details = _fetch_details(session, film_url, movie["Title"])
        films.append({
            "title": movie["Title"],
            "cinema": CINEMA_NAME,
            "image_url": movie.get("image_url", ""),
            "url": film_url,
            "showtimes": week_showtimes,
            # Hints for TMDB matching, and cinema-provided content preferred over TMDB's.
            # The listing image is a 16:9 still, not a poster, so there's no poster_url.
            "directors": details["directors"],
            "runtime": None,
            "synopsis": details["synopsis"],
            "poster_url": "",
            "certificate": details["certificate"],
        })

    return films


def _fetch_details(session, url, title):
    """Director, synopsis and certificate from the film's page. Best-effort: blanks on failure."""
    details = {"directors": [], "synopsis": "", "certificate": ""}
    try:
        resp = session.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; CambridgeFilmNewsletter/1.0)"}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"    Picturehouse details page failed for {title!r}: {e}")
        return details
    soup = BeautifulSoup(resp.text, "html.parser")

    # <ul><li class="directorInner">Director :</li><li>Danny Boyle</li></ul>
    for ul in soup.select("div.directorDiv ul"):
        items = ul.find_all("li")
        if len(items) < 2:
            continue
        label = items[0].get_text(strip=True).rstrip(":").strip().lower()
        value = items[1].get_text(" ", strip=True)
        if label in ("director", "directors"):
            details["directors"] = [d.strip() for d in value.split(",") if d.strip()]
        elif label == "certificate":
            details["certificate"] = value

    # The synopsis <p> wraps further <p>s; the first innermost one is the synopsis proper
    # (later ones are notices like flashing-light warnings).
    synopsis_div = soup.find("div", class_="synopsisDiv")
    if synopsis_div:
        for p in synopsis_div.find_all("p"):
            if not p.find("p") and p.get_text(strip=True):
                details["synopsis"] = p.get_text(" ", strip=True)
                break
    return details


if __name__ == "__main__":
    results = scrape()
    for film in results:
        print(f"\n{film['title']} ({len(film['showtimes'])} showings)"
              f"  dir={film['directors']} cert={film['certificate']!r}")
        for st in film["showtimes"][:3]:
            print(f"  {st['date']} {st['time']}")
    print(f"\nTotal: {len(results)} films")
