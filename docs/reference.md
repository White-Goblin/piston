# Reference

Detailed behavior that is not needed to install or use Piston. The README covers install, update and options.

## Actions

Actions (Developer tools, automations and scripts):

- `piston.show_toast` on the Toast entity: `message`, optional `title`, `duration` (seconds), `icon` (bell, bolt, camera, car, door, fire, home, light, lock, motion, temperature, warning, water), `silent`, and `id` (comes back in the click event so you can tell toasts apart).
- `piston.show_pip` on the PiP camera entity: `camera` (any camera entity) and optional `position`, `size`, `duration` and `mute`, overriding the options for that call. A new camera replaces the one showing.
- `piston.hide_pip` on the PiP camera entity.

## Setup parameters

Setting up needs no typing when the device is found automatically: you confirm it and enter the pairing code. If Home Assistant cannot see the device (for example it is on another VLAN), add it by address instead:

| Parameter | Meaning |
|---|---|
| Host or IP address | The address of the device, for example `192.168.1.20`. Only asked when you add the device by address. |
| Port | The port the plugin listens on. Default `47830`; only change it if you changed it on the device. |
| Pairing code | The 6 digits shown in the plugin panel on the device, valid for five minutes. Five wrong codes lock pairing for a minute. |

To change the address later (the device got a new IP address and Home Assistant does not find it by itself) use the three-dot menu of the integration entry > Reconfigure. The pairing is kept.

## Sendspin player

