import { PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { PanelState } from "./types";

/** Whether Home Assistant's remote card may move the pointer and type on this device. */
export function RemoteInputSection({ status, setRemoteInput }: Pick<PanelState, "status" | "setRemoteInput">) {
  return (
    <PanelSection title="Remote input">
      <PanelSectionRow>
        <ToggleField
          label="Allow remote input"
          description="Lets Home Assistant move the pointer and type on this device. It is used by the Piston remote card."
          checked={status.remote_input}
          onChange={setRemoteInput}
        />
      </PanelSectionRow>
      {status.remote_input && status.remote_input_error && (
        <PanelSectionRow>
          <b>Remote input cannot start: {status.remote_input_error}</b>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}
