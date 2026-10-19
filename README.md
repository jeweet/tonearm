# Tonearm

A GTK4 + libadwaita music remote for Kodi, for the desktop and for Linux phones
(Phosh, Plasma Mobile, postmarketOS, Mobian…).

## Features

- **Spotlight, Albums, Artists** — cover grids and lists, album pages with a
  big-cover header and track list (multi-disc aware), artist pages with bio and
  discography. Right-click or long-press anything for Play / Shuffle / Play Next /
  Add to Queue.
- **Spotlight** — what's new as a magazine page: the latest album, new arrivals,
  artists in the spotlight, a random artist to rediscover, releases coming from Lidarr,
  **Jump Back In** (albums you left half-way — one tap plays on) and **radio tiles**
  from what you play most, then this week's and earlier arrivals.
- **Playlists** — make your own: New Playlist, then "Add to Playlist" in any album or
  song menu; play or shuffle them on Kodi, remove songs, rename or delete. (Kodi's API
  can read its own playlist files but not write them, so Tonearm keeps yours per Kodi
  instance.) Kodi's smart and .m3u playlists are listed too, read-only.
- **Favorites** — a Favorites section with Kodi's favorites (albums and artists open
  in Tonearm, songs play, Kodi screens open on the TV); ★ on album covers, track rows,
  artist pages and the player, and Add to / Remove from Favorites in the menus.
- **View options everywhere albums are shown** — Grid or List (Nautilus-style
  columns with sortable headers that drop out as the window narrows), small / medium /
  large covers, with or without titles (a "cover wall"). One setting, applied to
  every album view including artist pages.
- **What should I play?** — the radio button next to the search bar: pick a decade
  and a genre (or "Surprise Me") for an endless random mix from your library. It
  tops itself up before the queue runs out; playing anything else ends it.
- **Recently Played** and **Most Played** — two sections, straight from Kodi's play
  counts (remembered for offline use).
- **Genres** — your top genres as coloured tiles, then every genre (sort A–Z or by
  size, hide one-album genres). A genre's page has Play / Shuffle / Radio, its top
  artists, decade chips to narrow the albums and the genres it's often paired with.
- **Decades** — coloured tiles with a fan of covers and a one-tap radio.
- **Now playing** — a persistent mini player (swipe up to open, swipe sideways to
  skip) and a full player with seek, shuffle, repeat, volume and an editable queue.
  On phones the full player opens as a bottom sheet.
- **MPRIS** — Kodi appears in the lock screen, the Phosh/GNOME/KDE media
  controls, headset buttons and KDE Connect. Artwork is served to the lock screen
  from the local cache. With *Keep Running in Background* enabled, closing the
  window keeps the controls alive.
