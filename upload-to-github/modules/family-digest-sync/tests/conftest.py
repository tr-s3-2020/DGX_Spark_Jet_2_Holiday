import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from family_digest import (  # noqa: E402
    Acquisition,
    ConsentScope,
    HealthSignalRecord,
    Policy,
    SafetyEventRecord,
    SafetyLevel,
    Severity,
    SignalType,
    Tier,
)
from family_digest.adapters import MockChannel  # noqa: E402
from family_digest.store import JsonStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return JsonStore(str(tmp_path / "store.json"))
