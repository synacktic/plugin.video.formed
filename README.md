# FORMED Kodi Repository

A Kodi add-on repository served as static files straight from GitHub. No server
and no GitHub Pages build is required — Kodi only ever performs plain HTTP GETs.

Unofficial. Not affiliated with or endorsed by FORMED or the Augustine Institute.

## Install (once per device)

1. In Kodi: **Settings → System → Add-ons → Unknown sources** → **on**.
   Kodi refuses to install anything from outside its own repository without
   this, and it fails quietly enough to be confusing.
2. Download `repository.formed/repository.formed-1.0.0.zip` from this repo.
3. **Settings → Add-ons → Install from zip file** → choose that zip.
4. **Settings → Add-ons → Install from repository → FORMED Add-on Repository →
   Video add-ons → FORMED → Install**.

From then on Kodi updates the add-on by itself.

## Publishing an update

```sh
# 1. bump <addon version="..."> in plugin.video.formed/addon.xml
python build.py
git add -A && git commit -m "plugin.video.formed 0.2.6" && git push
```

`build.py` repackages the add-on, regenerates `addons.xml` and recomputes
`addons.xml.md5`. Kodi polls the md5; when it changes it re-reads `addons.xml`
and offers any higher version it finds. **The version number is the trigger** —
pushing changed code under an unchanged version updates nobody.

## If your GitHub user or repo name differs

The URLs are baked into the repository add-on, so regenerate rather than
hand-editing:

```sh
python build.py --user <you> --repo <repo-name> --branch master
# or, to serve from GitHub Pages instead of raw.githubusercontent.com:
python build.py --user <you> --repo <repo-name> --host pages
```

Then reinstall the bootstrap zip, since the URLs inside it changed.

## Layout

    addons.xml             every add-on's metadata, concatenated
    addons.xml.md5         checksum Kodi polls to detect changes
    plugin.video.formed/   the add-on, as <id>-<version>.zip
    repository.formed/     the bootstrap add-on holding the three URLs
    build.py               regenerates all of the above
