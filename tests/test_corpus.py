"""The Gmail stand-in's corpus is a pure function of its seed: same bytes on every run and every machine, all nine
malformed kinds present, synthetic names only."""
import collections
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gmail"))
import corpus  # noqa: E402

# sha256 over (8-byte little-endian length, raw message) of generate_gmail(), in order.
DIGEST = "8bd763de842cd6a4f23e5f634e27b76fa37a6af2db719ec539d0a73fb46a8178"


class Corpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = list(corpus.generate_gmail())

    def test_bytes_are_pinned(self):
        h = hashlib.sha256()
        for item in self.items:
            raw = item[2]
            h.update(len(raw).to_bytes(8, "little"))
            h.update(raw)
        self.assertEqual((len(self.items), h.hexdigest()), (5000, DIGEST))

    def test_every_malformed_kind_occurs(self):
        kinds = collections.Counter(item[3]["malformed"] for item in self.items if item[3]["malformed"])
        self.assertEqual(set(kinds), set(corpus.MALFORMED_KINDS))

    def test_labels_are_the_modelled_ones(self):
        labels = {label for item in self.items for label in item[4]}
        self.assertEqual(labels, set(corpus.GMAIL_LABEL_FOLDERS))

    def test_names_are_synthetic(self):
        # Every English surname is invented (no real or fictional public figure); all names are distinct.
        names = corpus.NAMES["en"]
        self.assertEqual(len(names), len(set(names)))
        blob = b"".join(item[2] for item in self.items[:500])
        for real in (b"Roberts", b"Scully", b"Palmer", b"Walker", b"Gallagher"):
            self.assertNotIn(real, blob)


if __name__ == "__main__":
    unittest.main()
