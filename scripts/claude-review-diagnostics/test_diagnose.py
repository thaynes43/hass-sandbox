import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import diagnose


class DiagnosticTests(unittest.TestCase):
    def run_diagnostic(self, messages):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude-execution-output.json"
            path.write_text(json.dumps(messages), encoding="utf-8")
            return diagnose.diagnose(path)

    def test_failure_categories(self):
        cases = (
            ("OAuth token has expired. SECRET", "oauth_expired"),
            ("Invalid bearer token SECRET", "oauth_invalid"),
            ("API Error: rate_limit_error SECRET", "quota_or_rate_limit"),
            ("You've hit your limit SECRET", "quota_or_rate_limit"),
            ("You\u2019ve hit your limit SECRET", "quota_or_rate_limit"),
            ("You have exceeded your rate limit SECRET", "quota_or_rate_limit"),
            ("Out of usage credits SECRET", "quota_or_rate_limit"),
            ("Invalid API key SECRET", "oauth_invalid"),
            ("Model SECRET is not available", "model_unavailable"),
            ('API Error: {"type":"not_found_error","message":"model: SECRET"}', "model_unavailable"),
            ("Something else SECRET", "other"),
        )
        for text, category in cases:
            with self.subTest(category=category, text=text):
                report = self.run_diagnostic([{"type": "result", "subtype": "success", "is_error": True, "result": text}])
                self.assertEqual(report["cause"], category)
                self.assertNotIn("SECRET", json.dumps(report))

    def test_errors_array_and_first_turn_metadata(self):
        report = self.run_diagnostic([{"type": "system", "apiKey": "SECRET"}, {
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "errors": ["Invalid OAuth token SECRET", {"secret": "SECRET"}],
            "num_turns": 1, "total_cost_usd": 0, "modelUsage": {}, "permission_denials": [],
        }])
        self.assertEqual(report["cause"], "oauth_invalid")
        for key in ("first_turn", "zero_cost", "empty_model_usage", "result_present", "reported_error", "error_text_present"):
            self.assertTrue(report[key])
        self.assertFalse(report["permission_denials_present"])
        self.assertNotIn("SECRET", json.dumps(report))

    def test_successful_review_and_assistant_text_are_not_error_evidence(self):
        report = self.run_diagnostic([
            {"type": "assistant", "message": {"content": "Invalid OAuth token SECRET"}},
            {"type": "result", "subtype": "success", "is_error": False, "result": "Review mentions invalid OAuth token SECRET"},
        ])
        self.assertEqual(report["cause"], "other")
        self.assertFalse(report["reported_error"])

    def test_last_result_wins_and_arbitrary_metadata_is_not_emitted(self):
        report = self.run_diagnostic([
            {"type": "result", "is_error": True, "result": "Invalid bearer token SECRET"},
            {"type": "result", "is_error": True, "result": "SECRET", "subtype": "SECRET",
             "num_turns": "SECRET", "total_cost_usd": "SECRET", "modelUsage": {"SECRET": {}},
             "permission_denials": [{"SECRET": "SECRET"}]},
        ])
        self.assertEqual(report["cause"], "other")
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertTrue(report["permission_denials_present"])

    def test_missing_invalid_and_oversized_execution_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SECRET"
            self.assertEqual(diagnose.diagnose(path)["execution_status"], "unreadable")
            for content, expected in ((b"SECRET", "invalid_json"), (b"{}", "invalid_shape"), (b"\xff", "invalid_json")):
                path.write_bytes(content)
                self.assertEqual(diagnose.diagnose(path)["execution_status"], expected)
            path.write_bytes(b"SECRET")
            with patch.object(diagnose, "MAX_EXECUTION_BYTES", 2):
                self.assertEqual(diagnose.diagnose(path)["execution_status"], "too_large")

    def test_malformed_result_fields_and_nul_path_are_safe(self):
        report = self.run_diagnostic([{"type": "result", "subtype": ["SECRET"], "errors": "SECRET"}])
        self.assertEqual(report["cause"], "other")
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertEqual(diagnose.diagnose(Path("SECRET\x00"))["execution_status"], "unreadable")

    def test_fallback_file_and_summary_contain_only_safe_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude-execution-output.json"
            path.write_text(json.dumps([{"type": "result", "is_error": True, "result": "Invalid bearer token SECRET\n::error::SECRET"}]))
            summary = Path(directory) / "summary"
            output = io.StringIO()
            with patch.dict(os.environ, {"RUNNER_TEMP": directory, "CLAUDE_EXECUTION_FILE": "", "GITHUB_STEP_SUMMARY": str(summary)}, clear=True):
                with contextlib.redirect_stdout(output):
                    diagnose.main()
            self.assertEqual(json.loads(output.getvalue())["cause"], "oauth_invalid")
            self.assertNotIn("SECRET", output.getvalue() + summary.read_text())
            self.assertNotIn("::error::", output.getvalue() + summary.read_text())


if __name__ == "__main__":
    unittest.main()
