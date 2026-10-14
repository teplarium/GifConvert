# Third-party software

GifConvert's own source code is released under the [MIT License](LICENSE).
The ready-made apps (the `.exe`, the `.app` and the Linux binary) also bundle the
following third-party software, each under its own license.

## FFmpeg — GNU GPL

The apps include an unmodified `ffmpeg` executable, which GifConvert runs as a separate
program. These are GPL builds of FFmpeg, redistributed through the
[imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) wheels:

| App | FFmpeg version | Built by | License |
|---|---|---|---|
| Windows x64 | 7.1 | [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) | GPL v3 |
| Windows x86 (32-bit) | 4.2.2 | Zeranoe | GPL v3 |
| Linux x64 | 7.0.2 | [John Van Sickle](https://johnvansickle.com/ffmpeg/) | GPL v3 |
| macOS Intel | 7.1 | imageio-ffmpeg | GPL v3 |
| macOS Apple Silicon | 7.1 | imageio-ffmpeg | GPL v2 or later |

FFmpeg is © the FFmpeg developers. The source code of these versions is available at
<https://ffmpeg.org/releases/> (`ffmpeg-7.1`, `ffmpeg-7.0.2`, `ffmpeg-4.2.2`).
Run `ffmpeg -version` to see the exact configuration of a build.
License texts: <https://www.gnu.org/licenses/gpl-3.0.html> and
<https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>.

## Other components

| Component | License |
|---|---|
| [Python](https://www.python.org/) runtime | PSF License |
| [Tcl/Tk](https://www.tcl.tk/) | Tcl/Tk License (BSD-style) |
| [Pillow](https://python-pillow.org/) | MIT-CMU License |
| [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) and [tkdnd](https://github.com/petasis/tkdnd) | MIT / BSD |
| [PyInstaller](https://pyinstaller.org/) bootloader | GPL v2 with an exception that allows distributing apps under any license |
