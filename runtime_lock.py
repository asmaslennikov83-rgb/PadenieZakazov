"""OS-held instance lock. A stale lock file does not block restart after a crash."""
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def instance_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    file=path.open('a+b')
    try:
        if file.seek(0,2)==0:file.write(b'0');file.flush()
        file.seek(0)
        try:
            try:
                import fcntl
                fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except ImportError:
                import msvcrt
                msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
        except OSError as exc:raise RuntimeError('Другой экземпляр бота уже использует эту папку data') from exc
        yield
    finally:
        file.close()
