import { DropdownItem, PanelSectionRow, ToggleField } from "@decky/ui";
import { useEffect, useState } from "react";
import { CollapsibleSection } from "./CollapsibleSection";
import { Library, LibraryGame, isInstalled, listLibrary } from "../control/device";
import { PanelState } from "./types";

/** The apps offered as sources in Home Assistant's media player. */
export function AppsSection({ settings, setApps }: Pick<PanelState, "settings" | "setApps">) {
  const [library, setLibrary] = useState<Library>({ steam: [], nonSteam: [] });
  useEffect(() => setLibrary(listLibrary()), []);

  const isOffered = (g: LibraryGame) => settings.apps.some((x) => x.appid === g.appid);
  const add = (list: LibraryGame[], appid: string) => {
    const app = list.find((g) => g.appid === appid);
    if (app && !isOffered(app)) setApps([...settings.apps, app]);
  };
  const options = (list: LibraryGame[]) =>
    list.filter((g) => !isOffered(g)).map((g) => ({ data: g.appid, label: g.name }));

  return (
    <CollapsibleSection title="Apps in Home Assistant" defaultOpen>
      {settings.apps.length === 0 && <PanelSectionRow>No apps yet. Add the games and apps you want as sources.</PanelSectionRow>}
      {settings.apps.map((g) => (
        <PanelSectionRow key={g.appid}>
          <ToggleField
            label={isInstalled(g.appid) ? g.name : `${g.name} (not installed)`}
            description={`${g.kind === "shortcut" ? "Non-Steam" : "Steam"} · ${g.appid}`}
            checked
            onChange={() => setApps(settings.apps.filter((x) => x.appid !== g.appid))}
          />
        </PanelSectionRow>
      ))}
      <PanelSectionRow>
        <DropdownItem
          label="Add Steam game"
          rgOptions={options(library.steam)}
          selectedOption={null}
          onChange={(o) => add(library.steam, o.data)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Add non-Steam game"
          rgOptions={options(library.nonSteam)}
          selectedOption={null}
          onChange={(o) => add(library.nonSteam, o.data)}
        />
      </PanelSectionRow>
    </CollapsibleSection>
  );
}
