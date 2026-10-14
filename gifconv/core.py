"""Conversion core: finding ffmpeg, probing videos and building GIFs.

The module has no UI dependencies — it is used by both the window (gui.py)
and the command-line mode (cli.py).
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

OUTPUT_DIR_NAME = "gif"
MAX_GIF_FPS = 50  # GIF stores frame delays in 1/100 s; browsers mishandle anything above 50 fps
SCALE_FLAGS = "lanczos+accurate_rnd+full_chroma_int"  # precise color conversion

# ffmpeg reads almost any video; this list is only used by the file dialog and folder drops
VIDEO_EXTENSIONS = (
    ".mp4", ".m4v", ".mov", ".qt", ".mkv", ".webm", ".avi", ".wmv", ".asf",
    ".flv", ".f4v", ".mpg", ".mpeg", ".m1v", ".m2v", ".vob", ".ts", ".mts", ".m2ts",
    ".3gp", ".3g2", ".ogv", ".mxf", ".dv", ".divx", ".gif",
)
FORMATS_HINT = "MP4, MOV, MKV, WebM, AVI, WMV and more"

_CODEC_NAMES = {
    "h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp8": "VP8", "vp9": "VP9",
    "prores": "ProRes", "mpeg4": "MPEG-4", "msmpeg4v3": "MS MPEG-4", "msmpeg4v2": "MS MPEG-4",
    "wmv1": "WMV", "wmv2": "WMV", "wmv3": "WMV", "vc1": "VC-1", "mpeg2video": "MPEG-2",
    "mpeg1video": "MPEG-1", "mjpeg": "MJPEG", "theora": "Theora", "flv1": "FLV",
    "h263": "H.263", "dvvideo": "DV", "gif": "GIF", "png": "PNG", "dnxhd": "DNxHD",
}

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


@dataclass(frozen=True)
class Mode:
    key: str
    title: str
    hint: str
    palettegen: str
    paletteuse: str
    per_frame: bool  # a separate palette for every frame (single pass)


MODES = {
    "1": Mode("1", "Maximum quality",
              "palette per frame, largest file",
              "palettegen=stats_mode=single:max_colors=256:reserve_transparent=0",
              "paletteuse=new=1:dither=sierra2_4a",
              True),
    "2": Mode("2", "Balanced",
              "one palette for the whole video",
              "palettegen=stats_mode=full",
              "paletteuse=dither=sierra2_4a",
              False),
    "3": Mode("3", "Compact",
              "palette from motion, smaller file",
              "palettegen=stats_mode=diff",
              "paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
              False),
}


@dataclass
class Settings:
    mode: str = "1"
    width: int | None = None        # None — original width
    fps: float | None = None        # None — same as the source (capped at MAX_GIF_FPS)
    start: float | None = None      # seconds from the beginning
    duration: float | None = None   # seconds
    out_dir: Path | None = None     # None — a "gif" folder next to each video


@dataclass
class VideoInfo:
    duration: float | None = None
    fps: float | None = None
    width: int | None = None        # display width (rotation and pixel aspect applied)
    height: int | None = None
    has_video: bool = False
    codec: str = ""
    sar: float = 1.0                # non-square pixels (DV, DVD, old camcorders)
    interlaced: bool = False        # interlaced video (camcorders, TV: .mts, .ts, .vob)

    def describe(self) -> str:
        parts = []
        if self.width and self.height:
            parts.append(f"{self.width}×{self.height}")
        if self.duration:
            parts.append(format_duration(self.duration))
        if self.fps:
            parts.append(f"{format_number(self.fps, 2)} fps")
        if self.codec:
            parts.append(self.codec)
        return " · ".join(parts)


@dataclass
class Result:
    path: Path
    size: int
    fps: float | None


class ConversionError(Exception):
    pass


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- helpers

def format_number(x: float, digits: int = 3) -> str:
    """A number without exponent or trailing zeros: 30.0 -> '30', 29.970 -> '29.97'."""
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    if m >= 60:
        h, m = divmod(int(m), 60)
        return f"{h}:{m:02d}:{s:04.1f}"
    if m:
        return f"{int(m)}:{s:04.1f}"
    return f"{format_number(s, 1)} s"


def format_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{max(1, round(n / 1024))} KB"


def parse_time(text: str | None) -> float | None:
    """'90', '1:30', '1:02:03.5', '2,5' -> seconds. Empty -> None."""
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3 or any(p.strip() == "" for p in parts):
        raise ValueError(text)
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    if not math.isfinite(total) or total < 0:
        raise ValueError(text)
    return total


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def resource_path(relative: str) -> Path:
    """Path to bundled app files — both when running the script and inside a PyInstaller build."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).resolve().parent.parent / relative


