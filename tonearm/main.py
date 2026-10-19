"""Application entry point."""
from __future__ import annotations

import os
import signal
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .config import APP_ID, Config  # noqa: E402
from .logger import debug_report, log, setup_logging  # noqa: E402
from .session import Session  # noqa: E402

VERSION = "0.4.0"
WEBSITE = "https://github.com/jeweet/tonearm"


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name("Tonearm")
        self.config = Config()
        self.session = Session(self.config)
        self.window = None
        self.mpris = None
        self._held = False
        self._quitting = False

        # Quick actions — also the launcher's long-press menu (Phosh, GNOME): the
        # .desktop file runs "tonearm --wake" etc., which reach the running Tonearm
        self._pending = None          # one asked for at start-up, run once the window is up
        for opt, text in (("wake", "Wake up the current Kodi (Wake-on-LAN)"),
                          ("surprise", "Start the radio with a random decade and genre"),
                          ("now-playing", "Open the player")):
            self.add_main_option(opt, 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE, text, None)

        for name, cb, accels in (("quit", self._quit, ["<Ctrl>q"]),
                                 ("about", self._about, []),
                                 ("wake", lambda *_: self._quick("wake"), []),
                                 ("surprise", lambda *_: self._quick("surprise"), []),
                                 ("now-playing", lambda *_: self._quick("now-playing"), [])):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_handle_local_options(self, options):
        """``--wake`` / ``--surprise`` / ``--now-playing``: done by the Tonearm that's
        already running (this one then exits), or by this one once it's started."""
        for name in ("wake", "surprise", "now-playing"):
            if options.contains(name):
                try:
                    self.register(None)
                except GLib.Error as err:
                    log.warning("Can't register the application: %s", err)
                    return 1
                if self.get_is_remote():
                    self.activate_action(name, None)
                    return 0
                self._pending = name
                return -1               # go on starting; do_activate runs it
        return -1

    def _quick(self, name: str):
        """A quick action: show the window (that's where its result shows) and do it."""
        self.activate()
        win, s = self.window, self.session
        if win is None:
            return
        if name == "now-playing":
            win.activate_action("win.show-player", None)
        elif name == "wake":
            if s.instance is None:
                win.toast("No Kodi set up yet")
            elif s.props.connected:
                win.toast(f"{s.instance.name} is awake")
            elif s.can_wake():
                s.wake()
            else:
                win.toast("Tonearm doesn't know this Kodi's network address yet — "
                          "set it in Preferences")
        elif name == "surprise":
            self._when_connected(s.surprise_radio)

    def _when_connected(self, fn, timeout: int = 30):
        """Run ``fn`` now, or once Kodi is connected (a launch from the home screen
        happens before the connection is made)."""
        s = self.session
        if s.props.connected:
            fn()
            return
        state = {"hid": 0, "timer": 0}

        def done(*_):
            if not s.props.connected:
                return
            s.disconnect(state["hid"])
            GLib.source_remove(state["timer"])
            fn()

        def give_up():
            s.disconnect(state["hid"])
            if self.window is not None:
                self.window.toast("Kodi didn't answer — try again once it's connected")
            return GLib.SOURCE_REMOVE

        state["hid"] = s.connect("notify::connected", done)
        state["timer"] = GLib.timeout_add_seconds(timeout, give_up)

    def do_startup(self):
        Adw.Application.do_startup(self)
        if Adw.get_minor_version() < 5:
            log.error("Tonearm needs libadwaita ≥ 1.5 (this is %d.%d)",
                      Adw.get_major_version(), Adw.get_minor_version())
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(os.path.dirname(__file__), "style.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Album / artist lists and grids sit on the page, without the darker "view"
        # colour. A user theme (~/.config/gtk-4.0/gtk.css, priority USER) may paint
        # list rows and grid cells itself, so these few rules go above it.
        over = Gtk.CssProvider()
        over.load_from_path(os.path.join(os.path.dirname(__file__), "override.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), over, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        # Bundled symbolic icons (tonearm-*-symbolic), so the look doesn't depend on the icon theme.
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(
            os.path.join(os.path.dirname(__file__), "icons"))
        # The app icon for windows and the About dialog (installed builds find it in the
        # system icon theme; from the source tree it's in the bundled icons above)
        Gtk.Window.set_default_icon_name(APP_ID)

        from .mpris import MprisServer
        self.mpris = MprisServer(self, self.session, APP_ID)
        self._log_session()

    def _log_session(self):
        """What a bug report needs: connections, syncs, and what went wrong."""
        s = self.session

        was = [False]

        def connected(*_):
            inst = s.instance
            if s.props.connected:
                info = s.kodi_info or {}
                log.info("Connected to %s (%s %s, JSON-RPC %s)", inst.name if inst else "?",
                         info.get("name", "Kodi"), info.get("version", "?"), info.get("api", "?"))
            elif was[0] and inst is not None:
                # only a real loss is worth a warning — not the reset when switching
                # to a Kodi (or starting up), before the first connection
                log.warning("Lost the connection to %s", inst.name)
            was[0] = s.props.connected

        s.connect("notify::connected", connected)
        s.connect("sync-finished", lambda _s, r: log.info(
            "Library sync: %s", r.summary() if hasattr(r, "summary") else r))
        # toasts: news ("Party Mode: Whole library") — failures are logged as warnings
        # where they happen (Session._toast_problem)
        s.connect("toast", lambda _s, msg: log.info("Toast: %s", msg))
        s.connect("problem", lambda _s, msg, details: log.error("%s\n%s", msg, details))

    def do_activate(self):
        if self.window is None:
            from .ui.window import Window
            self.window = Window(self)
            if self.config.active is not None:
                self.session.activate(self.config.active)
        if self._held:
            self._held = False
            self.release()
        self.window.set_visible(True)
        self.session.set_foreground(True)
        self.window.present()
        if self._pending:             # started by a quick action ("tonearm --wake")
            name, self._pending = self._pending, None
            GLib.idle_add(lambda: (self._quick(name), False)[1])

    def enter_background(self):
        """Window closed but keep running so lock-screen/MPRIS controls work."""
        if not self._held:
            self._held = True
            self.hold()

    def _quit(self, *_):
        """Quit for real, also when running in the background (lock-screen mode)."""
        if self._quitting:
            return GLib.SOURCE_REMOVE
        self._quitting = True
        from .util import shutdown_workers
        shutdown_workers()
        self.session.shutdown()
        if self.mpris:
            self.mpris.close()
            self.mpris = None
        if self._held:
            self._held = False
            self.release()
        if self.window:
            self.window.destroy()
            self.window = None
        self.quit()
        return GLib.SOURCE_REMOVE  # also used as a unix signal handler

    def _about(self, *_):
        s = self.session
        toolkit = {"GTK": f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}."
                          f"{Gtk.get_micro_version()}",
                   "libadwaita": f"{Adw.get_major_version()}.{Adw.get_minor_version()}."
                                 f"{Adw.get_micro_version()}",
                   "PyGObject": ".".join(map(str, gi.version_info))}
        extra = {"Kodi systems": str(len(self.config.instances)),
                 "Connected": "yes" if s.props.connected else "no",
                 "Live updates": "yes" if s.props.live else "no",
                 "Lidarr": "set up" if s.lidarr is not None else "no"}
        if s.cache is not None:
            extra["Library"] = (f"{s.cache.count('album')} albums, "
                                f"{s.cache.count('artist')} artists, "
                                f"{s.cache.count('song')} songs")
        about = Adw.AboutDialog(
            application_name="Tonearm",
            application_icon=APP_ID,
            version=VERSION,
            developer_name="Tonearm contributors",
            license_type=Gtk.License.MIT_X11,
            comments="A music remote for Kodi, built for desktop and mobile Linux.",
            website=WEBSITE,
            # About → Report an Issue
            issue_url=WEBSITE + "/issues",
            # About → Troubleshooting → Debugging Information: copy or save it
            debug_info=debug_report(VERSION, toolkit, s.kodi_info if s.instance else None,
                                    extra),
            debug_info_filename="tonearm-debug.txt",
        )
        about.add_link("Kodi JSON-RPC API", "https://kodi.wiki/view/JSON-RPC_API")
        about.present(self.window)


def main(argv=None) -> int:
    log_file = setup_logging()
    log.info("Tonearm %s starting (Python %s, GTK %d.%d, libadwaita %d.%d%s)%s", VERSION,
             sys.version.split()[0], Gtk.get_major_version(), Gtk.get_minor_version(),
             Adw.get_major_version(), Adw.get_minor_version(),
             ", Flatpak" if os.environ.get("FLATPAK_ID") else "",
             f"; log: {log_file}" if log_file else "")
    app = Application()
    # Ctrl+C / SIGTERM: shut down cleanly from inside the main loop. Installing our own
    # handlers also stops PyGObject from re-raising KeyboardInterrupt after run().
    def on_signal():
        if app._quitting:  # second Ctrl+C while shutting down: leave immediately
            os._exit(130)
        app._quit()
        return GLib.SOURCE_CONTINUE

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, on_signal)
    try:
        return app.run(argv if argv is not None else sys.argv)
    except KeyboardInterrupt:  # signal arrived before the loop was running
        return 130


if __name__ == "__main__":
    sys.exit(main())
