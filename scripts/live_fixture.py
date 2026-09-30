"""Disposable desktop window for live input verification; never touches other apps."""

import json
import os
from pathlib import Path
import sys
import tkinter as tk


def main():
    state_path = Path(sys.argv[1])
    root = tk.Tk()
    root.title("Pi Desktop Bridge — verification")
    root.geometry("640x430+200+150")
    root.attributes("-topmost", True)
    state = {"clicks": 0, "scrolls": 0, "hotkeys": 0, "drag": [], "text": ""}
    text = tk.StringVar()

    def save(*_):
        state["text"] = text.get()
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, state_path)

    tk.Label(root, text="This temporary window tests screenshot, click, type, key, scroll and drag.",
             wraplength=600, font=("Sans", 13)).pack(pady=16)
    entry = tk.Entry(root, textvariable=text, font=("Sans", 18))
    entry.pack(fill="x", padx=30, pady=8)
    text.trace_add("write", save)

    def hotkey(event):
        state["hotkeys"] += 1
        entry.selection_range(0, tk.END)
        save()
        return "break"

    entry.bind("<Control-a>", hotkey)

    def click():
        state["clicks"] += 1
        button.configure(text=f"Clicks received: {state['clicks']}")
        save()

    button = tk.Button(root, text="Click to verify", command=click, font=("Sans", 16))
    button.pack(pady=8)
    canvas = tk.Canvas(root, width=560, height=150, bg="#153046", highlightthickness=0)
    canvas.pack(pady=8)
    canvas.create_text(280, 25, text="Drag here and scroll here", fill="white", font=("Sans", 14))

    def drag_start(event):
        state["drag"] = [[event.x, event.y]]
        save()

    def drag_end(event):
        state["drag"].append([event.x, event.y])
        canvas.create_line(*state["drag"][0], event.x, event.y, fill="#89e6b5", width=5)
        save()

    def scroll(event):
        state["scrolls"] += 1
        save()

    canvas.bind("<ButtonPress-1>", drag_start)
    canvas.bind("<ButtonRelease-1>", drag_end)
    canvas.bind("<Button-4>", scroll)
    canvas.bind("<Button-5>", scroll)
    canvas.bind("<MouseWheel>", scroll)

    def ready():
        root.update_idletasks()
        state["geometry"] = {
            name: {"x": widget.winfo_rootx(), "y": widget.winfo_rooty(),
                   "width": widget.winfo_width(), "height": widget.winfo_height()}
            for name, widget in (("entry", entry), ("button", button), ("canvas", canvas))
        }
        state["pid"] = os.getpid()
        save()
        root.lift()
        entry.focus_force()

    root.after(300, ready)
    root.mainloop()


if __name__ == "__main__":
    main()
