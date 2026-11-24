"""The deterministic eval suite must stay green in CI."""
from __future__ import annotations

import unittest

from webchat.evals import run_evals


class EvalSuiteTest(unittest.TestCase):
    def test_all_eval_cases_pass(self):
        results = run_evals()
        failures = [result.public() for result in results if not result.ok]
        self.assertEqual(failures, [])
        self.assertGreaterEqual(len(results), 6)


if __name__ == "__main__":
    unittest.main()
