"""RM37/AC28: POSIX private files for registrations and the erasure key."""

import os
import stat

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX private-file path")


def private(path, data=b"x" * 32):
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def test_a_configured_path_through_an_ancestor_link_is_resolved(tmp_path):
    """An operator path under an ancestor link (like macOS /tmp) is resolved where configured.

    The private-file checks themselves still refuse a link anywhere in the path.
    """
    from relaymind.recommendations.private_files import read_private_file
    from relaymind.recommendations.registrations import parse_user_action_settings

    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    private(real / "registrations.json", b"{}")
    settings = parse_user_action_settings(
        {"registrations_path": "registrations.json"}, config_dir=tmp_path / "alias"
    )
    assert settings.registrations_path == real / "registrations.json"
    assert read_private_file(settings.registrations_path) == b"{}"


def test_a_configured_link_to_the_private_file_itself_is_not_resolved(tmp_path):
    from relaymind.recommendations.private_files import PrivateFileError, read_private_file
    from relaymind.recommendations.registrations import parse_user_action_settings

    target = private(tmp_path / "target.json", b"{}")
    (tmp_path / "link.json").symlink_to(target)
    settings = parse_user_action_settings({"registrations_path": "link.json"}, config_dir=tmp_path)
    assert settings.registrations_path == tmp_path / "link.json"
    with pytest.raises(PrivateFileError):
        read_private_file(settings.registrations_path)


def test_a_symlinked_private_file_is_still_refused(tmp_path):
    from relaymind.recommendations.private_files import PrivateFileError, read_private_file

    target = private(tmp_path / "target.json", b"{}")
    (tmp_path / "link.json").symlink_to(target)
    with pytest.raises(PrivateFileError):
        read_private_file(tmp_path / "link.json")


def test_dot_dot_in_a_private_path_is_refused(tmp_path):
    from relaymind.recommendations.private_files import PrivateFileError, read_private_file

    (tmp_path / "a").mkdir()
    private(tmp_path / "key", b"{}")
    with pytest.raises(PrivateFileError):
        read_private_file(tmp_path / "a" / ".." / "key")


def test_a_failed_key_write_leaves_no_partial_file_and_can_be_retried(tmp_path, monkeypatch):
    """A crash or ENOSPC mid-write never leaves a truncated key under the final name."""
    from relaymind.recommendations import private_files
    from relaymind.recommendations.private_files import (
        PrivateFileError,
        create_private_file_exclusive,
    )

    path = tmp_path / "store.erasure-key"
    real_write = os.write

    def failing_write(fd, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(private_files.os, "write", failing_write)
    with pytest.raises(PrivateFileError):
        create_private_file_exclusive(path, b"k" * 32)
    assert not path.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []
    monkeypatch.setattr(private_files.os, "write", real_write)
    assert create_private_file_exclusive(path, b"k" * 32) is True
    assert path.read_bytes() == b"k" * 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_crash_leftover_partial_file_is_cleaned_and_never_used(tmp_path):
    from relaymind.recommendations.private_files import create_private_file_exclusive

    path = tmp_path / "store.erasure-key"
    leftover = private(tmp_path / ".store.erasure-key.partial-deadbeef", b"kk")
    assert create_private_file_exclusive(path, b"k" * 32) is True
    assert path.read_bytes() == b"k" * 32
    assert not leftover.exists()


def test_an_existing_key_is_never_replaced(tmp_path):
    from relaymind.recommendations.private_files import create_private_file_exclusive

    path = private(tmp_path / "store.erasure-key", b"o" * 32)
    assert create_private_file_exclusive(path, b"n" * 32) is False
    assert path.read_bytes() == b"o" * 32
    assert [p.name for p in tmp_path.iterdir()] == ["store.erasure-key"]