def clean_env() -> dict | None:
    """Environment for external programs.

    A PyInstaller build on Linux points LD_LIBRARY_PATH to its own folder;
    external programs (xdg-open, the file manager) need the original value back.
    """
    if not getattr(sys, "frozen", False) or IS_WINDOWS or IS_MAC:
        return None
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original is not None:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_bundled_static_ffmpeg(ffmpeg: str) -> bool:
    """The Linux ffmpeg builds from imageio-ffmpeg (and the one bundled into the app) are static."""
    meipass = getattr(sys, "_MEIPASS", None)
    p = Path(ffmpeg)
    return ("imageio_ffmpeg" in ffmpeg
            or bool(meipass and ffmpeg.startswith(meipass))
            or p.parent in (_app_dir(), _app_dir() / "ffmpeg"))


def popen_kwargs(ffmpeg: str | None = None) -> dict:
    kw = {"stdin": subprocess.DEVNULL}
    env = clean_env()
    if (ffmpeg and not IS_WINDOWS and not IS_MAC and "GCONV_PATH" not in os.environ
            and _is_bundled_static_ffmpeg(ffmpeg)):
        # a static ffmpeg crashes on .ts/.mts/.m2ts while trying to load the system's
        # iconv modules; with GCONV_PATH set it simply skips them
        env = dict(env if env is not None else os.environ)
        env["GCONV_PATH"] = "/nonexistent-gconv"
    if env is not None:
        kw["env"] = env
    if IS_WINDOWS:
        # without this flag the windowed build flashes a console for every ffmpeg run
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return kw


def _ensure_executable(path: Path) -> None:
    if IS_WINDOWS or os.access(path, os.X_OK):
        return
    try:
        path.chmod(path.stat().st_mode | 0o755)
    except OSError:
        pass


# ---------------------------------------------------------------- ffmpeg

def find_ffmpeg() -> str | None:
    """Look for ffmpeg: bundled -> next to the app -> PATH -> imageio-ffmpeg."""
    name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"

    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ffmpeg" / name)
    app_dir = _app_dir()
    candidates += [app_dir / "ffmpeg" / name, app_dir / name]
    for c in candidates:
        if c.is_file():
            _ensure_executable(c)
            return str(c)

    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    if IS_MAC:
        # an app started from Finder doesn't see the terminal's PATH
        for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
            if os.access(p, os.X_OK):
                return p

    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            _ensure_executable(Path(exe))
            return exe
    except Exception:
        pass
    return None


def ffmpeg_version(ffmpeg: str) -> str:
    res = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         **popen_kwargs(ffmpeg))
    return (res.stdout.splitlines() or ["?"])[0]


_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_RE_VIDEO = re.compile(r"Stream #\S+.*?: Video: (.*)")
_RE_SIZE = re.compile(r"(?:^|[\s,])(\d{2,5})x(\d{2,5})(?=[\s,\[]|$)")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)(k?) fps")
_RE_TBR = re.compile(r"(\d+(?:\.\d+)?)(k?) tbr")
_RE_SAR = re.compile(r"SAR (\d+):(\d+)")
_RE_INTERLACED = re.compile(r"\b(top first|bottom first|top coded first|bottom coded first)\b")
_RE_ROTATION = re.compile(r"rotation of (-?\d+(?:\.\d+)?) degrees|rotate\s*:\s*(-?\d+)")


