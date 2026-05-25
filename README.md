# magnifyarr

Periodically searches for missing items in Sonarr and/or Radarr, replicating the behaviour of clicking the magnifying glass in the _Wanted_ > _Missing_ list.

## What

What does it actually do?

1. Fetches all missing episodes from Sonarr and/or movies from Radarr
    * Optionally filters older items for less frequent searching
2. Trigger a Sonarr EpisodeSearch/Radarr MoviesSearch command for the first 10 (`SEARCH_LIMIT`) eligible episodes
3. Repeat every 5 (`SEARCH_INTERVAL_MINUTES`) minutes

It does **not**:
* Perform SeasonSearch, so it will not pick up season packs (it's on the to-do list)
* ~~Trigger Radarr searches (yet? PRs welcome)~~
* Support multiple Sonarr instances. You can deploy multiple instances of this, though (~26MB memory usage) and super simple configuration.

## Why

Huntarr died, and I didn't want to use a possibly unsupported fork ([elfhosted/newtarr](https://github.com/elfhosted/newtarr)) from before the BS, and I had problems with [egg82/fetcharr](https://github.com/egg82/fetcharr) not actually triggering a download of missing episodes (also it was using ~750MB of memory which seemed excessive). It is probably a much more fleshed-out project than this, so I would still recommend checking it out.

## Usage

Deploy via Docker, here's an example `compose.yaml`:

```yaml
services:
  magnifyarr:
    image: ghcr.io/bzly/magnifyarr:latest
    restart: unless-stopped
    environment:
      SONARR_API_KEY: "your_api_key_here" # Sonarr -> Settings -> General
      # SONARR_URL: "http://sonarr:8989"  # default
      RADARR_API_KEY: "your_other_key"
      # RADARR_URL: "http://radarr:7878
      SLOW_AFTER_DAYS: 7                  # optional, but recommended to avoid rate limiting
```

## Configuration

| Variable | Conditionally Required | Default | Description |
|---|---|---|---|
| `SONARR_API_KEY` | :ballot_box_with_check: | — | Sonarr API key. Either this or `RADARR_API_KEY` must be defined. |
| `SONARR_URL` |  | `http://sonarr:8989` | Sonarr instance URL |
| `RADARR_API_KEY` | :ballot_box_with_check: | — | Radarr API key. Either this or `SONARR_API_KEY` must be defined. |
| `RADARR_URL` |  | `http://radarr:7878` | Radarr instance URL |
| `SEARCH_LIMIT` |  | `10` | Number of episodes to search per run |
| `SEARCH_INTERVAL_MINUTES` |  | `5` | How frequently to run (minutes) |
| `SLOW_AFTER_DAYS` |  | — | Age threshold (days) to enter slow tier |
| `SLOW_INTERVAL_DAYS` |  | `1` | Search interval (days) in slow tier |
| `SLOWEST_AFTER_DAYS` |  | — | Age threshold (days) to enter slowest tier |
| `SLOWEST_INTERVAL_DAYS` |  | `7` | Search interval (days) in slowest tier |
| `LOG_LEVEL` |  | `INFO` | Logging verbosity. DEBUG will log per-episode tier eligibility decisions each run. |

## Avoiding indexer rate limits

By default every missing episode is searched every run. This can get quite heavy on API calls for indexers with moderately strict rate limits. Older content is less likely to suddenly appear on your indexer, so we can optionally enable a backoff where we search for old episodes less frequently.

Setting `SLOW_AFTER_DAYS` and/or `SLOWEST_AFTER_DAYS` enables 'tier filtering': episodes are assigned a tier by comparing this value to their age (Sonarr's `airDateUtc`). They are then searched for if their `lastSearchTime` is longer ago than the relevant one of `SLOW_INTERVAL_DAYS`/`SLOWEST_INTERVAL_DAYS`.

Episodes with no prior search are always eligible regardless of age.

Example for a large collection: 

```yaml
SLOW_AFTER_DAYS:       "7"   # older than 7 days: search daily (default `SLOW_INTERVAL_DAYS`)
SLOWEST_AFTER_DAYS:    "30"  # older than 30 days:
SLOWEST_INTERVAL_DAYS: "30"  #   search ~monthly
```
