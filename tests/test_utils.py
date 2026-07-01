"""Tests for shared subprocess helpers."""

import sys

from devready.utils import run_command_teed


def test_teed_captures_and_streams(capsys):
    result = run_command_teed([sys.executable, "-c", "print('hello-teed')"])
    assert result.returncode == 0
    assert "hello-teed" in result.stdout          # captured tail
    assert "hello-teed" in capsys.readouterr().out  # streamed live


def test_teed_heartbeat_on_silent_command(capsys):
    # A command that produces NO output for ~2s must trigger a liveness note, so
    # a long quiet build (e.g. `pip install .`) never looks frozen.
    result = run_command_teed(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        heartbeat_secs=1,
    )
    out = capsys.readouterr().out
    assert result.returncode == 0
    assert "still working" in out


def test_teed_heartbeat_disabled(capsys):
    result = run_command_teed(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        heartbeat_secs=0,
    )
    out = capsys.readouterr().out
    assert result.returncode == 0
    assert "still working" not in out
