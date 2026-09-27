"""Thin wrappers over the Kodi API so the rest of the add-on stays readable."""

import json
import os

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from . import const

ADDON = xbmcaddon.Addon(const.ADDON_ID)


def log(msg, level=xbmc.LOGINFO):
    xbmc.log("[%s] %s" % (const.ADDON_ID, msg), level)


def debug(msg):
    log(msg, xbmc.LOGDEBUG)


def error(msg):
    log(msg, xbmc.LOGERROR)


def setting(key, default=""):
    try:
        return ADDON.getSetting(key) or default
    except Exception:
        return default


def bool_setting(key, default=False):
    val = setting(key, "")
    if val == "":
        return default
    return val.lower() == "true"


def set_setting(key, value):
    ADDON.setSetting(key, value)


def localise(string_id, fallback=""):
    try:
        return ADDON.getLocalizedString(string_id) or fallback
    except Exception:
        return fallback


def profile_dir():
    path = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def read_json(name):
    path = os.path.join(profile_dir(), name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, OSError) as exc:
        error("could not read %s: %s" % (name, exc))
        return None


def write_json(name, payload):
    path = os.path.join(profile_dir(), name)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return True
    except OSError as exc:
        error("could not write %s: %s" % (name, exc))
        return False


def delete_file(name):
    path = os.path.join(profile_dir(), name)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def has_addon(addon_id):
    """True if an add-on is installed and enabled."""
    return bool(xbmc.getCondVisibility("System.HasAddon(%s)" % addon_id))


def notify(message, heading="FORMED", icon=xbmcgui.NOTIFICATION_INFO, ms=5000):
    xbmcgui.Dialog().notification(heading, message, icon, ms)


def ok_dialog(message, heading="FORMED"):
    xbmcgui.Dialog().ok(heading, message)


def yesno(message, heading="FORMED"):
    return xbmcgui.Dialog().yesno(heading, message)


def text_input(heading, hidden=False):
    kb = xbmc.Keyboard("", heading, hidden)
    kb.doModal()
    if not kb.isConfirmed():
        return None
    return kb.getText()


def progress_dialog(heading, message=""):
    dialog = xbmcgui.DialogProgress()
    dialog.create(heading, message)
    return dialog
