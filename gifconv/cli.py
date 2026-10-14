"""Console mode: interactive questions, or fully driven by command-line options."""

from __future__ import annotations

import sys
from pathlib import Path

from . import APP_NAME, __version__, core


def _pick_files() -> list[Path]:
    """A file dialog, or a question in the console if tkinter is not available."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        exts = " ".join("*" + e for e in core.VIDEO_EXTENSIONS)
        names = filedialog.askopenfilenames(
            title="Choose videos to convert to GIF",
            filetypes=[("Videos", exts), ("All files", "*")])
        names = root.tk.splitlist(names) if names else ()
        root.destroy()
        return [Path(n) for n in names]
    except Exception:
        raw = _ask("Path to a video (you can drag the file into this window): ")
        raw = raw.strip().strip("\"'")
        return [Path(raw)] if raw else []


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _ask_number(prompt, cast):
    while True:
        raw = _ask(prompt).strip().replace(",", ".")
        if not raw:
            return None
        try:
            value = cast(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        print("  Enter a positive number or just press Enter.")


def _ask_time(prompt):
    while True:
        try:
            return core.parse_time(_ask(prompt))
        except ValueError:
            print("  Seconds (12.5) or minutes:seconds (1:30). Enter to skip.")


def _ask_settings() -> core.Settings:
    print("\nSettings (Enter = default):")
    for key, mode in core.MODES.items():
        print(f"  {key}. {mode.title} — {mode.hint}")
    mode = _ask("Mode [1]: ").strip() or "1"
    if mode not in core.MODES:
        print("  No such mode, using 1.")
        mode = "1"
    width = _ask_number("GIF width in pixels [original]: ", int)
    fps = _ask_number(f"Frame rate [as source, at most {core.MAX_GIF_FPS}]: ", float)
    if fps and fps > core.MAX_GIF_FPS:
        print(f"  GIF can't do more than {core.MAX_GIF_FPS} fps, using {core.MAX_GIF_FPS}.")
        fps = core.MAX_GIF_FPS
    start = _ask_time("Start at (seconds or 1:30) [beginning]: ")
    duration = _ask_time("Duration in seconds [to the end]: ")
    return core.Settings(mode=mode, width=width, fps=fps, start=start, duration=duration)


def _settings_from_args(args) -> core.Settings:
    def time_arg(value, name):
        try:
            return core.parse_time(value)
        except ValueError:
            sys.exit(f"Invalid value for {name}: {value!r}")
    fps = args.fps
    if fps is not None and fps > core.MAX_GIF_FPS:
        print(f"GIF can't do more than {core.MAX_GIF_FPS} fps, using {core.MAX_GIF_FPS}.")
        fps = core.MAX_GIF_FPS
    return core.Settings(mode=args.mode or "1", width=args.width, fps=fps,
                         start=time_arg(args.start, "--start"),
                         duration=time_arg(args.duration, "--duration"))


class _ProgressPrinter:
    def __init__(self):
        self.stage = None

    def __call__(self, frac, stage):
        if stage == "Done":
            return
        if stage != self.stage and self.stage is not None:
            print()
        self.stage = stage
        print(f"\r  {stage}: {int(frac * 100):3d}%", end="", flush=True)

    def finish(self):
        if self.stage is not None:
            print()
        self.stage = None


def run(args, ffmpeg: str | None) -> int:
    print(f"=== Video -> GIF · {APP_NAME} {__version__} ===")
    if not ffmpeg:
        print("ffmpeg was not found. Install it with:\n"
              "    pip install imageio-ffmpeg\n"
              "and run the program again.")
        return 1

    has_options = any(v is not None for v in
                      (args.mode, args.width, args.fps, args.start, args.duration))
    interactive = not has_options and sys.stdin is not None and sys.stdin.isatty()

    files = [Path(f) for f in args.files] if args.files else _pick_files()
    for f in files:
        if not f.is_file():
            print(f"No such file: {f}")
    files = [f for f in files if f.is_file()]
    if not files:
        print("No files selected.")
        return _finish(interactive, 1)

    print(f"\nFiles: {len(files)}")
    for f in files:
        print("  -", f.name)

    settings = _ask_settings() if interactive else _settings_from_args(args)
    if args.out:
        settings.out_dir = Path(args.out)

    converter = core.Converter(ffmpeg)
    ok = 0
    try:
        for i, src in enumerate(files, 1):
            print(f"\n[{i}/{len(files)}] {src.name}")
            printer = _ProgressPrinter()
            try:
                result = converter.convert(src, settings, printer)
                printer.finish()
                fps_info = f", {core.format_number(result.fps)} fps" if result.fps else ""
                print(f"  Done: {result.path}  ({core.format_size(result.size)}{fps_info})")
                ok += 1
            except core.ConversionError as e:
                printer.finish()
                print(f"  Error: {e}")
    except KeyboardInterrupt:
        converter.cancel()
        print("\nStopped.")
    return _finish(interactive, 0 if ok == len(files) else 1)


def _finish(interactive: bool, code: int) -> int:
    if interactive:
        _ask("\nPress Enter to close...")
    return code
