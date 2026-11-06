"""The image decoder process behind web.ImageDecoder.

Started as `python -m rmbridge.imagedecode` with memory and CPU limits set
before it runs. Frames on stdin are a 4-byte big-endian length and an
image; each answer on stdout is one kind byte (0 nothing worth keeping,
1 PNG, 2 JPEG), a 4-byte length and the re-encoded image.

`python -m rmbridge.imagedecode fit` draws one page image instead
(tablet_system.fit_png): a JSON line on stdin names the file and size, and
stdout is a JSON line with the result followed by the PNG.
"""

from __future__ import annotations

import sys

KINDS = {"image/png": 1, "image/jpeg": 2}


def fit() -> int:
    import json
    from pathlib import Path

    from .tablet_system import fit_png_here

    request = json.loads(sys.stdin.buffer.readline())
    src = Path(request["path"])
    data = b""
    try:
        data, info = fit_png_here(src, int(request["width"]), int(request["height"]))
        header = {"ok": True, "info": info}
    except MemoryError:
        header = {"ok": False, "error": f"{src.name} needs more memory to draw than a page image should"}
    except Exception as e:
        header = {"ok": False, "error": str(e) if isinstance(e, ValueError) else f"can't draw {src.name}"}
    sys.stdout.buffer.write(json.dumps(header).encode() + b"\n" + data)
    sys.stdout.flush()
    return 0


def main() -> int:
    if sys.argv[1:] == ["fit"]:
        return fit()
    from .web import prepare_image

    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    while True:
        head = stdin.read(4)
        if len(head) < 4:
            return 0
        data = stdin.read(int.from_bytes(head, "big"))
        try:
            result = prepare_image(data)
        except Exception:
            result = None
        kind, body = (KINDS[result[1]], result[0]) if result else (0, b"")
        stdout.write(bytes([kind]) + len(body).to_bytes(4, "big") + body)
        stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
