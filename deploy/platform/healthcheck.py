"""Container health probe with no third-party dependency."""

from __future__ import annotations

import json
import os
from urllib.request import urlopen


def main() -> int:
    port = int(os.environ.get("FENORIUM_PLATFORM_PORT", "8516"))
    with urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:  # noqa: S310
        payload = json.load(response)
    return 0 if response.status == 200 and payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
