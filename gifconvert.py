#!/usr/bin/env python3
"""
GifConvert — convert videos (MP4, MOV, MKV, WebM, AVI, WMV and more) to GIF
with the best quality the format allows.

Run from source:
    python gifconvert.py                      - open the app window
    python gifconvert.py video1.mp4 ...       - open the window with these files
    python gifconvert.py --cli                - interactive console mode
    python gifconvert.py -m 2 -w 480 --fps 15 video.mp4
                                              - console mode without questions (for scripts)
    python gifconvert.py --help               - all options

GIFs are saved to a "gif" folder next to each video.
Requirements:  pip install -r requirements.txt
Ready-made apps for Windows / macOS / Linux need no Python — see README.md.
"""

import sys

from gifconv.app import main

if __name__ == "__main__":
    sys.exit(main())
