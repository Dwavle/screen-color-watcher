"""Pick a specific on-screen window by clicking it, and capture that
window's contents directly from its backing store -- which keeps working
even when the window is fully covered by other windows."""

from __future__ import annotations

import os
import tkinter as tk
import time

import numpy as np
import Quartz


def _onscreen_windows() -> list[dict]:
    # kCGWindowListOptionOnScreenOnly returns windows front-to-back.
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    return list(Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID))


def find_window_at_point(x: int, y: int) -> dict | None:
    own_pid = os.getpid()
    for w in _onscreen_windows():
        if w.get("kCGWindowLayer", 0) != 0:
            continue  # skip menu bar, dock, and other system layers
        if w.get("kCGWindowOwnerPID") == own_pid:
            continue  # skip our own picker overlay (may briefly linger after destroy())
        bounds = w.get("kCGWindowBounds")
        if not bounds:
            continue
        bx, by, bw, bh = bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"]
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return {
                "window_id": int(w["kCGWindowNumber"]),
                "owner": w.get("kCGWindowOwnerName", ""),
                "name": w.get("kCGWindowName", "") or "",
                "bounds": {"x": bx, "y": by, "width": bw, "height": bh},
            }
    return None


def list_windows() -> list[dict]:
    """All real, on-screen windows (regardless of what's visually on top of
    them), for picking by name instead of by clicking a visible pixel."""
    own_pid = os.getpid()
    results = []
    for w in _onscreen_windows():
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        if w.get("kCGWindowOwnerPID") == own_pid:
            continue
        bounds = w.get("kCGWindowBounds")
        if not bounds or bounds["Width"] < 50 or bounds["Height"] < 50:
            continue  # skip menu extras / tiny utility windows
        results.append({
            "window_id": int(w["kCGWindowNumber"]),
            "owner": w.get("kCGWindowOwnerName", "") or "",
            "name": w.get("kCGWindowName", "") or "",
            "bounds": {"x": bounds["X"], "y": bounds["Y"], "width": bounds["Width"], "height": bounds["Height"]},
        })
    return results


def get_window_bounds(window_id: int) -> dict | None:
    """Fresh screen bounds for a window id (it may have moved since it was
    first selected)."""
    for w in _onscreen_windows():
        if int(w.get("kCGWindowNumber", -1)) == window_id:
            b = w.get("kCGWindowBounds")
            if b:
                return {"x": b["X"], "y": b["Y"], "width": b["Width"], "height": b["Height"]}
    return None


def pick_subregion(bounds: dict) -> dict | None:
    """Show a translucent overlay positioned exactly over a window's bounds;
    drag to select a sub-region within it. Returns fractional coordinates
    (0-1, relative to the window) rather than raw pixels, so the selection
    stays correct across Retina scaling and later window resizes."""

    root = tk.Tk()
    root.overrideredirect(True)  # no title bar, so the overlay lines up exactly with the window's content
    root.geometry(f"{int(bounds['width'])}x{int(bounds['height'])}+{int(bounds['x'])}+{int(bounds['y'])}")
    root.attributes("-alpha", 0.25)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.config(cursor="crosshair")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        int(bounds["width"]) // 2, 16,
        text="Drag to select the sub-region to watch. Esc to cancel.",
        fill="white", font=("Helvetica", 13),
    )

    state = {"start": None, "rect": None, "result": None}

    def on_press(event):
        state["start"] = (event.x, event.y)
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="red", width=2)

    def on_drag(event):
        if state["rect"] is None:
            return
        x0, y0 = state["start"]
        canvas.coords(state["rect"], x0, y0, event.x, event.y)

    def on_release(event):
        x0, y0 = state["start"]
        x1, y1 = event.x, event.y
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w > 2 and h > 2:
            state["result"] = (x, y, w, h)
            root.quit()

    def on_escape(_event):
        state["result"] = None
        root.quit()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", on_escape)

    try:
        root.mainloop()
    finally:
        # overrideredirect + topmost windows can be left as a visual ghost
        # by a plain destroy() on macOS -- drop topmost and hide explicitly
        # first, and flush that to the window server before tearing down.
        root.attributes("-topmost", False)
        root.withdraw()
        root.update()
        root.destroy()
    time.sleep(0.1)

    if state["result"] is None:
        return None
    x, y, w, h = state["result"]
    return {
        "rel_x": x / bounds["width"],
        "rel_y": y / bounds["height"],
        "rel_width": w / bounds["width"],
        "rel_height": h / bounds["height"],
    }


