# Screen Color Watcher

Watches a specific window (or a fixed screen region) on macOS for target colors and sends a
desktop notification when one is detected. Built to keep working even when the watched window is
fully covered by other windows, since it captures the window's own backing store rather than
reading screen pixels.

Optional features layered on top of basic color detection:
- **Sub-regions** — watch just part of a window instead of the whole thing.
- **OCR** — read text from the area when a color is detected and include it in the notification.
- **Fuzzy word-list correction** — snap garbled OCR text (e.g. `Eric`) onto known-good vocabulary
  (e.g. `Epic`), and optionally color individual words in terminal output.
- **Key combos** — only actually notify when specific *combinations* of words appear together
  *within the same on-screen message*, while every detected message still always prints to the
  terminal.
- **Rainbow mode** — suppress alerts when every monitored color shows up in the same frame at
  once (useful for filtering out a loading/transition flash that happens to hit every color).

## Requirements

- macOS (uses Quartz/Vision frameworks for window capture and OCR)
- Python 3.10+
- Screen Recording permission for your terminal app (System Settings → Privacy & Security →
  Screen Recording) — you'll be prompted for this on first run

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

**First-time setup** — pick a window, add a color to watch for, and start watching in one command:

```bash
python watch_color.py --window-owner "Safari" --add-color "#FF0000:Red Alert"
```

This saves your setup to `config.json` (gitignored — see `config.example.json` for the shape of
the file). Every run after that just needs:

```bash
python watch_color.py
```

`Ctrl+C` to stop.

### Picking a target

- `--list-windows` — see all open windows and their app names
- `--window-owner "App Name"` — watch that app's window by name (works even if it's fully covered
  by other windows)
- `--pick-window` — click a window to select it (only works if it's actually visible on screen)
- `--pick-region` — drag-select a fixed screen region instead of a window
- `--pick-subregion` — after selecting a window, drag-select a smaller area within it

### Managing colors

- `--add-color "#RRGGBB[:Name[:Tolerance[:Message]]]"` — add a color to watch for. Including
  `{text}` in MESSAGE (e.g. `--add-color "#d78a3d:Legendary:30:{text}"`) marks this color as an
  **OCR trigger**: it doesn't matter which specific color triggers OCR, or what MESSAGE says
  around `{text}` — see "OCR-triggered colors" below for what actually gets printed/notified.
  Without `{text}`, MESSAGE is used as a fixed notification, sent immediately whenever that color
  is detected (no OCR involved at all), e.g. `--add-color "#FF0000:Red Alert:30:Red detected!"`.
- `--remove-color NAME_OR_HEX` / `--clear-colors`

### OCR-triggered colors, key combos, and message separation

If any color configured with `{text}` matches, OCR runs **at most once per poll**, no matter how
many such colors matched — which one(s) triggered it doesn't affect the output. The watched area
is often showing several distinct messages stacked at once (one printed just under the last), so
the OCR result is split into separate lines and each is treated as its own independent message:

- Each line is printed on its own, with no per-color prefix — just the (corrected, colorized)
  text of that message.
- `--ocr-key-combo "Word1,Word2,..."` — only notify for a line whose words contain **all** of a
  combo (repeatable, OR'd across combos; combos are checked against one line at a time, never
  across two different messages). No combos configured = always notify every detected line.
- `--remove-key-combo "Word1,Word2,..."` / `--clear-key-combos`
- Every line that passes gets its own separate notification — two genuinely different messages
  in the same poll means two notifications, not one merged one.

### OCR word list (fuzzy correction + terminal coloring)

- `--ocr-words "Word1,Word2,..."` — known-good vocabulary that OCR'd lines get snapped onto
  (also drops any word in a line with no good-enough match)
- `--ocr-word-color "Word:Color"` — color a word in terminal output (not the notification itself,
  which can't render color). Named colors: `gray`, `light_blue`, `purple`, `orange`,
  `reddish_pink`, `red`, `pink`, `green`, `blue`, `yellow`, `white`, `rainbow` (cycles per
  character), or any `#RRGGBB` hex code.

### Timers (scheduled reminder notifications)

Independent of color watching — a timer can be used on its own, with no window/region/color
configured at all.

- `--add-timer "START|INTERVAL|MESSAGE"` — notify at `START` (a 24-hour time of day, `HH:MM` or
  `HH:MM:SS`; if that time has already passed today, the first alert fires tomorrow), then every
  `INTERVAL` seconds after that, with `MESSAGE` as the notification text. E.g.
  `--add-timer "15:00|1800|Stretch break"` notifies at 3:00pm and every 30 minutes after. Can be
  repeated.
- `--remove-timer "MESSAGE_OR_START"` / `--clear-timers`

### Other options

- `--interval SECONDS` — polling interval (default 1.0)
- `--cooldown SECONDS` — minimum time between repeat alerts for the same color (default 5)
- `--confirm-delay SECONDS` — re-check before alerting to filter out momentary flicker (default
  0.5, set to 0 to disable)
- `--detection-timeout SECONDS` — exit if the target window/region can't be captured for this many
  consecutive seconds, e.g. the window was closed (default 60, set to 0 to retry forever instead)
- `--rainbow-mode {on,off}` — suppress alerts when every configured color is seen at once
- `--show-config` — print the current saved config

## Files

- `watch_color.py` — main entry point / CLI
- `window_picker.py` — window capture, picking, and sub-region selection (Quartz)
- `region_picker.py` — fixed screen-region picker
- `ocr.py` — text extraction via macOS's Vision framework
- `config.json` — your personal saved setup (gitignored, generated on first run)
- `config.example.json` — example showing the config file's structure

## License

MIT — see [LICENSE](LICENSE).
