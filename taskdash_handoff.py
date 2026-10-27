"""Hand the task account its sign-in without runas.

Driver: `taskdash signin --for-task-account --apply` writes the file; the next `taskdash collect --apply`
        run reads it (taskdash_publish calls consume_handoff before it connects)
Source: the ArcGIS password and, when smtp.user is set, the mail password, typed at hidden prompts
Target: one file, taskdash_handoff.bin, in the install folder. Both secrets are sealed with DPAPI at
        machine scope, and the file's ACL allows only the task account, SYSTEM and Administrators.
        The collector run as the task account saves them into its own ArcGIS profile and keyring
        entry, checks that the profile signs in, and only then deletes the file.

Machine scope is needed because the admin seals the file and another account opens it. It also means
any process on this computer could decrypt the bytes, so the ACL is the control that keeps them out.
The file never holds plaintext, and nothing here prints or logs a secret.
"""

from __future__ import annotations

import getpass
import json
from pathlib import Path
from typing import Any, Dict

from taskdash_common import (
    SMTP_KEYRING_SERVICE, disable_arcpy_import, emit, normalize_text, runtime_dir, see_troubleshooting,
)

HANDOFF_FILE = "taskdash_handoff.bin"
# wincrypt.h and winnt.h values. pywin32's win32crypt and ntsecuritycon do not export all of them.
CRYPTPROTECT_LOCAL_MACHINE = 0x4
FILE_ALL_ACCESS = 0x1F01FF
FILE_GENERIC_READ = 0x120089
DELETE = 0x10000
WRITE_DAC = 0x40000
GENERIC_WRITE = 0x40000000
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x80
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"


class StoreError(Exception):
    """A failure whose text this module wrote, so it is safe to print."""


def handoff_path() -> Path:
    return runtime_dir() / HANDOFF_FILE


def seal(payload: Dict[str, Any]) -> bytes:
    import win32crypt
    return win32crypt.CryptProtectData(json.dumps(payload).encode("utf-8"), "TaskDash hand-off",
                                       None, None, None, CRYPTPROTECT_LOCAL_MACHINE)


def unseal(blob: bytes) -> Dict[str, Any]:
    import win32crypt
    return json.loads(win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8"))


def restrict(handle: Any, account: str) -> None:
    """Replace the ACL of the open file: the task account (read, delete), SYSTEM and Administrators,
    nobody else."""
    import win32security
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, FILE_GENERIC_READ | DELETE,
                            win32security.LookupAccountName(None, account)[0])
    for sid in (SYSTEM_SID, ADMINISTRATORS_SID):
        acl.AddAccessAllowedAce(win32security.ACL_REVISION, FILE_ALL_ACCESS,
                                win32security.ConvertStringSidToSid(sid))
    # PROTECTED stops the folder's entries flowing in, so only the three entries above remain.
    win32security.SetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None)


def write_handoff(payload: Dict[str, Any], account: str) -> Path:
    """Create the file empty, restrict it, then write the sealed bytes. A new file takes the install
    folder's ACL, and machine-scope DPAPI lets any local process decrypt what it reads, so no secret
    may exist while that ACL applies. Windows checks access only when a handle opens, so a handle that
    another principal opened while the file was empty would keep read access after the ACL changed.
    The writer therefore creates the file with share mode 0, which refuses every other handle, and
    replaces the ACL through its own handle. That handle also keeps this writer's access if the
    restricted ACL leaves it none. A file that could not be restricted is removed, never left behind."""
    import win32file
    path = handoff_path()
    sealed = seal(payload)
    path.unlink(missing_ok=True)
    handle = win32file.CreateFile(str(path), GENERIC_WRITE | WRITE_DAC, 0, None, CREATE_NEW,
                                  FILE_ATTRIBUTE_NORMAL, None)
    try:
        try:
            restrict(handle, account)
            win32file.WriteFile(handle, sealed)
        finally:
            handle.Close()
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _bare_name(account: str) -> str:
    """'DOMAIN\\name' and 'name@domain' both become 'name', so the same account compares equal."""
    return account.replace("/", "\\").rsplit("\\", 1)[-1].split("@")[0].lower()


def _store(payload: Dict[str, Any]) -> None:
    """Save into this account's own stores the way `taskdash signin --apply` does, then prove it."""
    from taskdash_preflight import OK
    from taskdash_signin import _check_saved, save_profile
    # connect_agol installs this guard, but the collector and the --as-task preflight call this first.
    # The first arcgis import (ProfileManager) would otherwise probe arcpy under the task's logon.
    disable_arcpy_import()
    save_profile(payload["profile"], payload["url"], payload["username"], payload["password"])
    if payload.get("smtp_user") and payload.get("smtp_password"):
        try:
            import keyring
        except ImportError:
            raise StoreError("the mail password needs the keyring package; install the smtp extra as "
                             "step 1 of docs/INSTALL.md describes") from None
        keyring.set_password(SMTP_KEYRING_SERVICE, payload["smtp_user"], payload["smtp_password"])
    status, message = _check_saved(payload["url"], payload["profile"])
    if status != OK:
        raise StoreError(message)


def consume_handoff() -> None:
    """Store the hand-off sign-in for the account that runs this, then delete the file.

    No file is the normal case and costs one stat. A failure keeps the file and raises SystemExit,
    so the run fails the way a missing sign-in does. The messages carry an exception's type and
    never its text, because that text can quote what was sent."""
    path = handoff_path()
    if not path.is_file():
        return
    try:
        sealed = path.read_bytes()
    except PermissionError:
        # The ACL names only the task account, SYSTEM and Administrators, and the Administrators SID is
        # deny-only in a non-elevated window. So a refused read means the file is for another account.
        emit(f"WARNING: {path.name} is for another account, which this account cannot read, so this run "
             "left it alone.")
        return
    try:
        payload = unseal(sealed)
        account = normalize_text(payload["account"])
    except Exception as exc:  # noqa: BLE001 - a wrong-machine or damaged file is one failure to the user
        raise SystemExit(
            "The hand-off file could not be opened. It is kept. Make it again on this computer with "
            "taskdash signin --for-task-account --apply, or store the sign-in with runas as step 3 of "
            f"docs/INSTALL.md describes. Detail: {type(exc).__name__}\n"
            + see_troubleshooting("The hand-off file could not be opened")) from None
    if _bare_name(account) != _bare_name(getpass.getuser()):
        emit(f"WARNING: {path.name} is for {account}, so this run left it alone.")
        return
    try:
        _store(payload)
    except Exception as exc:  # noqa: BLE001 - the file stays, and the error goes to the log as text
        detail = str(exc) if isinstance(exc, StoreError) else type(exc).__name__
        raise SystemExit(
            "The hand-off sign-in was kept, not deleted, because storing or checking it failed. Fix the "
            "cause, then run the collector again. If the ArcGIS password is wrong, run taskdash signin "
            f"--for-task-account --apply again. Detail: {detail}\n"
            + see_troubleshooting("The hand-off sign-in was kept")) from None
    try:
        path.unlink()
    except OSError as exc:
        emit(f"WARNING: stored the sign-in, but could not delete {path.name} ({type(exc).__name__}). "
             "Delete it by hand; it holds the passwords sealed, not in plain text.")
        return
    emit(f"Stored the sign-in from {path.name} for {account} and removed the file.")
