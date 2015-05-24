"""Classes and supporting functions for subprocess-mode commands.

Includes an interface for running Python functions as subprocess-mode commands.

Code for several helper methods in the `ProcProxy` class have been reproduced
without modification from `subprocess.py` in the Python 3.4.2 standard library.
The contents of `subprocess.py` (and, thus, the reproduced methods) are
Copyright (c) 2003-2005 by Peter Astrand <astrand@lysator.liu.se> and were
licensed to the Python Software foundation under a Contributor Agreement.
"""
import io
import os
import re
import sys
import shlex
import signal
import inspect
import builtins

from threading import Thread
from subprocess import Popen, PIPE, DEVNULL, STDOUT
from collections import Sequence

from xonsh.tools import redirect_stdout, redirect_stderr, ON_WINDOWS,\
                        ON_POSIX, XonshError, string_types, suggest_commands

if ON_WINDOWS:
    import _winapi
    import msvcrt

    class Handle(int):
        closed = False

        def Close(self, CloseHandle=_winapi.CloseHandle):
            if not self.closed:
                self.closed = True
                CloseHandle(self)

        def Detach(self):
            if not self.closed:
                self.closed = True
                return int(self)
            raise ValueError("already closed")

        def __repr__(self):
            return "Handle(%d)" % int(self)

        __del__ = Close
        __str__ = __repr__


