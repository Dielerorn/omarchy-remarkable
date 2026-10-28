"""The image decoder process behind web.ImageDecoder.

Started as `python -m rmbridge.imagedecode` with memory and CPU limits set
before it runs. Frames on stdin are a 4-byte big-endian length and an
image; each answer on stdout is one kind byte (0 nothing worth keeping,
1 PNG, 2 JPEG), a 4-byte length and the re-encoded image.
"""

from __future__ import annotations

import sys

KINDS = {"image/png": 1, "image/jpeg": 2}


def main() -> int:
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
