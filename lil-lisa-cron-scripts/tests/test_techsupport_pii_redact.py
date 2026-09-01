"""
Unit tests for obvious-PII redaction on generated techsupport title/summary text.

Run from lil-lisa-cron-scripts:
    PYTHONPATH=. python3 tests/test_techsupport_pii_redact.py
"""

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from techsupport_pii import PII_REDACTED_PLACEHOLDER, redact_obvious_pii  # noqa: E402


class RedactObviousPiiTests(unittest.TestCase):
    def test_email(self):
        text = redact_obvious_pii("Contact jane.doe@acme.com for the bind DN.")
        self.assertNotIn("jane.doe@acme.com", text)
        self.assertIn(PII_REDACTED_PLACEHOLDER, text)

    def test_private_ipv4(self):
        text = redact_obvious_pii("The LDAP host is 10.4.2.18 on port 636.")
        self.assertNotIn("10.4.2.18", text)
        self.assertIn(PII_REDACTED_PLACEHOLDER, text)

    def test_product_version_is_not_treated_as_ip(self):
        body = "Fixed in IDDM 7.3.1.0 by setting the timeout."
        self.assertEqual(redact_obvious_pii(body), body)

    def test_slack_mention(self):
        text = redact_obvious_pii("Ask <@U012ABC345> to restart VDS.")
        self.assertNotIn("<@U012ABC345>", text)

    def test_ticket_prefixes(self):
        text = redact_obvious_pii("See INC12345 and SR-99 and TICKET_7.")
        self.assertNotIn("INC12345", text)
        self.assertNotIn("SR-99", text)
        self.assertNotIn("TICKET_7", text)

    def test_internal_fqdn(self):
        text = redact_obvious_pii("Point the connector at idvault.prod.internal.")
        self.assertNotIn("idvault.prod.internal", text)

    def test_aws_key_and_password_assignment(self):
        text = redact_obvious_pii("Key AKIAIOSFODNN7EXAMPLE password=hunter2")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", text)
        self.assertNotIn("hunter2", text)

    def test_pem_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
        text = redact_obvious_pii(f"Do not commit {pem} to git.")
        self.assertNotIn("BEGIN RSA PRIVATE KEY", text)
        self.assertIn(PII_REDACTED_PLACEHOLDER, text)

    def test_clean_technical_prose_unchanged(self):
        body = "Increase the Zookeeper GC logging interval in 7.3.X and restart the node."
        self.assertEqual(redact_obvious_pii(body), body)


if __name__ == "__main__":
    unittest.main()
