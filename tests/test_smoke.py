import shutil

import mathfinder


def test_package_imports():
    assert mathfinder.__version__ == "0.1.0"


def test_lean_project_exists():
    assert (mathfinder.LEAN_DIR / "lean-toolchain").exists()
    assert (mathfinder.LEAN_DIR / "lakefile.toml").exists()


def test_lake_is_installed():
    assert shutil.which("lake") is not None
