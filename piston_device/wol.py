"""Wake on LAN, device side: find the adapter and MAC, report whether wake is enabled and enable it in
NetworkManager. The integration sends the magic packet (this plugin is not running while the device sleeps)."""
import asyncio
import os
import re
import shlex
import subprocess



NET = "/sys/class/net"


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _run(*cmd: str, timeout: int = 8) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{cmd[0]} exited with {result.returncode}")
    return result.stdout.strip()


def _default_route_iface() -> str:
    best, metric = "", None
    for line in _read("/proc/net/route").split("\n")[1:]:
        parts = line.split()
        if len(parts) > 6 and parts[1] == "00000000":
            if metric is None or int(parts[6]) < metric:
                best, metric = parts[0], int(parts[6])
    return best


def _ipv4(iface: str):
    """(address, broadcast) of the interface, or None without an IPv4 address."""
    try:
        out = _run("ip", "-4", "-o", "addr", "show", "dev", iface)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"inet (\S+?)/\d+(?: brd (\S+))?", out)
    return (match.group(1), match.group(2) or "255.255.255.255") if match else None


def _is_wireless(iface: str) -> bool:
    return os.path.isdir(f"{NET}/{iface}/wireless")


def _candidates(wireless: bool) -> list:
    found = []
    for iface in sorted(os.listdir(NET)):
        base = f"{NET}/{iface}"
        # Physical Ethernet-type adapters only (no loopback, bridges, tunnels, containers)
        if iface == "lo" or not os.path.exists(f"{base}/device") or _read(f"{base}/type") != "1":
            continue
        if _is_wireless(iface) != wireless or _read(f"{base}/operstate") != "up":
            continue
        if _ipv4(iface):
            found.append(iface)
    return found


def choose_interface() -> str:
    """Wired is preferred whenever a wired link is up; wireless is the fallback."""
    default = _default_route_iface()
    for wireless in (False, True):
        found = _candidates(wireless)
        if found:
            return default if default in found else found[0]
    return ""


def _nmcli_value(prop: str, connection: str) -> str:
    try:
        return _run("nmcli", "-g", prop, "connection", "show", connection)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return ""


def wol_info() -> dict:
    """Blocking snapshot of the adapter used for Wake on LAN."""
    iface = choose_interface()
    if not iface:
        return {"interface": "", "error": "No network connection"}
    wireless = _is_wireless(iface)
    address = _ipv4(iface)
    try:
        connection = _run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        connection = ""
    info = {
        "interface": iface,
        "kind": "wireless" if wireless else "wired",
        "mac": _read(f"{NET}/{iface}/address").lower(),
        "broadcast": address[1] if address else "255.255.255.255",
        "connection": connection,
        "warnings": [],
        "error": "",
    }
    if wireless:
        phy = os.path.basename(os.path.realpath(f"{NET}/{iface}/phy80211")) if os.path.exists(f"{NET}/{iface}/phy80211") else ""
        try:
            info["supported"] = "wake up on magic packet" in _run("iw", "phy", phy, "info")
            show = _run("iw", "phy", phy, "wowlan", "show")
            info["wol_enabled"] = "magic packet" in show and "disabled" not in show.split("\n")[0]
        except (RuntimeError, OSError, subprocess.SubprocessError):
            info["supported"], info["wol_enabled"] = False, False
        if connection and _nmcli_value("802-11-wireless.cloned-mac-address", connection) == "random":
            info["warnings"].append(
                "This connection uses a random MAC address that changes on each connect; "
                "set it to stable or permanent for Wake on LAN to work."
            )
        info["warnings"].append("Wi-Fi can wake the device from suspend only, not from a full shutdown.")
    else:
        info["supported"] = os.path.exists(f"{NET}/{iface}/device/power/wakeup")
        nm = _nmcli_value("802-3-ethernet.wake-on-lan", connection) if connection else ""
        info["wol_enabled"] = _read(f"{NET}/{iface}/device/power/wakeup") == "enabled" or "magic" in nm
    return info


def enable_command(info: dict) -> str:
    """The commands to run in a terminal on the device to turn wake on, or "" when nothing needs doing.

    The plugin cannot apply this itself: NetworkManager asks for administrator authentication."""
    if not info.get("interface") or not info.get("supported") or info.get("wol_enabled") or not info.get("connection"):
        return ""
    prop = "802-11-wireless.wake-on-wlan" if info.get("kind") == "wireless" else "802-3-ethernet.wake-on-lan"
    return (f"sudo nmcli connection modify {shlex.quote(info['connection'])} {prop} magic"
            f" && sudo nmcli device reapply {shlex.quote(info['interface'])}")


class WolMixin:
    async def get_wol_info(self) -> dict:
        """Adapter, MAC, whether wake is enabled and, when it is not, the command that enables it."""
        info = await asyncio.get_running_loop().run_in_executor(None, wol_info)
        info["command"] = enable_command(info)
        return info
