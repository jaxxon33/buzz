"""Deliver the host's shared base prompt read-only, so edits need only a restart.

The host directory holds exactly one real file. A directory (not single-file)
bind keeps seeing the file after an atomic replace-by-rename on the host.
"""
import os
import stat
import sys
from pathlib import Path

SOURCE = Path('/home/paul/.config/buzz-shared-prompt')
TARGET = '/opt/buzz/shared'
FILE = 'base-prompt.md'


def launch_args():
    """Return the mount and prompt path, or nothing (baked prompt) when unusable."""
    try:
        directory = os.lstat(SOURCE).st_mode
        if not stat.S_ISDIR(directory):
            raise OSError('source is not a real directory')
        # Workers run as their own UID, so only the "other" bits grant access.
        if directory & (stat.S_IROTH | stat.S_IXOTH) != stat.S_IROTH | stat.S_IXOTH:
            raise OSError('source directory is not readable by worker UIDs')
        # A symlink would dangle inside the container; require the real file.
        info = os.lstat(SOURCE / FILE)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise OSError('prompt is not a non-empty regular file')
        if not info.st_mode & stat.S_IROTH:
            raise OSError('prompt is not readable by worker UIDs')
    except OSError as error:
        print(f'warning: shared prompt unavailable ({error}); using the baked prompt',
              file=sys.stderr)
        return []
    return ['--mount', f'type=bind,source={SOURCE},target={TARGET},readonly',
            '--env', f'BUZZ_ACP_BASE_PROMPT_FILE={TARGET}/{FILE}']


def audit_mounts(mounts):
    """Accept at most the exact read-only prompt bind and return all other mounts."""
    remaining = []
    found = False
    for mount in mounts:
        if mount['Destination'] != TARGET:
            remaining.append(mount)
            continue
        if found or (mount['Type'], mount['RW'], mount.get('Source')) != ('bind', False, str(SOURCE)):
            raise ValueError('Unexpected shared prompt mount authority')
        found = True
    return remaining
