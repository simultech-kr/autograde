"""Deterministic browser-state tests without a browser or a network connection."""
import base64
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from autograde.instructor_results_browser import SCRIPT, SCRIPT_CSP, SCRIPT_HASH, SCRIPT_TAG


def test_results_script_has_exact_hash_authorization():
    assert SCRIPT_HASH == base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    assert SCRIPT_CSP == f"; script-src 'sha256-{SCRIPT_HASH}'; connect-src 'self'"
    assert SCRIPT_TAG == '<script data-instructor-results-enhancement>' + SCRIPT + '</script>'


def test_results_browser_state_machine():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the deterministic JavaScript behavior checks')
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'check_instructor_results_browser.cjs'
    result = subprocess.run([node, str(script)], input=SCRIPT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'live results behavior scenarios passed' in result.stdout
    assignment_scenarios = re.search(r'(\d+) assignment filter scenarios passed', result.stdout)
    assert assignment_scenarios and int(assignment_scenarios[1]) >= 7, result.stdout
