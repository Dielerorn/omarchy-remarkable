# Third-party notices

The plugin's own code is MIT-licensed (see LICENSE). This file lists the code it
adapts and the programs and libraries it uses. Nothing below is bundled in this
repository except the adapted rmc code.

## Adapted code: rmc

`helper/rmbridge/render.py` adapts the pen models and text-anchor layout from
[rmc](https://github.com/ricklupton/rmc), which itself derives from maxio.

MIT License

Copyright (c) 2023 Rick Lupton

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Programs downloaded on first use

Each is a pinned release, checked against a SHA-256 written in the source
before it runs. They run as separate programs.

| Program | Version | Licence | When |
|---|---|---|---|
| [rmapi](https://github.com/ddvk/rmapi) | v0.0.35 | AGPL-3.0 | First cloud access, unless `rmapi` is already on your PATH. Stored in `~/.local/share/omarchy-remarkable/bin`. |
| [goMarkableStream](https://github.com/owulveryck/goMarkableStream) | v1.4.0 | MIT | Only when you start the live screen in developer mode. Copied to the tablet and run there while the stream is open. |

## Python libraries

`uv` installs these from PyPI into `~/.local/share/omarchy-remarkable/venv` on
first run, at the versions locked in `helper/uv.lock`.

| Library | Licence | Used for |
|---|---|---|
| [rmscene](https://github.com/ricklupton/rmscene) | MIT | Reading reMarkable pages (.rm v6) |
| [PyMuPDF](https://github.com/pymupdf/PyMuPDF) | AGPL-3.0 (or a commercial licence from Artifex) | Drawing pages, PDF export, EPUB layout |
| [trafilatura](https://github.com/adbar/trafilatura) | Apache-2.0 | Pulling the article out of a web page |
| [jeepney](https://gitlab.com/takluyver/jeepney) | MIT | Desktop notifications over D-Bus |

PyMuPDF and rmapi are AGPL-3.0. This repository doesn't contain or
redistribute either one: your computer downloads them from their own sources.
If you redistribute the plugin together with PyMuPDF, for example in a package
or an image, the AGPL's terms apply to that bundle.

## System programs it calls

These come from your system's packages and are used only when present:

- `uv`: required, to set up the Python environment.
- `ippeveprinter` (cups) and the avahi daemon (avahi): the "reMarkable" printer.
- `ssh` and `scp` (openssh): developer-mode features.
- `zenity`: the file chooser.
- `chromium` (or Chrome or Brave): the live-screen window, and web pages that only render in a browser.
- `wl-copy` (wl-clipboard) and `xdg-open` (xdg-utils).
- `claude` (Claude Code): handwriting to text, if you choose it.
- `omarchy-calendar` ([omarchy-google-calendar](https://github.com/shivam-g10/omarchy-google-calendar)): calendar events on the agenda page.
