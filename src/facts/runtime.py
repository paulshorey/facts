"""Single-host writer exclusion for supported collection and backup entrypoints."""
import contextlib
import fcntl
from functools import wraps
import os
from pathlib import Path
import threading

ROOT = Path(__file__).resolve().parents[2]
_held = {}
_guard = threading.RLock()


@contextlib.contextmanager
def writer_lock(root=ROOT):
    root = Path(root).resolve()
    owner = (os.getpid(), threading.get_ident())
    key = (root, owner)
    with _guard:
        if key in _held:
            _held[key][1] += 1
        else:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            storage = root/'storage'; storage.mkdir(exist_ok=True, mode=0o700)
            folder = storage/'locks'; folder.mkdir(exist_ok=True, mode=0o700)
            stream = (folder/'collector.lock').open('a')
            try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
                raise RuntimeError('Another collector/backup writer owns this root') from None
            _held[key] = [stream, 1]
    try: yield
    finally:
        with _guard:
            _held[key][1] -= 1
            if _held[key][1] == 0:
                stream, _ = _held.pop(key)
                fcntl.flock(stream, fcntl.LOCK_UN); stream.close()


def exclusive_writer(function):
    @wraps(function)
    def run(*args, **kwargs):
        if kwargs.get('dry_run'): return function(*args, **kwargs)
        first = args[0] if args else ROOT
        root = kwargs.get('root', first if isinstance(first, (str, os.PathLike)) else getattr(first, 'root', ROOT))
        if not isinstance(root, (str, os.PathLike)): root = ROOT
        with writer_lock(root): return function(*args, **kwargs)
    return run