class ProcProxy(Thread):
    """
    Class representing a function to be run as a subprocess-mode command.
    """
    def __init__(self, f, args,
                 stdin=None,
                 stdout=None,
                 stderr=None,
                 universal_newlines=False):
        """Parameters
        ----------
        f : function
            The function to be executed.
        args : list
            A (possibly empty) list containing the arguments that were given on
            the command line
        stdin : file-like, optional
            A file-like object representing stdin (input can be read from
            here).  If `stdin` is not provided or if it is explicitly set to
            `None`, then an instance of `io.StringIO` representing an empty
            file is used.
        stdout : file-like, optional
            A file-like object representing stdout (normal output can be
            written here).  If `stdout` is not provided or if it is explicitly
            set to `None`, then `sys.stdout` is used.
        stderr : file-like, optional
            A file-like object representing stderr (error output can be
            written here).  If `stderr` is not provided or if it is explicitly
            set to `None`, then `sys.stderr` is used.
        """
        self.f = f
        """
        The function to be executed.  It should be a function of four
        arguments, described below.

        Parameters
        ----------
        args : list
            A (possibly empty) list containing the arguments that were given on
            the command line
        stdin : file-like
            A file-like object representing stdin (input can be read from
            here).
        stdout : file-like
            A file-like object representing stdout (normal output can be
            written here).
        stderr : file-like
            A file-like object representing stderr (error output can be
            written here).
        """
        self.args = args
        self.pid = None
        self.returncode = None
        self.wait = self.join
        self.uninew = universal_newlines

        handles = self._get_handles(stdin, stdout, stderr)
        (self.p2cread, self.p2cwrite,
         self.c2pread, self.c2pwrite,
         self.errread, self.errwrite) = handles

        # default values
        self.stdin = stdin
        self.stdout = None
        self.stderr = None

        if ON_WINDOWS:
            if self.p2cwrite != -1:
                self.p2cwrite = msvcrt.open_osfhandle(self.p2cwrite.Detach(), 0)
            if self.c2pread != -1:
                self.c2pread = msvcrt.open_osfhandle(self.c2pread.Detach(), 0)
            if self.errread != -1:
                self.errread = msvcrt.open_osfhandle(self.errread.Detach(), 0)

        if self.p2cwrite != -1:
            self.stdin = io.open(self.p2cwrite, 'wb', -1)
            if universal_newlines:
                self.stdin = io.TextIOWrapper(self.stdin, write_through=True,
                                              line_buffering=False)
        if self.c2pread != -1:
            self.stdout = io.open(self.c2pread, 'rb', -1)
            if universal_newlines:
                self.stdout = io.TextIOWrapper(self.stdout)

        if self.errread != -1:
            self.stderr = io.open(self.errread, 'rb', -1)
            if universal_newlines:
                self.stderr = io.TextIOWrapper(self.stderr)

        Thread.__init__(self)
        self.start()

    def run(self):
        """Set up input/output streams and execute the child function in a new
        thread.  This is part of the `threading.Thread` interface and should
        not be called directly."""
        if self.f is None:
            return
        if self.stdin is not None:
            sp_stdin = io.TextIOWrapper(self.stdin)
        else:
            sp_stdin = io.StringIO("")

        if ON_WINDOWS:
            if self.c2pwrite != -1:
                self.c2pwrite = msvcrt.open_osfhandle(self.c2pwrite.Detach(), 0)
            if self.errwrite != -1:
                self.errwrite = msvcrt.open_osfhandle(self.errwrite.Detach(), 0)

        if self.c2pwrite != -1:
            sp_stdout = io.TextIOWrapper(io.open(self.c2pwrite, 'wb', -1))
        else:
            sp_stdout = sys.stdout
        if self.errwrite == self.c2pwrite:
            sp_stderr = sp_stdout
        elif self.errwrite != -1:
            sp_stderr = io.TextIOWrapper(io.open(self.errwrite, 'wb', -1))
        else:
            sp_stderr = sys.stderr

        r = self.f(self.args, sp_stdin, sp_stdout, sp_stderr)
        self.returncode = r if r is not None else True

    def poll(self):
        """Check if the function has completed.

        :return: `None` if the function is still executing, `True` if the
                 function finished successfully, and `False` if there was an
                 error
        """
        return self.returncode

    # The code below (_get_devnull, _get_handles, and _make_inheritable) comes
    # from subprocess.py in the Python 3.4.2 Standard Library
    def _get_devnull(self):
        if not hasattr(self, '_devnull'):
            self._devnull = os.open(os.devnull, os.O_RDWR)
        return self._devnull

    if ON_WINDOWS:
        def _make_inheritable(self, handle):
            """Return a duplicate of handle, which is inheritable"""
            h = _winapi.DuplicateHandle(
                _winapi.GetCurrentProcess(), handle,
                _winapi.GetCurrentProcess(), 0, 1,
                _winapi.DUPLICATE_SAME_ACCESS)
            return Handle(h)


        def _get_handles(self, stdin, stdout, stderr):
            """Construct and return tuple with IO objects:
            p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite
            """
            if stdin is None and stdout is None and stderr is None:
                return (-1, -1, -1, -1, -1, -1)

            p2cread, p2cwrite = -1, -1
            c2pread, c2pwrite = -1, -1
            errread, errwrite = -1, -1

            if stdin is None:
                p2cread = _winapi.GetStdHandle(_winapi.STD_INPUT_HANDLE)
                if p2cread is None:
                    p2cread, _ = _winapi.CreatePipe(None, 0)
                    p2cread = Handle(p2cread)
                    _winapi.CloseHandle(_)
            elif stdin == PIPE:
                p2cread, p2cwrite = _winapi.CreatePipe(None, 0)
                p2cread, p2cwrite = Handle(p2cread), Handle(p2cwrite)
            elif stdin == DEVNULL:
                p2cread = msvcrt.get_osfhandle(self._get_devnull())
            elif isinstance(stdin, int):
                p2cread = msvcrt.get_osfhandle(stdin)
            else:
                # Assuming file-like object
                p2cread = msvcrt.get_osfhandle(stdin.fileno())
            p2cread = self._make_inheritable(p2cread)

            if stdout is None:
                c2pwrite = _winapi.GetStdHandle(_winapi.STD_OUTPUT_HANDLE)
                if c2pwrite is None:
                    _, c2pwrite = _winapi.CreatePipe(None, 0)
                    c2pwrite = Handle(c2pwrite)
                    _winapi.CloseHandle(_)
            elif stdout == PIPE:
                c2pread, c2pwrite = _winapi.CreatePipe(None, 0)
                c2pread, c2pwrite = Handle(c2pread), Handle(c2pwrite)
            elif stdout == DEVNULL:
                c2pwrite = msvcrt.get_osfhandle(self._get_devnull())
            elif isinstance(stdout, int):
                c2pwrite = msvcrt.get_osfhandle(stdout)
            else:
                # Assuming file-like object
                c2pwrite = msvcrt.get_osfhandle(stdout.fileno())
            c2pwrite = self._make_inheritable(c2pwrite)

            if stderr is None:
                errwrite = _winapi.GetStdHandle(_winapi.STD_ERROR_HANDLE)
                if errwrite is None:
                    _, errwrite = _winapi.CreatePipe(None, 0)
                    errwrite = Handle(errwrite)
                    _winapi.CloseHandle(_)
            elif stderr == PIPE:
                errread, errwrite = _winapi.CreatePipe(None, 0)
                errread, errwrite = Handle(errread), Handle(errwrite)
            elif stderr == STDOUT:
                errwrite = c2pwrite
            elif stderr == DEVNULL:
                errwrite = msvcrt.get_osfhandle(self._get_devnull())
            elif isinstance(stderr, int):
                errwrite = msvcrt.get_osfhandle(stderr)
            else:
                # Assuming file-like object
                errwrite = msvcrt.get_osfhandle(stderr.fileno())
            errwrite = self._make_inheritable(errwrite)

            return (p2cread, p2cwrite,
                    c2pread, c2pwrite,
                    errread, errwrite)


    else:
        # POSIX versions
        def _get_handles(self, stdin, stdout, stderr):
            """Construct and return tuple with IO objects:
            p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite
            """
            p2cread, p2cwrite = -1, -1
            c2pread, c2pwrite = -1, -1
            errread, errwrite = -1, -1

            if stdin is None:
                pass
            elif stdin == PIPE:
                p2cread, p2cwrite = os.pipe()
            elif stdin == DEVNULL:
                p2cread = self._get_devnull()
            elif isinstance(stdin, int):
                p2cread = stdin
            else:
                # Assuming file-like object
                p2cread = stdin.fileno()

            if stdout is None:
                pass
            elif stdout == PIPE:
                c2pread, c2pwrite = os.pipe()
            elif stdout == DEVNULL:
                c2pwrite = self._get_devnull()
            elif isinstance(stdout, int):
                c2pwrite = stdout
            else:
                # Assuming file-like object
                c2pwrite = stdout.fileno()

            if stderr is None:
                pass
            elif stderr == PIPE:
                errread, errwrite = os.pipe()
            elif stderr == STDOUT:
                errwrite = c2pwrite
            elif stderr == DEVNULL:
                errwrite = self._get_devnull()
            elif isinstance(stderr, int):
                errwrite = stderr
            else:
                # Assuming file-like object
                errwrite = stderr.fileno()

            return (p2cread, p2cwrite,
                    c2pread, c2pwrite,
                    errread, errwrite)


