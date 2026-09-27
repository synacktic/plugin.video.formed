"""Turn a FORMED video id into something Kodi can play.

The chain is:

    /api/v2/videos/<id>   (bearer token)   -> short-lived signed "ticket"
    embed.vhx.tv/videos/<id>?ticket=...    -> player page
    <player page>                          -> HLS manifest
    manifest                               -> InputStream Adaptive

Two links in that chain were inferred rather than observed end to end: the
exact field carrying the ticket, and the shape of the player page. Both are
handled defensively and logged loudly, so a break tells you exactly where it
broke instead of failing silently.

This module resolves streams for playback only. It deliberately has no path
that writes media to disk.
"""

import json
import re
import time
import urllib.parse

import requests
import xbmc
import xbmcgui
import xbmcplugin

from . import api
from . import auth
from . import const
from . import kodiutils as ku

MANIFEST_RE = re.compile(r'https?://[^\s"\'\\<>]+\.(?:m3u8|mpd)(?:\?[^\s"\'\\<>]*)?')
_KEY_HINT = re.compile(r"ticket", re.IGNORECASE)


class PlaybackError(Exception):
    pass


class HostUnavailable(PlaybackError):
    """embed.vhx.tv refused to serve the player page.

    Vimeo OTT's edge returns a 460-byte Fastly error page with HTTP 503 (and
    ``Retry-After: 0``) in streaks that last anywhere from tens of seconds to
    several minutes, then recovers on its own. Measured directly: the request
    never reaches the origin - a healthy response carries x-request-id,
    x-runtime and ``via: 1.1 google``, and these carry only ``via: 1.1
    varnish``.

    Everything on our side was ruled out by testing: the account and
    subscription are active, the ticket is correct and stable, request headers
    make no difference (a full Chrome fingerprint fails identically), it is not
    tied to a particular Fastly node, and IPv4 and IPv6 fail alike. FORMED's own
    API carries no alternative route to the stream, so there is no second path
    to fall back to.

    Nothing the add-on does can prevent this. What it can do is fail in under a
    second and say plainly whose fault it is, so pressing play again is cheap.
    """


# --- Ticket extraction ------------------------------------------------------

def _find_ticket(payload):
    """Locate the playback ticket in the subscriber payload.

    The live shape is $.subscriber.vimeoAuthTicket. That nesting is checked
    first, then the top level, then a recursive scan for any ticket-ish key -
    so a rename or a reshuffle degrades instead of breaking outright.
    """
    scopes = [payload]
    nested = payload.get("subscriber")
    if isinstance(nested, dict):
        scopes.insert(0, nested)

    for scope in scopes:
        for key in const.TICKET_KEYS:
            value = scope.get(key)
            if isinstance(value, str) and value:
                ku.debug("ticket found at key %r" % key)
                return value

    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                here = "%s.%s" % (path, key)
                if (
                    isinstance(value, str)
                    and _KEY_HINT.search(key)
                    and len(value) > 16
                ):
                    found.append((here, value))
                else:
                    walk(value, here)
        elif isinstance(node, list):
            for idx, value in enumerate(node[:40]):
                walk(value, "%s[%d]" % (path, idx))

    walk(payload, "$")

    if found:
        path, value = found[0]
        ku.log("ticket found by fallback scan at %s" % path)
        return value

    return None


def _describe_access_error(payload):
    access_error = payload.get("accessError") or {}
    code = access_error.get("errorCode") or ""
    if code == "LOGIN_REQUIRED":
        return (
            "FORMED says you are not signed in. Try signing out and back in "
            "from the add-on's Account menu."
        )
    if code:
        return "FORMED refused playback: %s" % code
    return None


# --- Manifest extraction ----------------------------------------------------

CONFIG_URL_RE = re.compile(r'"config_url"\s*:\s*"([^"]+)"')


