import { staticClasses } from "@decky/ui";
import { definePlugin, routerHook } from "@decky/api"
import { PipOverlay } from "./pip/PipOverlay";
import { registerPipControl } from "./pip/control";
import { registerDeviceControl } from "./control/device";
import { registerToasts } from "./notify/toasts";
import { Panel } from "./panel/Panel";
import { FaHome } from "react-icons/fa";

export default definePlugin(() => {
  const unregisterToasts = registerToasts();

  routerHook.addGlobalComponent("DeckyHomeAssistantPip", PipOverlay);
  const unregisterPip = registerPipControl();
  const unregisterDevice = registerDeviceControl();

  return {
    name: "Decky Home Assistant",
    titleView: <div className={staticClasses.Title}>Decky Home Assistant</div>,
    content: <Panel />,
    icon: <FaHome />,
    onDismount() {
      unregisterToasts();
      unregisterPip();
      unregisterDevice();
      routerHook.removeGlobalComponent("DeckyHomeAssistantPip");
    },
  };
});
