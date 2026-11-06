# Changelog

## 0.8.0

**Update the plugin on the device and the integration in Home Assistant together; a plugin on 0.4.1 or newer updates itself to this one (you approve the install on the device).** This release keeps protocol version 3 and adds one frame, so an older plugin keeps working for everything else, but the remote card needs this plugin.

### Added
- **A remote card for your dashboard.** The new **Piston remote** card, shipped with the integration (no resource to add), gives you a touchpad, an arrow wheel, Steam menu, Quick Access and Back buttons, the phone keyboard, volume (a slider with system volume, buttons with HDMI CEC), a power button and an app picker for the device, from the Home Assistant app on your phone, anywhere Home Assistant is reachable. A cursor is drawn on the device's screen above menus and games. Turning the device off always asks first; turning it on sends Wake on LAN. The card follows your Home Assistant theme, and three sliders in its editor (touchpad height and two gaps) fit it to your screen. See the [reference](https://github.com/White-Goblin/piston/blob/main/docs/reference.md#remote-card).
- **Remote input is for administrators.** The card can type on the device, so only Home Assistant administrators may use it unless you turn on the new option **Remote input for all users**. The Piston panel on the device has an **Allow remote input** switch (on by default) that makes the device ignore the card.
- The device reports whether it can take remote input and why not (the card shows it): off on the device, the virtual input devices could not be created, desktop mode, and so on.

### Notes
- Only ASCII can be typed. On an iPhone a two-finger sideways drag switches dashboard tabs and the card cannot prevent it: use a dashboard with a single view.
- The integration now depends on Home Assistant's `frontend`, `http` and `websocket_api` components (always present).

## 0.7.0

**Update the plugin on the device and the integration in Home Assistant together; a plugin on 0.4.1 or newer updates itself to this one (you approve the install on the device).** This release keeps protocol version 3 and changes nothing in Home Assistant itself.