def _scan_for_manifest(blob):
    """Return the first manifest-shaped URL in a string, preferring HLS."""
    candidates = MANIFEST_RE.findall(blob.replace("\\/", "/"))
    if not candidates:
        candidates = [
            m.group(0) for m in MANIFEST_RE.finditer(blob.replace("\\/", "/"))
        ]
    for url in candidates:
        if ".m3u8" in url:
            return url
    return candidates[0] if candidates else None


def _extract_config_url(html):
    """Pull OTTData.config_url out of the embed page.

    The embed page does not carry the manifest itself - it carries a pointer
    to a Vimeo player config, keyed by a Vimeo video id that differs from the
    FORMED one.
    """
    match = CONFIG_URL_RE.search(html)
    if not match:
        return None
    return match.group(1).replace("\\/", "/").replace("\\u0026", "&")


def _manifest_from_config(config):
    """Dig the HLS manifest out of a Vimeo player config document.

    Each CDN entry offers two playlists: ``url``, which can advertise several
    codecs, and ``avc_url``, which is H.264 only. The multi-codec playlist is
    the usual cause of playback that starts fine and then dies on a seek, when
    the player tries to switch to a rendition the hardware cannot decode - so
    avc_url is preferred by default. The 'Prefer H.264' setting flips this.
    """
    prefer_avc = ku.bool_setting("prefer_avc", True)

    try:
        hls = config["request"]["files"]["hls"]
        cdns = hls.get("cdns") or {}
        preferred = hls.get("default_cdn")
        order = ([preferred] if preferred else []) + [
            n for n in cdns.keys() if n != preferred
        ]

        for name in order:
            entry = cdns.get(name) or {}
            keys = ("avc_url", "url") if prefer_avc else ("url", "avc_url")
            for key in keys:
                url = entry.get(key)
                if url:
                    ku.debug("manifest via cdn %r using %s" % (name, key))
                    return url
    except (KeyError, TypeError, AttributeError):
        pass

    found = _scan_for_manifest(json.dumps(config))
    if found:
        ku.debug("manifest via recursive scan of config")
    return found


def _primary_lang(tag):
    """'en-x-autogen' -> 'en'. Empty string if there is nothing usable."""
    return str(tag or "").strip().lower().split("-")[0]


def _manifest_has_captions(config):
    """True if the HLS manifest carries its own subtitle renditions.

    When it does, InputStream Adaptive surfaces them with proper language
    labels and attaching the same tracks as external files just produces
    duplicates listed as 'Unknown (External)'.
    """
    try:
        return bool(config["request"]["files"]["hls"].get("captions"))
    except (KeyError, TypeError, AttributeError):
        return False


def _subtitles_from_config(config):
    """Collect subtitle track URLs from the player config.

    Tracks live at request.text_tracks with URLs relative to the Vimeo host.
    Only tracks matching the preferred language are returned - Kodi cannot
    infer a language for externally attached subtitles, so returning several
    would just give a list of indistinguishable 'Unknown' entries.
    """
    if not ku.bool_setting("enable_subtitles", True):
        return []

    try:
        tracks = config["request"].get("text_tracks") or []
    except (KeyError, TypeError, AttributeError):
        return []

    if isinstance(tracks, dict):
        tracks = [tracks]

    base = config.get("vimeo_url") or "https://player.vimeo.com"
    if not base.startswith("http"):
        base = "https://" + base.lstrip("/")

    want = _primary_lang(ku.setting("subtitle_language", "en")) or "en"

    urls = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        url = track.get("url")
        if not url:
            continue

        lang = _primary_lang(track.get("lang"))
        if lang and lang != want:
            ku.debug("skipping %s subtitle track (want %s)" % (lang, want))
            continue

        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = base.rstrip("/") + url

        urls.append(url)
        ku.debug(
            "subtitle track: %s (%s)"
            % (track.get("label") or "?", track.get("lang") or "?")
        )

    return urls


