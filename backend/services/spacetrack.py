"""
Space-Track.org service
Fetches TLE (Two-Line Element) orbital data for debris objects.

Docs: https://www.space-track.org/documentation
"""
import random
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


class SpaceTrackClient:
    def __init__(self):
        self.session = requests.Session()
        self._authenticated = False

    def _login(self):
        payload = {
            "identity": Config.SPACETRACK_USERNAME,
            "password": Config.SPACETRACK_PASSWORD,
        }
        resp = self.session.post(SPACETRACK_LOGIN_URL, data=payload, timeout=10)
        resp.raise_for_status()
        self._authenticated = True
        logger.info("Space-Track login successful")

    def _ensure_auth(self):
        if not self._authenticated:
            self._login()

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

        self._ensure_auth()
        url = (
            f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/"
            f"OBJECT_TYPE/DEBRIS/orderby/BSTAR desc/limit/{limit}/format/json"
        )
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"Fetched {len(data)} debris TLE records (BSTAR desc)")

        cache.set(f"spacetrack:debris:{limit}", data, ttl=21600)  # 6 hours
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

        self._ensure_auth()
        url = (
            f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/"
            f"OBJECT_TYPE/DEBRIS/orderby/NORAD_CAT_ID asc/limit/{pool_size}/format/json"
        )
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        pool = resp.json()

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
        """Returns ISS TLE record."""
        self._ensure_auth()
        url = (
            f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/"
            f"NORAD_CAT_ID/{Config.ISS_NORAD_ID}/format/json"
        )
        resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
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

        self._ensure_auth()
        url = (
            f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/cdm_public/"
            f"orderby/TCA asc/limit/{limit}/format/json"
        )
        resp = self.session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
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

        self._ensure_auth()
        id_filter = ",".join(norad_ids)
        url = (
            f"{Config.SPACETRACK_BASE_URL}/basicspacedata/query/class/gp/"
            f"NORAD_CAT_ID/{id_filter}/format/json"
        )
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
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