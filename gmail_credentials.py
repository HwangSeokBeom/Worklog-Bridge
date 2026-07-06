"""Resolve Worklog Bridge Gmail credentials without persisting them in project files.

Environment variables remain the explicit, temporary override.  On macOS,
missing values fall back to generic-password items in the login Keychain.
All subprocess output that may contain a credential is captured and is never
included in errors or diagnostics.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Optional


DEFAULT_SENDER_ENV = "WORKLOGBRIDGE_GMAIL_ADDRESS"
DEFAULT_RECIPIENT_ENV = "WORKLOGBRIDGE_GMAIL_RECIPIENT"
DEFAULT_APP_PASSWORD_ENV = "WORKLOGBRIDGE_GMAIL_APP_PASSWORD"
KEYCHAIN_SERVICE = "com.worklogbridge.gmail"
KEYCHAIN_ACCOUNTS = {
    "sender": DEFAULT_SENDER_ENV,
    "recipient": DEFAULT_RECIPIENT_ENV,
    "app_password": DEFAULT_APP_PASSWORD_ENV,
}
SECURITY_CLI = "/usr/bin/security"


class CredentialResolutionError(ValueError):
    """A credential error whose message is safe to display and persist."""


@dataclass(frozen=True)
class GmailCredentials:
    sender: str
    recipient: str
    app_password: str
    source: str


def _keychain_value(
    account: str,
    *,
    runner: Callable[..., object] = subprocess.run,
    platform_name: str = sys.platform,
) -> str:
    """Return one Keychain value while discarding all unsafe diagnostics."""

    if platform_name != "darwin":
        return ""
    command = [
        SECURITY_CLI,
        "find-generic-password",
        "-s",
        KEYCHAIN_SERVICE,
        "-a",
        account,
        "-w",
    ]
    try:
        completed = runner(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, ValueError):
        return ""
    if getattr(completed, "returncode", 1) != 0:
        return ""
    value = getattr(completed, "stdout", "")
    return value.strip() if isinstance(value, str) else ""


def resolve_gmail_credentials(
    sender_env: str = DEFAULT_SENDER_ENV,
    app_password_env: str = DEFAULT_APP_PASSWORD_ENV,
    recipient_env: str = DEFAULT_RECIPIENT_ENV,
    *,
    environment: Optional[Mapping[str, str]] = None,
    keychain_runner: Callable[..., object] = subprocess.run,
    platform_name: str = sys.platform,
) -> GmailCredentials:
    """Resolve each value from the environment first, then macOS Keychain."""

    environ = os.environ if environment is None else environment
    fields = (
        ("sender", sender_env),
        ("app_password", app_password_env),
        ("recipient", recipient_env),
    )
    values: dict[str, str] = {}
    sources: set[str] = set()
    missing: list[str] = []
    for field_name, environment_name in fields:
        value = str(environ.get(environment_name, "")).strip()
        if value:
            values[field_name] = value
            sources.add("env")
            continue
        value = _keychain_value(
            KEYCHAIN_ACCOUNTS[field_name],
            runner=keychain_runner,
            platform_name=platform_name,
        )
        if value:
            values[field_name] = value
            sources.add("keychain")
        else:
            missing.append(environment_name)

    if missing:
        raise CredentialResolutionError(
            "Gmail credentials could not be resolved (missing: "
            + ", ".join(missing)
            + "). Run: python3.10 mac_collect_lorotopik_worklog.py "
            "--config config.local.json --setup-gmail-keychain"
        )
    source = "env+keychain" if len(sources) > 1 else next(iter(sources))
    return GmailCredentials(
        sender=values["sender"],
        recipient=values["recipient"],
        app_password=values["app_password"],
        source=source,
    )


def setup_macos_keychain(
    *,
    dry_run: bool = False,
    environment: Optional[Mapping[str, str]] = None,
    runner: Callable[..., object] = subprocess.run,
    platform_name: str = sys.platform,
    output: Callable[[str], object] = print,
) -> None:
    """Securely prompt macOS Keychain, or validate env input in dry-run mode."""

    if platform_name != "darwin":
        raise CredentialResolutionError(
            "macOS Keychain setup is available only on macOS."
        )

    ordered_accounts = (
        (DEFAULT_SENDER_ENV, KEYCHAIN_ACCOUNTS["sender"]),
        (DEFAULT_RECIPIENT_ENV, KEYCHAIN_ACCOUNTS["recipient"]),
        (DEFAULT_APP_PASSWORD_ENV, KEYCHAIN_ACCOUNTS["app_password"]),
    )
    if dry_run:
        environ = os.environ if environment is None else environment
        missing = [name for name, _account in ordered_accounts if not str(environ.get(name, "")).strip()]
        if missing:
            raise CredentialResolutionError(
                "Keychain setup dry-run requires these environment variables: "
                + ", ".join(missing)
            )
        if any(
            "\r" in str(environ.get(name, "")) or "\n" in str(environ.get(name, ""))
            for name, _account in ordered_accounts
        ):
            raise CredentialResolutionError(
                "Keychain setup dry-run rejected a credential containing a newline."
            )
        output(
            "Keychain setup DRY_RUN: required credentials are present; Keychain was not changed."
        )
        return

    for display_name, account in ordered_accounts:
        output(f"Store {display_name} in macOS Keychain (input is hidden).")
        command = [
            SECURITY_CLI,
            "add-generic-password",
            "-a",
            account,
            "-s",
            KEYCHAIN_SERVICE,
            "-l",
            f"Worklog Bridge Gmail: {display_name}",
            "-D",
            "Worklog Bridge Gmail credential",
            "-U",
            "-T",
            SECURITY_CLI,
            "-w",
        ]
        try:
            completed = runner(command, check=False)
        except OSError as exc:
            raise CredentialResolutionError(
                "macOS security CLI could not be started."
            ) from exc
        if getattr(completed, "returncode", 1) != 0:
            raise CredentialResolutionError(
                f"Keychain update failed for {display_name}; no credential value was logged."
            )
    output(
        "Gmail credentials stored in macOS Keychain service com.worklogbridge.gmail."
    )
