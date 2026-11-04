# Changelog

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
