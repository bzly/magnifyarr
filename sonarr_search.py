#!/usr/bin/env python3
"""
Sonarr missing episode searcher.
Periodically fetch the missing list and trigger episode searches,
replicating the behaviour of clicking the magnifying glass in the Sonarr UI.

General pattern:
* Fetch all missing episodes, sorted by last search time ascending.
*   -> Optionally filter out old (vs air date) episodes (slow/slowest tiers).
* Trigger a Sonarr EpisodeSearch command for the first SEARCH_LIMIT eligible episodes.
* Whilst Sonarr is processing the command, poll its status and log message updates.
* Repeat every SEARCH_INTERVAL_MINUTES minutes.

Tier logic (disabled if env vars _AFTER_DAYS not set):
* Episodes older than SLOW_AFTER_DAYS are only searched every SLOW_INTERVAL_DAYS days.
* Episodes older than SLOWEST_AFTER_DAYS are only searched every SLOWEST_INTERVAL_DAYS days.
* Episodes with no lastSearchTime are always eligible regardless of age.
"""

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

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
    base_url:               str
    api_key:                str
    search_limit:           int
    interval_minutes:       int
    slow_after_days:        int | None
    slow_interval_days:     int
    slowest_after_days:     int | None
    slowest_interval_days:  int

    @classmethod
    def from_env(cls) -> "Config":
        """Read environment variables, check validity, set defaults."""
        try:
            api_key = os.environ["SONARR_API_KEY"]
        except KeyError as e:
            log.error("Missing required environment variable: %s", e)
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
            base_url=             os.getenv("SONARR_URL", "http://sonarr:8989").rstrip("/"),
            api_key=              api_key,
            search_limit=         _positive_int("SEARCH_LIMIT", 10),
            interval_minutes=     _positive_int("SEARCH_INTERVAL_MINUTES", 5),
            slow_after_days=      slow_after_days,
            slow_interval_days=   _positive_int("SLOW_INTERVAL_DAYS", 1),
            slowest_after_days=   slowest_after_days,
            slowest_interval_days=_positive_int("SLOWEST_INTERVAL_DAYS", 7),
        )

    def log_startup(self) -> None:
        tiers = []
        if self.slow_after_days:
            tiers.append(f"slow=>{self.slow_after_days}+d: /{self.slow_interval_days}d")
        if self.slowest_after_days:
            tiers.append(f"slowest=>{self.slowest_after_days}+d: /{self.slowest_interval_days}d")

        log.info(
            "Starting magnifyarr | limit=%d | interval=%dm%s",
            self.search_limit,
            self.interval_minutes,
            f" | tiers: {', '.join(tiers)}" if tiers else " | tiers: disabled",
        )


class SonarrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v3{path}"

    def ping(self) -> bool:
        """Health check."""
        try:
            resp = self.session.get(self._url("/system/status"), timeout=10)
            resp.raise_for_status()
            version = resp.json().get("version", "unknown")
            log.info("Connected to Sonarr %s at %s", version, self.base_url)
            return True
        except requests.RequestException as exc:
            log.error("Cannot reach Sonarr: %s", exc)
            return False

    def get_missing_episodes(self, page_size: int) -> list[dict]:
        """
        Fetch up to page_size missing episodes (least recently searched first).
        Pass _CANDIDATE_POOL_SIZE to get a full pool for tier filtering.
        """
        params = {
            "pageSize": page_size,
            "page": 1,
            "sortKey": "episodes.lastSearchTime",
            "sortDirection": "ascending",
            "includeSeries": True,  # for better logging, but it does make the response bigglier
        }
        resp = self.session.get(self._url("/wanted/missing"), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("records", [])

    def trigger_episode_search(self, episode_ids: list[int]) -> dict:
        """
        Equivalent to clicking the magnifying glass on the missing list.
        Returns the initial GET /command object (status = 'queued').
        """
        payload = {"name": "EpisodeSearch", "episodeIds": episode_ids}
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

        yield current


def _parse_sonarr_dt(raw: str) -> datetime:
    """Parse a Sonarr UTC datetime string to an aware datetime."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _is_episode_eligible(ep: dict, config: Config) -> bool:
    """
    Returns True if the episode should be searched this run.

    Episodes with no lastSearchTime are always eligible:
    attempt at least one search before applying backoff.

    Otherwise, eligibility is determined by episode age (airDateUtc) and
    how recently it was last searched (lastSearchTime). Tiers are checked
    from most to least restrictive; the first matching threshold wins:
    * slowest tier: search at most every SLOWEST_INTERVAL_DAYS days
    * slow tier:    search at most every SLOW_INTERVAL_DAYS days
    * normal:       always eligible
    """
    last_search_raw = ep.get("lastSearchTime")
    if not last_search_raw:
        return True

    air_date_raw = ep.get("airDateUtc")
    if not air_date_raw:
        return True

    now = datetime.now(timezone.utc)
    age_days          = (now - _parse_sonarr_dt(air_date_raw)).days
    days_since_search = (now - _parse_sonarr_dt(last_search_raw)).days

    tiers = [
        (config.slowest_after_days, config.slowest_interval_days, "slowest"),
        (config.slow_after_days,    config.slow_interval_days,    "slow"),
    ]
    for after_days, interval_days, label in tiers:
        if after_days and age_days >= after_days:
            eligible = days_since_search >= interval_days
            break
    else:
        eligible, label = True, "normal"

    log.debug('\t→ %s/S%02dE%02d | air date: %s | last searched: %s | tier: %s | eligible: %s',
              ep.get("series", {}).get("title", "Unknown Series"), 
              ep.get("seasonNumber", "?"), 
              ep.get("episodeNumber", "?"), 
              air_date_raw, 
              last_search_raw, 
              label, 
              eligible
    )
    return eligible


def run_search_cycle(client: SonarrClient, config: Config) -> None:
    log.info("Fetching candidate missing episodes…")

    try:
        all_episodes = client.get_missing_episodes(page_size=_CANDIDATE_POOL_SIZE)
    except requests.RequestException as exc:
        log.error("Failed to fetch missing episodes: %s", exc)
        return

    if not all_episodes:
        log.info("No missing episodes.")
        return

    eligible = [ep for ep in all_episodes if _is_episode_eligible(ep, config)]
    episodes = eligible[:config.search_limit]

    log.info(
        "%d total missing episode(s) | %d eligible (%d skipped by tier) | searching %d",
        len(all_episodes),
        len(eligible),
        len(all_episodes) - len(eligible),
        len(episodes),
    )

    if not episodes:
        log.info("No episodes eligible for search this run.")
        return

    for ep in episodes:
        series_title = ep.get("series", {}).get("title", "Unknown Series")
        season = ep.get("seasonNumber", "?")
        number = ep.get("episodeNumber", "?")
        title = ep.get("title", "Unknown Episode")
        log.info('\t→ %s | S%02dE%02d | "%s" (id: %d)', series_title, season, number, title, ep["id"])

    try:
        command = client.trigger_episode_search([ep["id"] for ep in episodes])
    except requests.RequestException as exc:
        log.error("Failed to trigger episode search: %s", exc)
        return

    log.info("Search command queued (id: %s)", command["id"])

    for update in client.poll_command(command):
        status = update.get("status", "unknown")
        message = update.get("message", "")
        log.info("\t→ [%s: %s] %s", command["id"], status, message)


def main() -> None:
    config = Config.from_env()
    config.log_startup()

    client = SonarrClient(config.base_url, config.api_key)

    if not client.ping():
        log.warning("Initial connection failed (will retry every interval).")

    while True:
        run_search_cycle(client, config)
        log.info("Sleeping for %d minutes…", config.interval_minutes)
        time.sleep(60 * config.interval_minutes)


if __name__ == "__main__":
    main()