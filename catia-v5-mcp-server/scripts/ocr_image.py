"""OCR one rendered drawing page and print JSON spans.

Run in a subprocess by ``catia_mcp.drawing.pdf_evidence``. Keeping RapidOCR /
onnxruntime out of the long-lived MCP server process avoids a first-import hang
observed in the desktop gateway (2026-10-28).

    python scripts/ocr_image.py <image.png|-> [minConfidence]

``-`` reads the PNG from a private stdin pipe supplied by the caller.

Prints a JSON array: ``[{"text": ..., "confidence": ..., "bboxPx": [x0,y0,x1,y1]}]``.
Diagnostics go to stderr so stdout stays valid JSON.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    image_path = sys.argv[1]
    min_confidence = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - dependency issue
        print(f"rapidocr-onnxruntime is not installed: {exc}", file=sys.stderr)
        return 3
    import cv2
    import numpy as np
    encoded = (np.frombuffer(sys.stdin.buffer.read(), dtype=np.uint8) if image_path == "-"
               else np.fromfile(image_path, dtype=np.uint8))
    pixels = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if pixels is None:
        raise ValueError("Cannot decode OCR image")
    # Preserve small dimensions at the rendered resolution without allocating
    # a full-sheet detection tensor (A3 at 400 dpi can exhaust Windows commit).
    # A max limit also prevents RapidOCR's default "min" rule from upscaling.
    tile_size, overlap = 896, 128
    engine = RapidOCR(det_limit_side_len=tile_size, det_limit_type="max",
                      max_side_len=tile_size, intra_op_num_threads=2,
                      inter_op_num_threads=1, rec_batch_num=1, cls_batch_num=1)
    # The overview recovers long labels wider than the tile overlap. Detailed
    # passes retain the small dimensions lost when the whole sheet is reduced.
    overview, _ = engine(pixels, text_score=min_confidence)
    result = list(overview or [])
    for y in range(0, pixels.shape[0], tile_size - overlap):
        for x in range(0, pixels.shape[1], tile_size - overlap):
            tile = pixels[y:y + tile_size, x:x + tile_size]
            normal, _ = engine(tile, text_score=min_confidence)
            # The classifier handles 180 degrees, but vertical dimensions
            # need a 90-degree pass followed by restoration to page pixels.
            turned, _ = engine(cv2.rotate(tile, cv2.ROTATE_90_CLOCKWISE),
                               text_score=min_confidence)
            entries = list(normal or [])
            for box, text, score in turned or []:
                entries.append([[[p[1], tile.shape[0] - 1 - p[0]] for p in box], text, score])
            for box, text, score in entries:
                # Discard text cut by an interior tile edge; the overlapping
                # tile supplies its complete box instead.
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                if ((x and min(xs) <= 2) or (y and min(ys) <= 2)
                    or (x + tile_size < pixels.shape[1] and max(xs) >= tile.shape[1] - 2)
                    or (y + tile_size < pixels.shape[0] and max(ys) >= tile.shape[0] - 2)):
                    continue
                result.append([[[p[0] + x, p[1] + y] for p in box], text, score])
    spans = []
    for entry in result or []:
        try:
            box, text, score = entry
        except (TypeError, ValueError):
            continue
        confidence = float(score or 0.0)
        text = str(text or "").strip()
        if not text or confidence < min_confidence:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        spans.append({
            "text": text,
            "confidence": round(confidence, 3),
            "bboxPx": [min(xs), min(ys), max(xs), max(ys)],
        })
    json.dump(spans, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