- **Volume keys → Kodi** (opt-in, Preferences › General; phones and laptops) — while Kodi plays,
  the system volume (hardware keys, the shell's volume slider) sets Kodi's volume.
  Tonearm adds a virtual "Kodi (Tonearm)" audio output, makes it the default while
  playing, mirrors its volume to Kodi and back, and passes the phone's own sounds
  through to the speakers (they follow Kodi's level meanwhile). Everything is put back
  when playback stops or the app quits. Needs `pactl` (PipeWire or PulseAudio).
  To test it on its own: `python3 -m tonearm.volumebridge` (shows what fails, if anything).
- **fzf-style search** from the search bar in every page header — results pop up
  over the page as you type (Esc or a click outside closes them) — across artists, albums and songs with match highlighting,
  scopes, smart case, accent folding and fzf's extended syntax:
  `'exact` `^prefix` `suffix$` `!exclude` `a | b`. Narrowing as you type reuses the
  previous result set, so it stays fast on big libraries.
- **Robust offline cache** — one SQLite database plus an artwork cache per Kodi
  instance. The library is browsable instantly at start-up and when Kodi is off.
- **Incremental sync** — only changes are fetched (details below). Live updates
  arrive over Kodi's TCP notification port when enabled.
- **Lidarr** (optional) — search also asks Lidarr/MusicBrainz and lists artists
  that aren't in Kodi under *Not in Kodi · from Lidarr*. Opening one shows its
  bio, genres, rating and links, with **Add to Lidarr** (monitor mode, quality
  profile, root folder, search right away). Artists already in Lidarr show their
  albums with tracks-on-disk counts, a Monitored switch and *Search Missing*.
  A **Lidarr section** lists the Lidarr library, download queue, upcoming and
  missing albums; Kodi artist pages link to their Lidarr entry.
- **Multiple Kodi instances** — add, edit, test, switch and remove them in
  Preferences, which also shows the connected Kodi's version, JSON-RPC API,
  library counts, last sync and cache size, plus Sync / Rebuild / Scan / Clean.

## How the incremental sync works

1. `AudioLibrary.GetProperties` (Kodi 19+) returns timestamps such as
   `albumslastadded` and `songsmodified`. If none changed since the last sync,
   the whole sync is **one tiny request**.
2. For each kind that did change, an **ids-only** list is fetched and diffed
   against the cache to find deletions.
3. Items added or modified since the cache's **watermark** (newest
   `dateadded`/`datemodified`, in Kodi's own clock, so time zones don't matter)
   are fetched with a date filter.
4. Anything still missing is fetched by id in **batched JSON-RPC** calls; if a
   lot is missing a paged full fetch is used instead because it's cheaper.
5. Live `AudioLibrary.OnUpdate` / `OnRemove` events update single items; scan and
   clean events trigger an incremental sync.

Older Kodi versions without these properties or filters fall back gracefully
(ids diff + batched details). *Rebuild Cache* is always available.

## Kodi settings

Settings › Services › Control:

- **Allow remote control via HTTP** (port 8080 by default; set a username and
  password).
- **Allow remote control from applications on other systems** — needed for live
  updates over TCP port 9090. Without it the app polls instead.

## Lidarr setup

Preferences › Lidarr: switch on *Use Lidarr*, enter the URL (e.g.
`http://nas:8686`, a reverse-proxy path like `https://host/lidarr` works too)
and the API key from Lidarr › Settings › General, then *Test Connection*. Pick
the default root folder, quality/metadata profile and monitor mode for new
artists. The API key is stored in the keyring when available. Newly added music
appears in Kodi after Kodi's next library scan (Preferences › Kodi › Scan).

## Running

Dependencies: Python ≥ 3.10, PyGObject, GTK ≥ 4.12, libadwaita ≥ 1.5.
Optional: libsecret (passwords go to the keyring; otherwise a 0600 config file).

```sh
# Debian/Ubuntu/Mobian
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-secret-1
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita libsecret
# postmarketOS / Alpine
sudo apk add py3-gobject3 gtk4.0 libadwaita libsecret

./run.sh                      # from the source tree
```

Install system-wide with Meson:

```sh
meson setup build --prefix=/usr && meson install -C build
meson test -C build          # validates the desktop file and AppStream data (if the tools are there)
```

### Flatpak

The manifest is `build-aux/io.github.jeweet.Tonearm.json` (GNOME 50 runtime). It builds
the source tree it sits in:

```sh
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50
flatpak-builder --user --install --force-clean build-flatpak build-aux/io.github.jeweet.Tonearm.json
flatpak run io.github.jeweet.Tonearm
```

Or as a single-file bundle to copy to another machine (a phone, say):

```sh
flatpak-builder --repo=repo --force-clean build-flatpak build-aux/io.github.jeweet.Tonearm.json
flatpak build-bundle repo tonearm.flatpak io.github.jeweet.Tonearm
flatpak install --user tonearm.flatpak
```

Permissions: network (Kodi, Lidarr), Wayland/X11 and the GPU, PulseAudio (the phone
volume-key bridge), the MPRIS name for lock-screen controls, and the Secret Service for
stored passwords. The volume-key bridge needs `pactl`, which the GNOME runtime doesn't
ship: inside the Flatpak that option stays unavailable.

For Flathub, point the module's source at a release tag instead of the local
directory, and add screenshots to the metainfo file (there's a template in it).

## Keyboard shortcuts

| Keys | Action |
|---|---|
| Ctrl+F / Ctrl+K | Focus the search bar (↓ moves into results, Enter opens the first) |
| Ctrl+Space | Play / pause |
| Ctrl+← / Ctrl+→ | Previous / next |
| Ctrl+↑ / Ctrl+↓ | Volume |
| Ctrl+P | Full player |
| Ctrl+R | Sync changes |
| Ctrl+, | Preferences |
| Ctrl+? | Keyboard shortcuts (also in the main menu) |
| Alt+← | Go back |
| Ctrl+Q | Quit |

## Layout

- **Wide (> 860sp):** sidebar with sections + sync status, content on the right.
- **Medium:** sidebar collapses into an overlay toggled from the header.
- **Phone (≤ 560sp):** bottom view switcher, mini player above it, player as a
  bottom sheet.

## Code map

| File | Role |
|---|---|
| `tonearm/kodi.py` | JSON-RPC client (keep-alive HTTP, batching) and TCP event listener |
| `tonearm/cache.py` | SQLite library cache |
| `tonearm/sync.py` | Incremental sync engine |
| `tonearm/fuzzy.py` | fzf-style matcher and index |
| `tonearm/artwork.py` | Disk + memory artwork cache |
| `tonearm/session.py` | Per-instance controller: sync, search, player state, commands |
| `tonearm/mpris.py` | MPRIS2 D-Bus server |
| `tonearm/lidarr.py` | Lidarr v1 API client |
| `tonearm/ui/` | Window, pages, player, preferences |
| `tests/` | Mock Kodi server and tests (`python3 -m unittest discover -s tests -t .`) |

A fake Kodi for UI development: `python3 -m tests.mock_kodi --port 8080 --albums 300`,
then add `127.0.0.1` as an instance (no artwork, no live events).
A fake Lidarr: `python3 -m tests.mock_lidarr --port 8686` (API key `testkey`; try
searching “boards”).

## Troubleshooting and reporting issues

Tonearm keeps a log at `~/.local/state/tonearm/tonearm.log` (in the Flatpak:
`~/.var/app/io.github.jeweet.Tonearm/.local/state/tonearm/tonearm.log`): start-up
versions, connections, library syncs, errors from Kodi or Lidarr and any crash.

**Main menu → About Tonearm → Troubleshooting → Debugging Information** shows the
versions (Tonearm, Python, GTK, libadwaita, Kodi), the environment and the end of
the log, ready to copy or save — attach it to a report at
<https://github.com/jeweet/tonearm/issues> (**Report an Issue** in the same dialog).
It holds no addresses or passwords.

For more detail run `TONEARM_DEBUG=1 ./run.sh` (or
`flatpak run --env=TONEARM_DEBUG=1 io.github.jeweet.Tonearm`): every Kodi call with
its timing, artwork loads and backdrop choices go to the log and the terminal.

## License

MIT — see [LICENSE](LICENSE).
