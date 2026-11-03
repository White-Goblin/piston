# Protocol

The integration (Home Assistant) opens one websocket to the plugin on the device: `ws://<device>:47830/ws`. The device advertises itself over mDNS as `_deckyha._tcp.local.` with the TXT records `device_id`, `name`, `proto` and `paired`. The shared code lives in `decky_ha/proto/` (copied into the integration by `scripts/sync_proto.py`).

This is **protocol version 2**. Both sides accept only version 2, so the plugin and the integration have to be updated together (see the [changelog](../CHANGELOG.md)).

All frames are JSON text, at most 1 MB.

| Frame | Direction | Meaning |
|---|---|---|
| `hello` `{proto, min_proto, device_id, paired, name?}` | both | Version negotiation. Each side accepts the versions from its `min_proto` up to its `proto`, and both are 2 now; a peer on another version gets `version_skew` and the device closes with code 4000. Home Assistant shows a Repairs item that says which side to update |
| `challenge` / `proof` | device and HA | Mutual authentication for a paired device: the device sends a nonce, HA answers with its own nonce and an HMAC-SHA256 proof of the per-device secret, the device proves back. Proofs are labelled by role |
| `pair` `{step: begin / challenge / proof / done / error}` | both | Pairing for an unpaired device (see below) |
| `cmd` `{id, name, data}` | HA to device | A command; answered by a `result` with the same `id` |
| `result` `{id, ok, data?, error?}` | device to HA | `error.code` is one of `bad_request`, `unauthorized`, `busy`, `offline`, `not_running`, `unsupported`, `internal`, `version_skew` |
| `state` `{name, data, boot_id, seq}` | device to HA | The full latest value of a state group, with the sequencing fields below |
| `event` `{name, data}` | device to HA | A one-off event |

Close codes: 4000 version skew, 4001 replaced by a newer connection, 4002 unauthorized, 4003 bad request. The device keeps one authenticated connection; a new one replaces the old.

Keepalive: the device pings every 20 s and closes a connection that stays silent for 60 s. The integration pings every 15 s and closes a connection whose ping is not answered within half that time, so a connection left half-open by a suspend is noticed in about 22 s.

## Pairing

The device shows a 6-digit code (valid for 5 minutes, 5 wrong attempts, then a 1-minute lockout). The integration and the device run SRP-6a (RFC 5054 2048-bit group, SHA-256) with the code as the password, so the code is never sent and a passive observer cannot test guesses offline. Both sides derive the long-term secret from the exchange (`HMAC-SHA256(session key, "decky-ha-secret")`); the secret is never transmitted.

## Commands

`launch {appid}`, `quit`, `power {action: suspend | restart | shutdown | restart_steam | desktop | game}`, `audio {action: volume_up | volume_down | mute | set_volume | set_mute, value?, steps?}`, `media_key {key: play_pause | play | pause | next | previous | stop}`, `toast {message, title?, duration (ms), icon?, silent?, id?}`, `pip_show {camera, session, position, size, duration (ms), mute}`, `pip_hide`, `pip_signal {session, kind, ...}`, `set_sensor_interval {seconds}`, `unpair`, and `resync` (answered with `ok` first, then every state group again; see below). Commands run one at a time and a newer `launch` replaces a queued older one.

## State groups and events

State groups: `device` (id, name, MAC, broadcast address, Wake on LAN facts, protocol, `session` = game or desktop), `apps` (offered apps, running app, `screen`), `audio` (`mode` cec or system, level and mute in system mode), `sensors`, `controllers` (`connected` and `seen`), `pip`.

Events: `going_to_sleep {reason: "suspend"}`, `shutting_down {reason: "shutdown" | "restart"}`, `resumed`, `toast_click {id, at}`, `pip_signal {session, kind: start | offer | candidate | hls | close, ...}`. The three power events are described under [Sleep, shutdown and wake](#sleep-shutdown-and-wake).

## State sequencing and resync

Every `state` frame carries `boot_id`, a random string that is new each time the plugin's server starts, and `seq`, a counter per state group that starts at 1 and goes up by one for every push of that group.

- The first frame of a group on a connection is the snapshot the device sends right after authentication. It carries the group's current `seq` and does not count as a new push.
- The integration applies `seq` = last + 1; drops `seq` less than or equal to the last one it has (stale or duplicate); and applies a frame that skips ahead, then asks for a `resync`, at most one request every 5 seconds. A new `boot_id` starts the counting again without a request.
- `resync` is answered with `ok`, followed by every group again. Each of these frames counts as a new push (`seq` + 1), so the integration applies them by the same rules. A second `resync` queued behind one that has not run yet is answered `ok {"coalesced": true}` and sends nothing.

## Sleep, shutdown and wake

The device announces every way it can go down, not only the ones Home Assistant starts: the hardware power button, the idle timer, Home Assistant and the Steam menu all end in the system's suspend, which the plugin watches for through systemd-logind (`PrepareForSleep` / `PrepareForShutdown`). While the plugin runs it holds a delay inhibitor, so the system waits (at most 2 s) after announcing the suspend while the event is sent and flushed; the inhibitor is released as soon as the event is out and taken again after the wake.

- `going_to_sleep {reason: "suspend"}` is sent before the suspend. The integration shows the media player as standby and the other entities as unavailable.
- `shutting_down {reason: "shutdown" | "restart"}` is sent before a shutdown or restart. The integration shows the media player as off at once instead of guessing from a dropped connection.
- After a wake the plugin reads the real state again instead of resending what it had: session, audio, sensors, controllers, and the running app (the Steam interface is asked through an `app_sync` request and retried for up to 10 s in the background). It then waits for the network to return (up to 60 s; a wired link can need 10 s or more), restarts its mDNS announcement, reads the network and Wake on LAN facts, sends the groups that changed, and sends `resumed` last.

The TCP connection usually survives a suspend, so the integration normally gets these frames on the connection it already has. If the connection did drop, the integration reconnects (below). If the system's sleep announcement is not available on a device, the plugin falls back to detecting a wake from the wall clock jumping and announcing only sleeps that it starts itself.

## How the integration reconnects

When a connection ends the integration keeps trying to reconnect: every 5 seconds for the first 10 minutes, then every 30 seconds, with a 2 second timeout for each attempt. It also reconnects at once when the device announces itself on mDNS (a wake produces such an announcement) and then follows an address change if there is one. A rejected secret stops the attempts and starts a re-pairing flow; a version mismatch retries every 60 seconds and raises a Repairs item. Home Assistant stores the structure it knows (apps, controllers, sensor names, device facts and the time of the last connection) so entities exist again after a restart, with values unknown until the device reports.

## Picture-in-picture signalling

The player page runs in a Steam browser view and holds no Home Assistant credential. It long-polls the plugin on loopback (`/pip/events`, `/pip/send`, protected by a per-run nonce); the plugin forwards its messages to the integration as `pip_signal` events and queues the integration's `pip_signal` commands for it. The integration calls the camera entity's WebRTC handlers (offer, candidates) or builds an HLS stream URL. The video itself flows directly between Home Assistant and the device.
