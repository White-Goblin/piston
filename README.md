# Piston

**Media and control for SteamOS devices in your living room.** Piston pushes commands from Home Assistant to the box under the TV: launch an app, press play, wake it, put it to sleep, show a camera on screen. The SteamOS device can also be a background speaker for Music Assistant, with a now-playing card on its own screen.

The goal of Piston is to turn a SteamOS device into a fully featured media center, replacing smart TVs, smart soundbars and smart streaming boxes that are untrustworthy and likely spying on their users.

Piston has two parts: an integration for [Home Assistant](https://www.home-assistant.io/) and a small plugin for [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) on the SteamOS device. The device is found automatically and paired with a short code shown on its screen.

Requires Home Assistant 2026.10 or newer.

> **Disclaimer:** this plugin was developed with AI assistance.

## Media apps

Piston does not include any media apps. The user installs the apps they want on the SteamOS device, such as Jellyfin, YouTube or a web browser, and adds them as Steam games or non-Steam shortcuts. Apps added in the plugin panel become sources of the media player entity.

Not every app works with the media player controls. Play, pause, next, previous and the now-playing information only work for apps that publish their playback to the system through MPRIS. Apps that do not publish it can still be launched and closed from Home Assistant, but their playback cannot be controlled. Compatibility has been tested with Jellyfin and YouTube (VacuumTube); other apps and most browsers may work if they publish MPRIS.

## What you get

| Entity | What it does |
|---|---|
| Media player | Turn the SteamOS device on (Wake on LAN) and off, pick an app or game as the source, play, pause, next, previous, volume and mute |
| Desktop mode (switch) | Switch between game mode and desktop mode |
| Plugin update (update), Install plugin updates automatically (switch) | Keep the plugin on the device at the same version as the integration |
| Restart device, Restart Steam (buttons) | Reboot the device or restart the Steam client |
| Connectivity (binary sensor), Steam screen (sensor) | Whether the device is online and awake, and what the Steam interface is showing |
| CPU temperature, GPU temperature, Fan speed, CPU load, GPU load, Memory used, Disk used, Uptime, IP address, Wi-Fi signal, Battery | System sensors |
| Controller battery and charging | Battery state of each controller the device has seen |
| Toast (notify), Toast click (event) | Show a Steam notification and report when it is clicked |
| Sendspin player (switch), Sendspin status (sensor) | Let Music Assistant play audio on the device in the background, mixed with games and apps. Any computer on the same network can connect to the Sendspin player; turn the switch off if that is a concern |
| PiP camera (select), PiP state (sensor) | Show a camera over whatever is on screen, including a running game |

## Install

1. **Integration.** Download Piston from the Home Assistant Marketplace, formerly HACS, then restart Home Assistant.
2. **Plugin on the SteamOS device.** This step is best performed on the device using a mouse and keyboard in desktop mode. Download `piston-<version>.zip` from the [releases](https://github.com/White-Goblin/piston/releases). Go back to game mode. In Decky Loader's settings, enable Developer mode, then choose Developer > Install plugin from ZIP and select the file. Decky Loader must already be installed. This is only done by hand once; later updates are handled by Home Assistant.
3. **Pair.** The device appears in Home Assistant under Settings > Devices & services as a discovered device. Open Quick Access on the device, then Piston; the panel shows a 6-digit code. Enter it in Home Assistant.
4. **Choose your apps.** In the plugin panel, under Apps in Home Assistant, add the Steam games and non-Steam apps you want as media player sources.
5. **Wake on LAN.** To turn the device on from Home Assistant, set up [Wake on LAN](#wake-on-lan) on the device.

Home Assistant and the device must be on the same network segment, and Home Assistant must be able to reach the device on TCP port 47830.

## Options

Settings > Devices & services > Piston > Configure:

| Option | Default | What it does |
|---|---|---|
| What turning off does | Sleep | Sleep suspends the device; Shutdown powers it off. |
| Sensor update interval (seconds) | 10 | How often the device reports temperatures, load and battery: 10, 30, 60 or 300. |
| PiP cameras | none | The cameras the PiP camera selector offers. |
| PiP position | Top right | Where the camera picture appears by default. |
| PiP size | Medium | Small, medium, large or extra large. |
| PiP hide after | 30 seconds | 10, 20, 30, 60 or 300 seconds, or until hidden. |
| Mute PiP | On | Start camera pictures without sound. |

## Updating

Update the integration through the Home Assistant Marketplace and restart Home Assistant. The **Plugin update** entity then shows that the plugin on the SteamOS device is out of date. With **Install plugin updates automatically** on, Home Assistant starts the update when the device connects; otherwise press Install.

Decky Loader asks for approval on the device before it installs anything: tap OK in the "Install Piston" dialog in game mode. If the update is not approved within 30 minutes it is marked as not approved, and pressing Install asks again. The plugin is downloaded from GitHub and checked against the `SHA256SUMS` file of the release.

## Wake on LAN

Turning the device on from Home Assistant sends a Wake on LAN packet. The SteamOS device has to be set up to accept it, and the plugin cannot do this itself because changing network settings needs administrator rights.

1. Open a terminal on the device in desktop mode. If no administrator password is set yet, run `passwd` first.
2. Find the connection and interface with `nmcli connection show --active` and `nmcli device status`.
3. Turn wake on for that connection. For a wired connection:
   ```
   sudo nmcli connection modify "<connection name>" 802-3-ethernet.wake-on-lan magic
   sudo nmcli device reapply <interface>
   ```
   For Wi-Fi use `802-11-wireless.wake-on-wlan magic` instead, and make sure the connection uses a stable MAC address.
4. Put the device to sleep from Home Assistant (turn the media player off), then turn it on again.

A wired connection is recommended. Over Wi-Fi the device can wake from sleep but not from a full shutdown.

## Troubleshooting and FAQ

- **The device does not show up.** Home Assistant and the device are probably on different networks or VLANs, and mDNS does not cross. Use Settings > Devices & services > Add integration > Piston and enter the device's address instead.
- **Too many wrong codes.** The device waits a minute and then shows a new code.
- **Entities are unavailable.** The device is asleep or off, or Home Assistant cannot reach port 47830. Check the Connectivity entity and the plugin panel.
- **The device got a new IP address.** Use the three-dot menu of the integration entry > Reconfigure. The pairing is kept.
- **I removed the device or reinstalled Home Assistant.** In the plugin panel press Reset pairing, then pair again.
- **Home Assistant says the integration or the plugin is too old.** Update the one that is older. A Repairs item says which.
- **The plugin update never finishes.** Decky Loader's install dialog is waiting for approval on the device, or the device is in desktop mode. Switch to game mode.
- **The device does not show up in Music Assistant.** Check the Sendspin status sensor. `disabled` means the switch is off; `error` shows the reason. Music Assistant must be on the same network segment.
- **Music Assistant plays but there is no sound.** The Sendspin volume is separate from the system volume. Check the volume of the Music Assistant player and of the device's output.
- **Turn on does nothing.** Set up [Wake on LAN](#wake-on-lan). Some devices cannot wake from a full shutdown.

## Uninstall

Remove the device in Home Assistant, which also resets the plugin's pairing. Then remove the integration and the plugin in Decky Loader.

## License

BSD-3-Clause, see [LICENSE](LICENSE).
