"""Text extraction from a captured frame, via macOS's built-in Vision
framework (the same OCR engine behind Live Text in Preview/Photos)."""

from __future__ import annotations

import numpy as np
import Quartz
import Vision


def numpy_to_cgimage(frame: np.ndarray):
    height, width = frame.shape[:2]
    rgba = np.dstack([frame, np.full((height, width), 255, dtype=np.uint8)]).copy()
    data = rgba.tobytes()
    provider = Quartz.CGDataProviderCreateWithData(None, data, len(data), None)
    colorspace = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
    bytes_per_row = width * 4
    return Quartz.CGImageCreate(
        width, height, 8, 32, bytes_per_row, colorspace,
        Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big,
        provider, None, False, Quartz.kCGRenderingIntentDefault,
    )


def extract_text(frame: np.ndarray) -> str:
    """Run OCR on a captured frame. Returns recognized text lines joined by
    newlines, or '' if nothing was found or recognition failed."""
    image = numpy_to_cgimage(frame)

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)

    success, error = handler.performRequests_error_([request], None)
    if not success:
        return ""

    lines = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if candidates:
            lines.append(str(candidates[0].string()))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from PIL import Image
    img = Image.open(sys.argv[1]).convert("RGB")
    arr = np.array(img)
    print(repr(extract_text(arr)))