def probe(ffmpeg: str, src: Path) -> VideoInfo:
    """Duration, frame rate and size of a video (parsed from `ffmpeg -i` output)."""
    res = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(src)],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", **popen_kwargs(ffmpeg))
    err = res.stderr
    info = VideoInfo()

    m = _RE_DURATION.search(err)
    if m:
        h, mi, s = m.groups()
        info.duration = int(h) * 3600 + int(mi) * 60 + float(s)

    m = _RE_VIDEO.search(err)
    if m:
        info.has_video = True
        line = m.group(1)
        codec = line.split(" ", 1)[0].strip(",")
        info.codec = _CODEC_NAMES.get(codec, codec.upper())
        size = _RE_SIZE.search(line)
        if size:
            info.width, info.height = int(size.group(1)), int(size.group(2))
        for rx in (_RE_FPS, _RE_TBR):  # DV, WMV etc. may report a bogus "fps" — then use tbr
            rate = rx.search(line)
            if rate:
                value = float(rate.group(1)) * (1000 if rate.group(2) else 1)
                if 0 < value <= 240:
                    info.fps = value
                    break
        sar = _RE_SAR.search(line)
        if sar and int(sar.group(2)) and int(sar.group(1)):
            info.sar = int(sar.group(1)) / int(sar.group(2))
            if info.width and abs(info.sar - 1) > 0.01:
                info.width = round(info.width * info.sar)
        info.interlaced = bool(_RE_INTERLACED.search(line))
        rot = _RE_ROTATION.search(err)
        if rot and info.width:
            angle = float(rot.group(1) or rot.group(2))
            if round(abs(angle)) % 180 == 90:  # a phone video shot in portrait
                info.width, info.height = info.height, info.width
    return info


# ---------------------------------------------------------------- conversion

