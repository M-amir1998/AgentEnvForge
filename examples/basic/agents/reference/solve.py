"""Scripted reference agent for the ledger task.

It behaves like a (very well informed) coding agent: it reads the instructions, edits
``ledger/store.py`` in the workspace and runs the visible tests. It exists so the
end-to-end test can prove the whole pipeline works without any AI provider.

AgentEnvForge copies this directory to /agent in the agent sandbox and runs
``python /agent/solve.py`` with the workspace as the current directory.
"""

import os
import subprocess
import sys
from pathlib import Path

BUGGY = """        self._snapshots.append(dict(self._data))
        try:
            yield self
        except Exception:
            self._data = self._snapshots[0]
        finally:
            self._snapshots.pop()
"""

FIXED = """        # Deep copy: values may be mutable (e.g. the transfer history list).
        self._snapshots.append(copy.deepcopy(self._data))
        try:
            yield self
        except Exception:
            # Restore *this* transaction's snapshot, then let the error propagate.
            self._data = self._snapshots[-1]
            raise
        finally:
            self._snapshots.pop()
"""


def main() -> int:
    workspace = Path(os.environ["AGENTENV_WORKSPACE"])
    instructions = Path(os.environ["AGENTENV_INSTRUCTIONS_FILE"]).read_text(encoding="utf-8")
    print(f"received {len(instructions)} characters of instructions", flush=True)

    store = workspace / "ledger" / "store.py"
    source = store.read_text(encoding="utf-8")
    if BUGGY not in source:
        print("could not locate the transaction implementation", file=sys.stderr)
        return 1
    source = source.replace(BUGGY, FIXED)
    source = source.replace(
        "from collections.abc import Iterator", "import copy\nfrom collections.abc import Iterator"
    )
    store.write_text(source, encoding="utf-8")
    print("patched ledger/store.py", flush=True)

    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=workspace, check=False
    )
    return tests.returncode


if __name__ == "__main__":
    sys.exit(main())
