"""FORMED catalogue access.

Collection metadata is readable without a token. Video detail - the part that
carries the playback ticket - is not, so anything touching playback goes
through an authenticated session.

Transport notes: FORMED returns a transient 503 often enough to matter, and a
single one used to take out a whole menu. Requests are therefore retried with
backoff, and connections are pooled rather than renegotiating TLS per call.
"""

import random
import threading
import time

import requests

from . import auth
from . import const
from . import kodiutils as ku


class ApiError(Exception):
    pass


class TransientError(ApiError):
    """A failure that is FORMED's fault and may well not recur.

    Separated out so callers can tell "this menu is genuinely empty" from
    "the server is having a moment", which are worth handling differently.
    """


# Statuses worth another attempt. 401/403/404 are deliberately absent - they
# are answers, not failures, and retrying them just adds latency.
RETRY_STATUSES = frozenset((429, 500, 502, 503, 504))

# Kept deliberately small. Retrying is worth it for a one-off blip, but a
# menu that sits there for ten seconds before admitting defeat is worse than
# one that fails in two and lets you press it again - so the whole retry
# sequence is budgeted in seconds, not tens of seconds.
MAX_ATTEMPTS = 3
BACKOFF_BASE = 0.35     # seconds; doubles each attempt
BACKOFF_CAP = 1.2
RETRY_AFTER_CAP = 2.0   # ignore an absurd Retry-After rather than hang the UI

# One Session per thread. Sessions are not documented as thread-safe and the
# emptiness probes run on a pool, so they are not shared across threads; within
# a thread this turns a fresh TLS handshake per request into a reused
# connection, which is most of the cost of drawing a menu.
_local = threading.local()


def _session():
    sess = getattr(_local, "session", None)
    if sess is not None:
        return sess

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": const.USER_AGENT,
            "Accept": "application/json",
            "Origin": const.WEB_BASE,
            "Referer": const.WEB_BASE + "/",
        }
    )
    try:
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8)
        sess.mount("https://", adapter)
    except Exception as exc:  # pragma: no cover - defensive, pooling is a bonus
        ku.debug("could not mount pooling adapter (%s)" % exc)

    _local.session = sess
    return sess


def _message_for(status):
    """Something a person can act on, instead of a bare status code."""
    if status == 503:
        return (
            "FORMED is temporarily unavailable (HTTP 503). That is on their "
            "end, not yours - please try again in a few minutes."
        )
    if status == 429:
        return (
            "FORMED is rate-limiting this device (HTTP 429). Wait a moment "
            "and try again."
        )
    if status in (500, 502, 504):
        return "FORMED had a server error (HTTP %s). Please try again." % status
    if status == 403:
        return "FORMED refused this request (HTTP 403)."
    if status == 404:
        return "FORMED has no such item (HTTP 404)."
    return "Server returned HTTP %s" % status


def _retry_delay(resp, attempt):
    """Backoff for the next attempt, honouring Retry-After when it is sane."""
    if resp is not None:
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return min(float(raw), RETRY_AFTER_CAP)
            except (TypeError, ValueError):
                pass
    # Exponential, with jitter so a pool of probes does not retry in lockstep.
    delay = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
    return delay * (0.5 + random.random() * 0.5)


def _get(url, token=None, params=None, attempts=MAX_ATTEMPTS):
    """GET and decode JSON, retrying transient failures.

    ``attempts=1`` disables retrying, for callers where a fast negative answer
    beats a correct one - the emptiness probes being the case in point.
    """
    sess = _session()
    headers = {"Authorization": "Bearer " + token} if token else None
    last = None

    for attempt in range(attempts):
        resp = None
        try:
            resp = sess.get(
                url, params=params, headers=headers, timeout=const.DEFAULT_TIMEOUT
            )
        except requests.RequestException as exc:
            last = TransientError("Network error: %s" % exc)

        if resp is not None:
            if resp.status_code == 401:
                raise ApiError("Not authorised - you may need to sign in again.")

            if resp.status_code in RETRY_STATUSES:
                last = TransientError(_message_for(resp.status_code))
            elif resp.status_code >= 400:
                raise ApiError(_message_for(resp.status_code))
            else:
                try:
                    return resp.json()
                except ValueError:
                    raise ApiError("Unexpected non-JSON response from %s" % url)

        if attempt + 1 < attempts:
            delay = _retry_delay(resp, attempt)
            ku.debug(
                "%s - retrying in %.1fs (attempt %d/%d)"
                % (last, delay, attempt + 2, attempts)
            )
            time.sleep(delay)

    raise last


