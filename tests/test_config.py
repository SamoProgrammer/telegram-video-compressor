import unittest

from tgcompress.config import Config


class ConfigValidationTest(unittest.TestCase):
    def test_rejects_nonpositive_limits(self):
        for field in (
            "run_seconds",
            "reserve_seconds",
            "max_input_mb",
            "max_output_mb",
            "max_duration",
            "disk_budget_mb",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    Config(**{field: 0 if field != "reserve_seconds" else -1})

    def test_reserve_must_be_less_than_shift(self):
        with self.assertRaisesRegex(ValueError, "reserve_seconds"):
            Config(run_seconds=60, reserve_seconds=60)

    def test_valid_short_shift_is_accepted(self):
        config = Config(run_seconds=60, reserve_seconds=10)
        self.assertEqual(config.run_seconds, 60)


if __name__ == "__main__":
    unittest.main()
