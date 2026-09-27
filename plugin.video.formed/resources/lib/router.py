"""Plugin routing and directory listing."""

import re
import sys
import urllib.parse

import xbmcgui
import xbmcplugin

from . import api
from . import auth
from . import const
from . import kodiutils as ku
from . import play


class Router(object):
    def __init__(self, base_url, handle, query_string):
        self.base = base_url
        self.handle = handle
        self.args = urllib.parse.parse_qs(query_string.lstrip("?"))

    # -- helpers -------------------------------------------------------------

    def url(self, **kwargs):
        return self.base + "?" + urllib.parse.urlencode(kwargs)

    def arg(self, name, default=None):
        values = self.args.get(name)
        return values[0] if values else default

    def _add_dir(self, label, url, item=None, plot=""):
        li = xbmcgui.ListItem(label=label)
        art = api.artwork(item) if item else {}
        if art:
            li.setArt(art)
        info = li.getVideoInfoTag()
        info.setTitle(label)
        info.setPlot(plot or (api.plot(item) if item else ""))
        xbmcplugin.addDirectoryItem(self.handle, url, li, isFolder=True)

    def _add_video(self, label, vid, item):
        li = xbmcgui.ListItem(label=label)
        li.setArt(api.artwork(item))
        li.setProperty("IsPlayable", "true")
        info = li.getVideoInfoTag()
        info.setMediaType("video")
        info.setTitle(label)
        info.setPlot(api.plot(item))

        # Both are in seconds, and both are absent on titles the account has
        # never opened - so neither can be assumed present.
        duration = _as_seconds(item.get("duration"))
        played = _as_seconds(item.get("playedTime"))

        if duration:
            info.setDuration(duration)

        # Only mark a resume point for genuine mid-watch positions. Restoring
        # a position within a few seconds of either end is just annoying.
        if duration and played and 5 < played < duration - 15:
            # setResumePoint supersedes the ResumeTime/TotalTime properties,
            # which Kodi now warns about on every listing.
            info.setResumePoint(float(played), float(duration))

        xbmcplugin.addDirectoryItem(
            self.handle,
            self.url(action="play", vid=vid, label=label),
            li,
            isFolder=False,
        )

    def _end(self, content="videos"):
        xbmcplugin.setContent(self.handle, content)
        xbmcplugin.addSortMethod(self.handle, xbmcplugin.SORT_METHOD_NONE)
        xbmcplugin.endOfDirectory(self.handle)

    def _error(self, message):
        ku.ok_dialog(message)
        if self.handle >= 0:
            xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    # -- routes --------------------------------------------------------------

    def dispatch(self):
        action = self.arg("action", "root")
        route = getattr(self, "route_" + action, None)
        if route is None:
            ku.error("unknown action %r" % action)
            return self._error("Unknown action: %s" % action)
        return route()

    def route_root(self):
        # Sign-in lives in add-on settings, not here. Putting it in the main
        # list meant a stray click after authenticating could sign you out.
        if not auth.is_signed_in():
            self._add_dir(
                "[COLOR yellow]Not signed in - open Settings to sign in[/COLOR]",
                self.url(action="opensettings"),
                plot="Sign in from the add-on's settings, under Account.",
            )

        self._add_dir(
            "[B]Kids[/B]",
            self.url(action="collection", cid=const.KIDS_COLLECTION),
            plot="The whole children's catalogue.",
        )

        try:
            nav = api.nav_links()
        except api.ApiError as exc:
            return self._error(str(exc))

        hide_empty = ku.bool_setting("hide_empty_menus", True)
        budget = [16]

        for section, entries in nav.items():
            pairs = _nav_pairs(entries)
            if not pairs:
                continue

            # Books and Audio hold nothing this add-on will show, and are
            # named unambiguously - drop them without walking their trees.
            if section.strip().upper() in const.NON_VIDEO_SECTIONS:
                ku.debug("hiding non-video section %r" % section)
                continue

            if hide_empty:
                cids = [_collection_id(link) for _, link in pairs]
                if not api.any_has_videos(cids, budget):
                    ku.debug("hiding empty section %r" % section)
                    continue

            self._add_dir(
                section.title(),
                self.url(action="section", name=section),
                plot="%d categories" % len(pairs),
            )

        self._add_dir("Search", self.url(action="search"))
        self._end(content="")

    def route_opensettings(self):
        ku.ADDON.openSettings()
        if self.handle >= 0:
            xbmcplugin.endOfDirectory(self.handle, succeeded=False)

    def route_section(self):
        name = self.arg("name", "")
        try:
            nav = api.nav_links()
        except api.ApiError as exc:
            return self._error(str(exc))

        pairs = _nav_pairs(nav.get(name))
        if not pairs:
            return self._error("Nothing in %s." % name)

        entries = [(l, _collection_id(k)) for l, k in pairs]
        entries = [e for e in entries if e[1]]

        if ku.bool_setting("hide_empty_menus", True):
            entries = api.filter_nonempty(entries, key=lambda e: e[1])

        if not entries:
            return self._error("Nothing watchable in %s." % name)

        for label, cid in entries:
            self._add_dir(label, self.url(action="collection", cid=cid))
        self._end(content="")

    def route_collection(self):
        cid = self.arg("cid")
        if not cid:
            return self._error("Missing collection id.")

        try:
            items = self._descend_single_seasons(cid)
        except api.ApiError as exc:
            return self._error(str(exc))

        videos = [i for i in items if api.is_video(i)]
        subs = [i for i in items if api.is_collection(i)]

        if not videos and not subs:
            return self._error("Nothing watchable in here.")

        if subs and ku.bool_setting("hide_empty_menus", True):
            subs = api.filter_nonempty(subs, key=lambda i: i.get("id"))

        for item in subs:
            self._add_dir(
                item.get("name") or "Untitled",
                self.url(action="collection", cid=item["id"]),
                item,
            )

        for item in videos:
            self._add_video(item.get("name") or "Untitled", item["id"], item)

        self._end()

    def _descend_single_seasons(self, cid, max_depth=4):
        """Return a collection's items, skipping pointless single-season menus.

        Most shows have exactly one season, so the season list is a menu with a
        single entry in it. Where a collection holds one sub-collection and no
        videos of its own, descend into it and list those episodes directly.
        Shows with genuinely multiple seasons are left alone.
        """
        seen = set()
        items = []

        for _ in range(max_depth):
            items = api.collection_items(cid)
            videos = [i for i in items if api.is_video(i)]
            subs = [i for i in items if api.is_collection(i)]

            if len(subs) == 1 and not videos:
                nxt = subs[0].get("id")
                if not nxt or nxt in seen:
                    break
                seen.add(nxt)
                ku.debug("collapsing lone sub-collection %s -> %s" % (cid, nxt))
                cid = nxt
                continue

            break

        return items

    def route_play(self):
        vid = self.arg("vid")
        if not vid:
            return self._error("Missing video id.")
        play.resolve_and_play(self.handle, vid, self.arg("label", ""))

    def route_search(self):
        term = self.arg("q") or ku.text_input("Search FORMED")
        if not term:
            return xbmcplugin.endOfDirectory(self.handle, succeeded=False)

        try:
            results = api.search(term)
        except api.ApiError as exc:
            return self._error(str(exc))

        videos, collections = [], []
        for item in results:
            kind = api.search_kind(item)
            if kind == "video":
                videos.append(item)
            elif kind == "collection":
                collections.append(item)

        if not videos and not collections:
            return self._error('Nothing found for "%s".' % term)

        for item in collections:
            cid = item.get("id")
            if cid:
                self._add_dir(
                    "[%s] %s" % ("Series", item.get("name") or "Untitled"),
                    self.url(action="collection", cid=cid),
                    item,
                )

        for item in videos:
            vid = item.get("id")
            if vid:
                self._add_video(item.get("name") or "Untitled", vid, item)

        self._end()

    def route_signin(self):
        try:
            auth.login()
        except auth.AuthError as exc:
            ku.ok_dialog(str(exc))
            return self._finish(False)
        ku.notify("Signed in to FORMED")
        self._finish(True)

    def route_signout(self):
        auth.sign_out()
        ku.notify("Signed out")
        self._finish(True)

    def _finish(self, ok):
        """End a directory, tolerating being run outside one.

        Sign-in and sign-out are triggered from the settings screen via
        RunPlugin, where Kodi passes handle -1 and there is no directory to
        close.
        """
        if self.handle >= 0:
            xbmcplugin.endOfDirectory(self.handle, succeeded=ok, updateListing=True)


