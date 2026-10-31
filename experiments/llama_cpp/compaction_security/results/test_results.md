# Test results

- Existing repository baseline: `python3 -m unittest discover -v` found 0 tests and exited 5; Git HEAD contains only README.md.
- New module: `python3 -m unittest discover -s experiments/llama_cpp/compaction_security/tests -v` passed 14/14 tests on Python 3.12.3.
- Mock matrix: 12/12 experiment runs completed; outputs are in `mock_all_records.json` and per-run JSONL event files.
- Real initial matrix: 5/5 calls attempted; 2 action model errors and 3 compaction errors in the restricted sandbox (`URLError(PermissionError(1))`). An unrestricted localhost curl check then returned connection refused. No server or valid model output was available.
