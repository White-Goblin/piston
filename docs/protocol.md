# Protocol

The integration (Home Assistant) opens one websocket to the plugin on the device: `ws://<device>:47830/ws`. The device advertises itself over mDNS as `_deckyha._tcp.local.` with the TXT records `device_id`, `name`, `proto` and `paired`. The shared code lives in `decky_ha/proto/` (copied into the integration by `scripts/sync_proto.py`).

All frames are JSON text, at most 1 MB.

| Frame | Direction | Meaning |
|---|---|---|
| `hello` `{proto, min_proto, device_id, paired, name?}` | both | Version negotiation. Each side accepts its own version and the previous one; otherwise the device answers `version_skew` and closes with code 4000 |
| `challenge` / `proof` | device and HA | Mutual authentication for a paired device: the device sends a nonce, HA answers with its own nonce and an HMAC-SHA256 proof of the per-device secret, the device proves back. Proofs are labelled by role |
| `pair` `{step: begin / challenge / proof / done / error}` | both | Pairing for an unpaired device (see below) |
| `cmd` `{id, name, data}` | HA to device | A command; answered by a `result` with the same `id` |
| `result` `{id, ok, data?, error?}` | device to HA | `error.code` is one of `bad_request`, `unauthorized`, `busy`, `offline`, `not_running`, `unsupported`, `internal`, `version_skew` |
| `state` `{name, data}` | device to HA | The full latest value of a state group |
| `event` `{name, data}` | device to HA | A one-off event |

Close codes: 4000 version skew, 4001 replaced by a newer connection, 4002 unauthorized, 4003 bad request. The device keeps one authenticated connection; a new one replaces the old. Pings every 20 s, 60 s idle timeout.

## Pairing

The device shows a 6-digit code (valid for 5 minutes, 5 wrong attempts, then a 1-minute lockout). The integration and the device run SRP-6a (RFC 5054 2048-bit group, SHA-256) with the code as the password, so the code is never sent and a passive observer cannot test guesses offline. Both sides derive the long-term secret from the exchange (`HMAC-SHA256(session key, "decky-ha-secret")`); the secret is never transmitted.

## Commands

`launch {appid}`, `quit`, `power {action: suspend | restart | shutdown | restart_steam | desktop | game}`, `audio {action: volume_up | volume_down | mute | set_volume | set_mute, value?, steps?}`, `media_key {key: play_pause | play | pause | next | previous | stop}`, `toast {message, title?, duration (ms), icon?, silent?, id?}`, `pip_show {camera, session, position, size, duration (ms), mute}`, `pip_hide`, `pip_signal {session, kind, ...}`, `set_sensor_interval {seconds}`, `unpair`. Commands run one at a time and a newer `launch` replaces a queued older one.

## State groups and events

State groups: `device` (id, name, MAC, broadcast address, Wake on LAN facts, protocol, `session` = game or desktop), `apps` (offered apps, running app, `screen`), `audio` (`mode` cec or system, level and mute in system mode), `sensors`, `controllers` (`connected` and `seen`), `pip`.

Events: `going_to_sleep` (sent just before a suspend), `resumed` (after a wake), `toast_click {id, at}`, `pip_signal {session, kind: start | offer | candidate | hls | close, ...}`.

## Picture-in-picture signalling

The player page runs in a Steam browser view and holds no Home Assistant credential. It long-polls the plugin on loopback (`/pip/events`, `/pip/send`, protected by a per-run nonce); the plugin forwards its messages to the integration as `pip_signal` events and queues the integration's `pip_signal` commands for it. The integration calls the camera entity's WebRTC handlers (offer, candidates) or builds an HLS stream URL. The video itself flows directly between Home Assistant and the device.
