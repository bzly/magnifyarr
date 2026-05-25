#!/usr/bin/env python3
"""
magnifyarr: a missing media searcher for Sonarr and Radarr.

Periodically fetch the missing list(s) and trigger episode/movie searches,
replicating the behaviour of clicking the magnifying glass in the Wanted > Missing UI.

General pattern:
* Fetch all missing items, sorted by last search time ascending.
*   -> Optionally filter out old (vs air/release date) items (slow/slowest tiers).
* Trigger a EpisodeSearch or MoviesSearch command for the first SEARCH_LIMIT eligible items.
* Whilst the command is running, poll its status and log message updates.
* Repeat every SEARCH_INTERVAL_MINUTES minutes.

If both Sonarr and Radarr are configured, they are interleaved to avoid simultaneous runs, e.g.:
    if SEARCH_INTERVAL_MINUTES=10, Sonarr runs at :00, :10, … and Radarr runs at :05, :15, …

Endpoint docs:
* To get the missing list: https://sonarr.tv/docs/api/#v3/tag/missing
* To trigger an episode search: https://sonarr.tv/docs/api/#v3/tag/command/POST/api/v3/command
    Valid commands aren't documented, legacy docs here have them and parameters:
    https://nzbdrone.readthedocs.io/API/Command/
* To check the status of the search: https://sonarr.tv/docs/api/#v3/tag/command/GET/api/v3/command/{id}

Tier logic (disabled if env vars _AFTER_DAYS not set):
* Items older than SLOW_AFTER_DAYS are only searched every SLOW_INTERVAL_DAYS days.
* Items older than SLOWEST_AFTER_DAYS are only searched every SLOWEST_INTERVAL_DAYS days.
* Items with no lastSearchTime are always eligible regardless of age.
"""

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Self

import requests


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# Sonarr /command statuses that mean "in progress" - used in SonarrClient.poll_command()
_ACTIVE_STATUSES = {"queued", "started"}

# Fetch an arbitrarily large candidate pool to filter episodes from
#   a response with 1000 episodes in would be ~500KB (est)
_CANDIDATE_POOL_SIZE = 1000


def _positive_int(name: str, default: int) -> int:
    """Read a required or defaulted positive integer from the environment."""
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        log.error("Env var %s must be a positive integer, got: %r", name, raw)
        sys.exit(1)


def _optional_positive_int(name: str) -> int | None:
    """Read an optional positive integer from the environment."""
    if os.getenv(name) is None:
        return None
    return _positive_int(name, 0)  # var is set - default val unreachable


@dataclass
class Config:
    sonarr_base_url:       str
    radarr_base_url:       str
    sonarr_api_key:        str | None
    radarr_api_key:        str | None
    search_limit:          int
    interval_minutes:      int
    slow_after_days:       int | None
    slow_interval_days:    int
    slowest_after_days:    int | None
    slowest_interval_days: int

    @classmethod
    def from_env(cls) -> Self:
        """Read environment variables, check validity, set defaults."""
        sonarr_api_key = os.getenv("SONARR_API_KEY")
        radarr_api_key = os.getenv("RADARR_API_KEY")

        if not sonarr_api_key and not radarr_api_key:
            log.error("At least one of SONARR_API_KEY or RADARR_API_KEY must be set.")
            sys.exit(1)

        slow_after_days    = _optional_positive_int("SLOW_AFTER_DAYS")
        slowest_after_days = _optional_positive_int("SLOWEST_AFTER_DAYS")

        if slow_after_days and slowest_after_days and slow_after_days >= slowest_after_days:
            log.error(
                "SLOW_AFTER_DAYS (%d) must be less than SLOWEST_AFTER_DAYS (%d)",
                slow_after_days,
                slowest_after_days,
            )
            sys.exit(1)

        return cls(
            sonarr_base_url=       os.getenv("SONARR_URL", "http://sonarr:8989").rstrip("/"),
            radarr_base_url=       os.getenv("RADARR_URL", "http://radarr:7878").rstrip("/"),
            sonarr_api_key=        sonarr_api_key,
            radarr_api_key=        radarr_api_key,
            search_limit=          _positive_int("SEARCH_LIMIT", 10),
            interval_minutes=      _positive_int("SEARCH_INTERVAL_MINUTES", 5),
            slow_after_days=       slow_after_days,
            slow_interval_days=    _positive_int("SLOW_INTERVAL_DAYS", 1),
            slowest_after_days=    slowest_after_days,
            slowest_interval_days= _positive_int("SLOWEST_INTERVAL_DAYS", 7),
        )

    def log_startup(self, active: str) -> None:
        tiers = []
        if self.slow_after_days:
            tiers.append(f"slow=>{self.slow_after_days}+d: /{self.slow_interval_days}d")
        if self.slowest_after_days:
            tiers.append(f"slowest=>{self.slowest_after_days}+d: /{self.slowest_interval_days}d")

        log.info(
            "Starting magnifyarr [%s] | limit=%d | interval=%dm%s",
            active,
            self.search_limit,
            self.interval_minutes,
            f" | tiers: {', '.join(tiers)}" if tiers else " | tiers: disabled",
        )


