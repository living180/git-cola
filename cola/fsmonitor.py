# Copyright (c) 2008 David Aguilar
# Copyright (c) 2015 Daniel Harding
"""Provides an filesystem monitoring for Linux (via inotify) and for Windows
(via pywin32 and the ReadDirectoryChanges function)"""
from __future__ import division, absolute_import, unicode_literals

import errno
import os
import os.path
import select
from collections import defaultdict
from threading import Lock

from cola import utils

AVAILABLE = None

if utils.is_win32():
    try:
        import pywintypes
        import win32con
        import win32event
        import win32file
    except ImportError:
        pass
    else:
        AVAILABLE = 'pywin32'
elif utils.is_linux():
    try:
        from cola import inotify
    except ImportError:
        pass
    else:
        AVAILABLE = 'inotify'

from PyQt4 import QtCore
from PyQt4.QtCore import SIGNAL

from cola import core
from cola import gitcfg
from cola import gitcmds
from cola.compat import bchr
from cola.git import git
from cola.i18n import N_
from cola.interaction import Interaction


class _Monitor(QtCore.QObject):
    def __init__(self, thread_class):
        QtCore.QObject.__init__(self)
        self._thread_class = thread_class
        self._thread = None

    def start(self):
        if self._thread_class is not None:
            assert self._thread is None
            self._thread = self._thread_class(self)
            self._thread.start()

    def stop(self):
        if self._thread_class is not None:
            assert self._thread is not None
            self._thread.stop()
            self._thread.wait()
            self._thread = None

    def refresh(self):
        if self._thread is not None:
            self._thread.refresh()


class _BaseThread(QtCore.QThread):
    #: The delay, in milliseconds, between detecting file system modification
    #: and triggering the 'files_changed' signal, to coalesce multiple
    #: modifications into a single signal.
    _NOTIFICATION_DELAY = 888

    def __init__(self, monitor):
        QtCore.QThread.__init__(self)
        self._monitor = monitor
        self._running = True
        self._pending = False

    def refresh(self):
        """Do any housekeeping necessary in response to repository changes."""
        pass

    def notify(self):
        """Notifies all observers"""
        self._pending = False
        self._monitor.emit(SIGNAL('files_changed'))

    @staticmethod
    def _log_enabled_message():
        msg = N_('File system change monitoring: enabled.\n')
        Interaction.safe_log(msg)


if AVAILABLE == 'inotify':
    class _InotifyThread(_BaseThread):
        _TRIGGER_MASK = (
                inotify.IN_ATTRIB |
                inotify.IN_CLOSE_WRITE |
                inotify.IN_CREATE |
                inotify.IN_DELETE |
                inotify.IN_MODIFY |
                inotify.IN_MOVED_FROM |
                inotify.IN_MOVED_TO
        )
        _ADD_MASK = (
                _TRIGGER_MASK |
                inotify.IN_EXCL_UNLINK |
                inotify.IN_ONLYDIR
        )

        def __init__(self, monitor):
            _BaseThread.__init__(self, monitor)
            self._worktree = core.abspath(git.worktree())
            self._lock = Lock()
            self._inotify_fd = None
            self._pipe_r = None
            self._pipe_w = None
            self._wd_map = {}

        @staticmethod
        def _log_out_of_wds_message():
            msg = N_('File system change monitoring: disabled because the'
                     ' limit on the total number of inotify watches was'
                     ' reached.  You may be able to increase the the limit on'
                     ' the number of watches by running:\n'
                     '\n'
                     '    echo fs.inotify.max_user_watches=100000 |\n'
                     '    sudo tee -a /etc/sysctl.conf &&\n'
                     '    sudo sysctl -p\n')
            Interaction.log_safe(msg)

        def run(self):
            try:
                with self._lock:
                    self._inotify_fd = inotify.init()
                    self._pipe_r, self._pipe_w = os.pipe()

                poll_obj = select.poll()
                poll_obj.register(self._inotify_fd, select.POLLIN)
                poll_obj.register(self._pipe_r, select.POLLIN)

                self.refresh()

                self._log_enabled_message()

                while self._running:
                    if self._pending:
                        timeout = self._NOTIFICATION_DELAY
                    else:
                        timeout = None
                    try:
                        events = poll_obj.poll(timeout)
                    except OSError as e:
                        if e.errno == errno.EINTR:
                            continue
                        else:
                            raise
                    else:
                        if not self._running:
                            break
                        elif not events:
                            self.notify()
                        else:
                            for fd, event in events:
                                if fd == self._inotify_fd:
                                    self._handle_events()
            finally:
                with self._lock:
                    if self._inotify_fd is not None:
                        os.close(self._inotify_fd)
                        self._inotify_fd = None
                    if self._pipe_r is not None:
                        os.close(self._pipe_r)
                        self._pipe_r = None
                        os.close(self._pipe_w)
                        self._pipe_w = None

        def refresh(self):
            with self._lock:
                if self._inotify_fd is None:
                    return
                watched_dirs = set(self._wd_map)
                tracked_dirs = {os.path.dirname(os.path.join(self._worktree,
                                                             path))
                                for path in gitcmds.tracked_files()}
                for path in watched_dirs - tracked_dirs:
                    wd = self._wd_map.pop(path)
                    try:
                        inotify.rm_watch(self._inotify_fd, wd)
                    except OSError as e:
                        if e.errno == errno.EINVAL:
                            # This error can occur if the target of the wd was
                            # removed on the filesystem before we call
                            # inotify.rm_watch() so ignore it.
                            pass
                        else:
                            raise
                for path in tracked_dirs - watched_dirs:
                    try:
                        wd = inotify.add_watch(self._inotify_fd,
                                               core.encode(path),
                                               self._ADD_MASK)
                    except OSError as e:
                        if e.errno == errno.ENOSPC:
                            self._log_out_of_wds_message()
                            self._running = False
                            break
                        elif e.errno in (errno.ENOENT, errno.ENOTDIR):
                            # These two errors should only occur as a result of
                            # race conditions:  the first if the directory
                            # referenced by dirpath was removed or renamed
                            # before the call to inotify.add_watch(); the
                            # second if the directory referenced by dirpath was
                            # replaced with a file before the call to
                            # inotify.add_watch().  Therefore we simply ignore
                            # them.
                            pass
                        else:
                            raise
                    else:
                        self._wd_map[path] = wd

        def _handle_events(self):
            for wd, mask, cookie, name in \
                    inotify.read_events(self._inotify_fd):
                if mask & self._TRIGGER_MASK:
                    self._pending = True

        def stop(self):
            self._running = False
            with self._lock:
                if self._pipe_w is not None:
                    os.write(self._pipe_w, bchr(0))
            self.wait()


