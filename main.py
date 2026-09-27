"""Cambridge Film Newsletter — scrape, enrich, render, send."""

import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

import requests
# import resend
from jinja2 import Environment, FileSystemLoader

import smtplib
from email.mime.text import MIMEText

from scrapers import picturehouse, everyman, the_light


# --- TMDB Enrichment ---
#
# Cinemas give us little more than a title, so a TMDB title search often has several
# plausible answers (remakes, re-releases, same-named films). We use whatever else the
# cinemas tell us — director, runtime — to check a candidate before trusting it, and we
# prefer the cinema's own synopsis/poster/certificate, which are right by definition.
# TMDB then only contributes the rating and link (plus anything the cinemas lack).

TMDB_BASE = "https://api.themoviedb.org/3"
TITLE_MATCH_THRESHOLD = 0.6
PLAUSIBILITY_AGE_YEARS = 3
PLAUSIBILITY_MIN_VOTES = 100
SEARCH_RESULT_LIMIT = 10        # search results considered per title
MAX_CANDIDATE_CHECKS = 6        # detail lookups per title before giving up
RUNTIME_TOLERANCE_MINS = 8      # cinema runtimes vary a little (cuts, rounding)
DESCRIPTION_MAX_CHARS = 120
BBFC_CERTIFICATES = {"U", "PG", "12", "12A", "15", "18", "R18"}

# Manual fixes, keyed by cinema title (case/punctuation-insensitive, cleanup tokens ignored).
# Value is a TMDB movie id to force, or null to skip TMDB for that title entirely.
OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmdb_overrides.json")

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

IN_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

STATUS_LABELS = {
    "director":   ("✔", "verified by director"),
    "runtime":    ("✔", "verified by runtime"),
    "override":   ("↷", "manual override"),
    "unverified": ("?", "title match only (unverified)"),
    "rejected":   ("✘", "rejected: title matches failed checks"),
    "no-match":   ("✘", "no TMDB match"),
    "skipped":    ("–", "skipped (override null / no API key)"),
}


def _clean_title(title):
    """Strip known noise tokens; leaves the original intact when unmatched."""
    cleaned = title
    for token in TITLE_CLEANUP_TOKENS:
        cleaned = cleaned.replace(token, "")
    # Normalise curly quotes to straight, collapse whitespace
    cleaned = cleaned.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalise_title(t):
    t = t.lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\b(the|a|an)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _title_key(title):
    """Key that treats 'Heart Of The Beast' and 'Heart of the Beast' as the same film."""
    return _normalise_title(_clean_title(title))


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


