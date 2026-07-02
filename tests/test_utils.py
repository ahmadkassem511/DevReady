"""Tests for shared subprocess helpers."""

import os
import stat
import sys

from devready.utils import force_rmtree, run_command_teed


def test_force_rmtree_deletes_readonly_git_files(tmp_path):
    # Simulate git's read-only pack files, which defeat plain shutil.rmtree on
    # Windows and leave the .git folder behind.
    proj = tmp_path / "proj"
    objects = proj / ".git" / "objects" / "pack"
    objects.mkdir(parents=True)
    pack = objects / "pack-abc.idx"
    pack.write_text("data")
    os.chmod(pack, stat.S_IREAD)  # read-only

    assert force_rmtree(proj) is True
    assert not proj.exists()


def test_force_rmtree_deletes_readonly_directories(tmp_path):
    # POSIX regression: deletion rights live on the parent DIRECTORY there, and
    # the old S_IWRITE-only chmod (0o200) stripped read+execute from dirs so
    # rmtree could no longer traverse them — force_rmtree bricked its own tree.
    proj = tmp_path / "proj"
    inner = proj / ".git" / "objects"
    inner.mkdir(parents=True)
    (inner / "pack-abc.idx").write_text("data")
    os.chmod(inner, stat.S_IREAD | stat.S_IEXEC)  # read-only dir

    assert force_rmtree(proj) is True
    assert not proj.exists()


def test_force_rmtree_missing_path_is_ok(tmp_path):
    assert force_rmtree(tmp_path / "does-not-exist") is True


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
