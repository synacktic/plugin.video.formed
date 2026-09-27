# plugin.video.formed

An unofficial Kodi add-on for [FORMED](https://formed.org), plus the Kodi
repository that distributes it — both in this one repo, so a release is a
single commit and the published zip always matches the source it was built
from.

Playback only; it does not download or record. Not affiliated with or endorsed
by FORMED or the Augustine Institute. You need your own FORMED subscription.

## Install in Kodi

1. **Settings → System → Add-ons → Unknown sources** → **on**.
   Kodi refuses third-party installs without this and fails quietly enough to
   be confusing.
2. Download [`repo/repository.formed/repository.formed-1.0.0.zip`](repo/repository.formed/repository.formed-1.0.0.zip).
3. **Settings → Add-ons → Install from zip file** → pick that zip.
4. **Settings → Add-ons → Install from repository → FORMED Add-on Repository →
   Video add-ons → FORMED → Install**.

Updates arrive on their own after that.

Then sign in: **the add-on's Settings → Account → Sign in to FORMED**. You get
a short code to approve on a phone or laptop; no password is typed on the
remote or stored on the device.

## Layout

    plugin.video.formed/   the add-on source - edit here
    repo/                  generated; never edit by hand
      addons.xml           add-on metadata, concatenated
      addons.xml.md5       checksum Kodi polls to spot changes
      plugin.video.formed/ the add-on, as <id>-<version>.zip
      repository.formed/   bootstrap add-on holding the three URLs above
    build.py               regenerates everything under repo/

`build.py` resolves paths from its own location, so it works in any clone with
no arguments.

## Releasing

```sh
python build.py --install-hooks   # once per clone
```

After that a release is just:

```sh
# 1. edit under plugin.video.formed/
# 2. bump <addon version="..."> in plugin.video.formed/addon.xml
git commit -am "0.2.6" && git push
```

The pre-commit hook rebuilds `repo/` and stages it, so the published tree can
never lag the source. To check by hand, or from CI:

```sh
python build.py            # rebuild
python build.py --check    # exit 1 if repo/ is stale, writes nothing
```

**The version number is the trigger.** Kodi polls `addons.xml.md5`; when it
changes it re-reads `addons.xml` and offers any higher version it finds.
Pushing changed code under an unchanged version updates nobody.

## Renaming or moving the repo

The branch and repo name are baked into the bootstrap add-on's URLs, so
regenerate rather than hand-editing, then reinstall that zip:

```sh
python build.py --user <you> --repo <repo-name> --branch <branch>
python build.py --host pages          # serve via GitHub Pages instead
```

## Notes

Playback resolves through Vimeo OTT's `embed.vhx.tv`, the same endpoint
FORMED's own web player uses. That host returns HTTP 503 in streaks lasting
minutes at a time; it is not an add-on fault and not an account fault. The
add-on fails in about two seconds rather than hanging, and consults FORMED's
own `/api/vimeo/health-check` — the signal behind the outage banner on their
website — so a genuine FORMED outage says so instead of looking local.