def _fetch_config(config_url):
    """Fetch the player config.

    These URLs are single-use - fetching one a second time returns HTTP 410 -
    so this must happen immediately after reading it from the embed page.
    """
    headers = {
        "User-Agent": const.USER_AGENT,
        "Referer": "https://embed.vhx.tv/",
        "Accept": "application/json,*/*",
    }
    try:
        resp = requests.get(
            config_url, headers=headers, timeout=const.DEFAULT_TIMEOUT
        )
    except requests.RequestException as exc:
        raise PlaybackError("Could not reach the player config: %s" % exc)

    if resp.status_code == 410:
        raise PlaybackError(
            "The player config link had already expired. Try playing again."
        )
    if resp.status_code != 200:
        raise PlaybackError("Player config returned HTTP %s" % resp.status_code)

    try:
        return resp.json()
    except ValueError:
        raise PlaybackError("Player config was not JSON.")


def _resolve_stream(html):
    """Embed page -> config -> (manifest, subtitles, manifest_has_captions).

    The third value tells the caller whether the manifest already carries
    subtitle renditions of its own, so it can avoid attaching duplicates.
    The manifest may be None if nothing usable was found.
    """
    config_url = _extract_config_url(html)

    if config_url:
        config = _fetch_config(config_url)
        manifest = _manifest_from_config(config)
        subtitles = _subtitles_from_config(config)
        embedded = _manifest_has_captions(config)
        if manifest:
            return manifest, subtitles, embedded
        ku.error("player config fetched but contained no manifest")

    # Older/simpler embeds inline the manifest. Cheap to try before giving up.
    return _scan_for_manifest(html), [], False


# A failing edge answers in well under a second, so a couple of extra tries
# cost almost nothing and do catch the occasional one-off blip. There is
# deliberately no exponential backoff here: when this host is in a bad spell it
# stays bad for far longer than anyone will wait in front of a television, and
# sitting on a spinner for ten seconds before showing the same error is worse
# than failing immediately.
EMBED_ATTEMPTS = 3
EMBED_PAUSE = 0.4

# Shorter than the catalogue timeout. A hung embed request is just another
# shape of the same outage, and waiting 20s for it helps nobody.
EMBED_TIMEOUT = 5

# Hard ceiling on the whole resolve attempt. A refused request comes back in
# under half a second, so three of those finish well inside this; the budget
# exists for the other failure mode, where the host accepts the connection and
# then hangs. Whatever happens, play either starts or reports failure within
# about this long.
EMBED_BUDGET = 6.0


def _fetch_embed(vid, ticket):
    url = const.EMBED_URL.format(vid=vid)
    # Identical to the iframe the web client builds:
    #   https://embed.vhx.tv/videos/<id>?api=1&auto=1&ticket=<ticket>
    # Keeping the query byte-for-byte the same as the official player removes
    # one more way for our request to be treated differently from theirs.
    params = {"api": "1", "auto": "1", "ticket": ticket}
    headers = {
        "User-Agent": const.USER_AGENT,
        "Referer": const.WEB_BASE + "/",
        "Accept": "text/html,application/xhtml+xml,*/*",
    }

    last = None
    resp = None
    deadline = time.time() + EMBED_BUDGET

    for attempt in range(EMBED_ATTEMPTS):
        if attempt:
            if time.time() >= deadline:
                ku.debug("embed budget spent, not trying again")
                break
            time.sleep(EMBED_PAUSE)
        resp = None
        remaining = max(1.0, min(EMBED_TIMEOUT, deadline - time.time()))
        try:
            # A new connection each time. Keep-alive would pin every attempt to
            # the same edge node, which defeats the point of trying again.
            with requests.Session() as sess:
                resp = sess.get(
                    url, params=params, headers=headers, timeout=remaining
                )
        except requests.RequestException as exc:
            ku.debug("embed attempt %d: %s" % (attempt + 1, type(exc).__name__))
            last = HostUnavailable("Video host did not respond.")
            continue

        if resp.status_code == 200:
            break

        if resp.status_code >= 500:
            last = HostUnavailable(
                "Video host unavailable (HTTP %s)." % resp.status_code
            )
            continue

        raise PlaybackError("Player returned HTTP %s" % resp.status_code)

    if resp is None or resp.status_code != 200:
        ku.error(
            "embed fetch failed %d/%d attempts for video %s - this is Vimeo's "
            "edge (embed.vhx.tv), not FORMED and not this account"
            % (EMBED_ATTEMPTS, EMBED_ATTEMPTS, vid)
        )
        raise last or HostUnavailable("Video host unavailable.")

    if "not authorized" in resp.text.lower():
        raise PlaybackError(
            "The player rejected the ticket. This usually means the "
            "subscription does not cover this title, or the ticket expired "
            "before playback started."
        )

    return resp.text


