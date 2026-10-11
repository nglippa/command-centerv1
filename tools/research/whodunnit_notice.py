"""Small read-only notification display for the existing Command Center console."""
import json
from pathlib import Path


def read_notice(resources):
    path = Path(resources) / "whodunnit-notification.json"
    if not path.exists():
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError("Notification exceeds limit")
        notice = json.loads(raw)
        if not isinstance(notice, dict) or notice.get("version") != 1:
            raise ValueError("Invalid notification")
        return notice
    except (OSError, ValueError) as exc:
        return {"version": 1, "state": "UNAVAILABLE", "reason": str(exc)}


def print_notice(resources, display=str):
    notice = read_notice(resources)
    if not notice:
        return
    print("Whodunnit researcher: " + display(notice.get("state", "UNKNOWN")))
    print(display(notice.get("reason", "")))
    if notice.get("state") == "PAUSED":
        print("Operator procedures (not numbered console menu selections):")
        for number, option in enumerate(notice.get("options", []), 1):
            if isinstance(option, dict):
                label = chr(64 + number)
                print(f"  {label}. {display(option.get('label', ''))}: {display(option.get('procedure', ''))}")
        print("These are operator procedures; no process is killed or schedule changed automatically.")