### Added
- **FLAC and Opus for the Sendspin player.** The device now offers FLAC (16 and 24-bit, 44.1 and 48 kHz), PCM and Opus (48 kHz). Music Assistant picks the first format it can use, normally FLAC, which uses much less network than raw audio. A codec whose library does not load or fails its start-up self-test is simply not offered, and PCM always works.
- **A media card on the device.** While Music Assistant plays music on the device, a card shows the cover, title, artist and a progress bar, with previous, play/pause and next buttons and a volume slider for this device's player volume (the value Music Assistant shows for it, not the system volume). It is a new **Piston** tab in Steam's Quick Access menu, next to the Decky tab; if the tab cannot be added (for example after a Decky update), the card is shown at the top of the Piston plugin panel instead. It is empty while nothing plays and works with the controller. See the [reference](https://github.com/White-Goblin/piston/blob/main/docs/reference.md#media-card).

### Fixed (hardening of the Sendspin player)
- **The player no longer depends on the audio server answering.** Opening and controlling the audio output now happens in a task of its own with short time limits, so a PipeWire audio server that stops responding can no longer freeze the player's connection to Music Assistant (before, skipping or pausing could be ignored for about half a minute per track). If the output cannot be opened the player keeps the stream, retries with a growing delay (0.5 s up to 30 s), tells Music Assistant it is unavailable meanwhile, and plays on by itself when the output comes back. A `pactl` call that does not answer is killed instead of being left behind, a stalled output is replaced, and the log records what the audio processes were doing.
- **Long streams no longer stutter after 20 to 25 minutes.** With crossfade on, Music Assistant keeps one continuous stream across tracks, and on the development device the audio hardware consumed audio about 130 parts per million faster than the server's timeline. The small buffer between the player and the audio output slowly ran dry until the stream began to underrun, roughly 25 minutes in. The player now watches how full that buffer is and compensates by repeating single audio frames (at most a tenth of a percent of the audio, spread out, inaudible); frames are dropped only if the audio falls far behind. A control run with the correction off reproduced the stutter at about 28 minutes, and the same stream with it on ran 29 minutes with no underruns. The log shows the measured drift and the frames adjusted every minute while streaming.
- **The card's buttons ignore a faster press.** At least 0.4 seconds must pass between two commands from the card. Each pause or play makes Music Assistant restart the stream, so a mashed button used to cause a storm of restarts.
- **Unloading the plugin is immediate and leaves nothing behind.** Decky's plugin runner stops running the plugin's event loop right after it asks the plugin to unload, so the old unload could never finish and Decky killed the plugin after five seconds every time (a `gdbus monitor` helper was then left running for good, one per restart). The plugin now does all of its cleanup synchronously in the first moment of the unload: it saves a pending volume, says goodbye to Music Assistant and Home Assistant, withdraws its mDNS records, kills the audio output and its helper processes. It stops in about 0.1 s, and at start it also stops helpers that earlier runs left behind.

### Notes
- Still not supported: pairing, and lowering the game's sound during an announcement (Music Assistant gives the device no signal for it).
- The Quick Access tab relies on Decky Loader internals and is guarded: if it fails, the card moves to the plugin panel and nothing else is affected.

## 0.6.0

**Update the plugin on the device and the integration in Home Assistant together; a plugin on 0.4.1 or newer updates itself to this one (you approve the install on the device).** This release keeps protocol version 3.

### Added
- **Sendspin player.** The device is now a speaker for Music Assistant: music and text-to-speech announcements play through the device's audio output, mixed with the game, the apps and the system sounds. It has its own volume and mute (remembered across restarts) that do not touch the system or CEC volume. Music Assistant creates the media player for it in Home Assistant (named "<device name> audio"); Piston adds a **Sendspin player** switch and a **Sendspin status** sensor. See the [reference](https://github.com/White-Goblin/piston/blob/main/docs/reference.md#sendspin-player).
- The player is on by default and accepts unauthenticated Sendspin sessions, so any computer on the network can play audio on the device. Turn the switch off if you do not want that. Pairing is not supported yet.
- It listens on TCP port 8928 (the next free port if taken), says goodbye and stops advertising before a sleep or shutdown, and starts again after a wake.

### Notes
- PCM only for now (no FLAC or Opus), and no ducking of the game's sound during an announcement.
- The plugin uses the device's own OpenSSL through `ctypes`; it still has no dependencies to install.

## 0.5.0

**Piston: media and control for SteamOS devices in your living room.** A Home Assistant integration plus a small Decky Loader plugin that pushes commands from Home Assistant to the box under the TV (a Steam Deck, a Steam Machine or any other Decky Loader device).

### Highlights
- **Media player:** launch Steam games and apps as sources, transport keys (play, pause, next, previous), volume through CEC or the system, and what is playing with artwork.
- **Power:** turn the device on with Wake on LAN, put it to sleep or shut it down, and an accurate state after every wake.
- **Notifications:** show a toast on the device and get a click event back in your automations.
- **Camera picture-in-picture:** put any Home Assistant camera on the screen, WebRTC with an HLS fallback.
- **Device sensors and switches:** CPU, GPU, fan, memory, disk, controller batteries, the Steam screen, desktop mode, and restart buttons.
- **Easy setup:** the device is found automatically and paired with a 6-digit code shown on its screen; no Home Assistant credential is stored on it.
- **Self-updating plugin:** Home Assistant keeps the plugin on the device in step with the integration (you approve the install on the device).

### Upgrading from 0.4.x
**The project is now called Piston, and this is a clean break: it does not upgrade an install of 0.4.x.** Remove the old integration and plugin, install the new ones together (the integration and the plugin must be installed together) and pair again. This release keeps protocol version 3.

### Changed
- New name, logo and tagline ("Media and control for SteamOS devices in your living room").
- The integration domain is `piston` (was `decky_home_assistant`), the plugin folder and Decky Loader name are `Piston`, the device advertises `_piston._tcp` over mDNS and the plugin zip is `piston-<version>.zip`. Entity IDs, services and automations that used the old domain have to be recreated.
- The Python package on the device is `piston_device`.
- `hacs.json` declares Home Assistant 2026.10.0 as the minimum, the new icon replaces the old one, and each GitHub release body is now the changelog section (shown in Home Assistant's update entity).
- The README explains installing through the Marketplace (Home Assistant 2026.11) or HACS.

## 0.4.2

**Update the plugin on the device and the integration in Home Assistant together; a plugin on 0.4.1 updates itself to this one (you approve the install on the device).** This release keeps protocol version 3.

### Added
- A "Plugin update ready" toast on the device when Decky Loader's install dialog is requested. Tapping it asks for the dialog again (it never approves anything for you). Decky's dialog takes the focus while it is open, so the toast is mainly useful when the dialog has gone away.

### Fixed
- The install dialog was lost when the Steam interface restarted while it was open (switching between desktop and game mode, for example), while Home Assistant kept asking you to approve it. The plugin now asks for a fresh dialog when the Steam interface loads.
- The automatic return to the previous plugin version, when a new version installs but does not start, never ran: a call to a plugin backend that has stopped does not fail, it waits for ever. Every such call now gives up after 8 seconds.

## 0.4.1

**Update the plugin on the device and the integration in Home Assistant together; from 0.4.0 on the plugin updates itself.** This release keeps protocol version 3. It is the first release checked with the plugin self-update on a real device: with 0.4.0 installed on both sides, updating the integration to 0.4.1 makes Home Assistant offer the plugin update, which you approve on the device.

### Fixed
- The plugin could not download its update from GitHub: Decky Loader's Python has no certificate bundle, so the download failed with a certificate error. The system's certificates are now loaded explicitly.
- The plugin's Steam interface ignored an update offered for a version it had asked for before, and could mistake "no update pending" for a plugin that does not answer.

## 0.4.0

**Update the plugin on the device and the integration in Home Assistant together, by hand, this one last time.** This release keeps protocol version 3 and adds the first plugin that Home Assistant can update by itself; releases before it cannot be, so install the integration through HACS and the plugin zip through Decky Loader, then restart Home Assistant. From the next release on the plugin follows the integration.

### The plugin updates itself
- A **Plugin update** entity shows the plugin version on the device against the integration's version, with an Install button, and an **Install plugin updates automatically** switch (on by default).
- After you update the integration, Home Assistant tells the device to install the matching plugin release. The device downloads it from GitHub, checks it against the `SHA256SUMS` published with the release and hands it to Decky Loader, which asks you to approve the install on the device (Home Assistant says so on the entity and in Repairs). Nothing is installed without your approval.
- It works across protocol changes: the handshake is now stable, so a device and an integration on different protocol versions can still reach each other to update the plugin (everything else is still refused until the versions match).
- A failed update keeps the old plugin, shows a Repairs item and is not retried until you press Install; a dialog that is not approved within 30 minutes counts as not approved. A new plugin that installs but does not start is replaced by the previous version again.
- The release now includes `SHA256SUMS` next to the plugin zip.

### Changed
- The plugin reports its version in its `hello` (`plugin_version`, `lane`); diagnostics show the versions and the last update failure.

### Fixed
- The plugin failed to unload when Decky Loader unloaded it while it was still starting.

## 0.3.0

**Update the plugin on the device and the integration in Home Assistant together.** This release uses protocol version 3 and neither side talks to version 2: a mismatch shows a Repairs item in Home Assistant that names the side to update, and the entities are unavailable until both match. Install the integration through HACS and the plugin zip through Decky Loader, then restart Home Assistant.

### What is playing, with artwork
- The media player shows what is playing: title, artist, album, a progress bar and the picture. It works for any app that publishes its playback to the system (MPRIS), for example Jellyfin and YouTube (VacuumTube); the media player is for video, so the content type is always video.
- Apps and games that publish nothing keep showing their name, and get their Steam library image when there is one.
- A paused background player (a browser tab, for example) never replaces a running game; a playing one does.
- Whenever a player is playing or paused the media player shows playing or paused, also in desktop mode and when Steam reports no running app, instead of idle.
- The picture is fetched from the device only when it changes. Images over 2 MiB are not shown, and nothing is resized.
- Diagnostics show the shape of what is playing but not the title, artist or album.

### Changed
- Protocol version 3: a new `media` state group and a `get_artwork` command (see [docs/protocol.md](docs/protocol.md)).

## 0.2.0

**Update the plugin on the device and the integration in Home Assistant together.** This release uses protocol version 2 and neither side talks to version 1: a mismatch shows a Repairs item in Home Assistant that names the side to update, and the entities are unavailable until both match. Install the integration through HACS and the plugin zip through Decky Loader, then restart Home Assistant.

### Better wake-up and state handling
- The device announces every sleep and shutdown (power button, idle timer, Home Assistant, Steam menu), not only the ones Home Assistant starts. Before, a sleep from the power button left the media player showing the old state for the whole sleep.
- After a wake the device re-reads its real state (session, audio, sensors, controllers, running app) instead of resending what it had, waits for its network to return, announces itself on mDNS and then sends only what changed.
- Home Assistant reconnects every 5 seconds for ten minutes and then every 30 seconds (it used to back off up to five minutes), reconnects at once when the device announces itself, and notices a half-open connection within about 22 seconds.
- State frames are sequenced; Home Assistant asks for a full resend when it misses one.
- A shutdown shows the media player as off immediately.

### Availability and persistence
- The media player stays available while the device is off or asleep (Turn on keeps working); every other entity is unavailable then. A rejected pairing or a version mismatch makes everything unavailable.
- Home Assistant stores the apps, controllers, sensor names and device facts, so entities exist after a restart (values unknown, media player off until the device reports).
- A Repairs item appears when the device has not been reachable for more than 24 hours.

### Home Assistant quality
- Silver on the integration quality scale: `quality_scale.yaml` for all 54 rules, `PARALLEL_UPDATES` on every platform, wrong input reported as a validation error, descriptions for every setting, a reconfigure flow (change the address, keep the pairing), diagnostics with redaction, and a test suite that runs inside a real Home Assistant.
- Settings and setup parameters are documented in the README.

### Fixed
- A timer kept running after the integration was unloaded.
- The mDNS announcement left a network socket open after a failed start.
- Controller serial numbers could appear in diagnostics.
- The camera overlay's fallback address crashed when Home Assistant had no internal URL; it now says what to set.

## 0.1.1
- The media player shows paused when the running app reports it.

## 0.1.0
- First release: the HACS integration and the Decky plugin (discovery, pairing with a code, media player, sensors, notifications, camera picture-in-picture).
