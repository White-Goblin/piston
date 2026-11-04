import { ErrorBoundary, PanelSection, PanelSectionRow } from "@decky/ui";
import { FaMusic } from "react-icons/fa";
import { useCard } from "./card";
import { MediaCardBody } from "./MediaCard";
import { setRegistered } from "./tabState";

/**
 * A top-level tab of Steam's Quick Access menu, next to (not inside) the Decky tab.
 *
 * Decky calls `DeckyPluginLoader.tabsHook.render(existingTabs, visible)` on every render of the menu and appends its own tab to
 * Steam's array. We wrap that function and append one more entry afterwards. The entry is NOT marked `decky`, so Decky's own
 * bookkeeping is untouched. `tabsHook.add` must never be used for this: it makes Decky push its tabs again on every render and
 * corrupts the menu (swe/notes/sendspin/qam-tab-spike.md). This depends on Decky internals, so everything is feature-detected
 * and guarded: when anything is missing the function returns null and the card stays in the Piston panel.
 */

const TAB_KEY = 998;

function MediaTab() {
  const { card, config, now } = useCard();
  return (
    <ErrorBoundary>
      <PanelSection title="Now playing">
        <PanelSectionRow>
          {card.active ? <MediaCardBody card={card} config={config} now={now} /> : <div style={{ opacity: 0.7 }}>Nothing playing</div>}
        </PanelSectionRow>
      </PanelSection>
    </ErrorBoundary>
  );
}

/** Returns the function that removes the tab, or null when the tab cannot be registered (the panel card is then used). */
export function registerMediaTab(): (() => void) | null {
  try {
    const hook = (window as any).DeckyPluginLoader?.tabsHook;
    if (!hook || typeof hook.render !== "function" || !Array.isArray(hook.tabs)) return null;

    const original = hook.render;
    const entry = { key: TAB_KEY, title: "Piston", tab: <FaMusic />, panel: <MediaTab />, piston: true };
    let enabled = true;
    let lastTabs: any[] | undefined;

    const wrapped = function (this: unknown, existing: any, visible: any) {
      const result = original.call(this, existing, visible); // Decky's own logic first, untouched
      try {
        if (enabled && Array.isArray(existing)) {
          lastTabs = existing;
          if (!existing.some((t) => t && t.piston)) existing.push(entry);
        }
      } catch (e) {
        console.warn("Piston: could not add the media tab", e);
      }
      return result;
    };
    hook.render = wrapped;
    setRegistered(true);

    return () => {
      enabled = false;
      try {
        if (hook.render === wrapped) hook.render = original; // else another plugin wrapped us: ours stays, disabled
        if (lastTabs) {
          const i = lastTabs.findIndex((t) => t && t.piston);
          if (i >= 0) lastTabs.splice(i, 1);
        }
      } catch (e) {
        console.warn("Piston: could not remove the media tab", e);
      }
      setRegistered(false);
    };
  } catch (e) {
    console.warn("Piston: media tab not available, the card stays in the panel", e);
    setRegistered(false);
    return null;
  }
}
