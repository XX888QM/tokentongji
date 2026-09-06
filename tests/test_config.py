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

    def test_wildcard_host_falls_back_to_loopback(self):
        for raw in ("", "0.0.0.0", "::", "  0.0.0.0  "):
            self.assertEqual(config.resolve_host(raw), "127.0.0.1", raw)
        self.assertEqual(config.resolve_host("192.168.1.8"), "192.168.1.8")


if __name__ == "__main__":
    unittest.main()
