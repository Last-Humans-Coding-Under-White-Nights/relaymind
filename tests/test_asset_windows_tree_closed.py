"""RM34/AC25: a closed native source tree has one stable failure boundary."""

import errno

import pytest

from relaymind.assets._windows_source_io import WindowsSourceTree


def test_closed_tree_refuses_every_operation_before_native_access():
    class Native:
        close = object()

    class Control:
        def __init__(self):
            self.handles = []
            self.closed = 0

        def invoke(self, _function, handle, *, cleanup=False):
            assert cleanup
            self.handles.append(handle)

        def close(self):
            self.closed += 1

    control = Control()
    tree = WindowsSourceTree.__new__(WindowsSourceTree)
    tree.api = Native()
    tree.control = control
    tree._owns_control = True
    tree._handles = [11, 12]
    tree._directories = {(): 12}
    tree._closed = False

    tree.close()
    tree.close()
    assert control.handles == [12, 11]
    assert control.closed == 1
    for operation in (
        lambda: tree.info("SKILL.md"),
        lambda: tree.open_file("SKILL.md"),
        lambda: next(tree.names()),
        tree.verify_root,
    ):
        with pytest.raises(OSError) as failure:
            operation()
        assert failure.value.errno == errno.EBADF
    assert control.handles == [12, 11]
