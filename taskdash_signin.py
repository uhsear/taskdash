"""Store the ArcGIS sign-in profile that TaskDash reads, and check the account first.

Driver: taskdash signin [--apply] (run by hand, once as you and once as the task account)
Source: the config's arcgis.url, arcgis.profile and arcgis.username; the password from a getpass prompt
Target: with --apply, one ArcGIS profile (password in the Windows Credential Manager of the account
        that runs the command). Without --apply it signs in and checks, and saves nothing.

    taskdash signin            # check: sign in, check the account and its privileges, save nothing
    taskdash signin --apply    # the same checks, then save the profile and prove it signs in
    taskdash signin --for-task-account --apply   # after the checks, seal the sign-in for the task
                                                 # account instead (see taskdash_handoff.py)

The password is never an argument, never printed and never logged. Saving uses the same call the
install guide used before this command: ProfileManager().create(profile, url, user, password).
"""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from taskdash_common import (
    DEFAULT_CONFIG_FILE, TASKDASH_VERSION, _open_gis, disable_arcpy_import, emit, load_config, normalize_text,
    runtime_dir, see_troubleshooting,
)
from taskdash_handoff import HANDOFF_FILE, write_handoff
from taskdash_preflight import FAIL, MARKS, OK, Context, check_privileges

ONLINE_URL = "https://www.arcgis.com"
BUILT_IN_ONLY =("The account must be a built-in ArcGIS account. An account that signs in through "
                 "single sign-on (SAML) or needs multifactor authentication cannot be used today: "
                 "see 'The sign-in command fails' in docs/TROUBLESHOOTING.md.")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="taskdash signin", allow_abbrev=False,
        description="Sign in to ArcGIS, check the account, and store the sign-in profile. "
                    "Saves nothing unless you pass --apply.")
    parser.add_argument("--config", default="", metavar="PATH",
                        help="Config file. Default: the config next to the scripts.")
    parser.add_argument("--apply", action="store_true", help="Save the profile.")
    parser.add_argument("--for-task-account", action="store_true",
                        help="Seal the sign-in into a hand-off file that the task account stores on its "
                             "next collector run, instead of saving a profile for you. Needs no runas.")
    parser.add_argument("--task-user", default="", metavar="DOMAIN\\NAME",
                        help="With --for-task-account, the task account. Default: the account of the "
                             "collector task.")
    parser.add_argument("--version", action="version", version=f"TaskDash {TASKDASH_VERSION}")
    args = parser.parse_args(argv)
    if args.task_user and not args.for_task_account:
        parser.error("--task-user needs --for-task-account")
    return args


def save_profile(profile: str, url: str, user: str, password: str) -> None:
    """The one call INSTALL step 3 made by hand. It replaces a profile of the same name."""
    from arcgis.gis import ProfileManager
    ProfileManager().create(profile, url, user, password)


def _report(name: str, status: str, message: str) -> bool:
    emit(f"{MARKS[status]} {name}: {message}")
    return status == OK


def _sign_in(url: str, user: str, password: str) -> Tuple[Optional[Any], str]:
    """(gis, '') or (None, why). The text is cut and the password removed, because an ArcGIS
    error can quote what was sent."""
    try:
        return _open_gis(url, user, password), ""
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        text = (text.replace(password, "***") if password else text)[:200]
        return None, (f"{text}. The user name or password may be wrong; check them and run again. "
                      f"{BUILT_IN_ONLY}")


def _check_account(gis: Any) -> Tuple[str, str]:
    if gis.users.me is None:
        return FAIL, ("ArcGIS signed in but users.me is empty. Check the user name and password. "
                      + BUILT_IN_ONLY)
    return OK, f"signed in as {gis.users.me.username}"


def _check_saved(url: str, profile: str) -> Tuple[str, str]:
    try:
        gis = _open_gis(url, profile=profile)
    except Exception as exc:
        return FAIL, f"saved, but the profile does not sign in: {type(exc).__name__}. Run the command again."
    if gis.users.me is None:
        return FAIL, "saved, but the profile does not sign in. Run the command again."
    return OK, f"profile '{profile}' saved and signs in as {gis.users.me.username}"


def _settings(config: Dict[str, Any]) -> Tuple[str, str, str]:
    agol = config.get("arcgis", {})
    profile = normalize_text(agol.get("profile"))
    if not profile:
        raise SystemExit("arcgis.profile is empty, so there is no name to store the sign-in under. "
                         "Set arcgis.profile to taskdash in the config, then run again.\n"
                         + see_troubleshooting("arcgis.profile is empty"))
    url = normalize_text(agol.get("url"))
    if not url:
        url = normalize_text(input(f"ArcGIS URL [{ONLINE_URL}]: ")) or ONLINE_URL
    default_user = normalize_text(agol.get("username"))
    suffix = f" [{default_user}]" if default_user else ""
    user = normalize_text(input(f"ArcGIS user name{suffix}: ")) or default_user
    if not url or not user:
        raise SystemExit("There is no ArcGIS URL or no ArcGIS user name to sign in with. Type them at "
                         "the prompts, or set arcgis.url and arcgis.username in the config.\n"
                         + see_troubleshooting("There is no URL or user name"))
    return url, user, profile