def _name_parts(name):
    """'Alejandro González Iñárritu' -> ['alejandro', 'gonzalez', 'inarritu']."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z]+", ascii_name.lower())


def _name_tokens(name):
    return frozenset(_name_parts(name))


def _same_person(a, b):
    """Tolerates word order ('Park Chan-wook'), dropped middle names and initials
    ('Alejandro G. Iñárritu' vs 'Alejandro González Iñárritu')."""
    ap, bp = _name_parts(a), _name_parts(b)
    if not ap or not bp:
        return False
    at, bt = frozenset(ap), frozenset(bp)
    if at <= bt or bt <= at:
        return True
    if ap[0] != bp[0] or ap[-1] != bp[-1]:
        return False
    # Same first and last name: middles must agree as initials ('g' ~ 'gonzalez'),
    # so Paul W.S. Anderson is still not Paul Thomas Anderson
    short, long_ = sorted((ap[1:-1], bp[1:-1]), key=len)
    return all(any(m.startswith(s) or s.startswith(m) for m in long_) for s in short)


def _directors_match(cinema_directors, tmdb_directors):
    return any(_same_person(c, t) for c in cinema_directors for t in tmdb_directors)


def _truncate(text, limit=DESCRIPTION_MAX_CHARS):
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _year(movie):
    return (movie.get("release_date") or "????")[:4]


def _describe(movie):
    return f"id={movie.get('id')} {movie.get('title', '')!r} ({_year(movie)})"


def _load_overrides():
    if not os.path.exists(OVERRIDES_PATH):
        return {}
    with open(OVERRIDES_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    # Keys starting with "_" are comments
    return {_title_key(k): v for k, v in raw.items() if not k.startswith("_")}


def _gather_hints(films):
    """Combine what every cinema showing this title tells us about it.

    One cinema may give the director and another the runtime, so the lookup for the
    title uses all of them. Each value remembers which cinema it came from, for the logs.
    """
    hints = {"directors": [], "runtime": None, "synopsis": None, "poster": None, "certificate": None}
    for film in films:
        cinema = film["cinema"]
        for d in film.get("directors") or []:
            if not any(_name_tokens(d) == _name_tokens(x) for x, _ in hints["directors"]):
                hints["directors"].append((d, cinema))
        if film.get("runtime") and not hints["runtime"]:
            hints["runtime"] = (film["runtime"], cinema)
        if film.get("synopsis") and not hints["synopsis"]:
            hints["synopsis"] = (film["synopsis"], cinema)
        if film.get("poster_url") and not hints["poster"]:
            hints["poster"] = (film["poster_url"], cinema)
        cert = (film.get("certificate") or "").strip().upper()
        if cert in BBFC_CERTIFICATES and not hints["certificate"]:
            hints["certificate"] = (cert, cinema)
    return hints


def _format_hints(hints):
    parts = []
    if hints["directors"]:
        parts.append("director " + ", ".join(f"{d} ({c})" for d, c in hints["directors"]))
    if hints["runtime"]:
        parts.append(f"runtime {hints['runtime'][0]}m ({hints['runtime'][1]})")
    return "; ".join(parts) if parts else "none — can only match on title"


def _tmdb_get(path, api_key, **params):
    resp = requests.get(f"{TMDB_BASE}{path}", params={"api_key": api_key, **params}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _tmdb_details(movie_id, api_key):
    """Full movie record with directors and release dates, in one request."""
    return _tmdb_get(f"/movie/{movie_id}", api_key, append_to_response="credits,release_dates")


def _verify(details, hints):
    """Check a TMDB film against the cinema's hints.

    Returns (verdict, reason); verdict is 'director', 'runtime', 'unverified' (nothing to
    compare), or None when the film contradicts what the cinema says.
    """
    tmdb_directors = [
        c["name"] for c in details.get("credits", {}).get("crew", []) if c.get("job") == "Director"
    ]
    tmdb_runtime = details.get("runtime") or None
    cinema_directors = [d for d, _ in hints["directors"]]

    if cinema_directors and tmdb_directors:
        if _directors_match(cinema_directors, tmdb_directors):
            return "director", f"director matches ({', '.join(tmdb_directors)})"
        return None, (
            f"director mismatch: cinema says {', '.join(cinema_directors)}, "
            f"TMDB says {', '.join(tmdb_directors)}"
        )
    if hints["runtime"] and tmdb_runtime:
        cinema_runtime = hints["runtime"][0]
        diff = abs(cinema_runtime - tmdb_runtime)
        if diff <= RUNTIME_TOLERANCE_MINS:
            return "runtime", f"runtime matches ({cinema_runtime}m vs TMDB {tmdb_runtime}m)"
        return None, (
            f"runtime mismatch: cinema says {cinema_runtime}m, TMDB says {tmdb_runtime}m "
            f"(tolerance ±{RUNTIME_TOLERANCE_MINS}m)"
        )

    missing = []
    if not cinema_directors and not hints["runtime"]:
        missing.append("cinemas gave no director or runtime")
    if cinema_directors and not tmdb_directors:
        missing.append("TMDB lists no director")
    if hints["runtime"] and not tmdb_runtime:
        missing.append("TMDB lists no runtime")
    return "unverified", "can't verify: " + "; ".join(missing)


def _find_match(query, results, hints, api_key, log):
    """Pick the TMDB film for this title. Returns (details or None, status, reason)."""
    shortlist = []
    for candidate in results[:SEARCH_RESULT_LIMIT]:
        plausible = _is_plausible(candidate)
        score = max(
            _title_similarity(query, candidate.get("title", "")),
            _title_similarity(query, candidate.get("original_title", "")),
        )
        if not plausible:
            flag = "implausible"
        elif score < TITLE_MATCH_THRESHOLD:
            flag = "title-diff "
        else:
            flag = "shortlist  "
            shortlist.append((score, candidate))
        log(
            f"    [{flag}] score={score:.2f}  {candidate.get('title', '')!r} "
            f"(orig={candidate.get('original_title', '')!r}, {_year(candidate)}, "
            f"votes={candidate.get('vote_count', 0)}, id={candidate.get('id')})"
        )

    if not shortlist:
        best = max(
            (max(_title_similarity(query, c.get("title", "")),
                 _title_similarity(query, c.get("original_title", "")))
             for c in results[:SEARCH_RESULT_LIMIT]),
            default=0.0,
        )
        return None, "no-match", f"no plausible result with a similar title (best score {best:.2f})"

    # Stable sort keeps TMDB's popularity order among equally good titles
    shortlist.sort(key=lambda sc: -sc[0])
    have_hints = bool(hints["directors"] or hints["runtime"])
    log(f"  Checking {min(len(shortlist), MAX_CANDIDATE_CHECKS)} of {len(shortlist)} shortlisted:")

    fallback = None  # first candidate we couldn't verify either way
    rejections = []
    for _, candidate in shortlist[:MAX_CANDIDATE_CHECKS]:
        try:
            details = _tmdb_details(candidate["id"], api_key)
        except requests.RequestException as e:
            log(f"    ! {_describe(candidate)}: details lookup FAILED ({e})")
            continue
        verdict, reason = _verify(details, hints)
        mark = {"director": "✔", "runtime": "✔", "unverified": "?"}.get(verdict, "✘")
        log(f"    {mark} {_describe(details)}: {reason}")
        if verdict in ("director", "runtime"):
            return details, verdict, reason
        if verdict == "unverified":
            if not have_hints:
                return details, "unverified", reason  # nothing will verify; don't waste calls
            fallback = fallback or (details, reason)
        else:
            rejections.append(f"{_describe(details)}: {reason}")

    if fallback:
        return fallback[0], "unverified", fallback[1]
    return None, "rejected", "; ".join(rejections) or "all detail lookups failed"


def _gb_certificate(details):
    for country in details.get("release_dates", {}).get("results", []):
        if country.get("iso_3166_1") == "GB":
            for rel in country.get("release_dates", []):
                if rel.get("certification"):
                    return rel["certification"]
    return ""


def _build_enrichment(details, hints):
    """Cinema-provided content first, TMDB for the rest. Returns (enrichment, source notes)."""
    enrichment = {}
    sources = []

    if hints["synopsis"]:
        enrichment["description"] = _truncate(hints["synopsis"][0])
        sources.append(f"description: {hints['synopsis'][1]}")
    elif details and details.get("overview"):
        enrichment["description"] = _truncate(details["overview"])
        sources.append("description: TMDB")

    tmdb_poster = ""
    if details and details.get("poster_path"):
        tmdb_poster = f"https://image.tmdb.org/t/p/w200{details['poster_path']}"
        enrichment["tmdb_poster"] = tmdb_poster
    if hints["poster"]:
        enrichment["poster"] = hints["poster"][0]
        sources.append(f"poster: {hints['poster'][1]}")
    elif tmdb_poster:
        enrichment["poster"] = tmdb_poster
        sources.append("poster: TMDB")

    if hints["certificate"]:
        enrichment["age_rating"] = hints["certificate"][0]
        sources.append(f"cert {hints['certificate'][0]}: {hints['certificate'][1]}")
    elif details and _gb_certificate(details):
        enrichment["age_rating"] = _gb_certificate(details)
        sources.append(f"cert {enrichment['age_rating']}: TMDB")

    if details:
        enrichment["rating"] = details.get("vote_average")
        enrichment["tmdb_url"] = f"https://www.themoviedb.org/movie/{details['id']}"
        sources.append(f"rating {details.get('vote_average') or 0:.1f}: TMDB")

    return enrichment, sources


def _emit_group(headline, lines):
    """Print one title's log: collapsible in GitHub Actions, so the headlines scan as a list."""
    if IN_GITHUB_ACTIONS:
        print(f"::group::{headline}")
        for line in lines:
            print(line)
        print("::endgroup::")
    else:
        print(headline)
        for line in lines:
            print(line)


