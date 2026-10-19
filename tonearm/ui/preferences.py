"""Preferences: Kodi instances, connected Kodi details, library/cache tools."""
from __future__ import annotations

import datetime as _dt

from gi.repository import Adw, GLib, Gtk

from .. import volumebridge
from ..kodi import Instance, KodiClient
from ..lidarr import MONITOR_OPTIONS, LidarrClient
from ..util import human_bytes, run_async
from ..wol import normalize_mac
from .widgets import Spinner, plain_row


def _port(text: str, default: int) -> int:
    """A port number from a text field (anything else: ``default``)."""
    try:
        n = int(text.strip())
    except ValueError:
        return default
    return n if 0 < n < 65536 else default


class InstanceForm(Gtk.Box):
    """Fields for one Kodi instance, with a connection test."""

    def __init__(self, inst: Instance | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        conn = Adw.PreferencesGroup(
            title="Connection",
            description="In Kodi enable Settings › Services › Control › "
                        "“Allow remote control via HTTP”, and “…from applications on other "
                        "systems” for live updates.")
        self.name = Adw.EntryRow(title="Name")
        self.host = Adw.EntryRow(title="Host or IP address")
        # ports as plain number fields: a spin row (field + − + buttons) beside a long
        # title made the form wider than a 360 px phone
        self.port = Adw.EntryRow(title="HTTP port", input_purpose=Gtk.InputPurpose.DIGITS)
        self.tcp_port = Adw.EntryRow(title="Notification (TCP) port",
                                     input_purpose=Gtk.InputPurpose.DIGITS)
        self.https = Adw.SwitchRow(title="Use HTTPS")
        for r in (self.name, self.host, self.port, self.tcp_port, self.https):
            conn.add(r)

        auth = Adw.PreferencesGroup(title="Authentication")
        self.user = Adw.EntryRow(title="Username")
        self.password = Adw.PasswordEntryRow(title="Password")
        auth.add(self.user)
        auth.add(self.password)

        wake = Adw.PreferencesGroup(
            title="Wake-on-LAN",
            description="Wakes the Kodi machine when it's offline. Filled in by itself once "
                        "Tonearm has connected; the machine needs Wake-on-LAN switched on.")
        self.mac = Adw.EntryRow(title="MAC address")
        wake.add(self.mac)

        test = Adw.PreferencesGroup()
        self.test_row = plain_row(title="Test Connection", activatable=True,
                                      subtitle="Check that Kodi answers")
        self.test_spinner = Spinner(visible=False)
        self.test_row.add_suffix(self.test_spinner)
        self.test_row.add_suffix(Gtk.Image(icon_name="tonearm-go-next-symbolic"))
        self.test_row.connect("activated", lambda *_: self.test())
        test.add(self.test_row)

        for g in (conn, auth, wake, test):
            self.append(g)

        inst = inst or Instance(id="", name="", host="")
        self.name.set_text(inst.name)
        self.host.set_text(inst.host)
        self.port.set_text(str(inst.port))
        self.tcp_port.set_text(str(inst.tcp_port))
        self.https.set_active(inst.https)
        self.user.set_text(inst.username)
        self.password.set_text(inst.password)
        self.mac.set_text(inst.mac)

    def values(self) -> dict:
        host = self.host.get_text().strip()
        for prefix in ("http://", "https://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        host = host.rstrip("/")
        return {
            "name": self.name.get_text().strip() or host,
            "host": host,
            "port": _port(self.port.get_text(), 8080),
            "tcp_port": _port(self.tcp_port.get_text(), 9090),
            "https": self.https.get_active(),
            "username": self.user.get_text(),
            "password": self.password.get_text(),
            "mac": normalize_mac(self.mac.get_text()) or "",
        }

    def valid(self) -> bool:
        ok = bool(self.values()["host"])
        (self.host.remove_css_class if ok else self.host.add_css_class)("error")
        for row in (self.port, self.tcp_port):     # a typo'd port shows, not silently 8080
            text = row.get_text().strip()
            bad = bool(text) and _port(text, 0) == 0
            (row.add_css_class if bad else row.remove_css_class)("error")
            ok = ok and not bad
        text = self.mac.get_text().strip()
        bad = bool(text) and normalize_mac(text) is None
        (self.mac.add_css_class if bad else self.mac.remove_css_class)("error")
        return ok and not bad

    def test(self, on_ok=None):
        if not self.valid():
            return
        v = self.values()
        client = KodiClient(Instance(id="test", **v), timeout=6)
        self.test_spinner.set_visible(True)
        self.test_spinner.start()
        self.test_row.set_subtitle("Connecting…")

        def work():
            info = client.info()
            try:                       # the MAC address too, for Wake-on-LAN
                info["mac"] = client.mac_address()
            except Exception:          # noqa: BLE001  (old Kodi, busy label…)
                info["mac"] = ""
            return info

        def done(info):
            if info.get("mac") and not self.mac.get_text().strip():
                self.mac.set_text(info["mac"])
            self.test_spinner.stop()
            self.test_spinner.set_visible(False)
            self.test_row.set_subtitle(f"✓ {info['name']} {info['version']} · JSON-RPC {info['api']}")
            if not self.name.get_text().strip():
                self.name.set_text(v["host"])
            if on_ok:
                on_ok(info)

        def failed(exc):
            self.test_spinner.stop()
            self.test_spinner.set_visible(False)
            self.test_row.set_subtitle(f"✗ {exc}")

        run_async(work, done=done, error=failed)


class KodiAudioGroup(Adw.PreferencesGroup):
    """Kodi's own audio settings — output device, passthrough, crossfade, ReplayGain —
    read from and written to Kodi (only while connected; the options are Kodi's)."""

    TITLES = {"audiooutput.audiodevice": ("Audio Output", None),
              "audiooutput.passthrough": ("Passthrough",
                                          "Send surround and HD audio to the amplifier as is"),
              "musicplayer.crossfade": ("Crossfade", "Seconds of overlap between songs; 0 is off"),
              "musicplayer.crossfadealbumtracks": ("Crossfade Album Tracks",
                                                   "Also between songs of the same album"),
              "musicplayer.replaygaintype": ("ReplayGain",
                                             "Even out loudness between songs or albums")}
    ORDER = list(TITLES)

    def __init__(self, session):
        super().__init__(title="Kodi Audio",
                         description="Kodi's own settings, changed on Kodi itself")
        self.session = session
        self._rows: list = []
        self._timers: dict = {}
        # while loading / offline / failed: one row saying so
        self.note = plain_row(title="Loading…")
        self.note.add_css_class("dim-label")
        self.add(self.note)

    def load(self):
        self._clear()
        self.note.set_visible(True)
        if not self.session.props.connected:
            self.note.set_title("Available while connected to Kodi")
            return
        self.note.set_title("Loading…")
        self.session.fetch_audio_settings(self._fill)

    def _clear(self):
        for row in self._rows:
            self.remove(row)
        self._rows = []

    def _fill(self, settings: dict, error: str = ""):
        self._clear()
        for sid in self.ORDER:
            st = settings.get(sid)
            row = self._row(sid, st) if st else None
            if row is not None:
                self.add(row)
                self._rows.append(row)
        if self._rows:
            self.note.set_visible(False)
        elif error == "offline":
            self.note.set_title("Available while connected to Kodi")
        else:
            self.note.set_title("Kodi didn't share its audio settings"
                                + (f" ({error})" if error else ""))
        self._sync_sensitive()

    def _row(self, sid: str, st: dict):
        title, subtitle = self.TITLES.get(sid, (st.get("label", sid), None))
        value = st.get("value")
        options = st.get("options")
        if isinstance(options, list) and options:
            labels = [str(o.get("label", o.get("value"))) for o in options]
            values = [o.get("value") for o in options]
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
            if subtitle:
                row.set_subtitle(subtitle)
            if value in values:
                row.set_selected(values.index(value))
            row.connect("notify::selected", lambda r, _p: self._set(
                sid, values[r.get_selected()]) if r.get_selected() < len(values) else None)
            return row
        kind = st.get("type")
        if kind == "boolean":
            row = Adw.SwitchRow(title=title, active=bool(value))
            if subtitle:
                row.set_subtitle(subtitle)
            row.connect("notify::active", lambda r, _p: self._set(sid, r.get_active()))
            return row
        if kind == "integer":
            lo, hi = int(st.get("minimum", 0)), int(st.get("maximum", 15))
            row = Adw.SpinRow.new_with_range(lo, hi, max(1, int(st.get("step", 1))))
            row.set_title(title)
            if subtitle:
                row.set_subtitle(subtitle)
            row.set_value(int(value or 0))
            row.connect("notify::value", lambda r, _p: self._set_later(sid, int(r.get_value())))
            return row
        return None

    def _set(self, sid, value):
        self.session.set_kodi_setting(sid, value)
        self._sync_sensitive()

    def _set_later(self, sid, value):
        """Spin rows: one call when the clicking stops, not one per step."""
        if sid in self._timers:
            GLib.source_remove(self._timers.pop(sid))

        def go():
            self._timers.pop(sid, None)
            self._set(sid, value)
            return GLib.SOURCE_REMOVE

        self._timers[sid] = GLib.timeout_add(600, go)
        self._sync_sensitive()

    def _sync_sensitive(self):
        """"Crossfade album tracks" only means something with crossfade on."""
        fade = next((r for r in self._rows if r.get_title() == "Crossfade"), None)
        album = next((r for r in self._rows if r.get_title() == "Crossfade Album Tracks"), None)
        if fade is not None and album is not None:
            album.set_sensitive(fade.get_value() > 0)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, window):
        super().__init__(search_enabled=False)
        self.window = window
        self.session = window.session
        self.config = window.config
        self._handlers = []

        # -- Kodi page ----------------------------------------------------
        kodi = Adw.PreferencesPage(title="Kodi", icon_name="tonearm-server-symbolic", name="kodi")
        self.instances = Adw.PreferencesGroup(title="Instances",
                                              description="Switch between Kodi systems at any time; "
                                                          "each keeps its own library cache.")
        add = Gtk.Button(icon_name="tonearm-add-symbolic", tooltip_text="Add Instance",
                         valign=Gtk.Align.CENTER)
        add.add_css_class("flat")
        add.connect("clicked", lambda *_: self.edit_instance(None))
        self.instances.set_header_suffix(add)
        kodi.add(self.instances)

        self.details = Adw.PreferencesGroup(title="Connected Kodi")
        self.rows = {}
        for key, title in (("status", "Status"), ("version", "Kodi Version"), ("api", "JSON-RPC API"),
                           ("address", "Address"), ("library", "Library"),
                           ("synced", "Last Sync"), ("cache", "Cache on This Device")):
            row = plain_row(title=title)
            row.add_css_class("property")
            self.details.add(row)
            self.rows[key] = row
        self.wake_btn = Gtk.Button(label="Wake Up", valign=Gtk.Align.CENTER, visible=False)
        self.wake_btn.connect("clicked", lambda *_: self.session.wake())
        self.rows["status"].add_suffix(self.wake_btn)
        kodi.add(self.details)
        self.kodi_audio = KodiAudioGroup(self.session)
        kodi.add(self.kodi_audio)
        self.kodi_audio.load()
        self._handlers.append(self.session.connect(
            "notify::connected", lambda *_: self.kodi_audio.load()))

        lib = Adw.PreferencesGroup(title="Library")
        self.sync_row = self._button_row(lib, "Sync Changes", "Fetch only what changed on Kodi",
                                         lambda: self.session.sync(force=True))
        self._button_row(lib, "Rebuild Cache", "Download the whole library again",
                         lambda: self.session.sync(full=True))
        self._button_row(lib, "Scan Library on Kodi", "Ask Kodi to look for new music",
                         self.session.scan_library)
        self._button_row(lib, "Clean Library on Kodi", "Ask Kodi to remove missing files",
                         self.session.clean_library)
        self._button_row(lib, "Clear Artwork Cache", "Artwork is downloaded again when needed",
                         self._clear_art)
        kodi.add(lib)
        self.add(kodi)

        # -- General page -------------------------------------------------
        self.lidarr_page = LidarrPrefsPage(self)
        self.add(self.lidarr_page)

        general = Adw.PreferencesPage(title="General", icon_name="tonearm-settings-symbolic",
                                      name="general")
        g = Adw.PreferencesGroup(title="Behaviour")
        self._switch(g, "background", "Keep Running in Background",
                     "Lock-screen and media-key controls keep working when the window is closed")
        self._switch(g, "notifications", "Live Updates",
                     "Use Kodi's TCP port for instant player and library events")
        self._switch(g, "sync_on_connect", "Sync on Connect",
                     "Check for library changes whenever a connection is made")
        general.add(g)

        vk = Adw.PreferencesGroup(title="Volume Keys")
        row = self._switch(
            vk, "volume_keys", "Volume Keys Control Kodi While Playing",
            "The system volume — volume keys and the volume slider — sets Kodi's volume "
            "while something plays. Other sounds on this device follow that level meanwhile.")
        if not volumebridge.available():
            row.set_sensitive(False)
            row.set_subtitle("Needs pactl (PipeWire or PulseAudio utilities) to be installed")
        general.add(vk)
        c = Adw.PreferencesGroup(title="Storage")
        spin = Adw.SpinRow.new_with_range(50, 5000, 50)
        spin.set_title("Artwork Cache Limit")
        spin.set_subtitle("Megabytes kept on this device")
        spin.set_value(self.config.prefs.get("artwork_cache_mb", 400))
        spin.connect("notify::value", lambda r, _p: self._set_pref("artwork_cache_mb", int(r.get_value())))
        c.add(spin)
        general.add(c)
        self.add(general)

        for sig in ("instance-changed", "sync-finished", "library-changed"):
            self._handlers.append(self.session.connect(sig, lambda *_: self.refresh()))
        self._handlers.append(self.session.connect("notify::syncing", lambda *_: self.refresh()))
        self._handlers.append(self.session.connect("notify::live", lambda *_: self.refresh()))
        self._handlers.append(self.session.connect("notify::offline", lambda *_: self.refresh()))
        self.connect("closed", self._on_closed)
        self.refresh()

    def _on_closed(self, *_):
        for h in self._handlers:
            self.session.disconnect(h)
        self._handlers = []

    def _button_row(self, group, title, subtitle, cb):
        row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True)
        row.add_suffix(Gtk.Image(icon_name="tonearm-go-next-symbolic"))
        row.connect("activated", lambda *_: cb())
        group.add(row)
        return row

    def _switch(self, group, key, title, subtitle):
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.config.prefs.get(key)))
        row.connect("notify::active", lambda r, _p: self._set_pref(key, r.get_active()))
        group.add(row)
        return row

    def _set_pref(self, key, value):
        self.config.prefs[key] = value
        self.config.save()
        if key == "notifications":
            self.session.reconnect()
        elif key == "volume_keys":
            self.session.refresh_volume_bridge()

    def _clear_art(self):
        art = self.session.artwork
        if art:
            art.clear()
            self.add_toast(Adw.Toast(title="Artwork cache cleared"))
            self.refresh()

    # -- refresh ---------------------------------------------------------
    def refresh(self):
        self._fill_instances()
        s = self.session
        inst = s.instance
        self.details.set_visible(inst is not None)
        if inst is None:
            return
        if s.props.connected:
            status = "Connected" + (" · live updates" if s.props.live else " · polling")
        else:
            status = "Offline — browsing the cached library"
        if s.props.syncing:
            status += " · syncing…"
        info = s.kodi_info or {}
        self.rows["status"].set_subtitle(status)
        self.wake_btn.set_visible(bool(s.props.offline) and s.can_wake())
        self.rows["version"].set_subtitle(f"{info.get('name', 'Kodi')} {info.get('version', '—')}")
        self.rows["api"].set_subtitle(info.get("api", "—"))
        self.rows["address"].set_subtitle(f"{inst.base_url} · TCP {inst.tcp_port}")
        if s.cache:
            c = s.cache
            self.rows["library"].set_subtitle(
                f"{c.count('album')} albums · {c.count('artist')} artists · {c.count('song')} songs")
            ts = c.get_meta("synced")
            when = _dt.datetime.fromtimestamp(int(ts)).strftime("%c") if ts else "Never"
            summary = c.get_meta_json("last_sync_summary") or {}
            if summary:
                a, r = summary.get("added", {}), summary.get("removed", {})
                when += (" · full" if summary.get("full") else
                         f" · +{a.get('album', 0)} / −{r.get('album', 0)} albums")
            self.rows["synced"].set_subtitle(when)

            cache, art = s.cache, s.artwork
            run_async(lambda: (cache.size_bytes(), art.size_bytes() if art else 0),
                      done=lambda sizes: self.rows["cache"].set_subtitle(
                          f"Library {human_bytes(sizes[0])} · Artwork {human_bytes(sizes[1])}"))

    def _fill_instances(self):
        if not hasattr(self, "_inst_rows"):
            self._inst_rows = []
        for r in self._inst_rows:
            self.instances.remove(r)
        self._inst_rows = []
        group = None
        active = self.config.active
        for inst in self.config.instances:
            row = plain_row(title=inst.name, subtitle=f"{inst.host}:{inst.port}",
                                use_markup=False, activatable=True)
            check = Gtk.CheckButton(active=active is not None and inst.id == active.id,
                                    valign=Gtk.Align.CENTER)
            if group is None:
                group = check
            else:
                check.set_group(group)
            check.connect("toggled", self._on_choose, inst.id)
            row.add_prefix(check)
            row.set_activatable_widget(check)
            edit = Gtk.Button(icon_name="tonearm-edit-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Edit")
            edit.add_css_class("flat")
            edit.connect("clicked", lambda b, i=inst: self.edit_instance(i))
            row.add_suffix(edit)
            self.instances.add(row)
            self._inst_rows.append(row)
        if not self.config.instances:
            row = Adw.ActionRow(title="No Kodi instances yet",
                                subtitle="Add one with the + button")
            self.instances.add(row)
            self._inst_rows.append(row)

    def _on_choose(self, check, iid):
        if check.get_active() and iid != self.config.active_id:
            GLib.idle_add(lambda: (self.window.switch_instance(iid), False)[1])

    # -- add / edit ------------------------------------------------------
    def edit_instance(self, inst: Instance | None):
        page = Adw.NavigationPage(title="Edit Instance" if inst else "Add Instance")
        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        header.pack_end(save)
        tv.add_top_bar(header)
        form = InstanceForm(inst)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                          margin_top=24, margin_bottom=24, margin_start=12, margin_end=12)
        content.append(form)
        if inst is not None:
            del_group = Adw.PreferencesGroup()
            del_btn = Gtk.Button(label="Remove Instance", halign=Gtk.Align.CENTER)
            del_btn.add_css_class("destructive-action")
            del_btn.add_css_class("pill")
            del_btn.connect("clicked", lambda *_: self._remove(inst))
            del_group.add(del_btn)
            content.append(del_group)
        tv.set_content(Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=600, child=content),
                                          hscrollbar_policy=Gtk.PolicyType.NEVER))
        page.set_child(tv)

        def on_save(*_):
            if not form.valid():
                return
            v = form.values()
            if inst is None:
                new = self.config.new_instance(**v)
                self.window.switch_instance(new.id)
            else:
                for k, val in v.items():
                    setattr(inst, k, val)
                self.config.save()
                if self.session.instance and self.session.instance.id == inst.id:
                    self.window.switch_instance(inst.id, force=True)
            self.window.rebuild_instance_menu()
            self.pop_subpage()
            self.refresh()

        save.connect("clicked", on_save)
        self.push_subpage(page)

    def _remove(self, inst: Instance):
        dlg = Adw.AlertDialog(heading=f"Remove “{inst.name}”?",
                              body="Its cached library and artwork stay on disk until you "
                                   "clear the cache.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Remove")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_d, resp):
            if resp != "remove":
                return
            was_active = self.session.instance and self.session.instance.id == inst.id
            self.config.remove_instance(inst.id)
            if was_active:
                self.window.switch_instance(self.config.active_id)
            self.window.rebuild_instance_menu()
            self.pop_subpage()
            self.refresh()

        dlg.connect("response", on_response)
        dlg.present(self)


