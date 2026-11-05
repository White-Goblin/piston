import { DialogButton, Focusable, PanelSection, PanelSectionRow, ProgressBar, SliderField } from "@decky/ui";
import { ReactNode, useEffect, useRef, useState } from "react";
import { FaMusic, FaPause, FaPlay, FaStepBackward, FaStepForward } from "react-icons/fa";
import { useTabRegistered } from "./tabState";
import { CardState, coverUrl, formatTime, sendCommand, sendVolume, shownPosition, useCard } from "./card";

const VOLUME_DEBOUNCE_MS = 150;
const PRESS_GAP_MS = 350; // the backend ignores faster presses too; this keeps a mashed button from even calling it
const VOLUME_HOLD_MS = 600; // after the last change the slider keeps the chosen value until the backend has had time to confirm it

function Cover({ src }: { src?: string }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);
  const box = { width: "100%", aspectRatio: "1 / 1", borderRadius: "0.3em", overflow: "hidden", background: "rgba(255,255,255,0.08)" } as const;
  if (!src || failed) {
    return (
      <div style={{ ...box, display: "flex", alignItems: "center", justifyContent: "center", fontSize: "3em", opacity: 0.6 }}>
        <FaMusic />
      </div>
    );
  }
  // keyed by the source: a new track never shows the old cover while the new one loads
  return <img key={src} src={src} onError={() => setFailed(true)} style={{ ...box, objectFit: "cover", display: "block" }} />;
}

function Controls({ card }: { card: CardState }) {
  const offered = card.commands ?? [];
  const lastPress = useRef(0);
  const toggle = card.playing ? "pause" : "play";
  const button = (command: "previous" | "play" | "pause" | "next", label: string, icon: ReactNode) => (
    <DialogButton
      disabled={!offered.includes(command)}
      onClick={() => {
        const now = Date.now();
        if (now - lastPress.current < PRESS_GAP_MS) return;
        lastPress.current = now;
        void sendCommand(command);
      }}
      aria-label={label}
      style={{ minWidth: 0, flex: 1, padding: "0.6em 0", display: "flex", justifyContent: "center" }}
    >
      {icon}
    </DialogButton>
  );
  return (
    <Focusable style={{ display: "flex", gap: "0.5em", width: "100%" }} flow-children="horizontal">
      {button("previous", "Previous", <FaStepBackward />)}
      {button(toggle, toggle === "pause" ? "Pause" : "Play", toggle === "pause" ? <FaPause /> : <FaPlay />)}
      {button("next", "Next", <FaStepForward />)}
    </Focusable>
  );
}

function VolumeSlider({ volume }: { volume: number }) {
  const [draft, setDraft] = useState<number | null>(null);
  const timers = useRef<{ send?: ReturnType<typeof setTimeout>; hold?: ReturnType<typeof setTimeout> }>({});
  useEffect(() => () => { clearTimeout(timers.current.send); clearTimeout(timers.current.hold); }, []);
  const change = (value: number) => {
    setDraft(value);
    clearTimeout(timers.current.send);
    clearTimeout(timers.current.hold);
    timers.current.send = setTimeout(() => void sendVolume(value), VOLUME_DEBOUNCE_MS);
    timers.current.hold = setTimeout(() => setDraft(null), VOLUME_HOLD_MS);
  };
  return <SliderField label="Music volume" value={draft ?? volume} min={0} max={100} step={1} showValue onChange={change} />;
}

/** The card itself (no section wrapper): cover, text, progress, buttons and this device's music volume. */
export function MediaCardBody({ card, config, now }: { card: CardState; config?: { port: number; nonce: string }; now: number }) {
  const position = shownPosition(card, now);
  const duration = typeof card.duration_ms === "number" && card.duration_ms > 0 ? card.duration_ms : undefined;
  const progress = position !== undefined && duration ? Math.min(100, (position / duration) * 100) : 0;
  const text = [card.artist, card.album].filter(Boolean).join(" · ");
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.6em", width: "100%" }}>
      <Cover src={coverUrl(config, card.art)} />
      <div style={{ minWidth: 0 }}>
        <div style={{ fontWeight: "bold", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{card.title || card.artist || ""}</div>
        {text && card.title && <div style={{ opacity: 0.7, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{text}</div>}
      </div>
      {position !== undefined && (
        <div>
          {duration !== undefined && <ProgressBar nProgress={progress} />}
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.8em", opacity: 0.7 }}>
            <span>{formatTime(position)}</span>
            {duration !== undefined && <span>{formatTime(duration)}</span>}
          </div>
        </div>
      )}
      <Controls card={card} />
      <VolumeSlider volume={typeof card.volume === "number" ? card.volume : 0} />
    </div>
  );
}

/** The card as the first thing in the Piston panel; renders nothing while nothing plays. */
export function MediaCard() {
  const { card, config, now } = useCard();
  const inTab = useTabRegistered();
  if (inTab || !card.active) return null; // one placement at a time
  return (
    <PanelSection title="Now playing">
      <PanelSectionRow>
        <MediaCardBody card={card} config={config} now={now} />
      </PanelSectionRow>
    </PanelSection>
  );
}