def _as_seconds(value):
    """Coerce a duration/position field to whole seconds, or None.

    The API reports seconds - verified by matching a title's reported duration
    against the player's own. Millisecond values are still tolerated in case
    that ever changes, since nothing in this catalogue runs for 24 hours.
    """
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 86400:
        number //= 1000
    return number or None


def _nav_pairs(entries):
    """Normalise a nav-links section into a list of (label, link) pairs.

    The live API returns a plain dict of {label: "/app/collections/<id>"}, but
    the list-of-objects shape is accepted too so a future reshuffle doesn't
    silently empty the menu the way it did once already.
    """
    if not entries:
        return []

    if isinstance(entries, dict):
        return [
            (label, link)
            for label, link in entries.items()
            if isinstance(link, str)
        ]

    if isinstance(entries, list):
        pairs = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            label = entry.get("name") or entry.get("label")
            link = entry.get("link") or entry.get("url")
            if label and isinstance(link, str):
                pairs.append((label, link))
        return pairs

    return []


def _collection_id(link):
    """Pull a collection id out of an app URL or bare path."""
    if not link:
        return None
    match = re.search(r"/collections/(\d+)", link)
    return match.group(1) if match else None


def run():
    # RunPlugin (used by the settings buttons) invokes the add-on without a
    # directory handle, and may not pass argv[1] or argv[2] at all.
    try:
        handle = int(sys.argv[1])
    except (IndexError, ValueError):
        handle = -1
    query = sys.argv[2] if len(sys.argv) > 2 else ""

    Router(sys.argv[0], handle, query).dispatch()
