"""MPRIS2 server so Kodi shows up on lock screens, Phosh's quick settings,
GNOME's media controls, KDE Connect, headset buttons and so on.

Uses GDBus directly (same approach as GNOME Music): the Properties interface
is implemented by hand, which works reliably with PyGObject.
"""
from __future__ import annotations

from gi.repository import Gio, GLib

from .session import Session

BUS_NAME = "org.mpris.MediaPlayer2.Tonearm"
OBJ_PATH = "/org/mpris/MediaPlayer2"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"

XML = """
<node>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg type="s" name="interface_name" direction="in"/>
      <arg type="s" name="property_name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="GetAll">
      <arg type="s" name="interface_name" direction="in"/>
      <arg type="a{sv}" name="properties" direction="out"/>
    </method>
    <method name="Set">
      <arg type="s" name="interface_name" direction="in"/>
      <arg type="s" name="property_name" direction="in"/>
      <arg type="v" name="value" direction="in"/>
    </method>
    <signal name="PropertiesChanged">
      <arg type="s" name="interface_name"/>
      <arg type="a{sv}" name="changed_properties"/>
      <arg type="as" name="invalidated_properties"/>
    </signal>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect">
      <arg type="s" name="xml_data" direction="out"/>
    </method>
  </interface>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""

ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
LOOP_TO_MPRIS = {"off": "None", "one": "Track", "all": "Playlist"}
LOOP_FROM_MPRIS = {v: k for k, v in LOOP_TO_MPRIS.items()}


class MprisServer:
    def __init__(self, app, session: Session, app_id: str):
        self.app = app
        self.session = session
        self.app_id = app_id
        self.con: Gio.DBusConnection | None = None
        self._reg_ids: list[int] = []
        self._owner_id = 0
        self._last_player: dict = {}
        self._art_file = ""
        self._art_for = None
        self._node = Gio.DBusNodeInfo.new_for_xml(XML)

        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired, None, None)

        session.connect("player-changed", lambda *_: self._sync_props())
        session.connect("track-changed", lambda *_: self._on_track_changed())
        session.connect("instance-changed", lambda *_: self._sync_root())
        session.connect("seeked", self._on_seeked)

    def close(self):
        if self.con:
            for rid in self._reg_ids:
                self.con.unregister_object(rid)
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)

    # -- D-Bus plumbing --------------------------------------------------
    def _on_bus_acquired(self, con, name):
        self.con = con
        for iface in self._node.interfaces:
            rid = con.register_object(OBJ_PATH, iface, self._on_method_call, None, None)
            self._reg_ids.append(rid)

    def _on_method_call(self, con, sender, path, iface, method, params, invocation):
        args = list(params.unpack())
        try:
            if iface == "org.freedesktop.DBus.Properties":
                if method == "Get":
                    props = self._props(args[0])
                    if args[1] not in props:
                        raise KeyError(args[1])
                    invocation.return_value(GLib.Variant("(v)", (props[args[1]],)))
                elif method == "GetAll":
                    invocation.return_value(GLib.Variant("(a{sv})", (self._props(args[0]),)))
                elif method == "Set":
                    self._set(args[0], args[1], args[2])
                    invocation.return_value(None)
                return
            if iface == "org.freedesktop.DBus.Introspectable":
                invocation.return_value(GLib.Variant("(s)", (XML,)))
                return
            s = self.session
            if iface == ROOT_IFACE:
                if method == "Raise":
                    self.app.activate()
                elif method == "Quit":
                    self.app.quit()
            elif iface == PLAYER_IFACE:
                if method == "Next":
                    s.next()
                elif method == "Previous":
                    s.previous()
                elif method == "Pause":
                    s.pause()
                elif method == "Play":
                    s.play()
                elif method == "PlayPause":
                    s.play_pause()
                elif method == "Stop":
                    s.stop()
                elif method == "Seek":
                    s.seek(s.state.now() + args[0] / 1e6)
                elif method == "SetPosition":
                    if args[0] == self._track_id():
                        s.seek(args[1] / 1e6)
                elif method == "OpenUri":
                    pass
            invocation.return_value(None)
        except Exception as exc:  # noqa: BLE001
            invocation.return_dbus_error("org.mpris.MediaPlayer2.Error", str(exc))

    def _set(self, iface, prop, value):
        s = self.session
        if iface != PLAYER_IFACE:
            return
        if prop == "Volume":
            s.set_volume(round(max(0.0, min(1.0, value)) * 100))
        elif prop == "Shuffle":
            s.set_shuffle(bool(value))
        elif prop == "LoopStatus":
            s.set_repeat(LOOP_FROM_MPRIS.get(value, "off"))

    # -- properties ------------------------------------------------------
    def _track_id(self) -> str:
        st = self.session.state
        if not st.active:
            return NO_TRACK
        return f"/io/github/jeweet/Tonearm/Track/{st.songid or abs(hash(st.title)) % 10_000_000}"

    def _metadata(self) -> dict:
        st = self.session.state
        if not st.active:
            return {"mpris:trackid": GLib.Variant("o", NO_TRACK)}
        md = {
            "mpris:trackid": GLib.Variant("o", self._track_id()),
            "mpris:length": GLib.Variant("x", int(st.duration * 1e6)),
            "xesam:title": GLib.Variant("s", st.title or "Unknown"),
            "xesam:album": GLib.Variant("s", st.album),
            "xesam:artist": GLib.Variant("as", [a.strip() for a in st.artist.split(",") if a.strip()]
                                         or ["Unknown"]),
        }
        if self._art_file and self._art_for == st.thumbnail:
            md["mpris:artUrl"] = GLib.Variant("s", "file://" + self._art_file)
        return md

    def _props(self, iface) -> dict:
        st = self.session.state
        if iface == ROOT_IFACE:
            name = self.session.instance.name if self.session.instance else "Kodi"
            return {
                "CanQuit": GLib.Variant("b", True),
                "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", f"Kodi · {name}"),
                "DesktopEntry": GLib.Variant("s", self.app_id),
                "SupportedUriSchemes": GLib.Variant("as", []),
                "SupportedMimeTypes": GLib.Variant("as", []),
            }
        if iface == PLAYER_IFACE:
            status = "Stopped" if not st.active else ("Playing" if st.playing else "Paused")
            can = self.session.props.connected
            return {
                "PlaybackStatus": GLib.Variant("s", status),
                "LoopStatus": GLib.Variant("s", LOOP_TO_MPRIS.get(st.repeat, "None")),
                "Rate": GLib.Variant("d", 1.0),
                "Shuffle": GLib.Variant("b", st.shuffle),
                "Metadata": GLib.Variant("a{sv}", self._metadata()),
                "Volume": GLib.Variant("d", st.volume / 100.0),
                "Position": GLib.Variant("x", int(st.now() * 1e6)),
                "MinimumRate": GLib.Variant("d", 1.0),
                "MaximumRate": GLib.Variant("d", 1.0),
                "CanGoNext": GLib.Variant("b", can and st.active),
                "CanGoPrevious": GLib.Variant("b", can and st.active),
                "CanPlay": GLib.Variant("b", can),
                "CanPause": GLib.Variant("b", can and st.active),
                "CanSeek": GLib.Variant("b", can and st.active and st.duration > 0),
                "CanControl": GLib.Variant("b", True),
            }
        return {}

    def _emit_changed(self, iface, changed: dict):
        if not self.con or not changed:
            return
        self.con.emit_signal(
            None, OBJ_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (iface, changed, [])))

    def _sync_props(self):
        props = self._props(PLAYER_IFACE)
        props.pop("Position", None)  # position is polled by clients, never signalled
        snapshot = {k: v.print_(False) for k, v in props.items()}
        changed = {k: props[k] for k, v in snapshot.items() if self._last_player.get(k) != v}
        self._last_player = snapshot
        self._emit_changed(PLAYER_IFACE, changed)

    def _sync_root(self):
        self._emit_changed(ROOT_IFACE, {"Identity": self._props(ROOT_IFACE)["Identity"]})
        self._sync_props()

    def _on_seeked(self, _session, seconds):
        if self.con:
            self.con.emit_signal(None, OBJ_PATH, PLAYER_IFACE, "Seeked",
                                 GLib.Variant("(x)", (int(seconds * 1e6),)))

    def _on_track_changed(self):
        st = self.session.state
        art = self.session.artwork
        thumb = st.thumbnail
        if not thumb or art is None:
            self._art_file, self._art_for = "", None
            self._sync_props()
            return
        cached = art.cached_file(thumb, "large")
        if cached:
            self._art_file, self._art_for = cached, thumb
            self._sync_props()
            return
        # Lock screens need a local file: fetch it, then update metadata.
        self._sync_props()

        def got(_tex):
            path = art.cached_file(thumb, "large")
            if path and self.session.state.thumbnail == thumb:
                self._art_file, self._art_for = path, thumb
                self._sync_props()

        art.load(thumb, "large", got)
