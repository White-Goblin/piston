"""System sensors read from /proc and /sys."""
import asyncio
import os
import re
import shutil
import socket

import decky



class SensorsMixin:
    @staticmethod
    def _read_file(path: str) -> str:
        try:
            with open(path) as f:
                return f.read().strip()
        except OSError:
            return ""

    def _hwmon(self, chip: str, attr: str):
        base = "/sys/class/hwmon"
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return None
        for name in names:
            if self._read_file(f"{base}/{name}/name") == chip:
                raw = self._read_file(f"{base}/{name}/{attr}")
                if raw.lstrip("-").isdigit():
                    return int(raw)
        return None

    def _read_sensors(self) -> dict:
        """Blocking read of system stats; sources the device lacks are omitted."""
        out = {}
        cpu_temp = self._hwmon("k10temp", "temp1_input")
        gpu_temp = self._hwmon("amdgpu", "temp1_input")
        fan = self._hwmon("steamdeck_hwmon", "fan1_input")
        if cpu_temp is not None:
            out["cpu_temp"] = round(cpu_temp / 1000, 1)
        if gpu_temp is not None:
            out["gpu_temp"] = round(gpu_temp / 1000, 1)
        if fan is not None:
            out["fan_rpm"] = fan

        fields = self._read_file("/proc/stat").split("\n", 1)[0].split()[1:]
        if fields:
            values = [int(v) for v in fields]
            idle, total = values[3] + values[4], sum(values)
            if self.cpu_prev is not None and total > self.cpu_prev[1]:
                out["cpu_load"] = round(100 * (1 - (idle - self.cpu_prev[0]) / (total - self.cpu_prev[1])), 1)
            self.cpu_prev = (idle, total)

        gpu_load = self._read_file("/sys/class/drm/card0/device/gpu_busy_percent")
        if gpu_load.isdigit():
            out["gpu_load"] = int(gpu_load)

        mem = {}
        for line in self._read_file("/proc/meminfo").split("\n"):
            key, _, rest = line.partition(":")
            if rest:
                mem[key] = int(rest.split()[0])
        if mem.get("MemTotal"):
            out["mem_used"] = round(100 * (1 - mem.get("MemAvailable", 0) / mem["MemTotal"]), 1)

        try:
            usage = shutil.disk_usage("/home")
            out["disk_used"] = round(100 * usage.used / usage.total, 1)
        except OSError:
            pass

        uptime = self._read_file("/proc/uptime").split()
        if uptime:
            out["uptime"] = int(float(uptime[0]))

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("10.255.255.255", 1))  # no packet is sent; picks the outbound interface
                out["ip"] = sock.getsockname()[0]
        except OSError:
            pass

        for line in self._read_file("/proc/net/wireless").split("\n")[2:]:
            parts = line.split()
            if len(parts) > 3:
                try:
                    out["wifi_signal"] = int(float(parts[3].rstrip(".")))
                except ValueError:
                    pass
                break

        supply = "/sys/class/power_supply"
        try:
            names = sorted(os.listdir(supply))
        except OSError:
            names = []
        for name in names:
            if name.startswith("hid-"):
                continue  # Bluetooth controllers and other HID devices: see controllers.py
            capacity = self._read_file(f"{supply}/{name}/capacity")
            if capacity.isdigit():
                out["battery_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")] = int(capacity)
        return out

    async def _sensor_loop(self):
        while True:
            try:
                sensors = await self.loop.run_in_executor(None, self._read_sensors)
            except Exception as e:  # noqa: BLE001
                decky.logger.warning(f"Sensor read failed: {e}")
                sensors = {}
            await self._refresh_os_controllers()
            if sensors:
                self.sensor_values = sensors
                self._push("sensors", sensors)
            try:
                await asyncio.wait_for(self.sensor_wake.wait(), self._load_settings()["sensor_interval"])
            except asyncio.TimeoutError:
                pass
            self.sensor_wake.clear()