class Converter:
    """Converts one file at a time; cancel() may be called from another thread."""

    def __init__(self, ffmpeg: str):
        self.ffmpeg = ffmpeg
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    # --- control
    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def reset(self) -> None:
        self._cancel.clear()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def probe(self, src: Path) -> VideoInfo:
        return probe(self.ffmpeg, src)

    # --- the actual work
    def convert(self, src: Path, s: Settings, progress=None,
                info: VideoInfo | None = None) -> Result:
        """progress(fraction 0..1, stage text) is called from the worker thread."""
        report = progress or (lambda frac, stage: None)
        if self.cancelled:
            raise Cancelled()
        if not src.is_file():
            raise ConversionError("File not found")

        info = info or self.probe(src)
        if not info.has_video:
            raise ConversionError("No video stream found (or the format is not supported)")
        if s.start and info.duration and s.start >= info.duration:
            raise ConversionError(
                f"Start ({format_duration(s.start)}) is past the end of the video "
                f"({format_duration(info.duration)})")

        mode = MODES.get(s.mode, MODES["1"])
        fps = s.fps or (min(info.fps, MAX_GIF_FPS) if info.fps else None)
        if fps:
            fps = min(fps, MAX_GIF_FPS)

        total = info.duration
        if total and s.start:
            total = max(total - s.start, 0.0)
        if s.duration:
            total = min(total, s.duration) if total else s.duration

        filters = []
        if info.interlaced:
            filters.append("yadif=deint=1")  # remove interlacing "combs"
        if abs(info.sar - 1) > 0.01:
            # GIF has no notion of non-square pixels — stretch to the correct proportions
            filters.append(f"scale=round(iw*{info.sar:.6f}/2)*2:ih:flags={SCALE_FLAGS},setsar=1")
        if fps:
            filters.append(f"fps={format_number(fps)}")
        filters.append(f"scale={s.width or 'iw'}:-1:flags={SCALE_FLAGS}")
        pre = ",".join(filters)

        out_dir = Path(s.out_dir) if s.out_dir else src.parent / OUTPUT_DIR_NAME
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ConversionError(f"Can't create folder {out_dir}: {e.strerror or e}")
        out = unique_path(out_dir / f"{src.stem}.gif")

        trim = []
        if s.start:
            trim += ["-ss", format_number(s.start)]
        if s.duration:
            trim += ["-t", format_number(s.duration)]

        try:
            if mode.per_frame:
                self._run(trim + [
                    "-i", str(src), "-an",
                    "-vf", f"{pre},split[a][b];[a]{mode.palettegen}[p];[b][p]{mode.paletteuse}",
                    "-loop", "0", "-f", "gif", str(out)],
                    total, lambda f: report(f, "Building GIF"))
            else:
                # two passes: first the palette into a file, then the GIF itself
                # (this way the whole video doesn't have to sit in memory)
                with tempfile.TemporaryDirectory(prefix="gifconvert_") as tmp:
                    palette = Path(tmp) / "palette.png"
                    # the extra branch into "null" only exists so that ffmpeg reports progress
                    self._run(trim + [
                        "-i", str(src), "-an",
                        "-filter_complex", f"[0:v]{pre},split[a][b];[a]{mode.palettegen}[p]",
                        "-map", "[p]", "-frames:v", "1", "-update", "1", str(palette),
                        "-map", "[b]", "-f", "null", "-"],
                        total, lambda f: report(f * 0.35, "Pass 1/2: palette"))
                    if not palette.is_file():
                        raise ConversionError("ffmpeg did not create a palette")
                    self._run(trim + [
                        "-i", str(src), "-i", str(palette), "-an",
                        "-filter_complex", f"[0:v]{pre}[x];[x][1:v]{mode.paletteuse}",
                        "-loop", "0", "-f", "gif", str(out)],
                        total, lambda f: report(0.35 + f * 0.65, "Pass 2/2: building GIF"))
        except BaseException:
            try:
                out.unlink()
            except OSError:
                pass
            raise

        if not out.is_file() or out.stat().st_size == 0:
            raise ConversionError("ffmpeg did not create a GIF")
        report(1.0, "Done")
        return Result(out, out.stat().st_size, fps)

    def _run(self, args: list, total: float | None, on_fraction) -> None:
        if self.cancelled:
            raise Cancelled()
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-nostats", "-progress", "pipe:1", "-y"] + args
        errors: list[str] = []
        with self._lock:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", bufsize=1, **popen_kwargs(self.ffmpeg))
            proc = self._proc
        if self.cancelled:  # cancel arrived while the process was starting
            proc.terminate()

        def drain_stderr():
            for line in proc.stderr:
                errors.append(line.rstrip())
        reader = threading.Thread(target=drain_stderr, daemon=True)
        reader.start()

        try:
            for line in proc.stdout:
                if not line.startswith("out_time=") or not total:
                    continue
                t = _parse_progress_time(line[9:].strip())
                if t is not None:
                    on_fraction(min(max(t / total, 0.0), 1.0))
            proc.wait()
            reader.join(timeout=2)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            with self._lock:
                self._proc = None

        if self.cancelled:
            raise Cancelled()
        if proc.returncode != 0:
            details = "\n".join(e for e in errors if e.strip())[-600:]
            raise ConversionError(details or f"ffmpeg exited with code {proc.returncode}")
        on_fraction(1.0)


def _parse_progress_time(value: str) -> float | None:
    """'00:01:02.345678' -> 62.345678; 'N/A' and negative values -> None."""
    if not value or value.startswith("-") or value == "N/A":
        return None
    try:
        h, m, s = value.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return None