class SimpleProcProxy(ProcProxy):
    """
    Variant of `ProcProxy` for simpler functions.

    The function passed into the initializer for `SimpleProcProxy` should have
    the form described in the xonsh tutorial.  This function is then wrapped to
    make a new function of the form expected by `ProcProxy`.
    """
    def __init__(self, f, args, stdin=None, stdout=None, stderr=None,
                 universal_newlines=False):
        def wrapped_simple_command(args, stdin, stdout, stderr):
            try:
                i = stdin.read()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    r = f(args, i)
                if isinstance(r, str):
                    stdout.write(r)
                elif isinstance(r, Sequence):
                    if r[0] is not None:
                        stdout.write(r[0])
                    if r[1] is not None:
                        stderr.write(r[1])
                elif r is not None:
                    stdout.write(str(r))
                return True
            except:
                return False
        super().__init__(wrapped_simple_command,
                         args, stdin, stdout, stderr,
                         universal_newlines)

_REDIR_NAME = "(o(?:ut)?|e(?:rr)?|a(?:ll)?|&?\d?)"
_REDIR_REGEX = re.compile("{r}(>?>|<){r}$".format(r=_REDIR_NAME))
_MODES = {'>>': 'a', '>': 'w', '<': 'r'}
_WRITE_MODES = frozenset({'w', 'a'})
_REDIR_ALL = frozenset({'&', 'a', 'all'})
_REDIR_ERR = frozenset({'2', 'e', 'err'})
_REDIR_OUT = frozenset({'', '1', 'o', 'out'})
_E2O_MAP = frozenset({'{}>{}'.format(e, o)
                      for e in _REDIR_ERR
                      for o in _REDIR_OUT
                      if o != ''})


def _is_redirect(x):
    return isinstance(x, str) and _REDIR_REGEX.match(x)


def _open(fname, mode):
    # file descriptors
    if isinstance(fname, int):
        return fname
    try:
        return open(fname, mode)
    except:
        raise XonshError('xonsh: {0}: no such file or directory'.format(fname))


