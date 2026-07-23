"""Interactive drag-to-select screen region picker (macOS/tkinter)."""

import tkinter as tk


def pick_region() -> tuple[int, int, int, int]:
    """Show a full-screen transparent overlay and let the user drag-select
    a rectangle. Returns (x, y, width, height) in screen coordinates."""

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.25)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.config(cursor="crosshair")

    canvas = tk.Canvas(root, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    hint = canvas.create_text(
        root.winfo_screenwidth() // 2,
        30,
        text="Drag to select the region to watch. Press Esc to cancel.",
        fill="white",
        font=("Helvetica", 16),
    )

    state = {"start": None, "rect": None, "result": None}

    def on_press(event):
        state["start"] = (event.x_root, event.y_root)
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="red", width=2
        )

    def on_drag(event):
        if state["rect"] is None:
            return
        x0, y0 = state["start"]
        canvas.coords(state["rect"], x0, y0, event.x_root, event.y_root)

    def on_release(event):
        x0, y0 = state["start"]
        x1, y1 = event.x_root, event.y_root
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if w > 2 and h > 2:
            state["result"] = (int(x), int(y), int(w), int(h))
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
        root.attributes("-topmost", False)
        root.withdraw()
        root.update()
        root.destroy()

    return state["result"]


if __name__ == "__main__":
    region = pick_region()
    print("Selected region:", region)
