# -*- mode: python ; coding: utf-8 -*-
"""
Build GifConvert for the CURRENT system:  python -m PyInstaller --noconfirm --clean GifConvert.spec

  Windows -> dist/GifConvert.exe        (a single file; bitness = bitness of the Python used)
  Linux   -> dist/GifConvert            (a single file)
  macOS   -> dist/GifConvert.app        (an app bundle; architecture = that of the Python/Mac)

ffmpeg is taken from the imageio-ffmpeg package installed in the build environment and
bundled into the app, so users don't need to install anything.
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT))
from gifconv import APP_NAME, __version__ as VERSION  # noqa: E402

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
BUNDLE_ID = "io.github.teplarium.gifconvert"

# ---- ffmpeg for this platform -> build/ffmpeg_bin/ffmpeg(.exe)
import imageio_ffmpeg  # noqa: E402

ffmpeg_src = Path(imageio_ffmpeg.get_ffmpeg_exe())
stage = ROOT / "build" / "ffmpeg_bin"
stage.mkdir(parents=True, exist_ok=True)
ffmpeg_staged = stage / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
shutil.copy2(ffmpeg_src, ffmpeg_staged)
os.chmod(ffmpeg_staged, 0o755)
print(f"[GifConvert] ffmpeg: {ffmpeg_src}")

a = Analysis(
    [str(ROOT / "gifconvert.py")],
    pathex=[str(ROOT)],
    binaries=[(str(ffmpeg_staged), "ffmpeg")],
    datas=[
        (str(ROOT / "assets" / "icon.png"), "assets"),
        (str(ROOT / "LICENSE"), "licenses"),
        (str(ROOT / "THIRD_PARTY_NOTICES.md"), "licenses"),
    ],
    hiddenimports=["tkinterdnd2", "PIL.ImageTk", "PIL._tkinter_finder"],
    # imageio_ffmpeg is excluded: we bundle its ffmpeg ourselves (otherwise it would be included twice)
    excludes=["imageio_ffmpeg", "numpy", "setuptools", "pkg_resources"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if IS_MAC:
    signing = os.environ.get("MACOS_CODESIGN_IDENTITY") or None
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name=APP_NAME,
        console=False,
        argv_emulation=False,   # files dropped on the icon are handled by tkinter (::tk::mac::OpenDocument)
        target_arch=None,       # the architecture of the Python used for the build
        codesign_identity=signing,
        entitlements_file=str(ROOT / "packaging" / "entitlements.plist") if signing else None,
        icon=str(ROOT / "assets" / "icon.icns"),
        upx=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(ROOT / "assets" / "icon.icns"),
        bundle_identifier=BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": True,  # the UI is light by design
            "LSApplicationCategoryType": "public.app-category.graphics-design",
            # lets users drop videos onto the icon and use "Open With"
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Video",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.movie", "public.video", "public.mpeg-4",
                                       "com.apple.quicktime-movie", "public.avi",
                                       "org.matroska.mkv", "org.webmproject.webm",
                                       "com.microsoft.windows-media-wmv",
                                       "com.compuserve.gif"],
            }],
        },
    )
else:
    version_info = None
    if IS_WIN:
        from PyInstaller.utils.win32.versioninfo import (  # noqa: E402
            FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
            VarStruct, VSVersionInfo)
        nums = tuple(int(x) for x in (VERSION.split(".") + ["0"] * 4)[:4])
        version_info = VSVersionInfo(
            ffi=FixedFileInfo(filevers=nums, prodvers=nums),
            kids=[
                StringFileInfo([StringTable("040904B0", [
                    StringStruct("FileDescription", "GifConvert — video to GIF converter"),
                    StringStruct("ProductName", APP_NAME),
                    StringStruct("CompanyName", "teplarium"),
                    StringStruct("LegalCopyright", "Copyright (c) 2026 teplarium. MIT License."),
                    StringStruct("FileVersion", VERSION),
                    StringStruct("ProductVersion", VERSION),
                    StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                    StringStruct("InternalName", APP_NAME),
                ])]),
                VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
            ],
        )
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP_NAME,
        console=False,          # no black console window
        upx=False,              # UPX tends to trigger antivirus false positives
        icon=str(ROOT / "assets" / "icon.ico") if IS_WIN else None,
        version=version_info,
    )
