from pathlib import Path
import tempfile
import unittest

from benchmark.io import atomic_json, digest, read_json
from benchmark.providers.openai import ProviderError
from benchmark.state import ValidationRejected, execute_unit


class StateTests(unittest.TestCase):
    def test_resume_does_not_repeat_paid_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def action(attempt):
                calls.append(attempt)
                return {"score": 1}
            self.assertEqual(execute_unit(root, "gate", "role/person", {"version": 1}, action), {"score": 1})
            execute_unit(root, "gate", "role/person", {"version": 1}, action)
            self.assertEqual(calls, [1])
            with self.assertRaises(ValueError):
                execute_unit(root, "gate", "role/person", {"version": 2}, action)

    def test_uncertain_call_needs_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fail(attempt):
                raise ProviderError("WRANGLE_REQUEST_UNCERTAIN")
            with self.assertRaises(ProviderError):
                execute_unit(root, "search", "key", {}, fail)
            with self.assertRaises(RuntimeError):
                execute_unit(root, "search", "key", {}, lambda _: {"done": True})
            self.assertEqual(execute_unit(root, "search", "key", {}, lambda attempt: {"attempt": attempt}, retry_uncertain=True), {"attempt": 2})

    def test_locally_rejected_response_is_failed_not_uncertain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def reject(attempt):
                raise ValidationRejected("fixture")
            with self.assertRaises(ValidationRejected):
                execute_unit(root, "gate", "key", {}, reject)
            row = read_json(root / "units" / "gate" / f"{digest('key')}.json")
            self.assertEqual((row["status"], row["error"]["code"]), ("failed", "LOCAL_VALIDATION_REJECTED"))
            with self.assertRaisesRegex(RuntimeError, "failed"):
                execute_unit(root, "gate", "key", {}, lambda _: {}, retry_uncertain=True)
            self.assertEqual(execute_unit(root, "gate", "key", {}, lambda attempt: {"attempt": attempt}, retry_failed=True), {"attempt": 2})

    def test_tampered_completed_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            execute_unit(root, "gate", "key", {}, lambda _: {"score": 1})
            path = root / "units" / "gate" / f"{digest('key')}.json"
            row = read_json(path); row["result"]["score"] = 0; atomic_json(path, row)
            with self.assertRaises(ValueError):
                execute_unit(root, "gate", "key", {}, lambda _: {})


if __name__ == "__main__":
    unittest.main()