def find_window_by_owner_name(owner: str, name: str) -> dict | None:
    """Best-effort re-lookup if a window id goes stale (app restarted, etc)."""
    for w in _onscreen_windows():
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        if w.get("kCGWindowOwnerName", "") == owner and (w.get("kCGWindowName", "") or "") == name:
            bounds = w.get("kCGWindowBounds")
            return {
                "window_id": int(w["kCGWindowNumber"]),
                "owner": owner,
                "name": name,
                "bounds": {"x": bounds["X"], "y": bounds["Y"], "width": bounds["Width"], "height": bounds["Height"]},
            }
    return None


def pick_window() -> dict | None:
    """Show a full-screen overlay; click a window to select it."""

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.15)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.config(cursor="crosshair")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        root.winfo_screenwidth() // 2,
        30,
        text="Click the window you want to watch. Press Esc to cancel.",
        fill="white",
        font=("Helvetica", 16),
    )

    state = {"point": None}

    def on_click(event):
        state["point"] = (event.x_root, event.y_root)
        root.quit()

    def on_escape(_event):
        state["point"] = None
        root.quit()

    canvas.bind("<ButtonPress-1>", on_click)
    root.bind("<Escape>", on_escape)

    try:
        root.mainloop()
    finally:
        root.attributes("-topmost", False)
        root.withdraw()
        root.update()
        root.destroy()
    time.sleep(0.1)  # let WindowServer register the overlay's removal

    if state["point"] is None:
        return None
    return find_window_at_point(*state["point"])


def cgimage_to_numpy(image) -> np.ndarray:
    # CGWindowListCreateImage returns pixels in the display's native color
    # space, not sRGB -- reading its bytes directly shifts colors (e.g. a
    # true #FF0000 window came back as ~(234, 51, 35)). Drawing into an
    # explicit sRGB bitmap context forces ColorSync to convert properly, so
    # captured pixels match the hex values a user would actually pick.
    width = int(Quartz.CGImageGetWidth(image))
    height = int(Quartz.CGImageGetHeight(image))
    bytes_per_row = width * 4
    colorspace = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, bytes_per_row, colorspace,
        Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big,
    )
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    data = Quartz.CGBitmapContextGetData(context)
    buf = data.as_buffer(bytes_per_row * height)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))
    # Copy out: `arr` is a zero-copy view into memory owned by `context`,
    # which gets released when this function returns -- without the copy,
    # later reads risk a use-after-free (segfault).
    return arr[:, :, :3].copy()


def capture_window(window_id: int) -> np.ndarray | None:
    """Capture a window's contents by id, even if it's covered by other
    windows on screen. Returns None if the window is gone or minimized."""

    image = Quartz.CGWindowListCreateImage(
        Quartz.CGRectNull,
        Quartz.kCGWindowListOptionIncludingWindow,
        window_id,
        Quartz.kCGWindowImageBoundsIgnoreFraming,
    )
    if image is None or Quartz.CGImageGetWidth(image) == 0:
        return None
    return cgimage_to_numpy(image)


if __name__ == "__main__":
    win = pick_window()
    print("Selected window:", win)
    if win:
        frame = capture_window(win["window_id"])
        print("Captured shape:", None if frame is None else frame.shape)
