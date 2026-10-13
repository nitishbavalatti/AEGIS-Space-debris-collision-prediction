"""
Space-Track.org service
Fetches TLE (Two-Line Element) orbital data for debris objects.

Docs: https://www.space-track.org/documentation
"""
import json
import os
import random
import threading
import time
import requests
import logging
from backend.config import Config
from backend.services.cache import cache

logger = logging.getLogger(__name__)

SPACETRACK_LOGIN_URL = f"{Config.SPACETRACK_BASE_URL}/ajaxauth/login"
SPACETRACK_LOGOUT_URL = f"{Config.SPACETRACK_BASE_URL}/ajaxauth/logout"

# Fetch top N debris objects by BSTAR drag (high drag = more dangerous)
# NOTE: this ordering is intentional for the LIVE DASHBOARD (surface the
# most concerning objects first) — but it makes this query unsuitable for
# GNN TRAINING, since it always returns the most extreme decayers in the
# entire catalog with zero low-risk objects for contrast. Use
# get_debris_tle_sample() for training instead.
TLE_QUERY_URL = (
    f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/"
    "OBJECT_TYPE/DEBRIS/orderby/BSTAR desc/limit/500/format/json"
)

# Conjunction Data Messages (close approach warnings)
CDM_QUERY_URL = (
    f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/cdm_public/"
    "orderby/TCA asc/limit/100/format/json"
)


# ── Disk cache ────────────────────────────────────────────────────────────────
# The in-memory cache is wiped on every restart, so each restart re-queried
# Space-Track (slow, and their API terms ask you not to pull the same GP data
# more than about once an hour — check their usage policy). The last good
# live-dashboard pull is therefore also kept on disk:
#   - younger than DISK_CACHE_TTL_S  -> used directly, no network call
#   - older, but Space-Track is down -> used as a stale fallback instead of failing
CACHE_DIR = os.getenv("AEGIS_CACHE_DIR", ".aegis_cache")
DISK_CACHE_TTL_S = int(os.getenv("AEGIS_TLE_DISK_TTL", "3600"))


def _save_disk(name: str, data) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, name)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)            # atomic: a crash never leaves a half-written file
    except Exception as e:
        logger.warning(f"Could not write disk cache {name}: {e}")


def _load_disk(name: str):
    """Returns (data, age_seconds) or (None, None)."""
    path = os.path.join(CACHE_DIR, name)
    try:
        age = time.time() - os.path.getmtime(path)
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data, age
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"Ignoring unreadable disk cache {name}: {e}")
    return None, None


def _add_launch_year(records: list[dict]) -> list[dict]:
    """
    Space-Track's gp class has no LAUNCH_YEAR field, so every object fell
    back to the model's default (2000): the age feature was constant and
    the dashboard showed "—" for launch year. The international designator
    in OBJECT_ID ("1999-025AZ") starts with the launch year, so derive it.
    """
    for rec in records:
        if rec.get("LAUNCH_YEAR") in (None, "", 0):
            oid = str(rec.get("OBJECT_ID", "") or "")
            if len(oid) >= 4 and oid[:4].isdigit():
                rec["LAUNCH_YEAR"] = int(oid[:4])
    return records