def _print_summary(outcomes):
    """Totals, then the titles worth checking — in the log and on the Actions run page."""
    counts = {status: 0 for status in STATUS_LABELS}
    for o in outcomes:
        counts[o["status"]] += 1

    print(f"\nTMDB matching summary ({len(outcomes)} titles):")
    for status, (mark, label) in STATUS_LABELS.items():
        if counts[status]:
            print(f"  {mark} {counts[status]:>3}  {label}")

    # Unverified picks may be the wrong film; rejections may be a right film with odd data
    risky = [o for o in outcomes if o["status"] in ("unverified", "rejected")]
    if risky:
        print("\nWorth a look (fix wrong ones in tmdb_overrides.json):")
        for o in risky:
            mark = STATUS_LABELS[o["status"]][0]
            print(f"  {mark} {o['title']!r} [{o['cinemas']}] — {o['summary']}")
    # Usually fine (events, foreign-language titles): cinema details are shown, just no rating
    unmatched = [o for o in outcomes if o["status"] == "no-match"]
    if unmatched:
        print("\nNo TMDB match, so no rating (cinema description/poster still used):")
        for o in unmatched:
            print(f"  ✘ {o['title']!r} [{o['cinemas']}] — {o['summary']}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    md = ["## TMDB matching", "", "| | Count |", "|---|---|"]
    md += [f"| {STATUS_LABELS[s][0]} {STATUS_LABELS[s][1]} | {n} |" for s, n in counts.items() if n]
    md += ["", "| | Cinema title | Cinemas | Result |", "|---|---|---|---|"]
    # Most likely to need attention first
    order = ["unverified", "rejected", "no-match", "override", "skipped", "runtime", "director"]
    for o in sorted(outcomes, key=lambda o: (order.index(o["status"]), o["title"].lower())):
        result = o["summary"].replace("|", "\\|")
        md.append(f"| {STATUS_LABELS[o['status']][0]} | {o['title']} | {o['cinemas']} | {result} |")
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")


def enrich_with_tmdb(films, api_key):
    """Add description, poster, age rating (cinema first, else TMDB), plus TMDB rating and link."""
    if not api_key:
        print("No TMDB_API_KEY set: using cinema-provided details only")
    overrides = _load_overrides()
    if overrides:
        print(f"Loaded {len(overrides)} TMDB override(s) from {os.path.basename(OVERRIDES_PATH)}")

    # Look each film up once, however many cinemas show it, using all their hints
    groups = {}
    for film in films:
        groups.setdefault(_title_key(film["title"]), []).append(film)
    print(f"Enriching {len(films)} listings ({len(groups)} distinct titles)")

    outcomes = []
    for key, group in groups.items():
        title = group[0]["title"]
        query = _clean_title(title)
        hints = _gather_hints(group)
        cinemas = ", ".join(sorted({f["cinema"] for f in group}))
        titles_seen = sorted({f["title"] for f in group})
        lines = [f"  Cinema titles: {' / '.join(repr(t) for t in titles_seen)} at {cinemas}"]
        lines.append(f"  Hints: {_format_hints(hints)}")
        log = lines.append

        details, status, reason = None, "no-match", ""
        if key in overrides:
            forced = overrides[key]
            if forced is None:
                status, reason = "skipped", "override: null (TMDB skipped on purpose)"
            elif api_key:
                try:
                    details = _tmdb_details(forced, api_key)
                    status, reason = "override", f"override: forced TMDB id {forced}"
                except requests.RequestException as e:
                    status, reason = "no-match", f"override id {forced} lookup FAILED: {e}"
        elif not api_key:
            status, reason = "skipped", "no TMDB_API_KEY"
        else:
            log(f"  Search {query!r}" + (f" (cleaned from {title!r})" if query != title else ""))
            try:
                results = _tmdb_get("/search/movie", api_key, query=query).get("results", [])
            except requests.RequestException as e:
                results = None
                status, reason = "no-match", f"search FAILED: {e}"
            if results == []:
                status, reason = "no-match", "TMDB search returned no results"
            elif results:
                details, status, reason = _find_match(query, results, hints, api_key, log)

        enrichment, sources = _build_enrichment(details, hints)
        for film in group:
            film.update(enrichment)

        mark, _ = STATUS_LABELS[status]
        if details:
            summary = f"TMDB {_describe(details)} — {reason}"
            log(f"  Result: {mark} {_describe(details)}  {enrichment['tmdb_url']}")
        else:
            summary = reason
            log(f"  Result: {mark} {reason}")
        log(f"  Using: {' · '.join(sources) if sources else 'nothing'}")
        _emit_group(f"{mark} {title} — {summary}", lines)
        outcomes.append({"title": title, "cinemas": cinemas, "status": status, "summary": summary})

    _print_summary(outcomes)
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
                "poster": film.get("poster", ""),
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
        if film.get("poster") and not entry["poster"]:
            entry["poster"] = film["poster"]
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
                "poster": f["poster"],
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
        recipient = "louisemclennan@gmail.com, cambridgecinemashowings@gmail.com"
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
    print("Enriching with cinema details and TMDB...")
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