From version 0.6.0 the device can also be a speaker for [Music Assistant](https://www.music-assistant.io/), using the open [Sendspin](https://github.com/Sendspin/spec) protocol. Music Assistant finds the device by itself, music and text-to-speech announcements play through the device's audio output, and they are **mixed with the game, the apps and the system sounds** instead of replacing them.

**Two players with two jobs.** The Piston media player controls the device and shows what its apps are playing (launch, transport keys, system volume, artwork). The Sendspin player is the background audio: Music Assistant creates its own media player for it in Home Assistant, with its own volume, mute and artwork, and that is where you play music and send announcements. It is named after the device with "audio" added (for example "Living room audio"), so you can tell it from the Piston media player. Piston does not add a second media player for it; it adds a switch and a status sensor (below).

How to use it:

- Play music: pick the device's Music Assistant player in Music Assistant, or call `media_player.play_media` on that entity.
- Announcements: use text to speech on the Music Assistant entity, for example `tts.speak` with `media_player_entity_id` set to it. Music Assistant raises the volume, plays the announcement and then continues the music.
- Volume and mute of the Sendspin player only change the Sendspin stream. They do not touch the system or HDMI CEC volume that the Piston media player controls, and they are remembered across restarts.

What Piston adds in Home Assistant, on the device page:

| Entity | What it does |
|---|---|
| Sendspin player (switch, configuration) | Turns the player on or off. On by default. The state shown is what the device reports. |
| Sendspin status (sensor, diagnostic) | `disabled`, `waiting` (listening, no server), `connected`, `streaming` or `error`. Attributes: `server_name`, `port`, `error`, `player_name`. |

Both only exist for devices whose plugin has the player.

**Security: anyone on your network can stream audio to the device.** Sendspin allows unauthenticated sessions, which is how Music Assistant connects, and Piston does not support Sendspin pairing yet. Any computer on the same network can therefore connect to the player and play sound on the device (it cannot read anything from the device or control anything except the sound and volume of the Sendspin player itself). The connection is encrypted but not authenticated. If that matters on your network, turn the **Sendspin player** switch off; the device then stops listening and stops advertising itself. Only a few connections are accepted at once and a browser page cannot connect (requests with an Origin header are refused).

Details:

- The player is named "<device name> audio". Music Assistant needs to reach TCP port 8928 on the device (Piston uses the next free port if 8928 is taken; the status sensor shows the port) and must be on the same network segment, because it finds the device with mDNS (`_sendspin._tcp`).
- It decodes FLAC (16 and 24-bit, 44.1 and 48 kHz), Opus (48 kHz) and raw PCM (16-bit stereo, 44.1 and 48 kHz). The plugin offers them in that order, so Music Assistant normally sends FLAC. The decoders are the device's own `libFLAC` and `libopus`, loaded at start and checked with a short self-test; one that is missing or fails is not offered (PCM always is), and the plugin log says which codecs are available. 24-bit audio is played at 24-bit precision.
- It follows the device: before a sleep or shutdown it says goodbye to Music Assistant and stops advertising, and after a wake it comes back by itself once the network is up. It is part of the plugin's background service, but it has not been tested in desktop mode yet.
- It is built to stay out of the way of the rest of the system. The audio output is opened and controlled by a separate task with short time limits, so an audio server that stops answering cannot freeze the connection to Music Assistant or the rest of the plugin; if the output cannot be opened the player retries with a growing delay (up to 30 seconds), tells Music Assistant it is unavailable meanwhile, and carries on when the output works again. Its `pactl` helper calls are killed if they hang. The Sendspin status sensor shows `error` with the reason while the output is down.
- **Long streams.** With crossfade (or gapless) on, Music Assistant keeps one stream going across tracks, sometimes for hours. The device's audio hardware does not run at exactly the speed of the server's clock, and left alone the small buffer in front of the audio output would drain and the stream would begin to stutter after about 20 to 25 minutes (measured: about 130 ppm on a Steam Machine). The player watches that buffer and keeps it full by repeating single audio frames, which is inaudible, and the plugin log notes the measured drift and the frames adjusted. This keeps playback in step with where the stream started; the absolute delay against other speakers still rests on the player's assumed 50 ms output delay, which Music Assistant's per-player output delay setting can adjust.
- One server at a time is connected; a newer connection normally takes over from the current one (Sendspin's rule: unless it has less activity, then it is turned away).
- Not supported yet: pairing, and ducking the game's sound during an announcement (Music Assistant tells the device nothing that marks an announcement, so there is nothing to react to).

### Media card

From version 0.7.0 the device shows what the Sendspin player is playing and has simple controls, for whoever is sitting at the device with a controller. Home Assistant is not involved.

- **Where:** a **Piston** tab in the Quick Access menu, next to the Decky tab. If that tab cannot be added (it relies on Decky Loader internals), the card appears at the top of the Piston plugin panel instead; it is never in both places.
- **What it shows:** the cover (square), title, artist, a progress bar with the elapsed and total time (only the elapsed time when the length is unknown), previous / play-pause / next, and a **Music volume** slider. The tab says "Nothing playing" and the panel shows nothing while there is no track.
- **Buttons** work through Music Assistant (a button pressed again within 0.4 seconds is ignored, because every pause or play restarts the stream), so one that Music Assistant does not offer for the current queue is disabled (for example next on a one-track queue does nothing). The slider sets this device's own Sendspin player volume, the same value Music Assistant shows; it does not change the system or HDMI CEC volume.
- **Limits:** no seeking, shuffle, repeat, mute button or queue. An announcement does not change the card: Music Assistant sends no information about it, so the card keeps showing the music. The card needs the Steam interface, so it is not available in desktop mode (the music keeps playing). The cover is a 512 pixel JPEG from Music Assistant.

## Plugin updates

From version 0.4.0 the plugin on the device follows the integration: when you update the integration and restart Home Assistant, the device is brought to the same version.

- The **Plugin update** entity (on the device page) shows the plugin version on the device and the version of the integration, with an Install button. **Install plugin updates automatically** (a switch, on by default) lets Home Assistant start the update as soon as the device connects; turn it off to start updates yourself with Install.
- **You approve the install on the device.** Decky Loader always asks first: a dialog ("Install Piston") appears in the Steam interface and you tap OK. While it waits, the update entity says so (in its release summary) and a Repairs item, "Approve the plugin update on ...", reminds you. The device also shows a toast ("Plugin update ready"); tapping it shows the dialog again, which helps if the dialog disappeared (for example after Steam restarted). If the device is in desktop mode, switch it to game mode: the dialog appears there. If you cancel or ignore it, after 30 minutes the update is marked as not approved (a Repairs item says so); press Install to ask again.
- The device downloads the release from GitHub itself and checks it against the checksum in the release's `SHA256SUMS` file before it gives it to Decky Loader. A compromised GitHub release would not be noticed by this check. If something goes wrong the old plugin keeps running; a failed update is shown in Repairs and is not retried until you press Install or a newer version is out.
- If a new plugin installs but does not start, the device asks Decky Loader to put the previous version back (you approve that dialog too). If the plugin is ever too broken to do even that, install the zip from the releases page by hand as in step 2.
- Plugins older than 0.4.0 cannot be updated this way and still show the version-mismatch Repairs item: install the zip once by hand.
- Downgrades follow the integration too: if you roll the integration back to 0.4.0 or newer, the plugin is brought back to that version. Versions before 0.4.0 are never targeted.

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
