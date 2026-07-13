import os
import unittest
from unittest.mock import patch

from tokenstat import config


class TestConfig(unittest.TestCase):
    def test_integer_environment_values_must_be_positive(self):
        for value in ("0", "-1"):
            with self.subTest(value=value), patch.dict(os.environ, {"TOKENSTAT_TEST": value}):
                with self.assertRaises(ValueError):
                    config._env_int("TOKENSTAT_TEST", 30)


if __name__ == "__main__":
    unittest.main()
