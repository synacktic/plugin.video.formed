#!/usr/bin/env python3
"""Build a Kodi repository tree that can be served straight from GitHub.

A Kodi repository is not a service - it is four static things behind any
plain HTTP server:

    addons.xml          every addon's metadata, concatenated
    addons.xml.md5      a checksum of that file, how Kodi spots changes
    <id>/<id>-<ver>.zip the addons themselves, in per-id folders
    repository.<name>   a tiny addon holding the three URLs above

Re-run this after bumping any addon version, then commit and push. Kodi
notices because addons.xml.md5 changed.
"""

import argparse
import hashlib
import io
import os
import re
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

REPO_ADDON_VERSION = "1.0.0"


def repo_addon_xml(repo_id, name, author, base_url):
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="{rid}"
       name="{name}"
       version="{ver}"
       provider-name="{author}">

  <extension point="xbmc.addon.repository" name="{name}">
    <dir>
      <!-- Kodi re-reads the checksum on a schedule; when it differs from the
           copy it has, it pulls addons.xml again and offers any new versions
           it finds there. Updating an addon is therefore: bump the version,
           rebuild, push. -->
      <info compressed="false">{base}/addons.xml</info>
      <checksum>{base}/addons.xml.md5</checksum>
      <datadir zip="true">{base}</datadir>
    </dir>
  </extension>

  <extension point="xbmc.addon.metadata">
    <summary lang="en_GB">Add-on repository for the unofficial FORMED client</summary>
    <description lang="en_GB">Installs and keeps the FORMED video add-on up to date. Hosted as static files; it is not affiliated with or endorsed by FORMED or the Augustine Institute.</description>
    <platform>all</platform>
    <assets>
      <icon>icon.png</icon>
    </assets>
  </extension>
</addon>
""".format(rid=repo_id, name=name, ver=REPO_ADDON_VERSION, author=author, base=base_url.rstrip("/"))


def read_addon_element(path):
    """Return an addon.xml's root <addon> element, declaration stripped."""
    tree = ET.parse(path)
    return tree.getroot()


# Everything is resolved from this file's own location, so the script works in
# any clone without editing paths into it.
ROOT = os.path.dirname(os.path.abspath(__file__))

