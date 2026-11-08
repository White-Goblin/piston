import { useEffect, useState } from "react";
import { LibraryGame } from "../control/device";
import { getPairingState, getSettings, getStatus, resetPairing, saveApps, setRemoteInput, setSeekBadge, setSeekNudge } from "./api";
import { PairingState, PanelState, Settings, Status } from "./types";

const POLL_MS = 2000;

/** Settings, connection status and pairing state shared by all panel sections. */
export function usePanelState(): PanelState | undefined {
  const [settings, setSettings] = useState<Settings | undefined>();
  const [status, setStatus] = useState<Status | undefined>();
  const [pairing, setPairing] = useState<PairingState | undefined>();

  useEffect(() => {
    getSettings().then(setSettings);
    const refresh = () => {
      getStatus().then(setStatus).catch(() => {});
      getPairingState().then(setPairing).catch(() => {});
    };
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, []);

  if (!settings || !status || !pairing) return undefined;

  return {
    settings,
    status,
    pairing,
    // The backend validates and returns the saved list
    setApps: (apps: LibraryGame[]) => {
      setSettings({ ...settings, apps });
      saveApps(apps).then((saved) => setSettings((cur) => cur && { ...cur, apps: saved }));
    },
    // Optimistic: the panel shows the choice at once, the backend's answer (and the next poll) confirm it
    setRemoteInput: (enabled: boolean) => {
      setStatus({ ...status, remote_input: enabled, remote_input_error: "" });
      setRemoteInput(enabled).then((r) => setStatus((cur) => cur && { ...cur, remote_input: r.remote_input, remote_input_error: r.error }));
    },
    setSeekBadge: (enabled: boolean) => {
      setStatus({ ...status, seek_badge: enabled });
      setSeekBadge(enabled).then((r) => setStatus((cur) => cur && { ...cur, seek_badge: r.seek_badge }));
    },
    setSeekNudge: (enabled: boolean) => {
      setStatus({ ...status, seek_nudge: enabled });
      setSeekNudge(enabled).then((r) => setStatus((cur) => cur && { ...cur, seek_nudge: r.seek_nudge }));
    },
    reset: async () => {
      setPairing(await resetPairing());
      setStatus({ ...status, paired: false, connected: false });
    },
  };
}
