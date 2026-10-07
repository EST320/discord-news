import unittest

from matplotlib.font_manager import FontProperties

from market_pulse import theme

FONT = FontProperties(size=10)


class WrapTest(unittest.TestCase):
    def test_short_text_stays_on_one_line(self):
        self.assertEqual(theme.wrap("Marsh", FONT, 2.0), ["Marsh"])

    def test_long_text_breaks_at_spaces_and_loses_nothing(self):
        text = "Taiwan Semiconductor Manufacturing Company Limited"
        lines = theme.wrap(text, FONT, 1.8)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), text)
        self.assertTrue(all(theme.text_width(line, FONT) <= 1.8 for line in lines))

    def test_a_word_wider_than_the_line_is_split(self):
        word = "Supercalifragilisticexpialidocious"
        lines = theme.wrap(word, FONT, 0.8)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), word)

    def test_empty_text(self):
        self.assertEqual(theme.wrap("", FONT, 1.0), [""])


class EllipsizeTest(unittest.TestCase):
    def test_fits_unchanged_or_shortened_with_an_ellipsis(self):
        self.assertEqual(theme.ellipsize("NYSE", FONT, 2.0), "NYSE")
        shortened = theme.ellipsize("A very long exchange name indeed", FONT, 1.0)
        self.assertTrue(shortened.endswith("…"))
        self.assertLessEqual(theme.text_width(shortened, FONT), 1.0)


class PlainTest(unittest.TestCase):
    def test_dollar_signs_are_escaped(self):
        self.assertEqual(theme.plain("$3,960 – $5,318"), "\\$3,960 – \\$5,318")


if __name__ == "__main__":
    unittest.main()
