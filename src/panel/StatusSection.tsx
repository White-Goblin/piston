import { ButtonItem, ConfirmModal, PanelSection, PanelSectionRow, showModal } from "@decky/ui";
import { PanelState } from "./types";

const group = (code: string) => (code.length === 6 ? `${code.slice(0, 3)} ${code.slice(3)}` : code);

export function StatusSection({ status, pairing, reset }: Pick<PanelState, "status" | "pairing" | "reset">) {
  const confirmReset = () =>
    showModal(
      <ConfirmModal
        strTitle="Reset pairing?"
        strDescription="Home Assistant will lose control of this device until you pair it again with a new code."
        strOKButtonText="Reset"
        onOK={() => reset()}
      />,
    );

  return (
    <PanelSection title="Status">
      {pairing.paired ? (
        <>
          <PanelSectionRow>{status.connected ? "Connected to Home Assistant" : "Paired, waiting for Home Assistant"}</PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={confirmReset}>Reset pairing</ButtonItem>
          </PanelSectionRow>
        </>
      ) : (
        <>
          <PanelSectionRow>Not paired. In Home Assistant, add the discovered device and enter this code:</PanelSectionRow>
          <PanelSectionRow>
            <div style={{ fontSize: "2.2em", fontWeight: "bold", letterSpacing: "0.12em", textAlign: "center", width: "100%" }}>
              {pairing.code ? group(pairing.code) : "- - -   - - -"}
            </div>
          </PanelSectionRow>
          {pairing.locked && <PanelSectionRow>Too many wrong codes. A new code appears in a minute.</PanelSectionRow>}
          {!pairing.locked && pairing.expires_in !== undefined && (
            <PanelSectionRow>Expires in {Math.max(1, Math.round(pairing.expires_in / 60))} min</PanelSectionRow>
          )}
        </>
      )}
      {status.error && <PanelSectionRow><b>{status.error}</b></PanelSectionRow>}
    </PanelSection>
  );
}
