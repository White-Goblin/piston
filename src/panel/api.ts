import { callable } from "@decky/api";
import { LibraryGame } from "../control/device";
import { PairingState, Settings, Status, WolInfo } from "./types";

export const getSettings = callable<[], Settings>("get_settings");
export const saveApps = callable<[apps: LibraryGame[]], LibraryGame[]>("save_apps");
export const getStatus = callable<[], Status>("get_status");
export const getPairingState = callable<[], PairingState>("get_pairing_state");
export const resetPairing = callable<[], PairingState>("reset_pairing");
export const getWolInfo = callable<[], WolInfo>("get_wol_info");
export const setSeekBadge = callable<[enabled: boolean], { seek_badge: boolean }>("set_seek_badge");
export const setSeekNudge = callable<[enabled: boolean], { seek_nudge: boolean }>("set_seek_nudge");
export const setRemoteInput = callable<[enabled: boolean], { remote_input: boolean; error: string }>("set_remote_input");
