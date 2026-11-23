"""Watch a running search from the terminal, one line per event.

  uv run python tools/watch.py <session id> [port]

It connects to the server's WebSocket and prints every event until the search finishes.
"""

import json
import sys

from websockets.sync.client import connect


def main() -> None:
    run_id = sys.argv[1]
    port = sys.argv[2] if len(sys.argv) > 2 else "8765"
    with connect(f"ws://localhost:{port}/ws/{run_id}") as ws:
        for message in ws:  # the loop ends when the server closes the connection
            event = json.loads(message)
            details = {k: v for k, v in event.items() if k not in ("kind", "depth")}
            print(f"{event['kind']:15} {json.dumps(details, ensure_ascii=False)[:140]}")


if __name__ == "__main__":
    main()
