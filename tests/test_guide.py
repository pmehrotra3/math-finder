"""The guide must match the real code. If this fails, run:
    uv run python tools/build_guide.py
"""

import importlib.util

from mathfinder import PROJECT_ROOT


def load_builder():
    path = PROJECT_ROOT / "tools" / "build_guide.py"
    spec = importlib.util.spec_from_file_location("build_guide", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guide_is_up_to_date():
    expected = load_builder().build()
    actual = (PROJECT_ROOT / "guide.html").read_text()
    assert actual == expected, "guide.html is stale: run tools/build_guide.py"
