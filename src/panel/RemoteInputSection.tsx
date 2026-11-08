import { PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { PanelState } from "./types";

/** Whether Home Assistant's remote card may move the pointer and type on this device. */
export function RemoteInputSection({
  status,
  setRemoteInput,
  setSeekBadge,
  setSeekNudge,
}: Pick<PanelState, "status" | "setRemoteInput" | "setSeekBadge" | "setSeekNudge">) {
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
      {status.remote_input && (
        <>
          <PanelSectionRow>
            <ToggleField
              label="Show seek amount on the cursor"
              description="When the remote card seeks, the cursor circle flows aside and shows how far it moved, such as +30."
              checked={status.seek_badge}
              onChange={setSeekBadge}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="Nudge the pointer on seek"
              description="Moves the pointer a few pixels and back so the playing app shows its progress bar. Turn it off if the pointer movement bothers you."
              checked={status.seek_nudge}
              onChange={setSeekNudge}
            />
          </PanelSectionRow>
        </>
      )}
      {status.remote_input && status.remote_input_error && (
        <PanelSectionRow>
          <b>Remote input cannot start: {status.remote_input_error}</b>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}