def _get_authed(url, params=None):
    token = auth.access_token()
    return _get(url, token=token, params=params)


# --- Catalogue --------------------------------------------------------------

NAV_CACHE_FILE = "nav-links.json"


def nav_links():
    """Top-level site navigation: PROGRAMS, MOVIES, FAMILY, ESPANOL, ...

    Kept on disk after every success. The entire root menu depends on this one
    call, so when FORMED is unreachable a stale copy beats an error dialog by a
    wide margin - these ids change maybe once a year, and everything below the
    root still works off them.
    """
    try:
        payload = _get(const.NAV_LINKS)
    except ApiError:
        cached = ku.read_json(NAV_CACHE_FILE)
        if cached:
            ku.log("nav-links unavailable, falling back to the cached copy")
            return cached
        raise

    if payload:
        ku.write_json(NAV_CACHE_FILE, payload)
    return payload


def carousel():
    return _get(const.CAROUSEL)


def collection(cid):
    return _get(const.COLLECTION.format(cid=cid))


def collection_items(cid, attempts=MAX_ATTEMPTS):
    """Direct children of a collection.

    Children carrying a ``collectionType`` are sub-collections (usually
    seasons); everything else is a playable item or an extra such as a PDF
    discussion guide.
    """
    payload = _get(const.COLLECTION_ITEMS.format(cid=cid), attempts=attempts)
    return payload.get("items", []) or []


def video_detail(vid):
    """Authenticated video payload: title, description, series, entitlement."""
    return _get_authed(const.VIDEO_V2.format(vid=vid))


# Emptiness probes are cached for the life of the process. A single menu render
# can ask about the same collection more than once.
_HAS_VIDEO_CACHE = {}


def has_videos(cid, depth=2, budget=None):
    """True if a collection yields at least one video the add-on would show.

    Menus that resolve to nothing - Books being the obvious case, since every
    ebook is filtered out - should not be offered at all. Determining that
    means looking, so this is deliberately bounded: two levels deep and a
    shared request budget, after which it answers True rather than hiding
    something real. A menu that turns out empty is a smaller failure than a
    menu that was wrongly hidden.
    """
    cid = str(cid)
    if cid in _HAS_VIDEO_CACHE:
        return _HAS_VIDEO_CACHE[cid]

    if budget is None:
        budget = [12]

    if depth <= 0 or budget[0] <= 0:
        return True

    budget[0] -= 1

    try:
        # One attempt only. A probe is an optimisation; making the user wait
        # through a backoff to find out whether a menu is worth hiding is a
        # worse outcome than simply showing it.
        items = collection_items(cid, attempts=1)
    except TransientError:
        # FORMED is struggling. Every remaining probe will fail the same way,
        # so spend the budget now and let the rest of the menu render.
        budget[0] = 0
        return True
    except ApiError:
        return True  # Unknown is not the same as empty.

    if any(is_video(i) for i in items):
        _HAS_VIDEO_CACHE[cid] = True
        return True

    for child in (i for i in items if is_collection(i)):
        if has_videos(child.get("id"), depth - 1, budget):
            _HAS_VIDEO_CACHE[cid] = True
            return True

    # Only trust an empty verdict if the search was not cut short.
    result = budget[0] > 0
    _HAS_VIDEO_CACHE[cid] = result
    return result


def any_has_videos(cids, budget=None):
    """True as soon as any one of these collections has video in it.

    Short-circuits, so the common case - the first category in a section is
    full of video - costs a single request.
    """
    if budget is None:
        budget = [12]
    for cid in cids:
        if cid and has_videos(cid, 2, budget):
            return True
    return False


