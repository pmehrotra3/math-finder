"""Turn a real trace into web/demo_events.json, so the web page can replay it.

  uv run python tools/make_demo.py runs/<folder>/trace.jsonl

Then open http://localhost:8765/#demo: the page shows that search as if it were live,
with no Lean and no model needed. Handy for working on the page's look.
"""

import json
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def main() -> None:
    trace = Path(sys.argv[1])
    events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    keep = [{k: v for k, v in e.items() if k != "time"} for e in events if e["kind"] not in ("meta", "result")]
    out = WEB_DIR / "demo_events.json"
    out.write_text(json.dumps(keep, ensure_ascii=False, indent=1))
    print(f"wrote {len(keep)} events to {out}")


if __name__ == "__main__":
    main()