# --- Kodi wiring ------------------------------------------------------------

def _kodi_major():
    try:
        return int(xbmc.getInfoLabel("System.BuildVersion").split(".")[0])
    except (ValueError, IndexError):
        return 21


ISA_ID = "inputstream.adaptive"


def _warn_if_no_isa():
    """Warn once per Kodi session if InputStream Adaptive is missing.

    ISA is declared optional in addon.xml, which means Kodi silently ignores
    every inputstream.adaptive.* property when it isn't installed and falls
    back to the built-in ffmpeg demuxer. Playback still starts, so the failure
    is invisible - but ffmpeg handles these separate audio/video HLS streams
    badly and seeking stalls the renderer until playback gives up.

    Silently degrading was the wrong call. Say so instead.
    """
    if ku.has_addon(ISA_ID):
        return True

    ku.error(
        "InputStream Adaptive is not installed - falling back to the ffmpeg "
        "demuxer. Playback will start but seeking is likely to stall."
    )

    if not xbmcgui.Window(10000).getProperty("formed.isa_warned"):
        xbmcgui.Window(10000).setProperty("formed.isa_warned", "1")
        ku.ok_dialog(
            "InputStream Adaptive is not installed.\n\n"
            "Video will play but seeking will stall and then stop.\n\n"
            "Install it from: Add-ons > Install from repository >\n"
            "Kodi Add-on repository > VideoPlayer InputStream >\n"
            "InputStream Adaptive.\n\n"
            "No Widevine or DRM component is needed."
        )
    return False


def _build_listitem(manifest_url, label="", subtitles=None, embedded_captions=False):
    item = xbmcgui.ListItem(label=label, path=manifest_url)

    is_dash = ".mpd" in manifest_url.lower()
    mime = "application/dash+xml" if is_dash else "application/vnd.apple.mpegurl"
    item.setMimeType(mime)
    item.setContentLookup(False)

    have_isa = _warn_if_no_isa()

    # Vimeo's CDN wants a plausible browser origin on every request, not just
    # the manifest. Segment requests after a seek are fetched by InputStream
    # Adaptive itself, so the headers have to be attached to those too or the
    # stream dies partway through.
    headers = urllib.parse.urlencode(
        {
            "User-Agent": const.USER_AGENT,
            "Referer": "https://embed.vhx.tv/",
            "Origin": "https://embed.vhx.tv",
        }
    )

    if have_isa:
        item.setProperty("inputstream", ISA_ID)
        item.setProperty("inputstream.adaptive.manifest_headers", headers)
        item.setProperty("inputstream.adaptive.stream_headers", headers)
        # common_headers covers every request type in one go on ISA 20+,
        # including the segment fetches the two above can miss.
        item.setProperty("inputstream.adaptive.common_headers", headers)

        # manifest_type was deprecated once ISA could infer it from the mime
        # type. Older builds still need it, so only set it where it is not a
        # warning.
        if _kodi_major() < 21:
            item.setProperty(
                "inputstream.adaptive.manifest_type", "mpd" if is_dash else "hls"
            )
    else:
        # Without ISA the ffmpeg demuxer handles the stream, and it only reads
        # headers appended to the path.
        item.setPath("%s|%s" % (manifest_url, headers))

    # Only attach external subtitles when nothing else is providing them.
    # InputStream Adaptive exposes the manifest's own subtitle renditions with
    # real language labels; adding the same tracks again via setSubtitles()
    # produces duplicates that Kodi lists as "Unknown (External)", because
    # externally attached subtitles carry no language metadata.
    if subtitles and not (have_isa and embedded_captions):
        item.setSubtitles(list(subtitles))
    elif subtitles:
        ku.debug(
            "%d external subtitle track(s) suppressed - the manifest already "
            "provides them with language labels" % len(subtitles)
        )

    return item


