import { PanelSection } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { MediaCard } from "../media/MediaCard";
import { AppsSection } from "./AppsSection";
import { RemoteInputSection } from "./RemoteInputSection";
import { StatusSection } from "./StatusSection";
import { WakeOnLanSection } from "./WakeOnLanSection";
import { usePanelState } from "./usePanelState";

export function Panel() {
  const state = usePanelState();

  // On some layouts the Quick Access window is much wider than the part that is
  // on screen, and Steam centers dropdown menus in the whole window, which puts
  // them off the right edge. Limit the menu overlay to the visible panel.
  const panelRef = useRef<HTMLDivElement>(null);
  const [menuMaxWidth, setMenuMaxWidth] = useState(0);
  useEffect(() => {
    const right = panelRef.current?.getBoundingClientRect().right;
    if (right) setMenuMaxWidth(Math.ceil(right) + 16);
  }, [state?.settings]);

  if (!state) return <MediaCard />;

  return (
    <>
      {menuMaxWidth > 0 && <style>{`.BasicUIContextMenu { max-width: ${menuMaxWidth}px; }`}</style>}
      <div ref={panelRef} />
      <MediaCard />
      <StatusSection status={state.status} pairing={state.pairing} reset={state.reset} />
      <PanelSection>
        <AppsSection settings={state.settings} setApps={state.setApps} />
        <WakeOnLanSection />
      </PanelSection>
      <RemoteInputSection status={state.status} setRemoteInput={state.setRemoteInput} />
    </>
  );
}
