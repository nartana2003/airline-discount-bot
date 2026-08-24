"""Settings loaded from the environment."""

import os
import unittest
from contextlib import contextmanager

from flightbot import config


@contextmanager
def env(**values):
    """Set/clear vars for the duration of a test, then put them all back."""
    before = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


BASE = dict(SMTP_USER="me@example.com", SMTP_PASSWORD="pw", ALERT_TO="me@example.com")


class EmailSettingsFromEnv(unittest.TestCase):
    def test_unset_host_and_port_use_defaults(self):
        with env(SMTP_HOST=None, SMTP_PORT=None, **BASE):
            s = config.EmailSettings.from_env()
        self.assertEqual((s.host, s.port), ("smtp.gmail.com", 465))

    def test_empty_host_and_port_use_defaults_too(self):
        """An unset GitHub secret arrives as "", not as absent - int("") used to crash."""
        with env(SMTP_HOST="", SMTP_PORT="", **BASE):
            s = config.EmailSettings.from_env()
        self.assertEqual((s.host, s.port), ("smtp.gmail.com", 465))

    def test_real_values_win(self):
        with env(SMTP_HOST="mail.example.com", SMTP_PORT="587", **BASE):
            s = config.EmailSettings.from_env()
        self.assertEqual((s.host, s.port), ("mail.example.com", 587))

    def test_configured_needs_user_password_and_recipient(self):
        with env(SMTP_HOST=None, SMTP_PORT=None, SMTP_USER="u",
                 SMTP_PASSWORD="", ALERT_TO="t"):
            self.assertFalse(config.EmailSettings.from_env().configured)
        with env(SMTP_HOST=None, SMTP_PORT=None, **BASE):
            self.assertTrue(config.EmailSettings.from_env().configured)

    def test_alert_to_defaults_to_the_sender(self):
        with env(SMTP_HOST=None, SMTP_PORT=None, SMTP_USER="me@example.com",
                 SMTP_PASSWORD="pw", ALERT_TO=None):
            self.assertEqual(config.EmailSettings.from_env().to, "me@example.com")


if __name__ == "__main__":
    unittest.main()
