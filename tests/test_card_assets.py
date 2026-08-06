"""Card asset safety: player photos and upload size.

Two failure modes these cover, both of which reach the public timeline:

  * the wrong person's face on a verified news card, because a Wikipedia
    search for an obscure player returned a politician or a different
    footballer with a similar name;
  * a card that renders perfectly and then fails to upload because the 4K
    PNG is over X's 5 MB limit — after the story was marked as posted.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from src.renderer import (  # noqa: E402
    MAX_UPLOAD_BYTES, _ensure_upload_safe, _name_tokens, _strip_accents,
    _wiki_page_is_player,
)


def summary(title, description="", extract=""):
    return {"title": title, "description": description, "extract": extract}


class NameNormalisation(unittest.TestCase):
    def test_accents_are_stripped_for_comparison(self):
        self.assertEqual(_strip_accents("Veljko Milosavljević"),
                         "Veljko Milosavljevic")
        self.assertEqual(_strip_accents("Nicolás Otamendi"), "Nicolas Otamendi")

    def test_parenthetical_qualifiers_are_ignored(self):
        self.assertNotIn("footballer",
                         _name_tokens("Joe Gomez (footballer, born 1997)"))
        self.assertIn("gomez", _name_tokens("Joe Gomez (footballer, born 1997)"))


class WikipediaIdentityGuard(unittest.TestCase):
    """A photo is only used when the article is that footballer."""

    def test_accepts_the_right_player_despite_diacritics(self):
        page = summary("Veljko Milosavljević", "Serbian footballer (born 2006)",
                       "is a Serbian footballer who plays as a defender")
        self.assertTrue(_wiki_page_is_player(page, "Veljko Milosavljevic"))

    def test_accepts_a_disambiguated_title(self):
        page = summary("Joe Gomez (footballer, born 1997)",
                       "English footballer", "English footballer")
        self.assertTrue(_wiki_page_is_player(page, "Joe Gomez"))

    def test_rejects_a_non_footballer_with_the_same_name(self):
        page = summary("Veljko Milosavljević", "Serbian politician",
                       "is a Serbian politician and lawyer")
        self.assertFalse(_wiki_page_is_player(page, "Veljko Milosavljevic"))

    def test_rejects_a_different_footballer(self):
        page = summary("Luka Modrić", "Croatian footballer",
                       "is a Croatian footballer")
        self.assertFalse(_wiki_page_is_player(page, "Veljko Milosavljevic"))

    def test_rejects_a_place_or_club_article(self):
        page = summary("Bournemouth", "town in Dorset",
                       "is a coastal resort town; it has a football club")
        self.assertFalse(_wiki_page_is_player(page, "Veljko Milosavljevic"))

    def test_rejects_an_empty_page(self):
        self.assertFalse(_wiki_page_is_player({}, "Veljko Milosavljevic"))
        self.assertFalse(_wiki_page_is_player(summary("X", "footballer"), ""))


class UploadSizeGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).resolve().parent / "_tmp_card.png"

    def tearDown(self):
        if self.tmp.exists():
            self.tmp.unlink()

    def _write(self, width, height, noisy=True):
        """Noise defeats PNG compression, standing in for a real 4K card."""
        import random
        rng = random.Random(7)
        image = Image.new("RGB", (width, height))
        if noisy:
            image.putdata([(rng.randrange(256), rng.randrange(256),
                            rng.randrange(256))
                           for _ in range(width * height)])
        image.save(self.tmp, format="PNG")
        return self.tmp.stat().st_size

    def test_an_oversized_card_is_brought_under_the_limit(self):
        before = self._write(3840, 2160)
        self.assertGreater(before, MAX_UPLOAD_BYTES,
                           "fixture is not big enough to exercise the guard")
        _ensure_upload_safe(self.tmp)
        self.assertLessEqual(self.tmp.stat().st_size, MAX_UPLOAD_BYTES)

    def test_it_stays_a_valid_image_after_downscaling(self):
        self._write(3840, 2160)
        _ensure_upload_safe(self.tmp)
        with Image.open(self.tmp) as im:
            im.verify()
        with Image.open(self.tmp) as im:
            self.assertLessEqual(im.width, 3840)
            self.assertGreater(im.width, 0)

    def test_aspect_ratio_is_preserved(self):
        self._write(3840, 2160)
        _ensure_upload_safe(self.tmp)
        with Image.open(self.tmp) as im:
            self.assertAlmostEqual(im.width / im.height, 16 / 9, places=2)

    def test_a_card_that_already_fits_is_left_untouched(self):
        self._write(600, 400)
        before = self.tmp.read_bytes()
        _ensure_upload_safe(self.tmp)
        self.assertEqual(self.tmp.read_bytes(), before,
                         "quality was given away on a card that already fit")

    def test_a_missing_file_is_not_an_error(self):
        _ensure_upload_safe(Path("/nonexistent/card.png"))


if __name__ == "__main__":
    unittest.main()
