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
    state = {"clicks": 0, "scrolls": 0, "hotkeys": 0, "drag": [], "text": "", "button_events": [],
             "visual": {"mode": "idle", "ticks": 0, "color": "#333333"}}
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

    def button_event(event):
        state["button_events"].append({"type": str(event.type), "x": event.x, "y": event.y})
        save()

    for event_name in ("<Enter>", "<ButtonPress-1>", "<ButtonRelease-1>"):
        button.bind(event_name, button_event, add="+")
    canvas = tk.Canvas(root, width=560, height=150, bg="#153046", highlightthickness=0)
    canvas.pack(pady=8)
    canvas.create_text(280, 25, text="Drag here and scroll here", fill="white", font=("Sans", 14))
    for index, color in enumerate(("#ff0000", "#00ff00", "#0000ff", "#ffffff")):
        canvas.create_rectangle(20 + index * 10, 72, 27 + index * 10, 79,
                                fill=color, outline="")
    visual_patch = canvas.create_rectangle(450, 60, 510, 120, fill="#333333", outline="")
    command_path = state_path.with_name("command.json")
    visual_job = None
    visual_interval = 75
    unique_frames = False

    def visual_tick():
        nonlocal visual_job
        visual = state["visual"]
        visual["ticks"] += 1
        if visual["mode"] == "settle" and visual["ticks"] >= 6:
            visual.update(mode="settled", color="#00ff00")
            visual_job = None
        else:
            if unique_frames:
                tick = visual["ticks"]
                visual["color"] = "#{:02x}{:02x}{:02x}".format((tick * 7) % 256, (tick * 13) % 256, (tick * 23) % 256)
            else:
                visual["color"] = "#ff0000" if visual["ticks"] % 2 else "#0000ff"
            visual_job = root.after(visual_interval, visual_tick)
        canvas.itemconfigure(visual_patch, fill=visual["color"])
        save()

    def read_command():
        nonlocal visual_job, visual_interval, unique_frames
        if command_path.exists():
            command = json.loads(command_path.read_text(encoding="utf-8"))
            command_path.unlink()
            if command.get("mode") not in {"settle", "animate", "idle"} or not isinstance(command.get("id"), str):
                raise ValueError("Invalid fixture command")
            interval = command.get("interval_ms", 75)
            if type(interval) is not int or not 8 <= interval <= 1000:
                raise ValueError("Invalid fixture animation interval")
            visual_interval = interval
            unique_frames = command.get("unique_frames", False) is True
            if visual_job is not None:
                root.after_cancel(visual_job)
                visual_job = None
            state["visual"] = {"mode": command["mode"], "ticks": 0, "color": "#333333",
                               "command_id": command["id"]}
            canvas.itemconfigure(visual_patch, fill="#333333")
            if command["mode"] != "idle":
                visual_job = root.after(visual_interval, visual_tick)
            save()
        root.after(25, read_command)

    def latency_key(event):
        nonlocal visual_job
        if visual_job is not None:
            root.after_cancel(visual_job)
            visual_job = None
        count = state.get("latency_keys", 0) + 1
        state["latency_keys"] = count
        color = "#00ff00" if count % 2 else "#ff00ff"
        state["visual"].update(mode="idle", color=color)
        canvas.itemconfigure(visual_patch, fill=color)
        save()
        return "break"

    root.bind("<F9>", latency_key)

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
    root.after(25, read_command)
    root.mainloop()


if __name__ == "__main__":
    main()
