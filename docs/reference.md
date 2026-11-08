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

## Remote card

The dashboard card `piston-remote-card` (name "Piston remote") ships inside the integration: when Home Assistant starts, the integration serves `custom_components/piston/www/piston-remote-card.js` and adds it to the frontend, with the integration version and a fingerprint of the file in the address so the Home Assistant app loads the new file after an update. If registering the card fails, only the card is lost; a warning is logged.

### Configuration

| Key | Default | Meaning |
|---|---|---|
| `type` | | `custom:piston-remote-card` |
| `device_id` | | A Piston device from Home Assistant's device registry. The card finds the media player of that device itself, so renaming an entity changes nothing |
| `speed` | 1 | Pointer speed multiplier, 0.25 to 4 (applied in the card; the device has no speed setting) |
| `pad_height` | 34 (with limits) | Touchpad height as a percentage of the screen height, 15 to 80. Setting it also lifts the touchpad's default minimum (220 px) and maximum (380 px), so a phone in a panel view can give the touchpad all the room |
| `gap_pad_volume` | 0 | Extra empty space in pixels (0 to 160) between the touchpad and the volume control |
| `gap_volume_buttons` | 0 | Extra empty space in pixels (0 to 160) between the volume control and the Steam, wheel and keyboard buttons. Without a volume control the first gap is used instead |
| `show_power`, `show_apps`, `show_volume`, `show_keyboard`, `show_transport` | true | Hide the power button, the app picker, the volume buttons, the keyboard button, or the previous, play/pause and next buttons |

The three layout settings are sliders in the card editor. In a panel (single card) view, raise `pad_height` and the gaps until the card fills your screen; they are independent, so you can give the extra room to the touchpad, to either gap, or split it.

### What the card does

- **Touchpad.** Drag moves the pointer, a short tap clicks, a two-finger tap right-clicks, two fingers scroll (a flick keeps scrolling and slows down by the speed of your finger). Moves are summed once per animation frame; clicks, keys and scroll steps are sent at once.
- **Arrow wheel.** A tap on a wedge sends the arrow, holding repeats it (after 350 ms, every 80 ms), and moving your thumb around the ring scrolls (clockwise is down) instead, linearly: it follows the thumb and stops when it stops. The centre sends Enter.
- **Buttons.** Steam menu and Quick Access open or close the menu like the physical buttons; Back (it sends Esc); a keyboard button that opens the phone keyboard. The card uses your Home Assistant theme's colors (text, primary and error colors, divider and card background), so it follows light and dark themes. Typing is mirrored into minimal edits, so swipe typing, autocorrect and dictation work. Only ASCII reaches the device: other characters are dropped and a notice says so.
- **Playback.** A row of five buttons at the bottom: previous track, seek back, play/pause, seek forward and next track. Previous, play/pause and next call the media player's `media_previous_track`, `media_play_pause` and `media_next_track`; the middle icon shows pause while something is playing. The two seek buttons (with the 10 second icons) move 10 seconds on each tap, and holding one for 0.4 seconds keeps going, one step every 0.4 seconds, through the media player's `media_seek`. Taps within three seconds of each other continue from where the last one aimed, and every step is sent as an absolute position, so a seek does not wait for the device to report a new one. Each step also tells the device how far the run of presses has moved (a `seek` input event), and the device, which draws the cursor, does two things with it, each with its own switch in the Piston panel under **Remote input** (both on by default). **Show seek amount on the cursor**: the circle that follows the pointer on the device's screen flows to the side (left for a seek back, right for forward), turns into a label (`+10`, `+30`, `-10`, or `+1:30` from a minute on; presses within three seconds of each other add up) and, after about a second, flows back. **Nudge the pointer on seek**: the device moves the pointer 8 pixels toward the middle of the screen and, 80 ms later, back, without moving the drawn cursor, because video apps show their progress bar when they see input and a seek is not input. Turn the nudge off if the pointer movement bothers you; the apps then do not show their bar. The seek buttons are dimmed when the player reports no length (a live stream, a game) and work only with a plugin that knows `media_seek` and a player on the device that accepts MPRIS seeks. Players differ in how they take a seek: the device asks for an exact position (MPRIS `SetPosition`) and falls back to stepping (`Seek`) when a player has no track id or refuses it. The whole row is off while the device is off and can be hidden with `show_transport`.
- **Volume, power, apps.** The volume control follows how the device's volume works. With system volume the media player has a level, so the card shows a slider (0 to 100, with the level shown and a mute button; the new level is sent at most every 100 ms while you drag and the final one when you let go). With HDMI CEC there are only steps and an assumed mute, so the card shows volume down, mute and up buttons (hold to repeat). Both use the media player's own services and switch by themselves if the device changes mode. The power button follows the media player: off or asleep wakes the device (Wake on LAN, no question); awake asks before turning it off (sleep or shutdown, as set in the options). The picker lists the media player's sources, which are Steam and the apps added under *Apps in Home Assistant* in the Piston panel on the device; choosing an app launches it, and choosing Steam closes the running app.

### States

| State | Pad, wheel, Back, keyboard | Steam and Quick Access | Power | Banner |
|---|---|---|---|---|
| On, game mode | yes | yes | yes | none |
| On, desktop mode | yes (no cursor is drawn: the real one shows) | no | yes | Desktop mode |
| Off or asleep | no | no | yes (wakes it) | Device is off |
| Not reachable | no | no | yes | Can't reach the device |
| Plugin older than 0.8.0 | no | no | yes | Update the Piston plugin |
| **Allow remote input** off on the device | no | no | yes | Remote input is turned off on the device |
| The device could not create its virtual input devices | no | no | yes | The device could not start remote input |
| Not an administrator, option off | no | no | yes | Only administrators can use remote input |
| Plugin and integration versions differ | no | no | yes | The Piston plugin and integration versions do not match |

Volume and the app picker follow the media player: they work while the device is on.

### Access and safety

- The card talks to Home Assistant, never to the device directly: it uses two websocket commands, `piston/input/subscribe` (the status above) and `piston/input/send` (the events), so it works over https and through Home Assistant's remote access. The device keeps its single paired, authenticated connection; nothing new listens.
- Both commands need a Home Assistant administrator unless the option **Remote input for all users** is on; this is checked on every call.
- Typed text passes through Home Assistant and the integration in memory only and is never logged or stored. Diagnostics report only whether remote input is available and counters.
- The device creates its virtual pointer and keyboard (`Piston Pointer`, `Piston Keyboard`) on the first input, and removes them when **Allow remote input** is turned off and when the plugin unloads. Anything held down is released when the card goes away, the page goes to the background, Home Assistant restarts or the connection drops.
- Several phones can use the card at the same time; the last input wins.

### Limits

- Phone first: portrait on a phone is what it is designed for; other sizes work but are not tuned.
- On an iPhone a two-finger sideways drag also switches dashboard tabs in the Home Assistant app and the card cannot block it; use a dashboard with one view.
- Only ASCII can be typed. Click-and-drag is not supported.
- The cursor and the Steam menu and Quick Access buttons need the Steam interface, so they are not available in desktop mode.

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
