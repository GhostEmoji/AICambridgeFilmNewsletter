"""Cambridge Film Newsletter — scrape, enrich, render, send."""

import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
# import resend
from jinja2 import Environment, FileSystemLoader

import smtplib
from email.mime.text import MIMEText

from scrapers import picturehouse, everyman, the_light


# --- TMDB Enrichment ---

TMDB_BASE = "https://api.themoviedb.org/3"
TITLE_MATCH_THRESHOLD = 0.6
PLAUSIBILITY_AGE_YEARS = 3
PLAUSIBILITY_MIN_VOTES = 100

# Exact substrings stripped from cinema titles before TMDB lookup.
# Whitelist-only: if you see a new pattern in the listings, add the exact string here.
# Case-sensitive — list each variant you see.
TITLE_CLEANUP_TOKENS = [
    # Series / programming prefixes (include the trailing colon)
    "National Theatre Live:",
    "NT Live:",
    "RBO Cinema Season 2025-26:",
    "RBO Live:",
    "RBO:",
    "Record Store Day:",
    "Throwback:",
    "Toddler Club:",
    "Beyond:",
    # Parentheticals
    "(2026 Re-release)",
    "(4K Re-Release)",
    "(4k Re-Release)",
    "(25th Anniversary)",
    "(Dubbed)",
    "(Subbed)",
    "(Hindi)",
    "(Mandarin)",
    "(Malayalam)",
    "(2026)",
    # Brackets
    "[Subtitled]",
    "[Dubbed]",
    # Suffix add-ons
    "+ Live Broadcast Q&A",
    "+ Q&A",
]


def _clean_title(title):
    """Strip known noise tokens; leaves the original intact when unmatched."""
    cleaned = title
    for token in TITLE_CLEANUP_TOKENS:
        cleaned = cleaned.replace(token, "")
    # Normalise curly quotes to straight, collapse whitespace
    cleaned = cleaned.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalise_title(t):
    t = t.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\b(the|a|an)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _title_similarity(query, candidate):
    return difflib.SequenceMatcher(
        None, _normalise_title(query), _normalise_title(candidate)
    ).ratio()


def _is_plausible(candidate, today=None):
    """Reject films that are both old AND obscure — unlikely Cambridge showings."""
    try:
        release = datetime.strptime(candidate.get("release_date", ""), "%Y-%m-%d").date()
    except ValueError:
        return True  # unknown date — give benefit of the doubt
    today = today or datetime.now().date()
    age_years = (today - release).days / 365.25
    if age_years <= PLAUSIBILITY_AGE_YEARS:
        return True  # recent enough that low vote counts are expected
    return candidate.get("vote_count", 0) >= PLAUSIBILITY_MIN_VOTES


def _best_tmdb_match(query_title, results, limit=5):
    """Pick the top-N result whose title best matches the query, or None."""
    best = None
    best_score = 0.0
    for candidate in results[:limit]:
        if not _is_plausible(candidate):
            continue
        score = max(
            _title_similarity(query_title, candidate.get("title", "")),
            _title_similarity(query_title, candidate.get("original_title", "")),
        )
        if score > best_score:
            best_score = score
            best = candidate
    if best_score < TITLE_MATCH_THRESHOLD:
        return None, best_score
    return best, best_score


def enrich_with_tmdb(films, api_key):
    """Add TMDB overview, rating, and poster to each film."""
    if not api_key:
        print("No TMDB_API_KEY set, skipping enrichment")
        return films

    seen_titles = {}
    for film in films:
        title = film["title"]
        query = _clean_title(title)
        # Avoid duplicate lookups for the same film at different cinemas
        if query in seen_titles:
            film.update(seen_titles[query])
            continue

        try:
            resp = requests.get(
                f"{TMDB_BASE}/search/movie",
                params={"api_key": api_key, "query": query},
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except requests.RequestException as e:
            print(f"  TMDB lookup failed for '{query}': {e}")
            results = []

        enrichment = {}
        top, score = _best_tmdb_match(query, results) if results else (None, 0.0)
        if results and top is None:
            print(f"  No confident TMDB match for '{title}' (best score {score:.2f}) — skipping enrichment")
        if top is not None:
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

        seen_titles[query] = enrichment
        film.update(enrichment)

    return films


# --- Age rating colours (WCAG AA contrast) ---

AGE_RATING_COLOURS = {
    "U":   {"bg": "#1b8a2a", "text": "#ffffff"},
    "PG":  {"bg": "#c98f00", "text": "#ffffff"},
    "12":  {"bg": "#d4710a", "text": "#ffffff"},
    "12A": {"bg": "#d4710a", "text": "#ffffff"},
    "15":  {"bg": "#c43e00", "text": "#ffffff"},
    "18":  {"bg": "#c82333", "text": "#ffffff"},
}
DEFAULT_RATING_COLOUR = {"bg": "#6c757d", "text": "#ffffff"}


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
                "showtimes": [],
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
            entry["showtimes"].append({
                "cinema": cinema,
                "date": st["date_iso"],
                "time": st["time"],
                "booking_url": st.get("booking_url", ""),
                "sold_out": st.get("sold_out", False),
                "attributes": st.get("attributes", []),
            })

    # Sort dates chronologically and format for display
    result = []
    for entry in merged.values():
        sorted_isos = sorted(entry["dates"])
        entry["dates_iso"] = sorted_isos
        entry["dates"] = [
            datetime.strptime(d, "%Y-%m-%d").strftime("%a %d")
            for d in sorted_isos
        ]
        entry["showtimes"].sort(key=lambda s: (s["date"], s["time"]))
        colours = AGE_RATING_COLOURS.get(entry["age_rating"], DEFAULT_RATING_COLOUR)
        entry["age_rating_bg"] = colours["bg"]
        entry["age_rating_text"] = colours["text"]
        result.append(entry)
    result.sort(key=lambda f: (-len(f["dates"]), f["title"].lower()))
    return result


# --- Export JSON ---

def export_json(films, path):
    """Write film showings to a JSON file for the website."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "films": [
            {
                "title": f["title"],
                "description": f["description"],
                "rating": f["rating"],
                "tmdb_url": f["tmdb_url"],
                "tmdb_poster": f["tmdb_poster"],
                "age_rating": f["age_rating"],
                "image_url": f["image_url"],
                "cinemas": f["cinemas"],
                "dates": f["dates_iso"],
                "showtimes": f["showtimes"],
            }
            for f in films
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"  Written {path}")


# --- Render email ---

def render_email(films, date_str, failed_cinemas=None):
    """Render the newsletter HTML from the template."""
    env = Environment(
        loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
        autoescape=True,
    )
    template = env.get_template("newsletter.html")
    return template.render(films=films, date=date_str, failed_cinemas=failed_cinemas or [])


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

    failed_cinemas = []
    for name, scrape_fn in scrapers:
        try:
            films = scrape_fn()
            print(f"  {name}: {len(films)} films")
            all_films.extend(films)
        except Exception as e:
            print(f"  {name}: FAILED — {e}")
            failed_cinemas.append(name)

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
    html = render_email(merged, date_str, failed_cinemas)

    # Step 4: Export JSON
    print("Exporting JSON...")
    export_json(merged, "data/showings.json")

    # Step 5: Send
    if args.test:
        print("Sending TEST email...")
    else:
        print("Sending email...")
    send_email(html, to_emails, from_email, test=args.test)

    print("Done!")


if __name__ == "__main__":
    main()