class SpaceTrackClient:
    def __init__(self):
        self.session = requests.Session()
        self._authenticated = False
        self._auth_lock = threading.Lock()
        self.last_source: dict[str, str] = {}   # disk_name -> "network" | "disk-fresh" | "disk-stale"

    def _login(self):
        payload = {
            "identity": Config.SPACETRACK_USERNAME,
            "password": Config.SPACETRACK_PASSWORD,
        }
        try:
            resp = self.session.post(SPACETRACK_LOGIN_URL, data=payload, timeout=10)
        except requests.exceptions.ConnectionError as e:
            if "getaddrinfo" in str(e) or "NameResolution" in repr(e) or "Lookup timed out" in str(e):
                raise requests.exceptions.ConnectionError(
                    "Cannot reach www.space-track.org: the hostname did not resolve. Python uses the "
                    "operating system's resolver, which can fail even when `nslookup` works (VPN or "
                    "adapter DNS settings). Test the same path Python uses: "
                    "python -c \"import socket; print(socket.getaddrinfo('www.space-track.org', 443)[0][4])\""
                ) from e
            raise
        resp.raise_for_status()
        self._authenticated = True
        logger.info("Space-Track login successful")

    def _ensure_auth(self):
        # Several background threads (debris pipeline, ISS updater, conjunction
        # screener) call this at startup; without the lock they each logged in.
        if self._authenticated:
            return
        with self._auth_lock:
            if not self._authenticated:
                self._login()

    def _fetch_list(self, url: str, timeout: int):
        self._ensure_auth()
        resp = self.session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _query_cached(self, disk_name: str, url: str, timeout: int, fallback_url: str | None = None):
        """GET a Space-Track JSON list with the same disk cache as the dashboard
        pulls: reused for DISK_CACHE_TTL_S, stale copy used if the network fails.
        Lets a training run that failed part-way (or a different network later)
        reuse what was already downloaded instead of re-pulling thousands of
        records, and spares Space-Track repeated identical queries.

        `fallback_url`: if the primary query is REJECTED (HTTP error / invalid or
        empty result) it is retried with this one. Network failures never trigger
        it — those go to the stale disk copy instead.
        `self.last_source[disk_name]` records where the data came from, so callers
        can tell fresh data from a stale fallback."""
        disk, age = _load_disk(disk_name)
        if disk is not None and age < DISK_CACHE_TTL_S:
            logger.info(f"Using on-disk {disk_name} ({len(disk)} records, {age/60:.0f} min old)")
            self.last_source[disk_name] = "disk-fresh"
            return disk
        try:
            try:
                data = self._fetch_list(url, timeout)
                if fallback_url and not (isinstance(data, list) and data):
                    raise ValueError("empty or invalid result")
            except (requests.exceptions.HTTPError, ValueError) as e:
                if not fallback_url:
                    raise
                logger.warning(f"Filtered Space-Track query rejected ({e}) — retrying without the on-orbit filter")
                data = self._fetch_list(fallback_url, timeout)
        except Exception as e:
            if disk is not None:
                logger.warning(f"Space-Track query failed ({e}) — using STALE on-disk {disk_name} ({age/3600:.1f} h old)")
                self.last_source[disk_name] = "disk-stale"
                return disk
            raise
        _save_disk(disk_name, data)
        self.last_source[disk_name] = "network"
        return data

    def get_debris_tle(self, limit: int = 500) -> list[dict]:
        """
        Returns list of debris TLE records, ordered by BSTAR descending
        (highest drag / most concerning first). Intended for the LIVE
        DASHBOARD display — NOT for GNN training (see get_debris_tle_sample).

        Each dict has: OBJECT_NAME, NORAD_CAT_ID, TLE_LINE1, TLE_LINE2,
                       BSTAR, INCLINATION, ECCENTRICITY, MEAN_MOTION, etc.
        """
        cached = cache.get(f"spacetrack:debris:{limit}")
        if cached is not None:
            return cached

        disk_name = f"debris_tle_{limit}.json"
        base = f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/OBJECT_TYPE/DEBRIS"
        tail = f"orderby/BSTAR desc/limit/{limit}/format/json"
        # The gp class includes DECAYED objects (with their final element set), and
        # the highest-BSTAR debris is dominated by them: 4,705 of the top 5,000 had a
        # decay date, leaving 97 real objects. DECAY_DATE/null-val keeps only objects
        # still on orbit and EPOCH/>now-30 only those with a recent element set —
        # the standard Space-Track "currently tracked" query. If Space-Track ever
        # rejects it, the old query is used and the pipeline filters client-side.
        data = _add_launch_year(self._query_cached(
            disk_name,
            f"{base}/DECAY_DATE/null-val/EPOCH/%3Enow-30/{tail}",
            30,
            fallback_url=f"{base}/{tail}",
        ))
        src = self.last_source.get(disk_name)
        logger.info(f"Loaded {len(data)} debris TLE records (BSTAR desc, on-orbit) [{src}]")
        if src != "disk-stale":                      # never pin stale data in memory for 6 h
            cache.set(f"spacetrack:debris:{limit}", data, ttl=21600)
        return data

    def get_debris_tle_sample(self, limit: int = 500, pool_size: int = 5000) -> list[dict]:
        """
        Returns a randomized, broad cross-section of debris TLE records
        for GNN TRAINING.

        Pulls `pool_size` objects ordered by NORAD_CAT_ID asc (spanning old
        and new launches within that ID range), then randomly subsamples
        `limit` of them client-side. Space-Track's REST API has no
        server-side "ORDER BY RANDOM()", so this is the pragmatic fix.

        Without this, `orderby/NORAD_CAT_ID asc/limit/{limit}` alone always
        returns the exact same lowest-numbered (= oldest launch) objects in
        the catalog on every run — deterministic, not representative, and
        structurally excludes most Starlink-era debris (high NORAD IDs),
        which matters because the GNN's dense-band labels include the
        Starlink shell (535-570km, see DENSE_BANDS in train.py).

        NOTE: no fixed random seed here on purpose — each training data
        pull should get a fresh sample. Reproducibility for the train/val
        SPLIT is handled separately by SEED in train.py.
        """
        cache_key = f"spacetrack:debris_sample:{limit}:{pool_size}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        base = f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/OBJECT_TYPE/DEBRIS"
        tail = f"orderby/NORAD_CAT_ID asc/limit/{pool_size}/format/json"
        # On-orbit objects with recent elements only. The old pool was the lowest
        # NORAD IDs of ALL debris, which are overwhelmingly long-decayed objects
        # whose final (junk) element sets the model was being trained on.
        # The POOL is cached on disk (new file name: the old pool was unfiltered);
        # the random sample from it is still drawn fresh each run.
        pool = _add_launch_year(self._query_cached(
            f"debris_pool_onorbit_{pool_size}.json",
            f"{base}/DECAY_DATE/null-val/EPOCH/%3Enow-30/{tail}",
            30,
            fallback_url=f"{base}/{tail}",
        ))

        data = pool if len(pool) <= limit else random.sample(pool, limit)
        logger.info(
            f"Fetched pool of {len(pool)} debris records, "
            f"randomly sampled {len(data)} for training"
        )

        # Shorter TTL than the live-dashboard queries: this sample is
        # randomized per pull, so caching it for 6 hours would mean every
        # training run in that window sees the same "random" sample,
        # defeating the point. 30 min keeps a dev session's repeated runs
        # cheap without staling out the variety benefit.
        cache.set(cache_key, data, ttl=1800)  # 30 min
        return data

    def get_iss_tle(self) -> dict | None:
        """Returns ISS TLE record. Cached on disk like the debris pull: reused for
        DISK_CACHE_TTL_S, and used as a stale fallback if Space-Track is unreachable
        (a failed ISS lookup used to abort the whole conjunction screening run)."""
        disk, age = _load_disk("iss_tle.json")
        if disk is not None and age < DISK_CACHE_TTL_S:
            return disk[0]
        try:
            self._ensure_auth()
            url = (
                f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/"
                f"NORAD_CAT_ID/{Config.ISS_NORAD_ID}/format/json"
            )
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
            data = _add_launch_year(resp.json())
        except Exception as e:
            if disk is not None:
                logger.warning(f"ISS TLE fetch failed ({e}) — using STALE on-disk copy ({age/3600:.1f} h old)")
                return disk[0]
            raise
        if data:
            _save_disk("iss_tle.json", data[:1])
        return data[0] if data else None

    def get_cdm(self, limit: int = 50) -> list[dict]:
        """
        Returns Conjunction Data Messages — pre-computed close approach warnings
        from Space-Track. Each CDM includes TCA (time of closest approach) and
        miss distance.
        """
        cached = cache.get(f"spacetrack:cdm:{limit}")
        if cached is not None:
            return cached

        url = (
            f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/cdm_public/"
            f"orderby/TCA asc/limit/{limit}/format/json"
        )
        data = self._query_cached(f"cdm_{limit}.json", url, 15)
        logger.info(f"Fetched {len(data)} CDM records")

        cache.set(f"spacetrack:cdm:{limit}", data, ttl=3600)  # 1 hour
        return data

    def get_tle_by_norad_ids(self, norad_ids: list[str]) -> list[dict]:
        """
        Fetches TLE/GP records for a specific list of NORAD IDs, regardless
        of OBJECT_TYPE (debris, payload, rocket body — CDMs can reference
        any of these). Used to guarantee CDM-referenced objects are present
        in the GNN training set, since a random debris-only sample
        essentially never intersects with the small set of CDM-referenced
        IDs by chance.
        """
        if not norad_ids:
            return []

        cache_key = f"spacetrack:tle_by_ids:{','.join(sorted(norad_ids))}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        import hashlib
        id_filter = ",".join(norad_ids)
        url = (
            f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/"
            f"NORAD_CAT_ID/{id_filter}/format/json"
        )
        digest = hashlib.md5(",".join(sorted(norad_ids)).encode()).hexdigest()[:12]
        data = _add_launch_year(self._query_cached(f"tle_by_ids_{digest}.json", url, 30))
        logger.info(f"Fetched {len(data)} TLE records for {len(norad_ids)} CDM-referenced NORAD IDs")

        cache.set(cache_key, data, ttl=3600)  # 1 hour — tied to CDM freshness
        return data

    def get_debris_tle_sample_with_cdm_positives(
        self, limit: int = 500, pool_size: int = 5000, cdm_limit: int = 100
    ) -> list[dict]:
        """
        Builds a training sample that GUARANTEES CDM-confirmed positive
        examples are present, instead of relying on random overlap between
        a debris sample and the CDM-referenced ID set.

        Combines:
          1. A random sample of `limit` debris objects (get_debris_tle_sample)
             — for label diversity / negatives
          2. TLE records for every NORAD ID referenced in the current CDM
             batch (get_tle_by_norad_ids) — guaranteed positives

        Objects already in the random sample are not duplicated. Final
        training set size is `limit` + however many CDM-referenced objects
        weren't already included — not exactly `limit`. train.py's label
        building and train/val split logic work fine with a variable-size
        list.
        """
        random_sample = self.get_debris_tle_sample(limit=limit, pool_size=pool_size)
        cdms = self.get_cdm(limit=cdm_limit)

        cdm_ids = set()
        for cdm in cdms:
            for key in ("SAT_1_ID", "SAT_2_ID"):
                v = cdm.get(key)
                if v:
                    cdm_ids.add(str(v))

        existing_ids = {str(obj.get("NORAD_CAT_ID", "")) for obj in random_sample}
        missing_ids = list(cdm_ids - existing_ids)

        cdm_objects = self.get_tle_by_norad_ids(missing_ids) if missing_ids else []

        combined = random_sample + cdm_objects
        logger.info(
            f"Training sample: {len(random_sample)} random + {len(cdm_objects)} "
            f"CDM-guaranteed ({len(missing_ids)} requested, {len(cdm_objects)} "
            f"found) = {len(combined)} total"
        )
        return combined

    def logout(self):
        if self._authenticated:
            self.session.get(SPACETRACK_LOGOUT_URL, timeout=5)
            self._authenticated = False


# Singleton client
_client = SpaceTrackClient()


def get_debris_tle(limit: int = 500) -> list[dict]:
    return _client.get_debris_tle(limit)


def last_debris_source(limit: int = 5000):
    """"network" | "disk-fresh" | "disk-stale" for the most recent live debris load."""
    return _client.last_source.get(f"debris_tle_{limit}.json")


def get_debris_tle_sample(limit: int = 500) -> list[dict]:
    return _client.get_debris_tle_sample(limit)


def get_debris_tle_sample_with_cdm_positives(
    limit: int = 500, pool_size: int = 5000, cdm_limit: int = 100
) -> list[dict]:
    return _client.get_debris_tle_sample_with_cdm_positives(limit, pool_size, cdm_limit)


def get_tle_by_norad_ids(norad_ids: list[str]) -> list[dict]:
    return _client.get_tle_by_norad_ids(norad_ids)


def get_iss_tle() -> dict | None:
    return _client.get_iss_tle()


def get_cdm(limit: int = 50) -> list[dict]:
    return _client.get_cdm(limit)