def _redirect_io(streams, r, loc=None):
    # special case of redirecting stderr to stdout
    if r.replace('&', '') in _E2O_MAP:
        if 'stderr' in streams:
            raise XonshError('Multiple redirects for stderr')
        streams['stderr'] = STDOUT
        return

    orig, mode, dest = _REDIR_REGEX.match(r).groups()

    # redirect to fd
    if dest.startswith('&'):
        try:
            dest = int(dest[1:])
            if loc is None:
                loc, dest = dest, ''
            else:
                e = 'Unrecognized redirection command: {}'.format(r)
                raise XonshError(e)
        except (ValueError, XonshError):
            raise
        except:
            pass

    mode = _MODES.get(mode, None)

    if mode == 'r':
        if len(orig) > 0 or len(dest) > 0:
            raise XonshError('Unrecognized redirection command: {}'.format(r))
        elif 'stdin' in streams:
            raise XonshError('Multiple inputs for stdin')
        else:
            streams['stdin'] = _open(loc, mode)
    elif mode in _WRITE_MODES:
        if orig in _REDIR_ALL:
            if 'stderr' in streams:
                raise XonshError('Multiple redirects for stderr')
            elif 'stdout' in streams:
                raise XonshError('Multiple redirects for stdout')
            elif len(dest) > 0:
                e = 'Unrecognized redirection command: {}'.format(r)
                raise XonshError(e)
            targets = ['stdout', 'stderr']
        elif orig in _REDIR_ERR:
            if 'stderr' in streams:
                raise XonshError('Multiple redirects for stderr')
            elif len(dest) > 0:
                e = 'Unrecognized redirection command: {}'.format(r)
                raise XonshError(e)
            targets = ['stderr']
        elif orig in _REDIR_OUT:
            if 'stdout' in streams:
                raise XonshError('Multiple redirects for stdout')
            elif len(dest) > 0:
                e = 'Unrecognized redirection command: {}'.format(r)
                raise XonshError(e)
            targets = ['stdout']
        else:
            raise XonshError('Unrecognized redirection command: {}'.format(r))

        f = _open(loc, mode)
        for t in targets:
            streams[t] = f

    else:
        raise XonshError('Unrecognized redirection command: {}'.format(r))

RE_SHEBANG = re.compile(r'#![ \t]*(.+?)$')


def _get_runnable_name(fname):
    if os.path.isfile(fname) and fname != os.path.basename(fname):
        return fname
    for d in builtins.__xonsh_env__['PATH']:
        if os.path.isdir(d):
            files = os.listdir(d)
            if fname in files:
                return os.path.join(d, fname)
            if ON_WINDOWS:
                PATHEXT = builtins.__xonsh_env__.get('PATHEXT', [])
                for dirfile in files:
                    froot, ext = os.path.splitext(dirfile)
                    if fname == froot and ext.upper() in PATHEXT:
                        return os.path.join(d, dirfile)
    return None


def _is_binary(fname, limit=80):
    with open(fname, 'rb') as f:
        for i in range(limit):
            char = f.read(1)
            if char == b'\0':
                return True
            if char == b'\n':
                return False
            if char == b'':
                return False
    return False


def _un_shebang(x):
    if x == '/usr/bin/env':
        return []
    elif any(x.startswith(i) for i in ['/usr/bin', '/usr/local/bin', '/bin']):
        x = os.path.basename(x)
    elif x.endswith('python') or x.endswith('python.exe'):
        x = 'python'
    if x == 'xonsh':
        return ['python', '-m', 'xonsh.main']
    return [x]


def get_script_subproc_command(fname, args):
    """
    Given the name of a script outside the path, returns a list representing
    an appropriate subprocess command to execute the script.  Raises
    PermissionError if the script is not executable.
    """
    # make sure file is executable
    if not os.access(fname, os.X_OK):
        raise PermissionError

    # if the file is a binary, we should call it directly
    if _is_binary(fname):
        return [fname] + args

    if ON_WINDOWS:
        # Windows can execute various filetypes directly
        # as given in PATHEXT
        _, ext = os.path.splitext(fname)
        if ext.upper() in builtins.__xonsh_env__.get('PATHEXT', []):
            return [fname] + args

    # find interpreter
    with open(fname, 'rb') as f:
        first_line = f.readline().decode().strip()
    m = RE_SHEBANG.match(first_line)

    # xonsh is the default interpreter
    if m is None:
        interp = ['xonsh']
    else:
        interp = m.group(1).strip()
        if len(interp) > 0:
            interp = shlex.split(interp)
        else:
            interp = ['xonsh']

    if ON_WINDOWS:
        o = []
        for i in interp:
            o.extend(_un_shebang(i))
        interp = o

    return interp + [fname] + args


def _subproc_pre():
    os.setpgrp()
    signal.signal(signal.SIGTSTP, lambda n, f: signal.pause())



