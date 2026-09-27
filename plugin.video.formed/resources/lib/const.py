"""Endpoints and constants for the FORMED add-on.

Everything here was determined by observing the official web client. None of it
is documented or guaranteed stable - if the add-on breaks after a FORMED
update, this file is almost certainly where the fix goes.
"""

ADDON_ID = "plugin.video.formed"

# --- Identity (Keycloak / OpenID Connect) -----------------------------------
# Discovered from the public discovery document at:
#   <ISSUER>/.well-known/openid-configuration
ISSUER = "https://id.augustineinstitute.org/auth/realms/formed"
TOKEN_URL = ISSUER + "/protocol/openid-connect/token"
DEVICE_URL = ISSUER + "/protocol/openid-connect/auth/device"
USERINFO_URL = ISSUER + "/protocol/openid-connect/userinfo"
LOGOUT_URL = ISSUER + "/protocol/openid-connect/logout"

# The web client's public client id. Public clients carry no secret.
CLIENT_ID = "formed-v3"

GRANT_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"
GRANT_PASSWORD = "password"
GRANT_REFRESH = "refresh_token"

# --- Catalogue --------------------------------------------------------------
API_BASE = "https://app.formed.org/api"
WEB_BASE = "https://app.formed.org"

NAV_LINKS = API_BASE + "/app-config/nav-links"
CAROUSEL = API_BASE + "/categories/carousel"
GROUPED_CATEGORIES = API_BASE + "/categories/grouped-categories"
COLLECTION = API_BASE + "/collections/{cid}"
COLLECTION_ITEMS = API_BASE + "/collections/{cid}/items"
VIDEO_V2 = API_BASE + "/v2/videos/{vid}"
SEARCH = API_BASE + "/search"
SUBSCRIBER = API_BASE + "/users/me/subscriber"

# FORMED's own liveness probe for Vimeo playback. Their web client polls this
# and, when it answers {"status": false}, drops a banner across the top of the
# site reading "We are facing some issues with some of our videos." It is the
# authoritative answer to "is this outage mine or theirs", so the add-on asks
# the same question before blaming anything local.
VIMEO_HEALTH = API_BASE + "/vimeo/health-check"
WATCHLIST = API_BASE + "/users/me/subscriber/watchlist"

# Pinned to the root for convenience - it's the reason this add-on exists.
# Reachable the long way round via FAMILY > KIDS.
KIDS_COLLECTION = "115524"

# Top-level nav sections holding no video whatsoever. Matched by name so the
# add-on can drop them instantly instead of walking an entire tree to discover
# it is empty. Anything not listed here is checked for real.
NON_VIDEO_SECTIONS = ("BOOKS", "AUDIO")

# Canonical watch page. Note the API still returns legacy watch.formed.org
# slug URLs in item["link"] - those are dead and must not be used.
WATCH_PAGE = WEB_BASE + "/app/videos/{vid}?cid={cid}"

# --- Playback ---------------------------------------------------------------
# Authorisation for the Vimeo OTT embed is a "ticket" that belongs to the
# SUBSCRIBER, not to the video. It arrives at $.subscriber.vimeoAuthTicket from
# the endpoint above and the same value is reused for every title. The web app
# fetches it once at startup, which is why it never appears in any per-video
# or per-collection response.
EMBED_URL = "https://embed.vhx.tv/videos/{vid}"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Field carrying the ticket inside the subscriber payload. The fallback scan in
# play.py looks for any key matching /ticket/i, so a rename is survivable, but
# keep this current.
TICKET_KEYS = ("vimeoAuthTicket", "ticket", "embedTicket", "playbackTicket")

DEFAULT_TIMEOUT = 20