# The generated repository lives in a subdirectory rather than at the root.
# That is not cosmetic: with the add-on source at the root, any slip in the
# packaging exclusions would zip this directory into the add-on, commit it,
# and then zip that again next release.
SUBDIR = "repo"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="synacktic")
    ap.add_argument("--repo", default="plugin.video.formed",
                    help="the GitHub repository name, not the add-on id")
    ap.add_argument("--branch", default="master")
    ap.add_argument("--host", choices=["raw", "pages"], default="raw",
                    help="raw=raw.githubusercontent.com (works on push); "
                         "pages=GitHub Pages (needs Pages enabled)")
    ap.add_argument("--out", default=os.path.join(ROOT, SUBDIR))
    ap.add_argument("--source", default=os.path.join(ROOT, "plugin.video.formed"))
    ap.add_argument("--install-hooks", action="store_true",
                    help="install the pre-commit hook into this clone, so "
                         "repo/ is rebuilt automatically on every commit")
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if repo/ is out of date with "
                         "the source. For hooks and CI.")
    args = ap.parse_args()

    if args.install_hooks:
        install_hooks()
        sys.exit(0)

    # --check builds somewhere disposable and compares, so it can answer
    # "would a rebuild change anything?" without touching the tree.
    check_against = None
    if args.check:
        check_against = os.path.abspath(args.out)
        args.out = tempfile.mkdtemp(prefix="kodirepo-check-")

    if args.host == "raw":
        base = "https://raw.githubusercontent.com/%s/%s/%s/%s" % (
            args.user, args.repo, args.branch, SUBDIR)
    else:
        base = "https://%s.github.io/%s/%s" % (args.user, args.repo, SUBDIR)

    out = os.path.abspath(args.out)
    repo_id = "repository.formed"
    plugin_dir_name = ET.parse(
        os.path.join(os.path.abspath(args.source), "addon.xml")
    ).getroot().get("id")

    # Clear only the directories this script owns, so a withdrawn version
    # cannot linger. Emphatically not an rmtree of `out`: that directory is
    # also the git repository, holding README.md, build.py and .git, and
    # wiping it would take all three with it on every rebuild.
    os.makedirs(out, exist_ok=True)
    for stale in ("addons.xml", "addons.xml.md5"):
        try:
            os.remove(os.path.join(out, stale))
        except OSError:
            pass
    for sub in (plugin_dir_name, repo_id):
        shutil.rmtree(os.path.join(out, sub), ignore_errors=True)

    addon_elements = []

    # ---- 1. the plugin ----------------------------------------------------
    src = os.path.abspath(args.source)
    plugin_xml = os.path.join(src, "addon.xml")
    el = read_addon_element(plugin_xml)
    plugin_id, plugin_ver = el.get("id"), el.get("version")
    addon_elements.append(el)

    pdir = os.path.join(out, plugin_id)
    os.makedirs(pdir)
    zip_name = "%s-%s.zip" % (plugin_id, plugin_ver)
    zpath = os.path.join(pdir, zip_name)
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
            for f in sorted(filenames):
                if f.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(dirpath, f)
                arc = plugin_id + "/" + os.path.relpath(full, src).replace(os.sep, "/")
                z.write(full, arc)
                n += 1
    print("  %s  (%d files, %d KB)" % (zip_name, n, os.path.getsize(zpath) // 1024))

    # Artwork alongside the zip, so the repo browser shows it without
    # downloading the addon first.
    for art in ("icon.png", "fanart.jpg"):
        cand = os.path.join(src, "resources", art)
        if os.path.isfile(cand):
            shutil.copy2(cand, os.path.join(pdir, art))

    # ---- 2. the repository addon itself -----------------------------------
    rdir = os.path.join(out, repo_id)
    os.makedirs(rdir)
    rxml = repo_addon_xml(repo_id, "FORMED Add-on Repository", args.user, base)
    addon_elements.append(ET.fromstring(rxml.encode("utf-8")))

    icon_src = os.path.join(src, "resources", "icon.png")
    rzip = os.path.join(rdir, "%s-%s.zip" % (repo_id, REPO_ADDON_VERSION))
    with zipfile.ZipFile(rzip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(repo_id + "/addon.xml", rxml)
        if os.path.isfile(icon_src):
            z.write(icon_src, repo_id + "/icon.png")
    if os.path.isfile(icon_src):
        shutil.copy2(icon_src, os.path.join(rdir, "icon.png"))
    print("  %s-%s.zip  (the one you install first)" % (repo_id, REPO_ADDON_VERSION))

    # ---- 3. addons.xml + checksum -----------------------------------------
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<addons>"]
    for el in addon_elements:
        # Emitted verbatim. Pretty-printing this would indent the inside
        # of multi-line <description> and <news> text too, silently
        # rewriting content that users read in the add-on browser.
        parts.append(ET.tostring(el, encoding="unicode").strip())
    parts.append("</addons>\n")
    addons_xml = "\n".join(parts)

    axml = os.path.join(out, "addons.xml")
    with io.open(axml, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(addons_xml)

    digest = hashlib.md5(io.open(axml, "rb").read()).hexdigest()
    with io.open(os.path.join(out, "addons.xml.md5"), "w",
                 encoding="utf-8", newline="\n") as fh:
        fh.write(digest + "\n")
    print("  addons.xml (%d addons)  md5=%s" % (len(addon_elements), digest))

    if check_against is not None:
        stale = compare(check_against, out)
        shutil.rmtree(out, ignore_errors=True)
        if stale:
            print("\n%s is out of date:" % SUBDIR)
            for line in stale:
                print("  %s" % line)
            print("\nRun: python build.py")
            sys.exit(1)
        print("\n%s is up to date with the source." % SUBDIR)
        sys.exit(0)

    return out, base, repo_id, plugin_id, plugin_ver


def install_hooks():
    """Copy hooks/ into .git/hooks.

    Git deliberately does not run hooks straight out of a tracked directory -
    cloning a repo must never execute its author's code - so they are kept in
    hooks/ under version control and copied in explicitly.
    """
    src_dir = os.path.join(ROOT, "hooks")
    dst_dir = os.path.join(ROOT, ".git", "hooks")
    if not os.path.isdir(dst_dir):
        print("no .git/hooks here - is this a clone?")
        return
    for name in sorted(os.listdir(src_dir)):
        src, dst = os.path.join(src_dir, name), os.path.join(dst_dir, name)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o755)
        print("installed .git/hooks/%s" % name)


def compare(published, fresh):
    """Differences between a published repo tree and a freshly built one.

    Zip archives are compared by their entry names and CRCs rather than by
    file bytes: two archives built from identical sources still differ byte
    for byte, because each stores its own modification timestamps.
    """
    problems = []

    def entries(path):
        with zipfile.ZipFile(path) as z:
            return {i.filename: i.CRC for i in z.infolist()}

    for dirpath, _, filenames in os.walk(fresh):
        for f in filenames:
            new = os.path.join(dirpath, f)
            rel = os.path.relpath(new, fresh)
            old = os.path.join(published, rel)
            if not os.path.exists(old):
                problems.append("missing: %s" % rel.replace(os.sep, "/"))
            elif f.endswith(".zip"):
                if entries(old) != entries(new):
                    problems.append("contents differ: %s" % rel.replace(os.sep, "/"))
            elif io.open(old, "rb").read() != io.open(new, "rb").read():
                problems.append("differs: %s" % rel.replace(os.sep, "/"))

    for dirpath, _, filenames in os.walk(published):
        for f in filenames:
            rel = os.path.relpath(os.path.join(dirpath, f), published)
            if not os.path.exists(os.path.join(fresh, rel)):
                problems.append("stale, no longer built: %s" % rel.replace(os.sep, "/"))

    return sorted(problems)


if __name__ == "__main__":
    out, base, rid, pid, pver = main()
    print("\nbase URL: %s" % base)