if AVAILABLE == 'pywin32':
    class _Win32Thread(_BaseThread):
        _FLAGS = (win32con.FILE_NOTIFY_CHANGE_FILE_NAME |
                  win32con.FILE_NOTIFY_CHANGE_DIR_NAME |
                  win32con.FILE_NOTIFY_CHANGE_ATTRIBUTES |
                  win32con.FILE_NOTIFY_CHANGE_SIZE |
                  win32con.FILE_NOTIFY_CHANGE_LAST_WRITE |
                  win32con.FILE_NOTIFY_CHANGE_SECURITY)

        def __init__(self, monitor):
            _BaseThread.__init__(self, monitor)
            self._worktree = self._transform_path(core.abspath(git.worktree()))
            self._git_dir = self._transform_path(git.git_dir())
            self._dir_handle = None
            self._buffer = None
            self._overlapped = None
            self._stop_event_lock = Lock()
            self._stop_event = None

        @staticmethod
        def _transform_path(path):
            return path.replace('\\', '/').lower()

        def run(self):
            try:
                with self._stop_event_lock:
                    self._stop_event = win32event.CreateEvent(None, True,
                                                              False, None)

                self._dir_handle = win32file.CreateFileW(
                        self._worktree,
                        0x0001, # FILE_LIST_DIRECTORY
                        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                        None,
                        win32con.OPEN_EXISTING,
                        win32con.FILE_FLAG_BACKUP_SEMANTICS |
                            win32con.FILE_FLAG_OVERLAPPED,
                        None)

                self._buffer = win32file.AllocateReadBuffer(8192)
                self._overlapped = pywintypes.OVERLAPPED()
                self._overlapped.hEvent = win32event.CreateEvent(None, True,
                                                                 False, None)

                self._log_enabled_message()

                while self._running:
                    win32file.ReadDirectoryChangesW(self._dir_handle,
                                                    self._buffer, True,
                                                    self._FLAGS,
                                                    self._overlapped)
                    if self._pending:
                        timeout = self._NOTIFICATION_DELAY
                    else:
                        timeout = win32event.INFINITE
                    rc = win32event.WaitForMultipleObjects(
                            [self._overlapped.hEvent, self._stop_event], False,
                            timeout)
                    if not self._running:
                        break
                    elif rc == win32event.WAIT_TIMEOUT:
                        self.notify()
                    else:
                        self._handle_results()
            finally:
                with self._stop_event_lock:
                    if self._stop_event is not None:
                        win32file.CloseHandle(self._stop_event)
                        self._stop_event = None
                if self._dir_handle is not None:
                    win32file.CancelIo(self._dir_handle)
                    win32file.CloseHandle(self._dir_handle)
                if self._overlapped is not None and self._overlapped.hEvent:
                    win32file.CloseHandle(self._overlapped.hEvent)

        def _handle_results(self):
            nbytes = win32file.GetOverlappedResult(self._dir_handle,
                                                   self._overlapped, True)
            results = win32file.FILE_NOTIFY_INFORMATION(self._buffer, nbytes)
            for action, path in results:
                if not self._running:
                    break
                path = self._worktree + '/' + self._transform_path(path)
                if (path != self._git_dir
                        and not path.startswith(self._git_dir + '/')):
                    self._pending = True

        def stop(self):
            self._running = False
            with self._stop_event_lock:
                if self._stop_event is not None:
                    win32event.SetEvent(self._stop_event)
            self.wait()


_instance = None

def instance():
    global _instance
    if _instance is None:
        _instance = _create_instance()
    return _instance


def _create_instance():
    thread_class = None
    cfg = gitcfg.current()
    if not cfg.get('cola.inotify', True):
        msg = N_('File system change monitoring: disabled because'
                 ' "cola.inotify" is false.\n')
        Interaction.log(msg)
    elif AVAILABLE == 'inotify':
        thread_class = _InotifyThread
    elif AVAILABLE == 'pywin32':
        thread_class = _Win32Thread
    else:
        if utils.is_win32():
            msg = N_('File system change monitoring: disabled because pywin32'
                     ' is not installed.\n')
            Interaction.log(msg)
        elif utils.is_linux():
            msg = N_('File system change monitoring: disabled because libc'
                     ' does not support the inotify system calls.\n')
            Interaction.log(msg)
    return _Monitor(thread_class)
