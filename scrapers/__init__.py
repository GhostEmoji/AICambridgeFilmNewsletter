"""Shared HTTP helpers for scrapers."""

from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def make_session():
    """requests.Session that retries transient failures with exponential backoff."""
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=10,  # roughly 0s, 10s, 20s, 40s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
