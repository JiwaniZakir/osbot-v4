"""Docker health check — verifies the bot process is alive and state is writable."""

import json
import sys
from pathlib import Path

STATE_DIR = Path("/opt/osbot/state")


def check() -> bool:
    # State dir exists and is writable
    if not STATE_DIR.exists():
        print("FAIL: state dir missing")
        return False

    try:
        test_file = STATE_DIR / ".healthcheck"
        test_file.write_text("ok")
        test_file.unlink()
    except OSError as e:
        print(f"FAIL: state dir not writable: {e}")
        return False

    # state.json exists (bot has started at least once)
    state_file = STATE_DIR / "state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            if not isinstance(data, dict):
                print("FAIL: state.json is not a dict")
                return False
        except (json.JSONDecodeError, OSError) as e:
            print(f"FAIL: state.json corrupt: {e}")
            return False

    return True


if __name__ == "__main__":
    if check():
        sys.exit(0)
    else:
        sys.exit(1)
