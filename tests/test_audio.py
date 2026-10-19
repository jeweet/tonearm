import unittest


class AudioInfoTest(unittest.TestCase):
    def test_describe(self):
        from tonearm.audioinfo import describe
        P = "MusicPlayer."

        def d(codec, rate, bits, kbps, ch="2"):
            return describe({P + "Codec": codec, P + "SampleRate": rate,
                             P + "BitsPerSample": bits, P + "BitRate": kbps, P + "Channels": ch})
        self.assertEqual(d("flac", "96", "24", "2800"), "FLAC · 24-bit / 96 kHz")
        self.assertEqual(d("mp3float", "44.1", "0", "320"), "MP3 · 320 kbps · 44.1 kHz")
        self.assertEqual(d("dsd_lsbf_planar", "2822.4", "1", ""), "DSD64 · 2.8 MHz")
        self.assertEqual(d("pcm_s16le", "44100", "16", "1411", "6"), "PCM · 16-bit / 44.1 kHz · 5.1")
        self.assertEqual(d("opus", "48", "", "128", "1"), "Opus · 128 kbps · 48 kHz · Mono")
        self.assertEqual(describe({}), "")


if __name__ == "__main__":
    unittest.main()
