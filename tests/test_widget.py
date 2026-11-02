"""Runs the iPhone widget tests (tests/widget/*.test.mjs) with Node, if Node is installed.

They execute iphone/CryptoTicker.js against a stand-in for the Scriptable API; see
tests/widget/scriptable_stub.mjs for what that does and doesn't simulate.
"""

import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_widget_suite():
    r = subprocess.run(["node", "--test", os.path.join(HERE, "widget", "widget.test.mjs")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
