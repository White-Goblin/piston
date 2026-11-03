# Changelog

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
