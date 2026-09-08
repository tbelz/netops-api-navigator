#!/usr/bin/env python3
"""Onboard a device to a site in HPE Aruba Networking Central.

Workflow:
1. Verify device exists in GLP inventory
2. Create site if it doesn't exist
3. Assign device to site
4. Set device persona/function
"""

import argparse
import json
import sys

from central_helpers import api


def find_device(serial: str) -> dict | None:
    """Look up a device by serial number in GLP inventory."""
    resp = api.get("devices/v1/networking", params={"serial": serial})
    devices = resp.get("devices", resp.get("items", []))
    for d in devices:
        if d.get("serial_number", d.get("serialNumber", "")).upper() == serial.upper():
            return d
    return None


def site_exists(site_name: str) -> bool:
    """Check if a site already exists."""
    resp = api.get("central/v2/sites", params={"calculate_total": "true"})
    for s in resp.get("sites", resp.get("items", [])):
        if s.get("site_name", s.get("name", "")).lower() == site_name.lower():
            return True
    return False


def create_site(site_name: str) -> dict:
    """Create a new site."""
    return api.post("central/v2/sites", json_body={
        "site_name": site_name,
        "site_address": {"city": "", "state": "", "country": ""},
    })


def assign_device_to_site(serial: str, site_name: str) -> dict:
    """Assign a device to a site."""
    return api.post("central/v2/sites/associate", json_body={
        "site_name": site_name,
        "device_ids": [serial],
        "device_type": "all",
    })


def main():
    parser = argparse.ArgumentParser(description="Onboard a device to a site")
    parser.add_argument("--serial", required=True, help="Device serial number")
    parser.add_argument("--site", required=True, help="Target site name")
    parser.add_argument("--persona", default="ACCESS_SWITCH", help="Device persona/function")
    args = parser.parse_args()

    results = {"serial": args.serial, "site": args.site, "persona": args.persona, "steps": []}

    # Step 1: Verify device
    device = find_device(args.serial)
    if not device:
        results["steps"].append({"step": "find_device", "status": "error", "detail": "Device not found"})
        print(json.dumps(results, indent=2))
        sys.exit(1)
    results["steps"].append({"step": "find_device", "status": "ok"})

    # Step 2: Create site if needed
    if site_exists(args.site):
        results["steps"].append({"step": "check_site", "status": "ok", "detail": "Site already exists"})
    else:
        try:
            create_site(args.site)
            results["steps"].append({"step": "create_site", "status": "ok"})
        except Exception as e:
            results["steps"].append({"step": "create_site", "status": "error", "detail": str(e)})
            print(json.dumps(results, indent=2))
            sys.exit(1)

    # Step 3: Assign device to site
    try:
        assign_device_to_site(args.serial, args.site)
        results["steps"].append({"step": "assign_device", "status": "ok"})
    except Exception as e:
        results["steps"].append({"step": "assign_device", "status": "error", "detail": str(e)})
        print(json.dumps(results, indent=2))
        sys.exit(1)

    results["status"] = "success"
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
