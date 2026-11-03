"""Copy one stopped agent's workspace through a network-disabled helper."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tarfile


def validate(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected the agent's existing real workspace directory")
    for p in root.rglob('*'):
        mode = p.lstat().st_mode
        if stat.S_ISLNK(mode):
            if not p.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Workspace external symlink needs migration planning: {p.relative_to(root)}")
        elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"Workspace special file needs migration planning: {p.relative_to(root)}")
    return root


def digest(root):
    root = Path(root)
    h = hashlib.sha256()
    count = 0
    for p in sorted(root.rglob('*')):
        h.update(str(p.relative_to(root)).encode() + b'\0')
        if p.is_symlink():
            h.update(b'L' + os.fsencode(os.readlink(p)))
        elif p.is_dir():
            h.update(b'D')
        else:
            h.update(b'F')
            content = hashlib.sha256()
            with p.open('rb') as f:
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    content.update(block)
            h.update(content.digest())
        h.update(b'\0')
        count += 1
    return {'sha256': h.hexdigest(), 'entries': count}


def receive(uid):
    root = Path('/workspace')
    if any(root.iterdir()):
        raise ValueError('Refusing to overwrite an existing isolated workspace')
    os.chown(root, 0, 0)
    root.chmod(0o700)
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
        archive.extractall(root, filter='data')
    validate(root)
    # Compute before ownership transfer: root has no DAC override and cannot
    # walk the worker's private mode-0700 directories after chown.
    copied = digest(root)
    for base, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in files + dirs:
            os.chown(Path(base) / name, uid, uid, follow_symlinks=False)
    os.chown(root, uid, uid)
    print(json.dumps(copied))


if __name__ == '__main__':
    receive(int(sys.argv[1]))
