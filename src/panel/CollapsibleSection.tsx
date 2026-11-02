import { ButtonItem, PanelSectionRow } from "@decky/ui";
import { ReactNode, useState } from "react";

// Remembered while Steam is running so a section stays open when the panel is reopened
const openSections = new Map<string, boolean>();

// Renders rows only: Panel wraps all collapsible sections in one PanelSection so the
// headers stack closely instead of each getting its own section spacing
export function CollapsibleSection(props: {
  title: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(openSections.get(props.title) ?? props.defaultOpen ?? false);
  const toggle = () => {
    openSections.set(props.title, !open);
    setOpen(!open);
  };
  return (
    <>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={toggle}>
          {open ? "▾" : "▸"} {props.title}
        </ButtonItem>
      </PanelSectionRow>
      {open && props.children}
    </>
  );
}