def filter_nonempty(entries, key, workers=8):
    """Drop entries whose collection has no video in it.

    ``key`` maps an entry to a collection id. Probes run concurrently because
    doing a dozen sequentially would be visible as menu lag.
    """
    entries = list(entries)
    if not entries:
        return []

    budget = [24]

    def keep(entry):
        cid = key(entry)
        return bool(cid) and has_videos(cid, 2, budget)

    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            verdicts = list(pool.map(keep, entries))
    except Exception as exc:  # threading unavailable, or a pool failure
        ku.debug("parallel emptiness probe failed (%s), falling back" % exc)
        verdicts = [keep(e) for e in entries]

    return [e for e, ok in zip(entries, verdicts) if ok]


def search(term, limit=200):
    """Search the catalogue.

    Returns a flat list mixing videos and collections. Matching is broad - a
    two-word query can return hundreds of loosely related hits - so results are
    capped and titles that actually contain the query are floated to the top.
    The API's own ordering is preserved within each group.

    Note the ``link`` field here is the dead watch.formed.org slug form, same
    as elsewhere; ids are the only reliable handle.
    """
    payload = _get(const.SEARCH, params={"query": term})
    results = payload if isinstance(payload, list) else (payload.get("items") or [])

    needle = term.strip().lower()
    exact, loose = [], []
    for item in results:
        if needle and needle in (item.get("name") or "").lower():
            exact.append(item)
        else:
            loose.append(item)

    return (exact + loose)[:limit]


def search_kind(item):
    """Classify a search hit as 'video', 'collection', or 'other'."""
    medium = (item.get("contentMedium") or "").strip().lower()
    if medium == "collection":
        return "collection"
    if medium in NON_VIDEO_MEDIA:
        return "other"
    if "/collections/" in (item.get("link") or ""):
        return "collection"
    return "video"


def vimeo_playback_healthy():
    """FORMED's own verdict on Vimeo playback: True, False, or None if unknown.

    Deliberately single-attempt and short-timeout: this only ever runs to
    decorate an error message that is already on its way, so it must never add
    a retry sequence or a stall to a failure the user is waiting on. Any
    trouble answering is reported as None - "no opinion" - rather than guessed.
    """
    try:
        payload = _get(const.VIMEO_HEALTH, attempts=1)
    except ApiError as exc:
        ku.debug("vimeo health-check unavailable: %s" % exc)
        return None
    if isinstance(payload, dict) and isinstance(payload.get("status"), bool):
        return payload["status"]
    return None


def subscriber():
    """Subscriber record. Carries the Vimeo OTT playback ticket.

    The ticket belongs to the account, not to any individual video, and the
    same value authorises every title.
    """
    return _get_authed(const.SUBSCRIBER)


# --- Classification helpers -------------------------------------------------

def is_collection(item):
    return bool(item.get("collectionType"))


def is_pdf_extra(item):
    """PDF guides are served straight off a signed CDN URL, not the player."""
    link = item.get("link") or ""
    return ".pdf" in link.lower() or "cloudfront.net" in link.lower()


# Media types this add-on deliberately does not surface. It is a video client;
# listing PDFs it cannot open and audiobooks it handles badly just clutters the
# menus.
NON_VIDEO_MEDIA = ("audio", "ebook", "book", "pdf", "document")


def is_video(item):
    """True if an item is a video the add-on should offer.

    Exclusion rather than inclusion: an unrecognised medium is assumed to be
    video, because a failed play is a better outcome than an episode silently
    missing from a season.
    """
    if is_collection(item) or is_pdf_extra(item):
        return False
    return (item.get("contentMedium") or "").strip().lower() not in NON_VIDEO_MEDIA


# Retained under the old name so nothing external breaks.
is_playable = is_video


def artwork(item):
    # Collection listings use thumbnailUrl; search hits use thumbnail.
    thumb = item.get("thumbnailUrl") or item.get("thumbnail") or ""
    return {"thumb": thumb, "icon": thumb, "poster": thumb, "fanart": thumb}


def plot(item):
    return (item.get("description") or "").strip()