def org_url(gis: Any) -> str:
    """The organization's own URL, https://<urlKey>.<customBaseUrl>, or '' when the portal has no urlKey
    (an ArcGIS Enterprise portal)."""
    props = getattr(gis, "properties", None)
    key = normalize_text(getattr(props, "urlKey", ""))
    base = normalize_text(getattr(props, "customBaseUrl", ""))
    return f"https://{key}.{base}" if key and base else ""


def _suggest_url(args: argparse.Namespace, config: Dict[str, Any], gis: Any, typed: str) -> None:
    """The config had no URL: print the line to put there, and with --apply write only that key."""
    url = org_url(gis) or typed
    emit(f"Set arcgis.url to {url} in the config. TaskDash signs in with the config's URL."
         + ("" if args.apply else " Run again with --apply to write it for you."))
    if not args.apply:
        return
    from taskdash_setup import backup_config, config_file, write_config_key
    path = config_file(args)
    backup_config(path)
    write_config_key(path, config, "url", url)


def _task_account(config: Dict[str, Any], task_user: str) -> str:
    """The account to seal the file for: the one named, else the collector task's. '' after a message."""
    if task_user:
        return task_user
    from taskdash_astask import choose_account, connect_scheduler
    from taskdash_dashboard import DEFAULT_COLLECTOR_TASK_NAME
    collector = normalize_text((config.get("scheduler") or {}).get("collector_task_name")) or DEFAULT_COLLECTOR_TASK_NAME
    chosen = choose_account(connect_scheduler().GetFolder("\\"), collector, "")
    return chosen[0] if chosen else ""


def _hand_off(args: argparse.Namespace, config: Dict[str, Any], settings: Tuple[str, str, str],
              password: str) -> int:
    """Seal the checked sign-in for the task account instead of saving a profile for this one."""
    account = _task_account(config, normalize_text(args.task_user))
    if not account:
        return 1
    url, user, profile = settings
    emit(f"Hand-off for {account}: the sign-in is sealed into {HANDOFF_FILE} in the TaskDash folder. The "
         f"next collector run as {account} stores it and deletes the file.")
    if not args.apply:
        emit("Nothing written. Run again with --apply to write the hand-off file.")
        return 0
    smtp_user = normalize_text((config.get("smtp") or {}).get("user"))
    mail = getpass.getpass(f"Mail password for {smtp_user} (not shown; press Enter to skip): ") if smtp_user else ""
    payload = {"version": 1, "account": account, "profile": profile, "url": url, "username": user,
               "password": password, "smtp_user": smtp_user if mail else "", "smtp_password": mail}
    try:
        path = write_handoff(payload, account)
    except Exception as exc:  # noqa: BLE001 - pywintypes.error has no importable base without pywin32
        _report("hand-off", FAIL, f"could not write {HANDOFF_FILE} ({type(exc).__name__}). Check that pywin32 "
                                  f"is installed and that '{account}' is a real account on this computer, then run "
                                  f"the command again. Or store the sign-in with runas, as the install guide's "
                                  f"fallback describes.")
        return 1
    _report("hand-off", OK, f"wrote {path.name} for {account}, sealed for this computer and readable only "
                            f"by that account, SYSTEM and Administrators")
    emit("Start the collector task, or wait for its next hour. The run that stores the sign-in logs "
         "'Stored the sign-in' and deletes the file.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config) if args.config else runtime_dir() / DEFAULT_CONFIG_FILE)
    url_was_blank = not normalize_text((config.get("arcgis") or {}).get("url"))
    url, user, profile = _settings(config)
    password = getpass.getpass("ArcGIS password (not shown): ")
    disable_arcpy_import()
    gis, why = _sign_in(url, user, password)
    if gis is None:
        _report("sign in", FAIL, why)
        return 1
    _report("sign in", OK, f"reached {url}")
    status, message = _check_account(gis)
    passed = _report("account", status, message)
    if passed:
        passed = _report("privileges", *check_privileges(Context(args=args, gis=gis)))
    if not passed:
        return 1
    if url_was_blank:
        _suggest_url(args, config, gis, url)
    if args.for_task_account:
        return _hand_off(args, config, (url, user, profile), password)
    if not args.apply:
        emit("Nothing saved. Run again with --apply to store the profile.")
        return 0
    try:
        save_profile(profile, url, user, password)
    except Exception as exc:
        _report("profile", FAIL, f"could not save the profile: {type(exc).__name__}. Run the command in a "
                                 f"normal, signed-in Windows window of the account that will use it.")
        return 1
    if not _report("profile", *_check_saved(url, profile)):
        return 1
    emit("This profile belongs to the Windows account that ran the command. Run it again as the "
         "task account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