class LidarrPrefsPage(Adw.PreferencesPage):
    """Lidarr connection and defaults for adding artists."""

    def __init__(self, dialog: PreferencesDialog):
        super().__init__(title="Lidarr", icon_name="tonearm-lidarr-symbolic", name="lidarr")
        self.dialog = dialog
        self.session = dialog.session
        self.config = dialog.config
        self.st = self.config.lidarr
        self._apply_id = 0
        self._loading = False

        conn = Adw.PreferencesGroup(
            title="Connection",
            description="Search shows artists that aren't in Kodi yet, and lets you add them to "
                        "Lidarr. Find the API key in Lidarr › Settings › General.")
        self.enabled = Adw.SwitchRow(title="Use Lidarr", active=self.st.enabled)
        self.url = Adw.EntryRow(title="URL (e.g. http://nas:8686)")
        self.url.set_text(self.st.url)
        self.key = Adw.PasswordEntryRow(title="API key")
        self.key.set_text(self.st.api_key)
        self.test_row = plain_row(title="Test Connection", activatable=True,
                                      subtitle="Check that Lidarr answers")
        self.test_spinner = Spinner(visible=False)
        self.test_row.add_suffix(self.test_spinner)
        self.test_row.add_suffix(Gtk.Image(icon_name="tonearm-go-next-symbolic"))
        self.test_row.connect("activated", lambda *_: self.test())
        for r in (self.enabled, self.url, self.key, self.test_row):
            conn.add(r)
        self.add(conn)

        defaults = Adw.PreferencesGroup(title="When Adding Artists",
                                        description="Defaults; the add page lets you change them per artist.")
        self.root = self._combo("Root folder")
        self.quality = self._combo("Quality profile")
        self.metadata = self._combo("Metadata profile")
        self.monitor = self._combo("Monitor")
        self._set_options(self.monitor, MONITOR_OPTIONS, self.st.monitor)
        self.search = Adw.SwitchRow(title="Search for missing albums right away",
                                    active=self.st.search_on_add)
        for r in (self.root, self.quality, self.metadata, self.monitor, self.search):
            defaults.add(r)
        self.add(defaults)
        self.defaults = defaults

        # placeholders until profiles load from Lidarr
        self._set_options(self.root, [(self.st.root_folder, self.st.root_folder or "—")], self.st.root_folder)
        self._set_options(self.quality, [(self.st.quality_profile, "—")], self.st.quality_profile)
        self._set_options(self.metadata, [(self.st.metadata_profile, "—")], self.st.metadata_profile)

        self.enabled.connect("notify::active", lambda *_: self._changed(reconnect=True))
        self.url.connect("changed", lambda *_: self._changed(reconnect=True))
        self.key.connect("changed", lambda *_: self._changed(reconnect=True))
        for r in (self.root, self.quality, self.metadata, self.monitor):
            r.connect("notify::selected", lambda *_: self._changed())
        self.search.connect("notify::active", lambda *_: self._changed())
        self._sync_sensitivity()
        if self.st.enabled and self.st.url and self.st.api_key:
            self.test(quiet=True)

    def _combo(self, title):
        row = Adw.ComboRow(title=title)
        row.values = []
        return row

    def _set_options(self, row, options, current):
        self._loading = True
        row.set_model(Gtk.StringList.new([str(lbl) for _, lbl in options]))
        row.values = [v for v, _ in options]
        if current in row.values:
            row.set_selected(row.values.index(current))
        elif row.values:
            row.set_selected(0)
        self._loading = False

    def _value(self, row, default):
        i = row.get_selected()
        if row.values and 0 <= i < len(row.values):
            return row.values[i]
        return default

    def _sync_sensitivity(self):
        on = self.enabled.get_active()
        for r in (self.url, self.key, self.test_row):
            r.set_sensitive(on)
        self.defaults.set_sensitive(on)

    def _changed(self, reconnect=False):
        if self._loading:
            return
        st = self.st
        st.enabled = self.enabled.get_active()
        st.url = self.url.get_text().strip()
        st.api_key = self.key.get_text().strip()
        st.root_folder = self._value(self.root, st.root_folder) or ""
        st.quality_profile = int(self._value(self.quality, st.quality_profile) or 0)
        st.metadata_profile = int(self._value(self.metadata, st.metadata_profile) or 0)
        st.monitor = self._value(self.monitor, st.monitor)
        st.search_on_add = self.search.get_active()
        self._sync_sensitivity()
        if self._apply_id:
            GLib.source_remove(self._apply_id)
        self._apply_id = GLib.timeout_add(700, self._apply, reconnect)

    def _apply(self, reconnect):
        self._apply_id = 0
        self.config.save()
        if reconnect:
            self.session.configure_lidarr()
        return GLib.SOURCE_REMOVE

    def test(self, quiet=False):
        url, key = self.url.get_text().strip(), self.key.get_text().strip()
        if not url or not key:
            self.test_row.set_subtitle("Enter the URL and API key first")
            return
        client = LidarrClient(url, key, timeout=8)
        self.test_spinner.set_visible(True)
        self.test_spinner.start()
        if not quiet:
            self.test_row.set_subtitle("Connecting…")

        def work():
            status = client.status()
            return status, client.root_folders(), client.quality_profiles(), client.metadata_profiles()

        def done(res):
            status, roots, qps, mps = res
            self.test_spinner.stop()
            self.test_spinner.set_visible(False)
            self.test_row.set_subtitle(f"✓ {status.get('instanceName') or 'Lidarr'} "
                                       f"{status.get('version', '')}")
            st = self.st
            self._set_options(self.root, [(r["path"], r["path"]) for r in roots] or [("", "—")],
                              st.root_folder)
            self._set_options(self.quality, [(p["id"], p["name"]) for p in qps] or [(0, "—")],
                              st.quality_profile)
            self._set_options(self.metadata, [(p["id"], p["name"]) for p in mps] or [(0, "—")],
                              st.metadata_profile)
            self._changed(reconnect=not quiet)

        def failed(exc):
            self.test_spinner.stop()
            self.test_spinner.set_visible(False)
            self.test_row.set_subtitle(f"✗ {exc}")

        run_async(work, done=done, error=failed)
