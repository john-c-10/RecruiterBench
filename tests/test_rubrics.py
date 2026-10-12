from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from benchmark.io import atomic_json, digest, read_json
from benchmark.models import REASONING_EFFORT, prompt, schema
from benchmark.rubrics import extract
from support import dataset


class RubricExtractionTests(unittest.TestCase):
    def setup_saved(self, root, model):
        data, role = dataset(root / "dataset")
        path = data / role["rubric_path"]
        record = read_json(path)
        inputs = {"snapshot_sha256": role["snapshot_sha256"], "prompt_sha256": digest(prompt("rubric")),
                  "schema_sha256": digest(schema("rubric")), "model": model, "reasoning_effort": REASONING_EFFORT}
        atomic_json(path, {**record, "input_sha256": digest(inputs), "extraction": inputs})
        return data, path

    @patch.dict(os.environ, {}, clear=True)
    def test_frozen_rubric_from_historical_model_is_cached_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, _ = self.setup_saved(Path(temporary), "gpt-6-sol")
            report = extract(data, Path(temporary) / "private")
            self.assertEqual(report["results"], [{"role_id": "test-role", "status": "cached", "model": "gpt-6-sol"}])

    def test_changed_prompt_or_tampered_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data, path = self.setup_saved(Path(temporary), "gpt-6-sol")
            record = read_json(path); record["extraction"]["model"] = "other"
            atomic_json(path, record)
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                extract(data, Path(temporary) / "private")
        with tempfile.TemporaryDirectory() as temporary:
            data, path = self.setup_saved(Path(temporary), "gpt-6-sol")
            record = read_json(path); record["extraction"]["prompt_sha256"] = digest("old prompt")
            record["input_sha256"] = digest(record["extraction"])
            atomic_json(path, record)
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                extract(data, Path(temporary) / "private")


if __name__ == "__main__":
    unittest.main()
