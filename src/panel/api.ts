import { callable } from "@decky/api";
import { LibraryGame } from "../control/device";
import { PairingState, Settings, Status, WolInfo } from "./types";

export const getSettings = callable<[], Settings>("get_settings");
export const saveApps = callable<[apps: LibraryGame[]], LibraryGame[]>("save_apps");
export const getStatus = callable<[], Status>("get_status");
export const getPairingState = callable<[], PairingState>("get_pairing_state");
export const resetPairing = callable<[], PairingState>("reset_pairing");
export const getWolInfo = callable<[], WolInfo>("get_wol_info");
