"""Entry point: the window by default, the console with --cli or conversion options."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from . import APP_NAME, __version__, cli, core


def parse_args(argv):
    # macOS sometimes adds a service argument like -psn_0_12345 when starting an .app
    argv = [a for a in argv if not a.startswith("-psn_")]
    modes = ", ".join(f"{k} = {m.title.lower()}" for k, m in core.MODES.items())
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Convert videos to GIF with the best quality the format allows. "
                    "Without options the app window opens.")
    p.add_argument("files", nargs="*", help="video files (you can also drop them onto the app)")
    p.add_argument("--cli", action="store_true", help="work in the console, without a window")
    p.add_argument("-m", "--mode", choices=list(core.MODES), help=f"quality mode: {modes}")
    p.add_argument("-w", "--width", type=int, help="GIF width in pixels")
    p.add_argument("--fps", type=float, help=f"frame rate (at most {core.MAX_GIF_FPS})")
    p.add_argument("-s", "--start", help="start time: seconds or 1:30")
    p.add_argument("-d", "--duration", help="duration in seconds")
    p.add_argument("-o", "--out", help='output folder (default: a "gif" folder next to each video)')
    p.add_argument("--selftest", metavar="REPORT", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args, _unknown = p.parse_known_args(argv)
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.selftest:
        return selftest(Path(args.selftest))

    ffmpeg = core.find_ffmpeg()
    wants_cli = args.cli or any(v is not None for v in
                                (args.mode, args.width, args.fps, args.start,
                                 args.duration, args.out))
    if wants_cli:
        return cli.run(args, ffmpeg)

    try:
        import tkinter
        from . import gui
    except ImportError as e:
        print(f"Can't open the window ({e}).\n"
              "Install the missing parts with  pip install -r requirements.txt\n"
              "(on Linux tkinter comes from the system: sudo apt install python3-tk).\n"
              "Switching to the console mode.\n")
        return cli.run(args, ffmpeg)
    try:
        return gui.run(args.files, ffmpeg)
    except tkinter.TclError as e:  # e.g. Linux without a graphical session
        print(f"Can't open the window ({e}) — switching to the console mode.")
        return cli.run(args, ffmpeg)


def selftest(report: Path) -> int:
    """Check a built app (used by the GitHub Actions build)."""
    rep = {"app": f"{APP_NAME} {__version__}", "python": sys.version,
           "platform": platform.platform(), "machine": platform.machine(),
           "frozen": bool(getattr(sys, "frozen", False)), "ok": False}
    try:
        ffmpeg = core.find_ffmpeg()
        rep["ffmpeg"] = ffmpeg
        rep["ffmpeg_version"] = core.ffmpeg_version(ffmpeg)
        with tempfile.TemporaryDirectory() as tmp:
            # a space and non-Latin letters in the name on purpose
            src = Path(tmp) / "test видео.mp4"
            subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                            "-i", "testsrc=size=320x180:rate=25", "-t", "1",
                            "-c:v", "mpeg4", "-pix_fmt", "yuv420p", str(src)],
                           check=True, capture_output=True, **core.popen_kwargs(ffmpeg))
            info = core.probe(ffmpeg, src)
            rep["probe"] = info.describe()
            conv = core.Converter(ffmpeg)
            rep["gif_sizes"] = {}
            for key in core.MODES:
                r = conv.convert(src, core.Settings(mode=key, width=160, out_dir=Path(tmp)),
                                 info=info)
                rep["gif_sizes"][key] = r.size

        import tkinter
        rep["tcl"] = tkinter.Tcl().eval("info patchlevel")  # Tcl/Tk made it into the build

        from . import draw  # Pillow made it into the build and renders the UI shapes
        img = draw.dashed_rounded(240, 120, 16, draw.NAVY, draw.CARD, fill=draw.BG)
        rep["pillow"] = img.getpixel((120, 60))
        rep["ok"] = all(v > 0 for v in rep["gif_sizes"].values())

        # a window only if there is a graphical session (build servers may not have one)
        try:
            root = tkinter.Tk()
            root.withdraw()
            rep["tk_window"] = root.tk.call("info", "patchlevel")
            from PIL import ImageTk
            ImageTk.PhotoImage(img)
            rep["imagetk"] = "ok"
            try:
                from tkinterdnd2 import TkinterDnD
                rep["tkdnd"] = TkinterDnD._require(root)
            except Exception as e:
                rep["tkdnd"] = f"missing: {e}"
            root.destroy()
        except Exception as e:
            rep["tk_window"] = f"no graphical session: {e}"
    except Exception:
        rep["error"] = traceback.format_exc()
    report.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if rep["ok"] else 1
