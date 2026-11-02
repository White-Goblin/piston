import { addEventListener, callable, removeEventListener } from "@decky/api";

import { PipPosition, getPipState, hidePip, showPip, subscribePipState } from "./store";

export interface PipCommand {
  action: "show" | "hide";
  entity_id?: string;
  /** Signalling session shared with the integration; the player passes it on every request */
  session?: string;
  position?: PipPosition;
  size?: number;
  duration?: number;
  mute?: boolean;
}

const getPipConfig = callable<[], { port: number; nonce: string }>("get_pip_config");
const reportPipState = callable<[state: string, entityId: string, message: string], void>(
  "report_pip_state"
);

let hideTimer: ReturnType<typeof setTimeout> | undefined;
let currentEntity = "";

const clearHideTimer = () => {
  if (hideTimer !== undefined) clearTimeout(hideTimer);
  hideTimer = undefined;
};

export const hideCamera = () => {
  clearHideTimer();
  hidePip();
};

export const showCamera = async (
  entityId: string,
  { session = "", position, size, duration = 30000, mute = true }: Omit<PipCommand, "action" | "entity_id"> = {}
) => {
  const { port, nonce } = await getPipConfig();
  const params = new URLSearchParams({
    entity: entityId,
    nonce,
    session,
    mute: mute ? "1" : "0",
    t: String(Date.now()),
  });
  currentEntity = entityId;
  clearHideTimer();
  showPip(`http://127.0.0.1:${port}/player.html?${params}`, {
    ...(position && { position }),
    ...(size && { size }),
  });
  if (duration > 0) hideTimer = setTimeout(hideCamera, duration);
};

// "error: msg" -> state "error", message "msg"
const splitStatus = (status: string): [string, string] => {
  const i = status.indexOf(": ");
  return i < 0 ? [status, ""] : [status.slice(0, i), status.slice(i + 2)];
};

/** Wire the integration's PiP commands and state reporting; returns a cleanup function. */
export const registerPipControl = () => {
  const listener = addEventListener<[command: PipCommand]>("pip", (command) => {
    if (command.action === "hide") {
      // A hide for one camera must not dismiss a different one that is showing
      if (command.entity_id && getPipState().visible && command.entity_id !== currentEntity) return;
      return hideCamera();
    }
    if (command.entity_id) {
      showCamera(command.entity_id, command).catch((e) =>
        reportPipState("error", command.entity_id ?? "", String(e)).catch(() => {})
      );
    }
  });

  let last = "";
  const unsubscribe = subscribePipState((s) => {
    const key = `${s.visible}|${s.status}|${s.error}`;
    if (key === last) return;
    last = key;
    const [state, message] = s.error ? ["error", s.error] : splitStatus(s.status);
    reportPipState(state, s.visible ? currentEntity : "", message).catch(() => {});
  });

  return () => {
    removeEventListener("pip", listener);
    unsubscribe();
    hideCamera();
  };
};
