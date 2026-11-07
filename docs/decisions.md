# Decisions and closed questions

What was tried, rejected or accepted, with the reason, so nobody (human or agent) re-opens it by accident. Add an entry when you abandon an approach, accept a risk or close an open item; reopen one only when the engineer asks or the problem is seen again.

## Abandoned

### Keeping the remote cursor in place across app switches (2026-11-05)
**What:** keep the remote card's cursor where it was when the Steam interface regains focus from an app. **Findings:** the window system recenters the pointer on the screen centre on an app-to-Steam focus change, and one compensating relative move at the first input restored it exactly in a scripted spike (7 of 7). **Outcome:** a simplified build was run live with the card and made the snapping worse, so it was reverted and nothing was committed. **Why not retried:** the cause was not investigated (the live path differs from the script: real card input, the overlay view, the resync loop). Do not restart this unless asked.

## Accepted untested
These were closed on the engineer's acceptance. Describe them as "accepted untested" if they come up; do not list them as open work.

- **Cursor overlay stacking with the picture-in-picture window and the media tab.** The overlay was verified above a running game, the Steam menu and Quick Access. The PiP window is not clickable, so cursor-versus-PiP does not matter, and the original remote-control plugin was already vetted in those scenarios.
- **Remote card degraded states** (2026-11-05): non-admin user with and without "Remote input for all users", an old plugin version, a Home Assistant restart mid-use, two phones, desktop mode with the card, the system-volume slider on a real system-audio device, Wake on LAN from the card, two minutes of backgrounding, and pointer-speed numbers.
- **Sendspin** (2026-11-04): runs longer than 30 minutes, desktop mode, a running game, power-button suspend, live panel fallback, L1/R1 tab switching, 24-bit by ear, CEC, the default player name, `play_announcement`. The long-stream stutter (the cushion draining) is assumed fixed by `syncfix.py` unless it is seen again.

## Deliberately not built
- Ducking and Sendspin pairing in the Sendspin player.

## Operational notes (Decky Loader)
- **Decky stops itself after three Steam web helper crashes.** If the plugin vanishes from the Quick Access menu, look in `journalctl -u plugin_loader` for "webhelper crashed within a minute from last crash! crash count: 3" followed by "Shutting down". It exits with status 0, so systemd does not restart it; `sudo systemctl restart plugin_loader` brings it back. Seen once, after a Steam beta update crashed Steam four times.
- **Decky starves the plugin's event loop right after the unload request** (seen on Decky Loader 3.3.1). Its plugin-side socket reader stops yielding, so timers, subprocess waits and goodbye messages in `_unload` never finish, Decky kills the plugin after five seconds and child processes (`gdbus`, `systemd-inhibit`, `pw-cat`) are left running. This is why `Plugin._unload_now` is synchronous and every component has `abort()` (see [architecture.md](architecture.md)). After a restart, `journalctl -u plugin_loader` should show the plugin unloaded in well under a second; a "still alive 5 seconds after stop request" line is a regression.
