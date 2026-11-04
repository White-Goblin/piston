# Decky Home Assistant

Control a SteamOS device from [Home Assistant](https://www.home-assistant.io/). A custom integration (installed through [HACS](https://hacs.xyz/)) gives you a media player, sensors, notifications and camera picture-in-picture for a Steam Deck, Steam Machine or any other [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) device. A small companion plugin on the device does the work there.

The device is found automatically and paired with a short code shown on its screen.

Tested with Home Assistant 2026.10 and a Steam Machine running SteamOS.

## What you get

| Entity | What it does |
|---|---|
| Media player (named after the device) | Turn on (Wake on LAN) and off (sleep, or shut down if you choose), pick an app or game as the source (the running app is closed first), play, pause, next and previous for the running app, volume and mute |
| Desktop mode (switch) | On switches the device to the desktop session, off returns to game mode |
| Plugin update (update) and Install plugin updates automatically (switch) | The plugin version on the device against the version of this integration, with an Install button; the switch (on by default) lets Home Assistant update the plugin on its own |
| Restart device, Restart Steam (buttons) | Reboot the device, or restart the Steam client |
| Connectivity (binary sensor) | Whether the device is online and awake |
| Steam screen (sensor) | `game`, `steam_over_game`, `steam` or `desktop` |
| CPU temperature, GPU temperature, Fan speed, CPU load, GPU load, Memory used, Disk used, Uptime, IP address, Wi-Fi signal, Battery | System sensors; only the ones your device has are created |
| Controller battery and charging | One pair per controller the device has seen; unavailable while the controller is off |
| Toast (notify) and Toast click (event) | Show a Steam notification; fires when someone clicks it |
| PiP camera (select) and PiP state (sensor) | Show a camera over whatever is on screen (including a running game); choose Off to hide it |

Actions (Developer tools, automations and scripts):

- `decky_home_assistant.show_toast` on the Toast entity: `message`, optional `title`, `duration` (seconds), `icon` (bell, bolt, camera, car, door, fire, home, light, lock, motion, temperature, warning, water), `silent`, and `id` (comes back in the click event so you can tell toasts apart).
- `decky_home_assistant.show_pip` on the PiP camera entity: `camera` (any camera entity) and optional `position`, `size`, `duration` and `mute`, overriding the options for that call. A new camera replaces the one showing.
- `decky_home_assistant.hide_pip` on the PiP camera entity.

## Install

1. **Integration.** In HACS open the menu, choose Custom repositories, add `https://github.com/White-Goblin/steamos-media-control` as category Integration, then download Decky Home Assistant and restart Home Assistant.
2. **Plugin on the device.** Download `decky-home-assistant-<version>.zip` from the [releases](https://github.com/White-Goblin/steamos-media-control/releases). In Decky Loader's settings enable Developer mode, then choose Developer > Install plugin from ZIP and select the file. (Decky Loader itself must already be installed.) You only do this by hand once: from version 0.4.0 on, Home Assistant updates the plugin for you, see [Plugin updates](#plugin-updates).
3. **Pair.** The device appears in Home Assistant under Settings > Devices & services as a discovered device. Open Quick Access on the device, then Decky Home Assistant; the panel shows a 6-digit code. Enter it in Home Assistant. Done: the entities appear.
4. **Choose your apps.** In the plugin panel, under Apps in Home Assistant, add the Steam games and non-Steam apps you want as media player sources.
5. **Wake on LAN** (to turn the device on from Home Assistant). Set this up on the device yourself, see [Wake on LAN](#wake-on-lan) below.

Home Assistant and the device must be on the same network segment because the device is found with mDNS, and Home Assistant needs to reach the device on TCP port 47830.

## Setup parameters

Setting up needs no typing when the device is found automatically: you confirm it and enter the pairing code. If Home Assistant cannot see the device (for example it is on another VLAN), add it by address instead:

| Parameter | Meaning |
|---|---|
| Host or IP address | The address of the device, for example `192.168.1.20`. Only asked when you add the device by address. |
| Port | The port the plugin listens on. Default `47830`; only change it if you changed it on the device. |
| Pairing code | The 6 digits shown in the plugin panel on the device, valid for five minutes. Five wrong codes lock pairing for a minute. |

To change the address later (the device got a new IP address and Home Assistant does not find it by itself) use the three-dot menu of the integration entry > Reconfigure. The pairing is kept.

## Options

Settings > Devices & services > Decky Home Assistant > Configure:

| Option | Default | What it does |
|---|---|---|
| What turning off does | Sleep | Sleep suspends the device (Wake on LAN brings it back); Shutdown powers it off (most devices need the power button to start again). |
| Sensor update interval (seconds) | 10 | How often the device reports temperatures, load and battery: 10, 30, 60 or 300. Longer uses a little less power. |
| PiP cameras | none | The cameras the PiP camera selector offers. The `show_pip` action works with any camera. |
| PiP position | Top right | Where the camera picture appears by default (top, top right, right, bottom right, bottom, bottom left, left, top left). |
| PiP size | Medium | Small, medium, large or extra large (a share of the screen width). |
| PiP hide after | 30 seconds | 10, 20, 30, 60 or 300 seconds, or until hidden. The `show_pip` action can override it. |
| Mute PiP | On | Start camera pictures without sound. |

## Plugin updates

From version 0.4.0 the plugin on the device follows the integration: when you update the integration in HACS and restart Home Assistant, the device is brought to the same version.

- The **Plugin update** entity (on the device page) shows the plugin version on the device and the version of the integration, with an Install button. **Install plugin updates automatically** (a switch, on by default) lets Home Assistant start the update as soon as the device connects; turn it off to start updates yourself with Install.
- **You approve the install on the device.** Decky Loader always asks first: a dialog ("Install Decky Home Assistant") appears in the Steam interface and you tap OK. While it waits, the update entity says so (in its release summary) and a Repairs item, "Approve the plugin update on ...", reminds you. The device also shows a toast ("Plugin update ready"); tapping it shows the dialog again, which helps if the dialog disappeared (for example after Steam restarted). If the device is in desktop mode, switch it to game mode: the dialog appears there. If you cancel or ignore it, after 30 minutes the update is marked as not approved (a Repairs item says so); press Install to ask again.
- The device downloads the release from GitHub itself and checks it against the checksum in the release's `SHA256SUMS` file before it gives it to Decky Loader. A compromised GitHub release would not be noticed by this check. If something goes wrong the old plugin keeps running; a failed update is shown in Repairs and is not retried until you press Install or a newer version is out.
- If a new plugin installs but does not start, the device asks Decky Loader to put the previous version back (you approve that dialog too). If the plugin is ever too broken to do even that, install the zip from the releases page by hand as in step 2.
- Plugins older than 0.4.0 cannot be updated this way and still show the version-mismatch Repairs item: install the zip once by hand.
- Downgrades follow the integration too: if you roll the integration back in HACS to 0.4.0 or newer, the plugin is brought back to that version. Versions before 0.4.0 are never targeted.

## Wake on LAN

Turning the device on from Home Assistant sends a Wake on LAN packet, and the device has to be set up to accept it. **You have to do this on the SteamOS device yourself; the plugin cannot do it for you** (changing network settings needs administrator rights).

1. Open a terminal on the device (Desktop mode, for example Konsole). If you have not set an administrator password yet, run `passwd` first.
2. Find your connection and interface with `nmcli connection show --active` and `nmcli device status`.
3. Turn wake on for that connection. For a wired connection:
   ```
   sudo nmcli connection modify "<connection name>" 802-3-ethernet.wake-on-lan magic
   sudo nmcli device reapply <interface>
   ```
   For Wi-Fi use `802-11-wireless.wake-on-wlan magic` instead, and make sure the connection uses a stable MAC address.
4. Check it: put the device to sleep from Home Assistant (turn the media player off), then turn it on again.

The Wake on LAN section of the plugin panel shows your adapter, its MAC address and whether wake is enabled, and gives the exact command for your adapter. A wired connection is recommended: over Wi-Fi the device can wake from sleep but not from a full shutdown.

## Notes

- **Sleep and shutdown.** The device announces every way it goes down: the power button, the idle timer, Home Assistant and the Steam menu. The media player then shows standby (sleep) or off (shutdown) and the other entities become unavailable. Turn on sends a magic packet and everything comes back after the wake.
- **Waking up.** Home Assistant picks the device up again as soon as it announces itself on the network, and the device re-reads its real state first (a game that ended while it slept does not linger). How soon that is depends on how fast the device's network returns after a wake: a wired link can take ten seconds or more.
- **What the media player shows.** Idle, playing or paused while the device is awake; standby after a sleep; off after a shutdown or when it cannot be reached; unavailable only for a problem Home Assistant cannot fix by waiting (the device no longer accepts the pairing, or the plugin and the integration versions do not match). The media player stays available while the device is off so that Turn on works.
- **After a Home Assistant restart.** Home Assistant remembers the apps, controllers and sensors, so the entities exist at once; their values are unknown and the media player shows off until the device reports.
- **What is playing.** The media player shows the title, artist, album, a progress bar and the picture of whatever is playing, for every app that publishes its playback to the system (MPRIS). Tested with Jellyfin and YouTube (VacuumTube); other apps and most browsers should work the same way if they publish MPRIS, but Netflix, Twitch and Paramount have not been tested while playing. An app that publishes nothing shows its own name and its Steam library image, if it has one (for a non-Steam shortcut, set the artwork in Steam). When two players exist, a playing one wins; a paused one is used only when no Steam game is running. The progress bar uses the device's clock, so keep it set automatically. Images larger than 2 MiB are left out. The media player is for video, so the content type is always video (or app for the fallback).
- **Repairs.** If Home Assistant cannot reach the device for more than 24 hours a Repairs item says so. It goes away by itself when the device connects; if the device is normally switched off you can ignore it.
- **Diagnostics.** The device page has a Download diagnostics button. The file hides the pairing secret, addresses, MAC addresses, controller serial numbers, the network name and the title, artist and album that are playing, so it is safe to attach to an issue.
- **Desktop mode.** The plugin keeps running there, so the switch and the sensors keep working. Anything that needs the Steam interface (launching apps, toasts, picture-in-picture, suspend, restart) answers that it only works in game mode.
- **Volume.** With a soundbar or TV on HDMI CEC you get volume steps and an assumed mute (CEC has no readable level). Otherwise the media player has a real level and mute from the system mixer. This is detected automatically.
- **Camera picture-in-picture.** H.264 cameras play over WebRTC; cameras Home Assistant's WebRTC cannot carry (for example H.265) fall back to HLS automatically. Unmuted video works; the overlay is dropped when the device sleeps.

## Troubleshooting

- **The device does not show up.** Home Assistant and the device are probably on different networks or VLANs and mDNS (multicast DNS) does not cross. Use Settings > Devices & services > Add integration > Decky Home Assistant and type the device's address instead.
- **"Too many wrong codes".** The device waits a minute and then shows a new code.
- **Entities are unavailable.** The device is asleep, off, or Home Assistant cannot reach port 47830. Check the Connectivity entity and the plugin panel.
- **I removed the device or reinstalled Home Assistant.** In the plugin panel press Reset pairing, then pair again.
- **Home Assistant says the integration or the plugin is too old.** Update the one that is older (a Repairs item tells you which). A plugin of version 0.4.0 or newer is updated by Home Assistant itself; an older one needs the zip installed by hand once.
- **The plugin update never finishes.** Look at the device: Decky Loader's install dialog is waiting for you, or the device is in desktop mode (switch to game mode). The update entity and the Repairs item say which.
- **Turn on does nothing.** Wake on LAN has to be set up on the device (see [Wake on LAN](#wake-on-lan)), and some devices cannot wake from a full shutdown.

## Uninstall

Remove the device in Home Assistant (this also resets the plugin's pairing), then remove the integration in HACS and the plugin in Decky Loader.

## More

- [Protocol](docs/protocol.md): how the integration and the plugin talk.
- [Development](docs/development.md): building, testing and deploying.
- [Releasing](docs/releasing.md).

## License

BSD-3-Clause, see [LICENSE](LICENSE).
