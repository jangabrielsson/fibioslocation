#!/usr/bin/env python3
"""
fibioslocation.py - Fetch iCloud Find My device locations and push to Fibaro HC3.

Every 3 minutes (configurable) it:
  1. Fetches all device locations from iCloud (own + family)
  2. Pushes a JSON payload to the HC3 via:
       GET http://<hc3>/api/callAction?deviceID=<id>&name=iosLocation&arg1=<json>

Requirements:
    pip install pyicloud rich

Usage:
    python fibioslocation.py
    python fibioslocation.py --email you@icloud.com
    python fibioslocation.py --interval 60   # poll every 60s instead of 180s
    python fibioslocation.py --no-hc3        # display only, don't push
    python fibioslocation.py --debug         # print raw 2FA options from Apple

Credentials are read from ~/.env (HC3_HOST, HC3_USER, HC3_PASSWORD).
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
from datetime import datetime

import requests as _requests

try:
    from pyicloud import PyiCloudService
    from pyicloud.exceptions import PyiCloudFailedLoginException, PyiCloudAPIResponseException
except ImportError:
    print("ERROR: pyicloud not installed. Run:  pip install pyicloud rich")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
except ImportError:
    print("ERROR: rich not installed. Run:  pip install pyicloud rich")
    sys.exit(1)

console = Console()


# ── .env loader ──────────────────────────────────────────────────────────────

def load_env(path: str = "~/.env") -> dict[str, str]:
    """Parse a simple KEY=value .env file, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    try:
        with open(os.path.expanduser(path)) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?([^"]*)"?$', line)
                if m:
                    result[m.group(1)] = m.group(2)
    except FileNotFoundError:
        pass
    return result


ENV = load_env()

# HC3 defaults (overridable via CLI args)
HC3_HOST    = ENV.get("HC3_HOST",     "hc3")
HC3_USER    = ENV.get("HC3_USER",     "admin")
HC3_PASS    = ENV.get("HC3_PASSWORD", "")
HC3_DEVICE  = 4200


# ── helpers ──────────────────────────────────────────────────────────────────

def format_time(timestamp_ms: int | None) -> str:
    """Convert iCloud ms-epoch timestamp to readable local time."""
    if not timestamp_ms:
        return "?"
    dt = datetime.fromtimestamp(timestamp_ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def battery_bar(level: float | None) -> str:
    """Return a small visual bar for battery level (0.0-1.0 or 0-100)."""
    if level is None:
        return "?"
    pct = int(level * 100) if level <= 1.0 else int(level)
    filled = pct // 10
    bar = "█" * filled + "░" * (10 - filled)
    colour = "green" if pct > 40 else ("yellow" if pct > 15 else "red")
    return f"[{colour}]{bar}[/{colour}] {pct}%"


def get_map_link(lat: float, lon: float) -> str:
    """Return an Apple Maps URL for the given coordinates."""
    return f"https://maps.apple.com/?q={lat},{lon}"


# ── iCloud login ──────────────────────────────────────────────────────────────

def _request_sms_code(api: PyiCloudService) -> bool:
    """
    Explicitly ask Apple to send a 2FA code via SMS to the user's trusted phone.
    Uses Apple's internal auth endpoint (reverse-engineered, same as pyicloud uses).
    """
    auth_data = api._auth_data
    pnv = auth_data.get("phoneNumberVerification") or auth_data
    phone = pnv.get("trustedPhoneNumber") or (
        (pnv.get("trustedPhoneNumbers") or [None])[0]
    )
    if not phone:
        return False
    phone_id   = phone.get("id")
    non_fteu   = phone.get("nonFTEU", False)
    headers    = api._get_auth_headers({"Accept": "application/json"})
    try:
        api.session.put(
            f"{api._auth_endpoint}/verify/phone",
            json={"phoneNumber": {"id": phone_id, "nonFTEU": non_fteu}, "mode": "sms"},
            headers=headers,
        )
        # Patch auth_data so validate_2fa_code uses SMS path
        api._auth_data["mode"] = "sms"
        api._auth_data["trustedPhoneNumber"] = phone
        return True
    except Exception as exc:
        console.print(f"[dim]SMS request failed: {exc}[/dim]")
        return False


def login(email: str, password: str, debug: bool = False) -> PyiCloudService:
    console.print(f"\n[bold cyan]Connecting to iCloud as[/bold cyan] [yellow]{email}[/yellow] …")
    try:
        api = PyiCloudService(apple_id=email, password=password, with_family=True)
    except PyiCloudFailedLoginException as exc:
        console.print(f"[bold red]Login failed:[/bold red] {exc}")
        sys.exit(1)

    if api.requires_2fa:
        console.print("[bold yellow]Two-factor authentication required (HSA2).[/bold yellow]")

        # _auth_data may be empty if pyicloud used a cached session token and
        # skipped SRP authentication. Fetch fresh auth options from Apple now.
        if not api._auth_data:
            try:
                api._auth_data = api._get_mfa_auth_options()
            except Exception:
                pass

        auth_data = api._auth_data

        if debug:
            import json
            console.print("[dim]Raw auth options from Apple:[/dim]")
            console.print(json.dumps(auth_data, indent=2, default=str))

        # Apple nests the useful fields under "phoneNumberVerification" in newer API responses
        pnv = auth_data.get("phoneNumberVerification") or auth_data
        mode = pnv.get("mode", auth_data.get("mode", "trusteddevice"))
        phones = pnv.get("trustedPhoneNumbers") or (
            [pnv["trustedPhoneNumber"]] if pnv.get("trustedPhoneNumber") else []
        )

        if debug:
            console.print(f"[dim]mode={mode!r}  trusted phones found: {len(phones)}[/dim]\n")

        if mode == "sms":
            # Apple already decided to send SMS
            phone_display = phones[0].get("numberWithDialCode", "your phone") if phones else "your phone"
            console.print(f"[green]A 6-digit code has been sent via SMS to {phone_display}.[/green]")
        else:
            # Default: trusted device push
            console.print("[dim]Apple should pop up a 6-digit code on your trusted iPhone/Mac.[/dim]")
            if phones:
                phone_display = phones[0].get("numberWithDialCode", f'phone id={phones[0].get("id")}')
                console.print(f"[dim]SMS fallback available: {phone_display}[/dim]\n")
                choice = console.input(
                    "[bold]Press [green]Enter[/green] to wait for device push, "
                    "or type [yellow]sms[/yellow] to receive an SMS code instead: [/bold]"
                ).strip().lower()
                if choice == "sms":
                    console.print("Requesting SMS code …")
                    if _request_sms_code(api):
                        console.print(f"[green]SMS sent to {phone_display}.[/green]")
                    else:
                        console.print("[yellow]SMS request failed — waiting for device push code.[/yellow]")
            else:
                console.print("[dim]No trusted phone numbers found — only device push is available.[/dim]")
                console.print("[yellow]If no popup appears, make sure your Apple ID has a trusted phone number registered at appleid.apple.com[/yellow]\n")

        code = console.input("Enter 6-digit code: ").strip()
        result = api.validate_2fa_code(code)
        if not result:
            console.print("[bold red]Invalid 2FA code.[/bold red]")
            sys.exit(1)
        if not api.is_trusted_session:
            console.print("Trusting this session …")
            api.trust_session()

    elif api.requires_2sa:
        console.print("[bold yellow]Two-step verification required.[/bold yellow]")
        devices = api.trusted_devices
        for i, device in enumerate(devices):
            name = device.get("deviceName") or f"SMS to {device.get('phoneNumber', '?')}"
            console.print(f"  [{i}] {name}")
        idx = int(console.input("Choose device index: "))
        device = devices[idx]
        if not api.send_verification_code(device):
            console.print("[bold red]Failed to send verification code.[/bold red]")
            sys.exit(1)
        code = console.input("Enter the verification code: ").strip()
        if not api.validate_verification_code(device, code):
            console.print("[bold red]Invalid verification code.[/bold red]")
            sys.exit(1)

    console.print("[bold green]✓ Logged in successfully.[/bold green]\n")
    return api


# ── HC3 push ──────────────────────────────────────────────────────────────────

def push_to_hc3(payload: list[dict], host: str, user: str, password: str,
                device_id: int = HC3_DEVICE) -> bool:
    """
    Push device locations to HC3 via:
      POST http://<host>/api/devices/<id>/action/iosLocation
      {"args": [<json data>]}
    Returns True on success.
    """
    url = f"http://{host}/api/devices/{device_id}/action/iosLocation"
    try:
        resp = _requests.post(
            url,
            auth=(user, password),
            json={"args": [payload]},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        console.print(f"[bold red]HC3 push failed:[/bold red] {exc}")
        return False


# ── fetch + display ───────────────────────────────────────────────────────────

def fetch_device_data(api: PyiCloudService) -> list[dict]:
    """Fetch all devices and return a list of location dicts."""
    try:
        devices = api.devices
        devices.refresh(locate=True)
    except Exception as exc:
        console.print(f"[bold red]Error fetching devices:[/bold red] {exc}")
        return []

    result = []
    for device in devices:
        raw_loc  = device.location or {}
        raw_data = device.data
        battery  = raw_data.get("batteryLevel") or (
                       (raw_data.get("location") or {}).get("batteryLevel")
                   )
        lat = raw_loc.get("latitude")
        lon = raw_loc.get("longitude")
        ts  = raw_loc.get("timeStamp") or raw_loc.get("timestamp")
        result.append({
            "name":     device.name or "Unknown",
            "model":    device.model_name or device.model or "?",
            "lat":      lat,
            "lon":      lon,
            "accuracy": raw_loc.get("horizontalAccuracy"),
            "battery":  round(battery, 3) if battery is not None else None,
            "timestamp": ts,
            "map":      get_map_link(lat, lon) if lat and lon else None,
        })
    return result


def show_devices(data: list[dict]) -> None:
    """Render a Rich table from a list of device dicts."""
    if not data:
        console.print("[yellow]No devices found.[/yellow]")
        return

    table = Table(
        title=f"[bold]Find My – All Devices[/bold]  [dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("#",            justify="right",  style="dim",          no_wrap=True)
    table.add_column("Device",       style="bold white", no_wrap=True)
    table.add_column("Model",        style="dim",          no_wrap=True)
    table.add_column("Latitude",     justify="right",  style="cyan")
    table.add_column("Longitude",    justify="right",  style="cyan")
    table.add_column("Accuracy (m)", justify="right")
    table.add_column("Battery",      justify="center")
    table.add_column("Last Seen",    style="dim")
    table.add_column("Map",          style="blue")

    for idx, d in enumerate(data):
        lat, lon = d["lat"], d["lon"]
        table.add_row(
            str(idx + 1),
            d["name"],
            d["model"],
            f"{lat:.6f}"           if lat            is not None else "–",
            f"{lon:.6f}"           if lon            is not None else "–",
            f"{d['accuracy']:.0f}" if d["accuracy"]  is not None else "–",
            battery_bar(d["battery"]),
            format_time(d["timestamp"]),
            d["map"] or "–",
        )

    console.print(table)
    console.print(
        f"[dim]Total: {len(data)} device(s) — own + family members sharing location.[/dim]\n"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

DEFAULT_INTERVAL = 180  # 3 minutes


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fibioslocation.py",
        description=(
            "Fetch iCloud Find My device locations (your own + family members)\n"
            "and push them to a Fibaro HC3 home controller every N seconds.\n"
        ),
        epilog=(
            "Credentials / defaults are read from ~/.env:\n"
            "  HC3_HOST      – HC3 hostname or IP  (e.g. hc3 or 192.168.1.10)\n"
            "  HC3_USER      – HC3 login user       (default: admin)\n"
            "  HC3_PASSWORD  – HC3 login password\n"
            "\n"
            "HC3 API call made on each poll:\n"
            "  POST http://<hc3-host>/api/devices/<hc3-device>/action/iosLocation\n"
            "  Body: {\"args\": [[ {name, model, lat, lon, accuracy, battery,\n"
            "                       timestamp, map}, ... ]]}\n"
            "\n"
            "Examples:\n"
            "  python fibioslocation.py                          # poll every 3 min\n"
            "  python fibioslocation.py --once                   # single shot\n"
            "  python fibioslocation.py --no-hc3                 # display only\n"
            "  python fibioslocation.py -i 60                    # poll every 60s\n"
            "  python fibioslocation.py --hc3-host 192.168.1.10 --hc3-device 4200\n"
            "  python fibioslocation.py --debug                  # diagnose 2FA\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # iCloud
    icloud = parser.add_argument_group("iCloud options")
    icloud.add_argument("--email", "-e", metavar="EMAIL",
                        help="Apple ID e-mail (prompted if omitted)")
    icloud.add_argument("--debug", "-d", action="store_true",
                        help="Print raw 2FA payload from Apple (helps diagnose login issues)")

    # polling
    poll = parser.add_argument_group("polling options")
    poll.add_argument("--interval", "-i", type=int, default=DEFAULT_INTERVAL,
                      metavar="SECONDS",
                      help=f"Poll interval in seconds (default: {DEFAULT_INTERVAL})")
    poll.add_argument("--once", action="store_true",
                      help="Fetch once and exit instead of looping")

    # HC3
    hc3 = parser.add_argument_group("HC3 options")
    hc3.add_argument("--hc3-host", default=HC3_HOST, metavar="HOST",
                     help=f"HC3 hostname or IP (default from ~/.env: {HC3_HOST})")
    hc3.add_argument("--hc3-user", default=HC3_USER, metavar="USER",
                     help=f"HC3 username (default from ~/.env: {HC3_USER})")
    hc3.add_argument("--hc3-password", default=HC3_PASS, metavar="PASS",
                     help="HC3 password (default from ~/.env)")
    hc3.add_argument("--hc3-device", type=int, default=HC3_DEVICE, metavar="ID",
                     help=f"HC3 QuickApp device ID (default: {HC3_DEVICE})")
    hc3.add_argument("--no-hc3", action="store_true",
                     help="Display locations in terminal only, do not push to HC3")

    args = parser.parse_args()

    email    = args.email or console.input("[bold]Apple ID (email):[/bold] ").strip()
    password = getpass.getpass("Password: ")

    api = login(email, password, debug=args.debug)

    def cycle() -> None:
        data = fetch_device_data(api)
        show_devices(data)
        if not args.no_hc3:
            ok = push_to_hc3(data, host=args.hc3_host, user=args.hc3_user,
                             password=args.hc3_password, device_id=args.hc3_device)
            if ok:
                console.print(
                    f"[green]✓ Pushed {len(data)} device(s) to HC3 "
                    f"(device {args.hc3_device} @ {args.hc3_host})[/green]  "
                    f"[dim]{datetime.now().strftime('%H:%M:%S')}[/dim]\n"
                )

    if args.once:
        cycle()
    else:
        console.print(f"[dim]Polling every {args.interval}s — press Ctrl-C to stop.[/dim]\n")
        try:
            while True:
                console.clear()
                cycle()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            console.print("\n[dim]Stopped.[/dim]")


if __name__ == "__main__":
    main()
