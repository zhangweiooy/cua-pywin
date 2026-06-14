from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path


DEFAULT_TITLE = "CuaPywin Agent Smoke Fixture"


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch a small Windows GUI fixture for cua-win agent smoke tests."
    )
    parser.add_argument("--title", default=DEFAULT_TITLE, help="Fixture window title.")
    parser.add_argument(
        "--state-file",
        default=str(Path(tempfile.gettempdir()) / "cua-pywin-agent-smoke" / "state.json"),
        help="UTF-8 JSON file updated with fixture state.",
    )
    args = parser.parse_args()

    import tkinter as tk

    state_file = Path(args.state_file)
    state: dict[str, object] = {
        "title": args.title,
        "counter": 0,
        "text": "",
        "last_event": "started",
        "updated_at": time.time(),
    }

    root = tk.Tk()
    root.title(args.title)
    root.geometry("520x300")

    title_label = tk.Label(root, text="cua-pywin agent smoke fixture", font=("Segoe UI", 14))
    title_label.pack(pady=(18, 8))

    counter_var = tk.StringVar(value="Counter: 0")
    counter_label = tk.Label(root, textvariable=counter_var, font=("Segoe UI", 11))
    counter_label.pack(pady=4)

    entry = tk.Entry(root, width=44)
    entry.insert(0, "type here")
    entry.pack(pady=8)

    def sync_state(event: str) -> None:
        state["counter"] = int(state.get("counter", 0))
        state["text"] = entry.get()
        state["last_event"] = event
        state["updated_at"] = time.time()
        _write_state(state_file, state)

    def increment() -> None:
        state["counter"] = int(state.get("counter", 0)) + 1
        counter_var.set(f"Counter: {state['counter']}")
        sync_state("increment")

    button = tk.Button(root, text="Increment", command=increment)
    button.pack(pady=8)

    status = tk.Label(root, text=f"State file: {state_file}", font=("Segoe UI", 9))
    status.pack(pady=(10, 0))

    entry.bind("<KeyRelease>", lambda _event: sync_state("typing"))
    root.after(100, lambda: sync_state("ready"))
    root.mainloop()
    sync_state("closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
