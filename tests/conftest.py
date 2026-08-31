"""Shared test setup.

Discovery caches OpenSea's merged drop calendar in a module-level store so that
scanning a second network costs no extra API request. That cache is process
wide, so a test whose stubbed feed leaked into the next one would make failures
depend on test order. Clear it before every test.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discovery


@pytest.fixture(autouse=True)
def _clear_drop_calendar_cache():
    discovery.invalidate_calendar()
    yield
    discovery.invalidate_calendar()
