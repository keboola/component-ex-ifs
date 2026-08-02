"""Functional (datadir + VCR) tests for the IFS Cloud OData extractor.

Each case under ``tests/functional/`` replays a recorded cassette (no network)
and compares the produced ``out/tables`` against the committed ``expected/``.

``KBC_DATA_TYPE_SUPPORT=authoritative`` is forced before the component runs so
``create_out_table_definition`` emits the authoritative ``schema`` manifest
(``data_type.base.type``) — otherwise it auto-detects legacy mode and the
recorded ``expected/`` manifests would validate against the wrong shape.
"""

import os
import unittest
from pathlib import Path

import pytest

os.environ.setdefault("KBC_DATA_TYPE_SUPPORT", "authoritative")

from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases
from keboola.datadirtest.vcr.tester import VCRTestDataDir

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")

# Stateful cases: the datadir tester wipes source/in/state.json on setup, so a
# seeded incremental watermark must be injected via last_state_override. The value
# matches the (scrubbed) watermark that produced the `VoucherDate gt 2020-01-01`
# $filter recorded in the cassette, so replay reproduces that exact request.
STATEFUL_SEED = {"12_incremental_advance": {"last_value": "2020-01-01"}}

_FLAT_CASES = [name for name in get_test_cases(FUNCTIONAL_DIR) if name not in STATEFUL_SEED]


@pytest.mark.parametrize("test_name", _FLAT_CASES)
def test_functional(test_name):
    """Replay one stateless VCR functional case and compare against expected/."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()


@pytest.mark.parametrize("test_name,seed_state", sorted(STATEFUL_SEED.items()))
def test_functional_stateful(test_name, seed_state):
    """Replay a stateful VCR case with a seeded input state (incremental watermark)."""
    case = VCRTestDataDir(
        data_dir=str(Path(FUNCTIONAL_DIR) / test_name),
        component_script=COMPONENT_SCRIPT,
        last_state_override=seed_state,
    )
    result = unittest.TestResult()
    case(result)
    if not result.wasSuccessful():
        failures = [detail for _, detail in (result.errors + result.failures)]
        raise AssertionError(f"{test_name} failed:\n" + "\n".join(failures))
