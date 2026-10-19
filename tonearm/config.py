"""Instances and preferences, stored as JSON under $XDG_CONFIG_HOME.

Passwords go to the Secret Service (GNOME Keyring / KWallet) when libsecret is
available, otherwise into the config file which is created with mode 0600.
"""
from __future__ import annotations

import json
import os
import uuid

from .kodi import Instance
from .lidarr import LidarrSettings

APP_ID = "io.github.jeweet.Tonearm"
APP_DIR = "tonearm"


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_DIR)


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP_DIR)


def _secret():
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        attrs = {"instance": Secret.SchemaAttributeType.STRING}
        schema = Secret.Schema.new(APP_ID, Secret.SchemaFlags.NONE, attrs)
        return Secret, schema
    except (ImportError, ValueError):
        return None, None


DEFAULT_PREFS = {
    "background": True,          # keep running for lock-screen controls when window closes
    "notifications": True,       # use Kodi's TCP port for live events
    "sync_on_connect": True,
    "volume_keys": False,        # phone volume keys → Kodi volume while playing (opt-in)
    "album_sort": "title",
    "album_sort_reverse": False,  # Albums page: reverse the chosen order
    "album_view": "grid",        # grid | list — every album view
    "album_size": "medium",      # small | medium | large covers
    "album_titles": True,        # titles under the covers
    "artwork_cache_mb": 400,
}


class Config:
    def __init__(self):
        self.path = os.path.join(config_dir(), "config.json")
        self.instances: list[Instance] = []
        self.active_id: str | None = None
        self.prefs = dict(DEFAULT_PREFS)
        self.lidarr = LidarrSettings()
        self._secret, self._schema = _secret()
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        self.instances = [Instance.from_dict(d) for d in data.get("instances", [])]
        self.active_id = data.get("active")
        self.prefs.update(data.get("prefs", {}))
        for inst in self.instances:
            if not inst.password:
                inst.password = self._lookup_password(inst.id) or ""
        self.lidarr = LidarrSettings.from_dict(data.get("lidarr", {}))
        if not self.lidarr.api_key:
            self.lidarr.api_key = self._lookup_password("lidarr") or ""

    def save(self):
        os.makedirs(config_dir(), exist_ok=True)
        insts = []
        for inst in self.instances:
            d = inst.to_dict()
            if not self._store_password(inst.id, inst.password):
                d["password"] = inst.password
            insts.append(d)
        lid = self.lidarr.to_dict(with_key=not self._store_password("lidarr", self.lidarr.api_key))
        data = {"instances": insts, "active": self.active_id, "prefs": self.prefs, "lidarr": lid}
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    # -- passwords -------------------------------------------------------
    def _lookup_password(self, iid: str) -> str | None:
        if not self._secret:
            return None
        try:
            return self._secret.password_lookup_sync(self._schema, {"instance": iid}, None)
        except Exception:  # noqa: BLE001
            return None

    def _store_password(self, iid: str, password: str) -> bool:
        if not self._secret:
            return False
        try:
            if password:
                return self._secret.password_store_sync(
                    self._schema, {"instance": iid}, self._secret.COLLECTION_DEFAULT,
                    f"Tonearm ({iid[:8]})", password, None)
            self._secret.password_clear_sync(self._schema, {"instance": iid}, None)
            return True
        except Exception:
            return False

    # -- instances -------------------------------------------------------
    @property
    def active(self) -> Instance | None:
        for i in self.instances:
            if i.id == self.active_id:
                return i
        return self.instances[0] if self.instances else None

    def new_instance(self, **kw) -> Instance:
        inst = Instance(id=str(uuid.uuid4()), name=kw.pop("name", "") or kw.get("host", "Kodi"), **kw)
        self.instances.append(inst)
        if self.active_id is None:
            self.active_id = inst.id
        self.save()
        return inst

    def remove_instance(self, iid: str):
        self.instances = [i for i in self.instances if i.id != iid]
        self._store_password(iid, "")
        if self.active_id == iid:
            self.active_id = self.instances[0].id if self.instances else None
        self.save()

    def instance_cache_dir(self, iid: str) -> str:
        return os.path.join(cache_dir(), iid)
