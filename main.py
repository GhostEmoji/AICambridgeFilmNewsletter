"""Cambridge Film Newsletter — scrape, enrich, render, send."""

import argparse
import os
import sys
import time
from datetime import datetime

import requests
# import resend
from jinja2 import Environment, FileSystemLoader

import smtplib
from email.mime.text import MIMEText

from scrapers import picturehouse, everyman, the_light


# --- TMDB Enrichment ---

TMDB_BASE = "https://api.themoviedb.org/3"


def enrich_with_tmdb(films, api_key):
    """Add TMDB overview, rating, and poster to each film."""
    if not api_key:
        print("No TMDB_API_KEY set, skipping enrichment")
        return films

    seen_titles = {}
    for film in films:
        title = film["title"]
        # Avoid duplicate lookups for the same film at different cinemas
        if title in seen_titles:
            film.update(seen_titles[title])
            continue

        try:
            resp = requests.get(
                f"{TMDB_BASE}/search/movie",
                params={"api_key": api_key, "query": title},
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except requests.RequestException as e:
            print(f"  TMDB lookup failed for '{title}': {e}")
            results = []

        enrichment = {}
        if results:
            top = results[0]
            movie_id = top["id"]
            overview = top.get("overview", "")
            if len(overview) > 120:
                overview = overview[:117] + "..."
            enrichment["description"] = overview
            enrichment["rating"] = top.get("vote_average")
            enrichment["tmdb_url"] = f"https://www.themoviedb.org/movie/{movie_id}"
            poster_path = top.get("poster_path")
            if poster_path:
                enrichment["tmdb_poster"] = f"https://image.tmdb.org/t/p/w200{poster_path}"

            # Fetch GB age rating
            try:
                rd_resp = requests.get(
                    f"{TMDB_BASE}/movie/{movie_id}/release_dates",
                    params={"api_key": api_key},
                    timeout=10,
                )
                rd_resp.raise_for_status()
                for country in rd_resp.json().get("results", []):
                    if country["iso_3166_1"] == "GB":
                        for rel in country["release_dates"]:
                            cert = rel.get("certification", "")
                            if cert:
                                enrichment["age_rating"] = cert
                                break
                        break
            except requests.RequestException:
                pass

        seen_titles[title] = enrichment
        film.update(enrichment)

    return films


# --- Merge films across cinemas ---

CINEMA_URLS = {
    "Arts Picturehouse": "https://www.picturehouses.com/cinema/arts-picturehouse-cambridge",
    "Everyman": "https://www.everymancinema.com/venues-list/g02am-everyman-cambridge",
    "The Light": "https://cambridge.thelight.co.uk",
}


def merge_films(films):
    """Merge the same film across cinemas into a single entry with per-cinema info."""
    merged = {}
    for film in films:
        # Normalise title for matching
        key = film["title"].lower().strip()
        if key not in merged:
            merged[key] = {
                "title": film["title"],
                "description": film.get("description", ""),
                "rating": film.get("rating"),
                "tmdb_url": film.get("tmdb_url", ""),
                "tmdb_poster": film.get("tmdb_poster", ""),
                "age_rating": film.get("age_rating", ""),
                "image_url": film.get("image_url", ""),
                "cinemas": {},
                "dates": set(),
            }
        entry = merged[key]
        # Carry over TMDB data if this copy has it
        if film.get("description") and not entry["description"]:
            entry["description"] = film["description"]
        if film.get("rating") and not entry["rating"]:
            entry["rating"] = film["rating"]
        if film.get("tmdb_url") and not entry["tmdb_url"]:
            entry["tmdb_url"] = film["tmdb_url"]
        if film.get("tmdb_poster") and not entry["tmdb_poster"]:
            entry["tmdb_poster"] = film["tmdb_poster"]
        if film.get("age_rating") and not entry["age_rating"]:
            entry["age_rating"] = film["age_rating"]

        cinema = film["cinema"]
        entry["cinemas"][cinema] = film.get("url", CINEMA_URLS.get(cinema, ""))
        for st in film.get("showtimes", []):
            entry["dates"].add(st["date_iso"])

    # Sort dates chronologically and format for display
    result = []
    for entry in merged.values():
        sorted_isos = sorted(entry["dates"])
        entry["dates"] = [
            datetime.strptime(d, "%Y-%m-%d").strftime("%a %d")
            for d in sorted_isos
        ]
        result.append(entry)
    result.sort(key=lambda f: (-len(f["dates"]), f["title"].lower()))
    return result


# --- Render email ---

def render_email(films, date_str):
    """Render the newsletter HTML from the template."""
    env = Environment(
        loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
        autoescape=True,
    )
    template = env.get_template("newsletter.html")
    return template.render(films=films, date=date_str)


# --- Send email ---

def send_email(html, to_emails, from_email, test=False):
    """Send the newsletter via Gmail."""
    date_str = datetime.now().strftime("%d %b %Y")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if test:
        recipient = "louisemclennan@gmail.com"
        subject = f"[TEST] Films This Week — {date_str}"
    else:
        recipient = "cambridge-cinema-showings@googlegroups.com"
        subject = f"Films This Week — {date_str}"

    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = "Cambridge Cinema Showings"
    msg["To"] = recipient

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login("cambridgecinemashowings@gmail.com", gmail_app_password)
        server.send_message(msg)

    print(f"  Sent to {msg['To']}")

    # for i, email in enumerate(to_emails):
    #     if i > 0:
    #         time.sleep(1)
    #     resend.Emails.send({
    #         "from": from_email,
    #         "to": email.strip(),
    #         "subject": f"Cambridge Cinema This Week — {date_str}",
    #         "html": html,
    #     })
    #     print(f"  Sent to {email.strip()}")

# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Cambridge Film Newsletter")
    parser.add_argument("--test", action="store_true",
                        help="Send to TEST_EMAIL only instead of the subscriber list")
    args = parser.parse_args()

    tmdb_key = os.environ.get("TMDB_API_KEY", "")
    # resend_key = os.environ.get("RESEND_API_KEY", "")
    to_emails_str = os.environ.get("TO_EMAILS", "")
    from_email = os.environ.get("FROM_EMAIL", "Cambridge Films <newsletter@resend.dev>")

    # if not resend_key:
    #     print("ERROR: RESEND_API_KEY not set")
    #     sys.exit(1)
    # if not to_emails_str:
    #     print("ERROR: TO_EMAILS not set")
    #     sys.exit(1)

    # resend.api_key = resend_key
    to_emails = [e.strip() for e in to_emails_str.split(",") if e.strip()]

    # Step 1: Scrape
    print("Scraping cinema listings...")
    all_films = []

    scrapers = [
        ("Arts Picturehouse", picturehouse.scrape),
        ("Everyman", everyman.scrape),
        ("The Light", the_light.scrape),
    ]

    for name, scrape_fn in scrapers:
        try:
            films = scrape_fn()
            print(f"  {name}: {len(films)} films")
            all_films.extend(films)
        except Exception as e:
            print(f"  {name}: FAILED — {e}")

    if not all_films:
        print("No films found from any cinema. Exiting.")
        sys.exit(0)

    # Step 2: Enrich
    print("Enriching with TMDB...")
    all_films = enrich_with_tmdb(all_films, tmdb_key)

    # Step 3: Render
    print("Rendering email...")
    merged = merge_films(all_films)
    print(f"  {len(merged)} unique films across all cinemas")
    date_str = datetime.now().strftime("%d %b %Y")
    html = render_email(merged, date_str)

    # Step 4: Send
    if args.test:
        print("Sending TEST email...")
    else:
        print("Sending email...")
    send_email(html, to_emails, from_email, test=args.test)

    print("Done!")


if __name__ == "__main__":
    main()
