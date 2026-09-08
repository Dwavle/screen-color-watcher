#!/usr/bin/env python3
"""Watch a screen region or a specific window and send a macOS notification
when a target color appears in it.

Window mode captures the window's own backing store, so it keeps working
even when other windows (or browser tabs switched away from) cover it.

Usage:
    python watch_color.py --list-windows                    # see open windows and their app names
    python watch_color.py --window-owner Safari --add-color "#FF0000:Red Alert"
    python watch_color.py --pick-window --add-color "#FF0000:Red Alert"   # click to pick instead (only
                                                                            # works if the window is visible)
    python watch_color.py --pick-region --add-color "#FF0000:Red Alert"
    python watch_color.py                       # run with saved config
    python watch_color.py --add-color "#00FF00:Green"   # add another color
    python watch_color.py --add-color "#b4aeb1:Beskar:30:beskar detected"  # custom message
    python watch_color.py --window-owner Safari --pick-subregion --add-color "#b4aeb1:Beskar"  # watch just
                                                                                                   # part of the window
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import time
from pathlib import Path

import mss
import numpy as np

from ocr import extract_text
from region_picker import pick_region
from window_picker import (
    capture_window,
    find_window_by_owner_name,
    get_window_bounds,
    list_windows,
    pick_subregion,
    pick_window,
)

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS = {
    "mode": None,  # "region" or "window"
    "region": None,  # {"x": int, "y": int, "width": int, "height": int}
    "window": None,  # {"window_id": int, "owner": str, "name": str, "bounds": {...}, "subregion": {...} or absent}
    "colors": [],  # [{"name": str, "hex": "#RRGGBB", "tolerance": int}]
    "poll_interval": 1.0,
    "cooldown": 5,
    "confirm_delay": 0.5,  # re-check before alerting, to filter out momentary flicker
    "rainbow_mode": False,  # if on, suppress alerts when every monitored color is seen at once
    "ocr_word_list": [],  # [{"word": str, "color": str or None}] -- known-good words to snap fuzzy/misread
                          # OCR text onto, optionally colored in terminal output
    "ocr_key_combos": [],  # [[str, ...], ...] -- if non-empty, {text} colors only notify when the corrected
                           # OCR text contains ALL words of at least one combo (still always prints to
                           # terminal). E.g. [["Legendary","Galactic"]] notifies for that pair but not for
                           # "Legendary Diamond". Empty = always notify, as before.
    "notification_sound": "Glass",
}

NAMED_TERMINAL_COLORS = {
    "gray": (160, 160, 160),
    "grey": (160, 160, 160),
    "light_blue": (135, 206, 250),
    "purple": (155, 89, 182),
    "orange": (255, 140, 0),
    "reddish_pink": (255, 90, 120),
    "red": (220, 20, 60),
    "pink": (255, 105, 180),
    "green": (46, 204, 113),
    "blue": (52, 120, 246),
    "yellow": (255, 215, 0),
    "white": (255, 255, 255),
}

RAINBOW_CYCLE = [
    (255, 60, 60), (255, 150, 40), (230, 230, 60), (80, 220, 100),
    (70, 150, 255), (140, 90, 230), (230, 90, 200),
]

_ANSI_RESET = "\033[0m"


def _ansi_fg(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"\033[38;2;{r};{g};{b}m"


def colorize_word(word: str, color: str | None) -> str:
    """Wrap a single word in ANSI truecolor codes for terminal display.
    'rainbow' cycles a different color per character. Unrecognized colors
    (including None) return the word unchanged."""
    if not color:
        return word
    if color.lower() == "rainbow":
        out = ""
        for i, ch in enumerate(word):
            out += _ansi_fg(RAINBOW_CYCLE[i % len(RAINBOW_CYCLE)]) + ch
        return out + _ANSI_RESET
    key = color.strip().lower().replace(" ", "_")
    rgb = NAMED_TERMINAL_COLORS.get(key)
    if rgb is None:
        try:
            rgb = hex_to_rgb(color)
        except ValueError:
            return word
    return _ansi_fg(rgb) + word + _ANSI_RESET


def load_config() -> dict:
    if CONFIG_PATH.exists():
        config = {**DEFAULTS, **json.loads(CONFIG_PATH.read_text())}
    else:
        config = dict(DEFAULTS)
    # Migrate old flat-string ocr_word_list (just ["Word", ...]) to the
    # {"word": str, "color": str or None} shape used for terminal coloring.
    config["ocr_word_list"] = [
        w if isinstance(w, dict) else {"word": w, "color": None}
        for w in config.get("ocr_word_list", [])
    ]
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def correct_ocr_text(text: str, word_list: list[str], cutoff: float = 0.6) -> str:
    """Rebuild OCR'd text using only word_list's vocabulary: every word is
    snapped onto its closest match (e.g. 'Legendaty' -> 'Legendary'), and
    words with no good-enough match are dropped rather than kept raw -- so
    with a word list configured, the result is built entirely from that
    vocabulary. Case-insensitive; output uses word_list's casing. With no
    word list, returns text unchanged (nothing to correct against)."""
    if not word_list or not text:
        return text
    lower_to_original = {w.lower(): w for w in word_list}
    choices = list(lower_to_original.keys())
    kept = []
    for token in text.split():
        cleaned = token.strip(".,!?:;\"'()[]{}").lower()
        if not cleaned:
            continue
        match = difflib.get_close_matches(cleaned, choices, n=1, cutoff=cutoff)
        if match:
            kept.append(lower_to_original[match[0]])
    return " ".join(kept)


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Invalid hex color: {hex_color!r} (expected e.g. #FF0000)")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def parse_color_arg(arg: str, default_tolerance: int) -> dict:
    # Format: "#RRGGBB[:Name[:Tolerance[:Custom notification message]]]"
    # maxsplit=3 so the message itself may contain colons.
    parts = arg.split(":", 3)
    hex_color = parts[0]
    hex_to_rgb(hex_color)  # validate
    name = parts[1] if len(parts) > 1 and parts[1] else hex_color
    tolerance = int(parts[2]) if len(parts) > 2 and parts[2] else default_tolerance
    color = {"name": name, "hex": hex_color, "tolerance": tolerance}
    if len(parts) > 3 and parts[3]:
        color["message"] = parts[3]
    return color


def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_notification(title: str, message: str, sound: str | None) -> None:
    # message/title may now contain OCR'd on-screen text (untrusted content),
    # not just values the user typed themselves -- escape before embedding
    # them in the AppleScript source so stray quotes can't break out of the
    # string literal.
    script = f'display notification "{_escape_applescript(message)}" with title "{_escape_applescript(title)}"'
    if sound:
        script += f' sound name "{_escape_applescript(sound)}"'
    subprocess.run(["osascript", "-e", script], check=False)


def grab_region(sct: "mss.base.MSSBase", region: dict) -> np.ndarray:
    monitor = {
        "left": region["x"],
        "top": region["y"],
        "width": region["width"],
        "height": region["height"],
    }
    raw = sct.grab(monitor)
    # mss returns BGRA; drop alpha and reorder to RGB.
    arr = np.array(raw)[:, :, :3][:, :, ::-1]
    return arr


def color_present(frame: np.ndarray, rgb: tuple[int, int, int], tolerance: float) -> bool:
    diff = frame.astype(np.int32) - np.array(rgb, dtype=np.int32)
    dist = np.sqrt((diff ** 2).sum(axis=2))
    return bool(np.any(dist <= tolerance))


def crop_subregion(frame: np.ndarray, subregion: dict) -> np.ndarray:
    # Fractions of the captured frame rather than raw pixels, so the crop
    # stays correct across Retina scaling and if the window gets resized.
    h, w = frame.shape[:2]
    x0 = max(0, int(subregion["rel_x"] * w))
    y0 = max(0, int(subregion["rel_y"] * h))
    x1 = min(w, int((subregion["rel_x"] + subregion["rel_width"]) * w))
    y1 = min(h, int((subregion["rel_y"] + subregion["rel_height"]) * h))
    return frame[y0:y1, x0:x1]


def capture_frame(config: dict, sct: "mss.base.MSSBase | None") -> np.ndarray | None:
    if config["mode"] == "window":
        window = config["window"]
        frame = capture_window(window["window_id"])
        if frame is None:
            # Window id may have gone stale (app restarted). Try to relocate
            # it by owner + title and keep going without user intervention.
            relocated = find_window_by_owner_name(window["owner"], window["name"])
            if relocated:
                relocated["subregion"] = window.get("subregion")
                config["window"] = relocated
                save_config(config)
                frame = capture_window(relocated["window_id"])
                window = relocated
        if frame is not None and window.get("subregion"):
            frame = crop_subregion(frame, window["subregion"])
        return frame
    return grab_region(sct, config["region"])


def run_watch(config: dict) -> None:
    if config["mode"] == "window":
        if not config["window"]:
            sys.exit("No window configured. Run with --pick-window first.")
        w = config["window"]
        target_desc = f"window '{w['name'] or w['owner']}' ({w['owner']})"
        if w.get("subregion"):
            target_desc += " [sub-region]"
    elif config["mode"] == "region":
        if not config["region"]:
            sys.exit("No region configured. Run with --pick-region first.")
        target_desc = f"region {config['region']}"
    else:
        sys.exit("Nothing configured yet. Run with --pick-window or --pick-region first.")

    colors = config["colors"]
    if not colors:
        sys.exit("No colors configured. Run with --add-color '#RRGGBB:Name'.")

    print(f"Watching {target_desc} for colors: "
          f"{', '.join(c['name'] + ' ' + c['hex'] for c in colors)}")
    print(f"Poll interval: {config['poll_interval']}s, cooldown: {config['cooldown']}s, "
          f"confirm delay: {config.get('confirm_delay', 0.5)}s")
    if config.get("rainbow_mode"):
        if len(colors) > 1:
            print("Rainbow mode: ON (alerts suppressed if all colors are seen at once)")
        else:
            print("Rainbow mode: ON, but has no effect with only 1 color configured")
    print("Press Ctrl+C to stop.\n")

    last_alert: dict[str, float] = {}
    last_warning = 0.0
    last_rainbow_log = 0.0

    with mss.MSS() as sct:
        while True:
            frame = capture_frame(config, sct)
            now = time.time()

            if frame is None:
                if now - last_warning >= 10:
                    print(f"[{time.strftime('%H:%M:%S')}] Warning: target window not found "
                          "(closed or minimized?). Retrying...")
                    last_warning = now
                time.sleep(config["poll_interval"])
                continue

            rgb_by_name = {c["name"]: hex_to_rgb(c["hex"]) for c in colors}
            matched = [c for c in colors if color_present(frame, rgb_by_name[c["name"]], c["tolerance"])]

            if config.get("rainbow_mode") and len(colors) > 1 and len(matched) == len(colors):
                # Every monitored color showed up in the same frame -- almost
                # certainly a rainbow flash/transition, not a real hit for any
                # one of them. Skip alerting entirely for this poll.
                if now - last_rainbow_log >= 2:
                    print(f"[{time.strftime('%H:%M:%S')}] Rainbow detected (all colors present at once) "
                          "-- alert suppressed")
                    last_rainbow_log = now
                time.sleep(config["poll_interval"])
                continue

            for color in matched:
                rgb = rgb_by_name[color["name"]]
                since_last = now - last_alert.get(color["name"], 0)
                if since_last >= config["cooldown"]:
                    # Re-sample the same area after a short delay before alerting --
                    # filters out momentary flicker (e.g. a rainbow loading transition)
                    # that happens to match on a single frame but isn't a real hit.
                    confirm_delay = config.get("confirm_delay", 0.5)
                    alert_frame = frame
                    if confirm_delay > 0:
                        time.sleep(confirm_delay)
                        confirm_frame = capture_frame(config, sct)
                        if confirm_frame is None or not color_present(confirm_frame, rgb, color["tolerance"]):
                            continue
                        alert_frame = confirm_frame

                    ts = time.strftime("%H:%M:%S")
                    template = color.get("message") or f"Detected {color['name']} ({color['hex']})"
                    message = template
                    terminal_display = template
                    should_notify = True
                    if "{text}" in template:
                        word_entries = config.get("ocr_word_list", [])
                        word_list = [w["word"] for w in word_entries]
                        color_by_word = {w["word"].lower(): w.get("color") for w in word_entries}

                        recognized = extract_text(alert_frame).strip()
                        recognized = correct_ocr_text(recognized, word_list)
                        recognized = recognized.replace("\n", " | ")

                        plain_text = recognized or "(no text found)"
                        colorized_text = " ".join(
                            colorize_word(w, color_by_word.get(w.lower())) for w in recognized.split()
                        ) if recognized else plain_text

                        message = template.replace("{text}", plain_text)
                        terminal_display = template.replace("{text}", colorized_text)

                        # If key combos are configured, only actually notify when the
                        # corrected OCR text contains ALL words of at least one combo
                        # -- always print the detection either way, so nothing is
                        # silently lost from view.
                        key_combos = config.get("ocr_key_combos", [])
                        if key_combos:
                            recognized_tokens = {w.lower() for w in recognized.split()}
                            should_notify = any(
                                all(word.lower() in recognized_tokens for word in combo)
                                for combo in key_combos
                            )

                    suffix = "" if should_notify else "  (no key-combo match -- notification suppressed)"
                    print(f"[{ts}] {terminal_display}{suffix}")
                    if should_notify:
                        send_notification("Color Alert", message, config.get("notification_sound"))
                    last_alert[color["name"]] = time.time()

            time.sleep(config["poll_interval"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pick-window", action="store_true",
                         help="Interactively click a window to watch (survives being covered by other windows). "
                              "Only works if the window is visible at the click point -- use --list-windows / "
                              "--window-owner if it's fully hidden behind other windows.")
    parser.add_argument("--list-windows", action="store_true",
                         help="Print all open windows (app name + title), then exit. Use with --window-owner.")
    parser.add_argument("--window-owner", metavar="APP_NAME",
                         help="Watch the window belonging to APP_NAME (case-insensitive substring match against "
                              "--list-windows's output), e.g. --window-owner Safari. No click needed, works even "
                              "if the window is fully covered by others. Combine with --window-title to disambiguate "
                              "an app with multiple windows.")
    parser.add_argument("--window-title", metavar="TITLE",
                         help="Combined with --window-owner: also require this substring in the window title.")
    parser.add_argument("--pick-region", action="store_true", help="Interactively drag-select a fixed screen region to watch")
    parser.add_argument("--pick-subregion", action="store_true",
                         help="After a window is selected (this command or a previous one), drag-select a smaller "
                              "area within it to watch instead of the whole window. Bring the window to the front "
                              "first so you can see it to drag over it.")
    parser.add_argument("--clear-subregion", action="store_true",
                         help="Remove a previously set sub-region; go back to watching the whole window.")
    parser.add_argument("--add-color", action="append", default=[], metavar="HEX[:NAME[:TOLERANCE[:MESSAGE]]]",
                         help="Add a target color, e.g. '#b4aeb1:Beskar:30:beskar detected'. "
                              "MESSAGE overrides the default notification text. Include the literal text "
                              "'{text}' in MESSAGE to OCR the watched area and substitute whatever text was "
                              "found there, e.g. '#ffffff:Error:30:Error seen: {text}'. Can be repeated.")
    parser.add_argument("--remove-color", action="append", default=[], metavar="NAME_OR_HEX",
                         help="Remove a previously added color by its name or hex code. Can be repeated.")
    parser.add_argument("--clear-colors", action="store_true", help="Remove all previously configured colors")
    parser.add_argument("--tolerance", type=int, default=30, help="Default color-match tolerance (0-441, default 30)")
    parser.add_argument("--interval", type=float, help="Polling interval in seconds (default 1.0)")
    parser.add_argument("--cooldown", type=float, help="Seconds to wait before re-alerting on the same color (default 5)")
    parser.add_argument("--confirm-delay", type=float,
                         help="Seconds to wait and re-check before alerting, to filter out momentary flicker "
                              "(e.g. a rainbow loading transition). 0 disables this check. Default 0.5")
    parser.add_argument("--rainbow-mode", choices=["on", "off"],
                         help="If 'on', suppress alerts entirely when every monitored color is detected in the "
                              "same frame (e.g. a rainbow flash that happens to hit all your configured colors "
                              "at once). Only has an effect with 2+ colors configured. Default off.")
    parser.add_argument("--ocr-words", metavar="WORD1,WORD2,...",
                         help="Comma-separated list of known-good words to snap misread OCR text onto, e.g. "
                              "'Legendary,Mythic,Galactic,Epic,Rare,Common'. If OCR reads 'Eric' and 'Epic' is "
                              "in this list, {text} substitutes 'Epic' instead. Replaces the whole list each time "
                              "(colors set via --ocr-word-color are preserved for words that still appear).")
    parser.add_argument("--ocr-word-color", action="append", default=[], metavar="WORD:COLOR",
                         help="Color a word from --ocr-words for terminal display only (not the macOS "
                              "notification itself, which can't render color). COLOR is a name (gray, "
                              "light_blue, purple, orange, reddish_pink, red, pink, green, blue, yellow, white), "
                              "a '#RRGGBB' hex code, or 'rainbow' to cycle a color per character. Can be repeated.")
    parser.add_argument("--ocr-key-combo", action="append", default=[], metavar="WORD1,WORD2,...",
                         help="Add a required word combo: {text} colors only notify when the corrected OCR "
                              "text contains ALL words of at least one combo, e.g. '--ocr-key-combo "
                              "Legendary,Galactic' notifies for that pair but NOT for 'Legendary Diamond' "
                              "unless you also add that as its own combo. Detection still always prints to "
                              "terminal either way. No combos configured means always notify (previous "
                              "behavior). Can be repeated to add multiple combos.")
    parser.add_argument("--remove-key-combo", action="append", default=[], metavar="WORD1,WORD2,...",
                         help="Remove a previously added combo (must match the same words, any order). Can be repeated.")
    parser.add_argument("--clear-key-combos", action="store_true", help="Remove all configured key combos")
    parser.add_argument("--show-config", action="store_true", help="Print the current config and exit")
    args = parser.parse_args()

    config = load_config()

    if args.show_config:
        print(json.dumps(config, indent=2))
        return

    if args.list_windows:
        windows = list_windows()
        for i, w in enumerate(windows):
            label = w["name"] or "(untitled)"
            print(f"[{i}] {w['owner']} -- {label}  ({int(w['bounds']['width'])}x{int(w['bounds']['height'])})")
        if not windows:
            print("No windows found.")
        return

    if args.window_owner:
        windows = list_windows()
        matches = [w for w in windows if args.window_owner.lower() in w["owner"].lower()]
        if args.window_title:
            matches = [w for w in matches if args.window_title.lower() in w["name"].lower()]
        if not matches:
            sys.exit(f"No window found matching --window-owner {args.window_owner!r}"
                      + (f" --window-title {args.window_title!r}" if args.window_title else "")
                      + ". Run --list-windows to see current windows.")
        if len(matches) > 1:
            print("Multiple windows match; watching the first one. Use --window-title to narrow it down:")
            for m in matches:
                print(f"  {m['owner']} -- {m['name']!r}")
        window = matches[0]
        config["mode"] = "window"
        config["window"] = window
        print(f"Watching window '{window['name'] or window['owner']}' ({window['owner']})")

    if args.clear_colors:
        config["colors"] = []

    for target in args.remove_color:
        before = len(config["colors"])
        config["colors"] = [
            c for c in config["colors"]
            if c["name"].lower() != target.lower() and c["hex"].lower() != target.lower()
        ]
        if len(config["colors"]) == before:
            print(f"Warning: no color matching {target!r} found.")
        else:
            print(f"Removed {target!r}.")

    if args.pick_window:
        print("Click the window you want to watch...")
        window = pick_window()
        if window is None:
            sys.exit("Window selection cancelled.")
        config["mode"] = "window"
        config["window"] = window
        print(f"Watching window '{window['name'] or window['owner']}' ({window['owner']})")
    elif args.pick_region:
        print("Drag to select the region to watch...")
        region = pick_region()
        if region is None:
            sys.exit("Region selection cancelled.")
        x, y, w, h = region
        config["mode"] = "region"
        config["region"] = {"x": x, "y": y, "width": w, "height": h}
        print(f"Region set to {config['region']}")

    if args.clear_subregion:
        if config.get("window"):
            config["window"].pop("subregion", None)
        print("Sub-region cleared; watching the whole window.")

    if args.pick_subregion:
        if config["mode"] != "window" or not config["window"]:
            sys.exit("No window configured yet. Use --window-owner or --pick-window first "
                      "(can be combined with --pick-subregion in the same command).")
        bounds = get_window_bounds(config["window"]["window_id"])
        if bounds is None:
            sys.exit("Could not find the window on screen -- bring it to the front first so it can be measured.")
        print("Bring the window to the front, then drag to select the sub-region to watch...")
        subregion = pick_subregion(bounds)
        if subregion is None:
            sys.exit("Sub-region selection cancelled.")
        config["window"]["subregion"] = subregion
        print(f"Sub-region set: {subregion}")

    for raw in args.add_color:
        config["colors"].append(parse_color_arg(raw, args.tolerance))

    if args.interval is not None:
        config["poll_interval"] = args.interval
    if args.cooldown is not None:
        config["cooldown"] = args.cooldown
    if args.confirm_delay is not None:
        config["confirm_delay"] = args.confirm_delay
    if args.rainbow_mode is not None:
        config["rainbow_mode"] = args.rainbow_mode == "on"
    if args.ocr_words is not None:
        existing_colors = {w["word"].lower(): w.get("color") for w in config["ocr_word_list"]}
        config["ocr_word_list"] = [
            {"word": w.strip(), "color": existing_colors.get(w.strip().lower())}
            for w in args.ocr_words.split(",") if w.strip()
        ]

    for raw in args.ocr_word_color:
        target, _, color = raw.partition(":")
        target, color = target.strip(), color.strip()
        if not target or not color:
            sys.exit(f"Invalid --ocr-word-color {raw!r}, expected 'WORD:COLOR'")
        entry = next((w for w in config["ocr_word_list"] if w["word"].lower() == target.lower()), None)
        if entry is None:
            entry = {"word": target, "color": None}
            config["ocr_word_list"].append(entry)
        entry["color"] = color
        print(f"Set {entry['word']!r} to color {color!r}")

    if args.clear_key_combos:
        config["ocr_key_combos"] = []

    for raw in args.ocr_key_combo:
        combo = [w.strip() for w in raw.split(",") if w.strip()]
        if not combo:
            sys.exit(f"Invalid --ocr-key-combo {raw!r}, expected 'WORD1,WORD2,...'")
        config["ocr_key_combos"].append(combo)
        print(f"Added key combo: {combo}")

    for raw in args.remove_key_combo:
        target_set = {w.strip().lower() for w in raw.split(",") if w.strip()}
        before = len(config["ocr_key_combos"])
        config["ocr_key_combos"] = [c for c in config["ocr_key_combos"] if {w.lower() for w in c} != target_set]
        if len(config["ocr_key_combos"]) == before:
            print(f"Warning: no key combo matching {raw!r} found.")
        else:
            print(f"Removed key combo: {raw!r}")

    save_config(config)

    if (args.pick_window or args.pick_region or args.window_owner or args.add_color or args.clear_colors
            or args.remove_color or args.pick_subregion or args.clear_subregion or args.rainbow_mode is not None
            or args.ocr_key_combo or args.remove_key_combo or args.clear_key_combos
            or args.ocr_words is not None or args.ocr_word_color):
        has_target = config["mode"] == "window" and config["window"] or config["mode"] == "region" and config["region"]
        if not has_target or not config["colors"]:
            print("Config saved. Run again once both a target (window/region) and at least one color are configured.")
            return

    run_watch(config)


if __name__ == "__main__":
    main()
