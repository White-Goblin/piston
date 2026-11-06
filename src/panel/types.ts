import { LibraryGame } from "../control/device";

export interface Settings {
  device_id: string;
  device_name: string;
  paired: boolean;
  /** Apps offered as sources in Home Assistant's media player */
  apps: LibraryGame[];
  sensor_interval: number;
}

export interface Status {
  device_id: string;
  paired: boolean;
  /** Home Assistant is connected right now */
  connected: boolean;
  port: number;
  error: string;
  /** Home Assistant may move the pointer and type on this device (the remote card) */
  remote_input: boolean;
  /** Set while remote input is on but the virtual input devices could not be created */
  remote_input_error: string;
}

export interface PairingState {
  paired: boolean;
  /** Only while unpaired; empty while the code is locked out */
  code?: string;
  expires_in?: number;
  attempts_left?: number;
  locked?: boolean;
}

export interface WolInfo {
  interface: string;
  kind?: "wired" | "wireless";
  mac?: string;
  supported?: boolean;
  wol_enabled?: boolean;
  /** Terminal command that turns wake on; empty when nothing needs doing */
  command?: string;
  warnings?: string[];
  error?: string;
}

export interface PanelState {
  settings: Settings;
  status: Status;
  pairing: PairingState;
  setApps: (apps: LibraryGame[]) => void;
  setRemoteInput: (enabled: boolean) => void;
  reset: () => Promise<void>;
}
