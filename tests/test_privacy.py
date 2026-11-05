import json
import tempfile
import unittest
from pathlib import Path

from ada.privacy import PrivacyGate, PrivacyLevel


class TestPrivacyGate(unittest.TestCase):
    def test_l1_scrubs_and_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate = PrivacyGate(PrivacyLevel.L1_SCRUBBED,
                               Path(tmp) / "audit.jsonl")
            out = gate.outbound("call 13812345678", "cloud:some-api")
            self.assertNotIn("13812345678", out)
            lines = (Path(tmp) / "audit.jsonl").read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
            self.assertTrue(entry["scrubbed"])
            self.assertEqual(entry["destination"], "cloud:some-api")

    def test_l0_blocks_cloud(self):
        gate = PrivacyGate(PrivacyLevel.L0_LOCAL_ONLY)
        with self.assertRaises(PermissionError):
            gate.outbound("data", "cloud:any")
        # 本地放行
        self.assertEqual(gate.outbound("data", "ollama:local:m"), "data")

    def test_l2_no_scrub(self):
        gate = PrivacyGate(PrivacyLevel.L2_FULL)
        out = gate.outbound("call 13812345678", "cloud:x")
        self.assertIn("13812345678", out)

    def test_env_default(self):
        import os
        os.environ["ADA_PRIVACY"] = "L0"
        try:
            self.assertEqual(PrivacyGate().level, PrivacyLevel.L0_LOCAL_ONLY)
        finally:
            del os.environ["ADA_PRIVACY"]


if __name__ == "__main__":
    unittest.main()
