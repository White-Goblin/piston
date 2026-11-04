"""Battery sensors for connected controllers, from two sources.

- Controllers on Steam's own wireless protocol (the Steam Controller and its puck) have no
  operating system battery entry, so the plugin UI reads them from Steam's controller list.
- Bluetooth controllers (and other Bluetooth HID devices) get a real battery entry from the
  operating system (/sys/class/power_supply/hid-<address>-battery). Steam reports a flat
  100% for these, so its value is ignored and the operating system's is used.
"""
import os
import re
from datetime import datetime, timezone

import decky

MAX_CONTROLLERS = 8
SERIAL_RE = re.compile(r"[^a-z0-9]+")
POWER_SUPPLY = "/sys/class/power_supply"
HID_BATTERY_RE = re.compile(r"^hid-([0-9a-f:]{17})-battery")


def _slug(serial: str) -> str:
    return SERIAL_RE.sub("", serial.lower())[:24]


class ControllersMixin:
    async def report_controllers(self, controllers: list) -> None:
        """Frontend reports the connected controllers that have a battery level."""
        current = {}
        for c in controllers if isinstance(controllers, list) else []:
            if not isinstance(c, dict):
                continue
            slug = _slug(str(c.get("serial", "")))
            try:
                battery = int(c.get("battery"))
            except (TypeError, ValueError):
                continue
            if slug and 0 <= battery <= 100:
                current[slug] = {
                    "name": str(c.get("name") or "Controller")[:40],
                    "battery": battery,
                    "charging": bool(c.get("charging")),
                    "wireless": bool(c.get("wireless")),
                }
        self.controller_state = dict(list(current.items())[:MAX_CONTROLLERS])
        self._remember_controllers()
        self._publish_controllers()

    def _read_hid_batteries(self) -> dict:
        """Blocking read of Bluetooth HID battery entries, keyed like the Steam controllers."""
        found = {}
        try:
            names = sorted(os.listdir(POWER_SUPPLY))
        except OSError:
            return found
        for entry in names:
            match = HID_BATTERY_RE.match(entry)
            if not match:
                continue
            base = f"{POWER_SUPPLY}/{entry}"
            # The entry exists while the device is connected, but the kernel only has a
            # level once the device has sent a battery report (reads fail with ENODATA before)
            capacity = self._read_file(f"{base}/capacity")
            has_reading = capacity.isdigit()
            found[_slug(match.group(1))] = {
                "name": (self._read_file(f"{base}/model_name") or "Bluetooth device")[:40],
                "battery": int(capacity) if has_reading else None,
                "charging": self._read_file(f"{base}/status").lower() == "charging" if has_reading else None,
                "wireless": True,
            }
        return found

    async def _refresh_os_controllers(self) -> None:
        found = await self.loop.run_in_executor(None, self._read_hid_batteries)
        if found != self.os_controllers:
            self.os_controllers = found
            self._remember_controllers()
            self._publish_controllers()

    def _merged_controllers(self) -> dict:
        """Connected controllers with their battery; a connected one without a reading shows
        its last real reading, marked stale (or no level at all if it never had one)."""
        if self.readings is None:
            self.readings = {
                c["serial"]: {"battery": c["battery"], "charging": c.get("charging"), "at": c.get("at")}
                for c in self._load_settings()["controllers_seen"]
                if c.get("battery") is not None
            }
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        merged = {}
        for slug, c in {**self.os_controllers, **self.controller_state}.items():
            c = dict(c)
            if c["battery"] is not None:
                self.readings[slug] = {"battery": c["battery"], "charging": c["charging"], "at": now}
                c["stale"], c["updated"] = False, now
            else:
                last = self.readings.get(slug)
                c["battery"] = last["battery"] if last else None
                c["charging"] = None  # charging changes quickly; an old value would mislead
                c["stale"], c["updated"] = True, last["at"] if last else None
            merged[slug] = c
        return merged

    def _remember_controllers(self) -> None:
        """Keep every controller seen (with its last real reading) so its entities stay,
        unavailable while it is off, and a restart can still show the last level."""
        self._merged_controllers()  # refreshes self.readings
        s = self._load_settings()
        seen = {c["serial"]: c for c in s["controllers_seen"]}
        changed = False
        for slug, c in {**self.os_controllers, **self.controller_state}.items():
            entry = seen.get(slug) or {"serial": slug, "name": c["name"]}
            reading = self.readings.get(slug)
            wanted = {"serial": slug, "name": c["name"]}
            if reading:
                wanted.update(battery=reading["battery"], charging=reading["charging"], at=reading["at"])
            # Rewrite only when the level changed, so an unchanged level does not churn the file
            if slug not in seen or any(
                entry.get(k) != wanted.get(k) for k in ("name", "battery", "charging")
            ):
                seen[slug] = wanted
                changed = True
        if changed:
            self._update_settings({"controllers_seen": list(seen.values())[-MAX_CONTROLLERS:]})
            decky.logger.info(f"Controllers seen: {', '.join(c['name'] for c in seen.values())}")

    def _publish_controllers(self) -> None:
        """Controllers group: the connected ones with battery, and every controller ever seen
        (so the integration can keep their entities, unavailable while the controller is off)."""
        seen = [{"serial": c["serial"], "name": c["name"]} for c in self._load_settings()["controllers_seen"]]
        self._push("controllers", {"connected": self._merged_controllers(), "seen": seen})
