import tempfile
import unittest
from pathlib import Path

from app import find_gamedata, likely_gamedata


class GamedataDiscoveryTests(unittest.TestCase):
    def test_discovers_nested_gamedata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "SAVEDATA" / "SAVE" / "PS3"
            root.mkdir(parents=True)
            gd = root / "GAMEDATA"
            entry = bytearray(144)
            entry[128:132] = (1).to_bytes(4, "big")
            entry[132:136] = (152).to_bytes(4, "big")
            gd.write_bytes((8).to_bytes(4, "big") + (1).to_bytes(4, "big") + entry + b"x")

            self.assertTrue(likely_gamedata(gd))
            self.assertEqual(find_gamedata(Path(tmp)), [gd.resolve()])

    def test_rejects_unrelated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "GAMEDATA"
            p.write_bytes(b"not a save")
            self.assertFalse(likely_gamedata(p))
            self.assertEqual(find_gamedata(Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()