def get_proc(cmd, prev_proc=None, uninew=False, captured=False):
    """
    Given a list representing a single command, return a Popen or ProcProxy
    object representing that command.
    """
    ENV = builtins.__xonsh_env__
    typ = cmd[0]
    if typ == 'cmd':
        cmd = cmd[1]
    else:
        cmd = cmd[1:]
    stdin = None
    stdout = None
    stderr = None
    if isinstance(cmd, string_types):
        return
    streams = {}
    while True:
        if len(cmd) >= 3 and _is_redirect(cmd[-2]):
            _redirect_io(streams, cmd[-2], cmd[-1])
            cmd = cmd[:-2]
        elif len(cmd) >= 2 and _is_redirect(cmd[-1]):
            _redirect_io(streams, cmd[-1])
            cmd = cmd[:-1]
        elif len(cmd) >= 3 and cmd[0] == '<':
            _redirect_io(streams, cmd[0], cmd[1])
            cmd = cmd[2:]
        else:
            break
    # set standard input
    if 'stdin' in streams:
        if prev_proc is not None:
            raise XonshError('Multiple inputs for stdin')
        stdin = streams['stdin']
    elif prev_proc is not None:
        stdin = prev_proc.stdout
    # set standard output
    if 'stdout' in streams:
        if not uninew:
            raise XonshError('Multiple redirects for stdout')
        stdout = streams['stdout']
    elif captured or not uninew:
        stdout = PIPE
    else:
        stdout = None
    # set standard error
    if 'stderr' in streams:
        stderr = streams['stderr']

    if typ == 'and':
        proc = AndProc(cmd[0], cmd[1], stdin, stdout, stderr, uninew, captured, prev_proc)
    elif typ == 'or':
        proc = OrProc(cmd[0], cmd[1], stdin, stdout, stderr, uninew, captured, prev_proc)
    else:
        alias = builtins.aliases.get(cmd[0], None)
        if callable(alias):
            aliased_cmd = alias
        else:
            if alias is not None:
                cmd = alias + cmd[1:]
            n = _get_runnable_name(cmd[0])
            if n is None:
                aliased_cmd = cmd
            else:
                try:
                    aliased_cmd = get_script_subproc_command(n, cmd[1:])
                except PermissionError:
                    e = 'xonsh: subprocess mode: permission denied: {0}'
                    raise XonshError(e.format(cmd[0]))
        if callable(aliased_cmd):
            numargs = len(inspect.signature(aliased_cmd).parameters)
            if numargs == 2:
                cls = SimpleProcProxy
            elif numargs == 4:
                cls = ProcProxy
            else:
                e = 'Expected callable with 2 or 4 arguments, not {}'
                raise XonshError(e.format(numargs))
            proc = cls(aliased_cmd, cmd[1:],
                       stdin, stdout, stderr,
                       universal_newlines=uninew)
        else:
            subproc_kwargs = {}
            if ON_POSIX:
                subproc_kwargs['preexec_fn'] = _subproc_pre
            try:
                proc = Popen(aliased_cmd,
                             universal_newlines=uninew,
                             env=ENV.detype(),
                             stdin=stdin,
                             stdout=stdout,
                             stderr=stderr,
                             **subproc_kwargs)
            except PermissionError:
                e = 'xonsh: subprocess mode: permission denied: {0}'
                raise XonshError(e.format(aliased_cmd[0]))
            except FileNotFoundError:
                cmd = aliased_cmd[0]
                e = 'xonsh: subprocess mode: command not found: {0}'.format(cmd)
                sug = suggest_commands(cmd, ENV, builtins.aliases)
                if len(sug.strip()) > 0:
                    e += '\n' + suggest_commands(cmd, ENV, builtins.aliases)
                raise XonshError(e)
    return proc

def get_return_code(p):
    p = p.returncode
    if isinstance(p, bool):
        return p
    return p == 0

class AndProc(ProcProxy):
    def __init__(self, cmd1, cmd2, stdin, stdout, stderr, uninew, captured, prev_proc):
        self.cmd1 = cmd1
        self.cmd2 = cmd2
        self.captured = captured
        self.prev_proc = prev_proc
        super().__init__(None, [], stdin, stdout, stderr, uninew)

    def run(self):
        o = get_proc(self.cmd1, self.prev_proc, self.uninew, self.captured)
        o.wait()
        if not get_return_code(o):
            self.returncode = False
            return
        o = get_proc(self.cmd2, self.prev_proc, self.uninew, self.captured)
        o.wait()
        self.returncode = get_return_code(o)


class OrProc(ProcProxy):
    def __init__(self, cmd1, cmd2, stdin, stdout, stderr, uninew, captured, prev_proc):
        self.cmd1 = cmd1
        self.cmd2 = cmd2
        self.captured = captured
        self.prev_proc = prev_proc
        super().__init__(None, [], stdin, stdout, stderr, uninew)

    def run(self):
        o = get_proc(self.cmd1, self.prev_proc, self.uninew, self.captured)
        o.wait()
        if get_return_code(o):
            self.returncode = True
            return
        o = get_proc(self.cmd2, self.prev_proc, self.uninew, self.captured)
        o.wait()
        self.returncode = get_return_code(o)