def resolve_and_play(handle, vid, label=""):
    """Resolve a video id to a stream and hand it to Kodi.

    The ticket is a property of the account rather than of the video, so this
    is one subscriber lookup plus the embed fetch - no per-video authorisation
    call is involved.
    """
    try:
        account = api.subscriber()
    except api.ApiError as exc:
        _fail(handle, str(exc))
        return

    ticket = _find_ticket(account)

    if not ticket:
        ku.error(
            "no ticket in subscriber payload; keys were: %s / subscriber: %s"
            % (
                sorted(account.keys()),
                sorted((account.get("subscriber") or {}).keys()),
            )
        )
        _fail(
            handle,
            "No playback ticket on your account. If you are signed in and "
            "subscribed, FORMED has probably renamed the field - see the log.",
        )
        return

    try:
        html = _fetch_embed(vid, ticket)
    except HostUnavailable as exc:
        # The request never reached FORMED or Vimeo's application, so there is
        # nothing local to diagnose. Ask FORMED whether they already know:
        # their web client drives an outage banner off this same endpoint.
        healthy = api.vimeo_playback_healthy()
        if healthy is False:
            message = "FORMED reports a known problem with video playback."
        else:
            message = "%s Not your account - press play again." % exc
        ku.log("playback failed for %s; FORMED health-check says %s"
               % (vid, {True: "healthy", False: "UNHEALTHY"}.get(healthy, "unknown")))
        _fail(handle, message, transient=True)
        return
    except PlaybackError as exc:
        # The ticket is account-wide, so a rejection here is almost always
        # about this specific title rather than about sign-in. Ask the video
        # endpoint why, and prefer its answer if it has one.
        try:
            reason = _describe_access_error(api.video_detail(vid))
        except api.ApiError:
            reason = None
        _fail(handle, reason or str(exc))
        return

    try:
        manifest, subtitles, embedded_captions = _resolve_stream(html)
    except PlaybackError as exc:
        _fail(handle, str(exc))
        return

    if not manifest:
        ku.error(
            "no manifest for video %s (embed page was %d bytes, config_url %s)"
            % (vid, len(html), "found" if _extract_config_url(html) else "MISSING")
        )
        _fail(
            handle,
            "Found the player but no stream in it. The player page layout has "
            "probably changed - check the Kodi log.",
        )
        return

    ku.log(
        "resolved video %s -> %s manifest, %d external subtitle track(s), "
        "manifest captions: %s"
        % (
            vid,
            "DASH" if ".mpd" in manifest else "HLS",
            len(subtitles),
            "yes" if embedded_captions else "no",
        )
    )

    xbmcplugin.setResolvedUrl(
        handle,
        True,
        _build_listitem(manifest, label, subtitles, embedded_captions),
    )


def _fail(handle, message, transient=False):
    """Report a playback failure and tell Kodi the item did not resolve.

    Transient host trouble gets a notification rather than a modal dialog.
    Kodi already puts its own "one or more items failed to play" error on
    screen when a resolve fails, so a modal here makes it two dialogs to
    dismiss before you can press play again - which is pure friction for a
    fault that usually clears by itself.
    """
    ku.error(message)
    if transient:
        ku.notify(message, icon=xbmcgui.NOTIFICATION_WARNING, ms=6000)
    else:
        ku.ok_dialog(message)
    xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
