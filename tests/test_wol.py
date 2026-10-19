import socket
import unittest

from tonearm import wol
from tonearm.kodi import Instance


class WolTest(unittest.TestCase):
    def test_normalize(self):
        for text in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff",
                     "aabbccddeeff", " aa:bb:cc:dd:ee:ff "):
            self.assertEqual(wol.normalize_mac(text), "AA:BB:CC:DD:EE:FF")
        for bad in ("", None, "00:00:00:00:00:00", "Busy", "aa:bb:cc:dd:ee",
                    "aa:bb:cc:dd:ee:fg", "ff:ff:ff:ff:ff:ff", "aa:bb:cc:dd:ee:ff:00"):
            self.assertIsNone(wol.normalize_mac(bad), bad)

    def test_packet(self):
        p = wol.magic_packet("01:23:45:67:89:ab")
        self.assertEqual(len(p), 102)
        self.assertEqual(p[:6], b"\xff" * 6)
        self.assertEqual(p[6:12], bytes.fromhex("0123456789ab"))
        self.assertEqual(p[-6:], bytes.fromhex("0123456789ab"))
        with self.assertRaises(ValueError):
            wol.magic_packet("nope")

    def test_targets(self):
        self.assertEqual(wol.targets("192.168.1.20"), ["255.255.255.255", "192.168.1.255"])
        self.assertEqual(wol.targets("kodi.local"), ["255.255.255.255"])
        self.assertEqual(wol.targets("8.8.8.8"), ["255.255.255.255"])

    def test_wake_sends(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        port = rx.getsockname()[1]
        orig = wol.targets
        wol.targets = lambda host: ["127.0.0.1"]
        try:
            n = wol.wake("aa:bb:cc:dd:ee:ff", "x", ports=(port,))
        finally:
            wol.targets = orig
        self.assertEqual(n, 3)
        data, _ = rx.recvfrom(200)
        self.assertEqual(data, wol.magic_packet("aa:bb:cc:dd:ee:ff"))
        rx.close()

    def test_instance_roundtrip(self):
        inst = Instance(id="1", name="K", host="h", mac="AA:BB:CC:DD:EE:FF")
        self.assertEqual(Instance.from_dict(inst.to_dict()).mac, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(Instance.from_dict({"id": "2", "host": "h"}).mac, "")


if __name__ == "__main__":
    unittest.main()
