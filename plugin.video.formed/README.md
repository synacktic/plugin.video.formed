# plugin.video.formed

Unofficial Kodi client for [FORMED](https://formed.org). Signs in with your own
subscription, browses the catalogue, and streams.

**Playback only.** There is no download or record path anywhere in this add-on,
by design.

Not affiliated with or endorsed by FORMED or the Augustine Institute.

---

## Status

| Piece | State |
|---|---|
| OIDC device-code sign-in | Confirmed working against a live account |
| Token refresh / persistence | Confirmed working |
| Catalogue browsing | Endpoints verified against the live API |
| Ticket lookup | Confirmed - value matches the web player exactly |
| Embed page -> config_url | Confirmed |
| config -> manifest -> playback | Confirmed - plays |
| Resume points (read from FORMED) | Implemented |
| Subtitles | Confirmed working |
| Seeking | Root cause found - ISA was not installed |
| Search | Implemented against /api/search |

## Requirements

**InputStream Adaptive is required.** LibreELEC ships it; a desktop Kodi
install may not. Kodi should now pull it in automatically, but if seeking
stalls, verify it is installed and enabled before looking anywhere else.

No Widevine or DRM component is needed - see below.

### No DRM

Settled by observation, not assumption. With the player running, the `<video>`
element's `mediaKeys` is `null` while the browser fully supports EME - the
player had DRM available and did not use it. Plain HLS, no CDM required.
Nothing in this add-on touches Widevine.

### How playback authorisation actually works

The Vimeo OTT embed is authorised by a ticket that belongs to the **account,
not the video**. It lives at:

```
GET /api/users/me/subscriber   ->   $.subscriber.vimeoAuthTicket
```

One opaque 27-character value, reused for every title. The web app fetches it
once at startup and holds it for the session.

This was not obvious. The first two guesses - that the ticket came from
`/api/v2/videos/<id>`, then that it was embedded per-item in the collection
payload - were both wrong. It appears in no per-video response, no
per-collection response, and no network response at all during normal
browsing, because by then it has already been fetched. It was found by tracing
`vimeoAuthTicket` through the web bundle and confirmed by matching the value
against the one in the live player's iframe URL.

Practical consequence: playback costs one subscriber lookup, not a
per-video authorisation call.

### The rest of the chain

The embed page does not contain the stream. It contains a pointer:

```
GET /api/users/me/subscriber          -> subscriber.vimeoAuthTicket
GET embed.vhx.tv/videos/<formedId>
      ?api=1&ticket=<ticket>          -> OTTData.config_url
GET <config_url>                      -> HLS manifest
```

Two things to know about `config_url`:

- It is keyed by a **Vimeo** video id, unrelated to the FORMED id.
- It is **single-use**. Fetching one twice returns HTTP 410, so it must be
  consumed immediately after being read from the embed page. `play.py` does
  both in one pass for that reason.

## Install

Kodi 20 (Nexus) or newer.

**Test box (Windows / x86_64):**

```
# zip the folder so the archive contains plugin.video.formed/ at its root
Compress-Archive -Path D:\Formed\plugin.video.formed -DestinationPath D:\Formed\plugin.video.formed-0.1.0.zip
```

Kodi → Add-ons → Install from zip file. You may need to enable *Unknown
sources* in Settings → System → Add-ons first.

**Pi 5 / LibreELEC (ARM64):** same zip, same steps. Pure Python plus
`requests`, so nothing is architecture-specific.

Install `InputStream Adaptive` from the official Kodi repo if it isn't already
present. On LibreELEC it ships by default.

### About Widevine

Not needed unless the streams turn out to be DRM-encrypted, and current
evidence says they are not. Plain HLS plays through InputStream Adaptive with
no CDM at all. If a stream ever does come back encrypted, `play.py` logs a
note about it, and only then is the Widevine CDM add-on worth installing.

## Sign-in

The default is the OAuth device-code flow: the add-on shows a short code and a
URL, you approve it on a phone or laptop, and the add-on receives tokens.

Tokens go in the add-on's profile directory (`tokens.json`), not in
`settings.xml`. Kodi settings are plaintext and get swept up by backup and sync
tools, which is no place for a refresh token. Your password is never stored,
and under the device flow it is never even entered here.

The email/password fallback in settings exists only in case the device flow is
unavailable. It sends credentials straight to the identity provider and keeps
the password only for the duration of that one request.

## Layout

```
main.py                  entry point
resources/settings.xml   add-on settings
resources/lib/
  const.py               all endpoints - start here when FORMED changes
  kodiutils.py           Kodi API wrappers
  auth.py                OIDC device flow, password fallback, refresh
  api.py                 catalogue
  play.py                ticket -> embed -> manifest -> InputStream Adaptive
  router.py              routing and directory listings
```

`const.py` is deliberately the single place endpoints live. When something
breaks after a FORMED update, look there first.

## Resume points

FORMED's authenticated collection payload carries `duration` and `playedTime`,
both in **seconds** (confirmed by matching a title's reported duration against
the player's own). Where present, they become a Kodi resume point.

**This is one-way: FORMED -> Kodi.** Progress made while watching in Kodi does
not go back to FORMED, so the website's "continue watching" will not reflect it.
That is not an oversight - FORMED exposes no progress-write endpoint at all.
`playedTime` is populated by Vimeo OTT's own player telemetry, and a Kodi
add-on is not running that player. Kodi tracks its own local progress as usual;
the two simply do not sync back.

Both fields are absent on titles the account has never opened, so listings
degrade quietly rather than showing bogus resume points.

## Seeking, and why it failed

Symptom: playback starts fine, a seek stalls it, and the video quits shortly
after. The Kodi log showed `CVideoPlayerVideo::OutputPicture - timeout waiting
for buffer` with no HTTP errors at all.

The cause was **InputStream Adaptive not being installed**. It was declared
`optional="true"` here, so Kodi silently ignored every
`inputstream.adaptive.*` property and fell back to its built-in ffmpeg
demuxer. The give-away in the log was `CDVDVideoCodecFFmpeg::Open()` where ISA
lines should have been, and no `inputstream.adaptive` add-on directory
anywhere on disk.

ffmpeg plays these streams well enough to be misleading, but Vimeo delivers
them with `separate_av: true` - audio and video as separate renditions - and
ffmpeg's HLS demuxer handles seeking in that layout poorly. Hence a stall that
looks like a network or codec problem and is neither.

Fixed three ways:

1. ISA is now a **required** dependency, so Kodi installs it automatically.
2. If it is missing or disabled anyway, the add-on says so plainly once per
   session instead of degrading in silence.
3. Without ISA, headers are appended to the path, which is the only way the
   ffmpeg fallback will send them.

LibreELEC ships ISA by default, so the Pi 5 was never going to show this. A
desktop Kodi install is the odd one out.

### Not the cause

Worth recording so nobody re-investigates: codecs were fine. The log confirms
H.264 decoding, so the `avc_url` preference was already working. Header
handling was fine too - there were no 403s.

## Interface decisions

**Sign-in lives in settings, not the main menu.** A sign-out entry sitting in
the browse list is a stray click waiting to happen once you are already
authenticated. Settings > Account has both buttons.

**Video only.** PDFs, audiobooks and ebooks are filtered out of listings. This
is a video client; showing a PDF it cannot open is just clutter. Filtering is
by exclusion, so an unrecognised media type is still shown rather than
silently dropped.

**Empty menus are hidden.** Filtering out books and audio leaves some
categories with nothing in them - Books being the whole top-level section.
Rather than offer a menu that leads nowhere, those are dropped.

Determining emptiness means looking, so the check is bounded:

- `BOOKS` and `AUDIO` are matched by name and dropped instantly, since neither
  contains a single video and walking their trees would be pure waste.
- Everything else is probed for real, two levels deep, against a shared
  request budget. Probes run concurrently, are cached per process, and
  short-circuit on the first video found - so a healthy section usually costs
  one request.
- If the budget runs out, the answer is "keep it". A menu that turns out empty
  is a smaller failure than one wrongly hidden.

Toggle: *Hide empty menus*. Turn it off if browsing ever feels sluggish.

**Single seasons are collapsed.** Most shows have exactly one season, which
made for a menu containing a single entry. Selecting a show whose only child
is one season now goes straight to the episodes. Shows with genuine multiple
seasons still list them.

## Subtitles

Subtitles can arrive by two different routes, and for a while this add-on used
both at once:

1. **From the HLS manifest.** InputStream Adaptive exposes the subtitle
   renditions carried in the stream, properly language-labelled.
2. **From `request.text_tracks` in the player config**, attached with
   `setSubtitles()`.

Route 2 tracks are *external* subtitles as far as Kodi is concerned, and Kodi
has no way to know their language, so it lists them as "Unknown (External)".
When both routes fired, every track appeared twice - once named, once not.

External tracks are now attached only when nothing else is supplying them:
either ISA is absent, or the manifest carries no captions of its own
(`request.files.hls.captions`). When the add-on does supply them, only the
preferred language is attached, since a list of several indistinguishable
"Unknown" entries is worse than one.

### Choosing English by default

For tracks the add-on attaches, set *Subtitle language* in add-on settings
(English by default). Regional variants match on the primary subtag, so
English also covers `en-US` and Vimeo's auto-generated `en-x-autogen`.

For tracks coming from the manifest via ISA, selection is Kodi's own job, not
the add-on's - it applies globally rather than per-add-on. Set it under:

```
Settings > Player > Language > Preferred subtitle language
```

with *Settings > Player > Language > Enable subtitles by default* if you want
them on without pressing anything. Deliberately not overridden here; an
add-on silently changing a global playback preference would be rude.

## Search

`GET /api/search?query=<term>` returns a flat array of videos and collections.
Two quirks worth knowing:

- Matching is broad. A two-word query can return 300+ loosely related hits, so
  results are capped and titles genuinely containing the query are floated to
  the top, preserving the API's own order within each group.
- The `link` field is the dead `watch.formed.org` slug form, same as
  everywhere else in this API. Ids are the only reliable handle.

Search hits use `thumbnail` where collection listings use `thumbnailUrl`.

## Known gaps

- **Progress does not sync back to FORMED** - see above.
- **Artwork** is a generated placeholder. Replace `resources/icon.png` and
  `resources/fanart.jpg` with anything you prefer.
- **PDF discussion guides** appear greyed out in listings. They are served from
  signed, expiring CDN URLs and are not playable - open them in a browser.
- **Audio-only titles** are listed but routed through the video player.

## Debugging

Enable *Verbose logging* in add-on settings, reproduce, then read the Kodi log.
Every stage of the playback chain logs what it found or what it expected:

- `no ticket in subscriber payload; keys were: [...]` - `vimeoAuthTicket` was
  renamed or moved. The log prints the actual keys; add the new name to
  `TICKET_KEYS` in `const.py`.
- `The player rejected the ticket` - authorisation reached the player and was
  refused. Entitlement problem, or the ticket expired in flight.
- `no manifest for video <id> (... config_url MISSING)` - the embed page no
  longer exposes `OTTData.config_url`. Adjust `_extract_config_url`.
- `player config fetched but contained no manifest` - Vimeo reshuffled the
  config document. `_manifest_from_config` falls back to a recursive scan, so
  this means the shape changed enough that no `.m3u8` is present at all.
- `The player config link had already expired` - HTTP 410. The config URL is
  single-use; something retried a stale one. Playing again should clear it.