class ArrClient:
    # These are defined (and differ) in the subclasses
    sort_key:       str = NotImplemented
    search_command: str = NotImplemented
    id_field:       str = NotImplemented
    age_field:      str = NotImplemented

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    @property
    def display_name(self) -> str:
        return type(self).__name__#.replace("Client", "")

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v3{path}"

    def ping(self) -> bool:
        """Health check."""
        try:
            resp = self.session.get(self._url("/system/status"), timeout=10)
            resp.raise_for_status()
            version = resp.json().get("version", "unknown")
            log.info("Connected to %s %s at %s", self.display_name, version, self.base_url)
            return True
        except requests.RequestException as exc:
            log.error("Cannot reach %s: %s", self.display_name, exc)
            return False

    def get_missing(self, page_size: int) -> list[dict]:
        """Fetch up to page_size missing items (least recently searched first)."""
        params = {
            "pageSize": page_size,
            "page": 1,
            "sortKey": self.sort_key,
            "sortDirection": "ascending",
            "includeSeries": True,  # for better logging, but it does make the response bigglier
        }
        resp = self.session.get(self._url("/wanted/missing"), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("records", [])

    def trigger_search(self, ids: list[int]) -> dict:
        """
        Sends a POST /command to trigger a search for the given IDs.
        Equivalent to clicking the magnifying glass on the missing list.
        Returns the initial command object (status = 'queued').
        """
        payload = {"name": self.search_command, self.id_field: ids}
        resp = self.session.post(self._url("/command"), json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_command(self, command_id: int) -> dict:
        """Fetch the current state of a previously submitted command."""
        resp = self.session.get(self._url(f"/command/{command_id}"), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def poll_command(self, command: dict, poll_interval: int = 1) -> Iterator[dict]:
        """
        Poll GET /command until it leaves the active states.
        Yield responses where the message has changed.
        """
        command_id = command["id"]
        last_message: str | None = None
        current = command

        while current["status"] in _ACTIVE_STATUSES:
            message = current.get("message")
            if message and message != last_message:
                last_message = message
                yield current

            time.sleep(poll_interval)

            try:
                current = self.get_command(command_id)
            except requests.RequestException as exc:
                log.error("Error polling command %d: %s", command_id, exc)
                return

        # ensure terminal state is seen even if message didn't change
        yield current

    @staticmethod
    def _parse_dt(raw: str) -> datetime:
        """Parse a UTC datetime string to an aware datetime."""
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    
    def is_item_eligible(self, item: dict, config: Config) -> bool:
        """
        Returns True if the item should be searched this run.

        Items with no lastSearchTime are always eligible:
        attempt at least one search before applying backoff.

        Otherwise, eligibility is determined by age (airDateUtc/releaseDate) and
        how recently it was last searched (lastSearchTime). Tiers are checked
        from most to least restrictive; the first matching threshold wins:
        * slowest tier: search at most every SLOWEST_INTERVAL_DAYS days
        * slow tier:    search at most every SLOW_INTERVAL_DAYS days
        * normal:       always eligible
        """
        last_search_raw  = item.get("lastSearchTime")
        release_date_raw = item.get(self.age_field)
        if not last_search_raw or not release_date_raw:
            return True

        now               = datetime.now(timezone.utc)
        age_days          = (now - self._parse_dt(release_date_raw)).days
        days_since_search = (now - self._parse_dt(last_search_raw)).days
    
        # if <in tier window> then <check whether tier interval time passed>, else <normal tier>
        if config.slowest_after_days and age_days >= config.slowest_after_days:
            eligible, label = days_since_search >= config.slowest_interval_days, "slowest"
        elif config.slow_after_days and age_days >= config.slow_after_days:
            eligible, label = days_since_search >= config.slow_interval_days, "slow"
        else:
            eligible, label = True, "normal"

        log.debug("\t→ %s | release date: %s | last searched: %s | tier: %s | eligible: %s",
                  self.item_label(item), release_date_raw, last_search_raw, label, eligible)
        return eligible

    def item_label(self, item: dict) -> str:
        """Return a human-readable label for an item, used in logging."""
        # Except we form this in the subclasses
        raise NotImplementedError


class SonarrClient(ArrClient):
    sort_key       = "episodes.lastSearchTime"
    search_command = "EpisodeSearch"
    id_field       = "episodeIds"
    age_field      = "airDateUtc"

    def item_label(self, item: dict) -> str:
        series = item.get("series", {}).get("title", "Unknown Series")
        return f"{series}/S{item.get('seasonNumber', '?'):02d}E{item.get('episodeNumber', '?'):02d}"


class RadarrClient(ArrClient):
    sort_key       = "movies.lastSearchTime"
    search_command = "MoviesSearch"
    id_field       = "movieIds"
    age_field      = "releaseDate"

    def item_label(self, item: dict) -> str:
        return f"{item.get('title', 'Unknown')} ({item.get('year', '?')})"

def run_search_cycle(client: ArrClient, config: Config) -> None:
    log.info("[%s] Fetching candidate missing items…", client.display_name)

    try:
        all_items = client.get_missing(page_size=_CANDIDATE_POOL_SIZE)
    except requests.RequestException as exc:
        log.error("Failed to fetch missing items: %s", exc)
        return

    if not all_items:
        log.info("No missing items.")
        return

    eligible = [item for item in all_items if client.is_item_eligible(item, config)]
    items    = eligible[:config.search_limit]

    log.info(
        "%d total missing | %d eligible (%d skipped by tier) | searching %d",
        len(all_items),
        len(eligible),
        len(all_items) - len(eligible),
        len(items),
    )

    if not items:
        log.info("No items eligible for search this run.")
        return

    for item in items:
        log.info('\t→ %s (id: %d)', client.item_label(item), item["id"])

    try:
        command = client.trigger_search([item["id"] for item in items])
    except requests.RequestException as exc:
        log.error("Failed to trigger search: %s", exc)
        return

    log.info("Search command queued (id: %s)", command["id"])

    for update in client.poll_command(command):
        status = update.get("status", "unknown")
        message = update.get("message", "")
        log.info("\t→ [%s: %s] %s", command["id"], status, message)

def run_client(client: ArrClient, config: Config) -> None:
    """Run a search cycle for a single client."""
    run_search_cycle(client, config)


def main() -> None:
    config = Config.from_env()

    # At least one of these is guaranteed to be non-empty by Config.from_env()
    clients: list[ArrClient] = []
    if config.sonarr_api_key:
        clients.append(SonarrClient(config.sonarr_base_url, config.sonarr_api_key))
    if config.radarr_api_key:
        clients.append(RadarrClient(config.radarr_base_url, config.radarr_api_key))

    config.log_startup(" + ".join(c.display_name for c in clients))


    # If both clients are active, interleave them so they don't run simultaneously.
    # Each client still runs every SEARCH_INTERVAL_MINUTES
    while True:
        for client in clients:
            run_client(client, config)
            sleep_seconds = config.interval_minutes * 60 / len(clients)
            log.info("Sleeping for %g minutes…", sleep_seconds / 60)
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
