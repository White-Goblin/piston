import { PanelSectionRow } from "@decky/ui";
import { useEffect, useState } from "react";
import { CollapsibleSection } from "./CollapsibleSection";
import { getWolInfo } from "./api";
import { WolInfo } from "./types";

const message = (e: unknown) => {
  if (typeof e === "string") return e;
  if (e && typeof e === "object" && "message" in e) return String((e as { message: unknown }).message);
  return String(e);
};

/**
 * Wake on LAN is sent by Home Assistant; the device only has to allow it. Changing that needs administrator
 * rights the plugin does not have, so this section shows the state and the command to run in a terminal.
 */
export function WakeOnLanSection() {
  const [info, setInfo] = useState<WolInfo | undefined>();
  const [error, setError] = useState("");

  useEffect(() => {
    getWolInfo().then(setInfo).catch((e) => setError(message(e)));
  }, []);

  const adapter = info?.interface
    ? `${info.kind === "wireless" ? "Wireless" : "Wired"} adapter ${info.interface} · ${info.mac}`
    : info?.error || "Checking network adapter";
  const state = !info?.interface ? "" : !info.supported ? "Not supported by this adapter"
    : info.wol_enabled ? "Enabled: Home Assistant can wake this device" : "Disabled: Home Assistant cannot wake this device";

  return (
    <CollapsibleSection title="Wake on LAN">
      <PanelSectionRow>{adapter}</PanelSectionRow>
      {state && <PanelSectionRow>{state}</PanelSectionRow>}
      {info?.warnings?.map((w) => <PanelSectionRow key={w}>{w}</PanelSectionRow>)}
      {info?.command && (
        <>
          <PanelSectionRow>Run this in a terminal on the device (Desktop mode) to enable it:</PanelSectionRow>
          <PanelSectionRow>
            <code style={{ display: "block", wordBreak: "break-all", userSelect: "text", fontSize: "0.85em" }}>{info.command}</code>
          </PanelSectionRow>
        </>
      )}
      {error && <PanelSectionRow>{error}</PanelSectionRow>}
    </CollapsibleSection>
  );
}
