import { staticClasses } from "@decky/ui";
import { definePlugin, routerHook } from "@decky/api"
import { PipOverlay } from "./pip/PipOverlay";
import { registerPipControl } from "./pip/control";
import { registerDeviceControl } from "./control/device";
import { registerToasts } from "./notify/toasts";
import { registerUpdateInstaller } from "./update/install";
import { Panel } from "./panel/Panel";
import { FaHome } from "react-icons/fa";

export default definePlugin(() => {
  const unregisterToasts = registerToasts();

  routerHook.addGlobalComponent("DeckyHomeAssistantPip", PipOverlay);
  const unregisterPip = registerPipControl();
  const unregisterDevice = registerDeviceControl();
  const unregisterUpdate = registerUpdateInstaller();

  return {
    name: "Piston",
    titleView: <div className={staticClasses.Title}>Piston</div>,
    content: <Panel />,
    icon: <FaHome />,
    onDismount() {
      unregisterToasts();
      unregisterPip();
      unregisterDevice();
      unregisterUpdate();
      routerHook.removeGlobalComponent("DeckyHomeAssistantPip");
    },
  };
});
