"""Memory eval suite must stay green in CI."""
from __future__ import annotations

import unittest

from webchat.memory_evals import run_memory_evals


class MemoryEvalSuiteTest(unittest.TestCase):
    def test_all_memory_eval_cases_pass(self):
        results = run_memory_evals()
        failures = [result.public() for result in results if not result.ok]
        self.assertEqual(failures, [])
        self.assertGreaterEqual(len(results), 6)


if __name__ == "__main__":
    unittest.main()
