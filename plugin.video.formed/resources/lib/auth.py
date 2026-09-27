"""OpenID Connect sign-in against the FORMED identity provider.

Two grants are supported:

  device_code  (default)  You are shown a short code and a URL. You approve on
                          a phone or laptop; the add-on polls until the
                          provider hands back tokens. Nothing sensitive is
                          typed on a remote and no password is ever stored.

  password     (fallback) Direct username/password. Only used if you turn it
                          on in settings, for cases where the device flow is
                          unavailable.

Tokens live in the add-on's profile directory, not in settings.xml. Kodi
settings are world-readable plaintext and get picked up by backup tools; a
refresh token there is a bad idea.
"""

import time

import requests

from . import const
from . import kodiutils as ku

TOKEN_FILE = "tokens.json"

# Refresh this many seconds before actual expiry, so a slow request doesn't
# race the deadline.
EXPIRY_MARGIN = 60


class AuthError(Exception):
    """Sign-in failed in a way the user needs to know about."""


def _now():
    return int(time.time())


def _store(payload):
    """Persist a token response, converting relative lifetimes to absolute."""
    record = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "expires_at": _now() + int(payload.get("expires_in", 300)),
        "refresh_expires_at": (
            _now() + int(payload["refresh_expires_in"])
            if payload.get("refresh_expires_in")
            else None
        ),
    }
    ku.write_json(TOKEN_FILE, record)
    return record


def _load():
    return ku.read_json(TOKEN_FILE)


def sign_out():
    ku.delete_file(TOKEN_FILE)


def is_signed_in():
    record = _load()
    return bool(record and record.get("refresh_token"))


def _post_token(data):
    data = dict(data)
    data["client_id"] = const.CLIENT_ID
    try:
        resp = requests.post(
            const.TOKEN_URL,
            data=data,
            timeout=const.DEFAULT_TIMEOUT,
            headers={"User-Agent": const.USER_AGENT},
        )
    except requests.RequestException as exc:
        raise AuthError("Could not reach the sign-in server: %s" % exc)
    return resp


# --- Device code flow -------------------------------------------------------

def _start_device_flow():
    try:
        resp = requests.post(
            const.DEVICE_URL,
            data={"client_id": const.CLIENT_ID, "scope": "openid profile email"},
            timeout=const.DEFAULT_TIMEOUT,
            headers={"User-Agent": const.USER_AGENT},
        )
    except requests.RequestException as exc:
        raise AuthError("Could not reach the sign-in server: %s" % exc)

    if resp.status_code != 200:
        raise AuthError(
            "Device sign-in was refused (HTTP %s). You can enable the "
            "username/password fallback in add-on settings." % resp.status_code
        )
    return resp.json()


def _device_login():
    info = _start_device_flow()

    device_code = info["device_code"]
    user_code = info["user_code"]
    interval = max(int(info.get("interval", 5)), 1)
    expires_in = int(info.get("expires_in", 600))
    verify_uri = info.get("verification_uri") or ""
    complete_uri = info.get("verification_uri_complete") or ""

    # Both are logged so they can be recovered from kodi.log if the on-screen
    # text is ever unreadable on a given skin.
    ku.log("device sign-in: %s  code %s" % (verify_uri, user_code))

    # The code goes in its own OK dialog first. A progress dialog cannot be
    # scrolled and clips long lines, so showing a long URL and the code
    # together meant the code itself could end up cut off - which is exactly
    # the thing you need to read.
    ku.ok_dialog(
        "Go to this address:\n\n%s\n\nEnter code:  [B]%s[/B]" % (verify_uri, user_code),
        "FORMED sign-in",
    )

    if complete_uri and complete_uri != verify_uri:
        ku.debug("provider also offers a code-embedded verification URI")

    # Keep the polling dialog to two short lines so nothing can clip.
    message = "Code:  [B]%s[/B]\nWaiting for approval..." % user_code

    dialog = ku.progress_dialog("FORMED sign-in", message)
    deadline = _now() + expires_in

    try:
        while _now() < deadline:
            if dialog.iscanceled():
                raise AuthError("Sign-in cancelled.")

            remaining = deadline - _now()
            dialog.update(
                max(0, int(100 - (remaining / float(expires_in)) * 100)), message
            )

            # Sleep in short slices so cancel stays responsive.
            for _ in range(interval * 2):
                if dialog.iscanceled():
                    raise AuthError("Sign-in cancelled.")
                time.sleep(0.5)

            resp = _post_token(
                {"grant_type": const.GRANT_DEVICE, "device_code": device_code}
            )

            if resp.status_code == 200:
                return _store(resp.json())

            err = ""
            try:
                err = resp.json().get("error", "")
            except ValueError:
                pass

            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 2
                continue
            if err == "expired_token":
                raise AuthError("The code expired. Please try again.")
            if err == "access_denied":
                raise AuthError("Sign-in was denied.")

            raise AuthError("Sign-in failed: %s" % (err or resp.status_code))

        raise AuthError("The code expired. Please try again.")
    finally:
        dialog.close()


# --- Password grant fallback ------------------------------------------------

def _password_login():
    username = ku.setting("username")
    if not username:
        username = ku.text_input("FORMED email")
    if not username:
        raise AuthError("Sign-in cancelled.")

    password = ku.text_input("FORMED password", hidden=True)
    if not password:
        raise AuthError("Sign-in cancelled.")

    resp = _post_token(
        {
            "grant_type": const.GRANT_PASSWORD,
            "username": username,
            "password": password,
            "scope": "openid profile email",
        }
    )
    # Deliberately not retained beyond this call.
    del password

    if resp.status_code != 200:
        raise AuthError("Sign-in failed - check your email and password.")
    return _store(resp.json())


# --- Public surface ---------------------------------------------------------

def login():
    if ku.bool_setting("use_password_grant"):
        return _password_login()
    return _device_login()


def _refresh(record):
    resp = _post_token(
        {
            "grant_type": const.GRANT_REFRESH,
            "refresh_token": record["refresh_token"],
        }
    )
    if resp.status_code != 200:
        ku.debug("refresh rejected (HTTP %s), full sign-in required" % resp.status_code)
        sign_out()
        return None
    return _store(resp.json())


def access_token(interactive=True):
    """Return a usable access token, refreshing or signing in as needed.

    interactive=False is for background contexts where throwing a dialog at
    the user would be wrong; it returns None instead of prompting.
    """
    record = _load()

    if record and record.get("access_token"):
        if record.get("expires_at", 0) - EXPIRY_MARGIN > _now():
            return record["access_token"]

    if record and record.get("refresh_token"):
        refreshed = _refresh(record)
        if refreshed:
            return refreshed["access_token"]

    if not interactive:
        return None

    return login()["access_token"]
