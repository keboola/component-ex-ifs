"""Functional (datadir + VCR) tests for the IFS Cloud OData extractor.

Each case under ``tests/functional/`` replays a recorded cassette (no network)
and compares the produced ``out/tables`` against the committed ``expected/``.

``KBC_DATA_TYPE_SUPPORT=authoritative`` is forced before the component runs so
``create_out_table_definition`` emits the authoritative ``schema`` manifest
(``data_type.base.type``) — otherwise it auto-detects legacy mode and the
recorded ``expected/`` manifests would validate against the wrong shape.

Fetching is stateless: the customer-driven Date window (``date_field`` +
``date_start`` / ``date_end``) is recomputed from ``config.json`` each run, so
every case is a plain, self-contained replay — no seeded state, no cursor.
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("KBC_DATA_TYPE_SUPPORT", "authoritative")

from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")

_CASES = get_test_cases(FUNCTIONAL_DIR)


@pytest.mark.parametrize("test_name", _CASES)
def test_functional(test_name):
    """Replay one stateless VCR functional case and compare against expected/."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()
