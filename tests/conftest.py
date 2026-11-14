import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def restore_mod():
    path = ROOT / "orin_updater" / "target" / "orin_restore.py"
    spec = importlib.util.spec_from_file_location("orin_restore", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
