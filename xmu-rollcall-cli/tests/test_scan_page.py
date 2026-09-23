import re
import unittest
from pathlib import Path

import xmu_rollcall


SCAN_HTML = Path(xmu_rollcall.__file__).parent / "templates" / "scan.html"


class ScanPageTest(unittest.TestCase):
    """Guards the two decode settings that were measured, not guessed.

    A 61-module rollcall QR filling ~20% of the aiming square is undecodable
    below a ~2160px sensor short axis, and no single downscale covers every
    distance. Both were found by instrumenting real phones; nothing in the
    page itself explains why the numbers are what they are, so pin them here.
    """

    def setUp(self):
        self.html = SCAN_HTML.read_text(encoding="utf-8")

    def test_requests_full_sensor_resolution(self):
        # A phone held upright reports portrait, so "ideal" lands on the LONG
        # axis; asking for 1920 leaves only 1080 across the QR.
        ideal = [int(n) for n in re.findall(r"ideal:\s*(\d+)", self.html)]
        self.assertTrue(ideal, "camera constraints lost their ideal resolution")
        self.assertGreaterEqual(min(ideal), 2160, f"resolution request too low: {ideal}")

    def test_decodes_at_more_than_one_scale(self):
        match = re.search(r"const SCALES\s*=\s*\[([^\]]+)\]", self.html)
        self.assertIsNotNone(match, "SCALES constant is gone")
        scales = [int(n) for n in re.findall(r"\d+", match.group(1))]
        self.assertGreaterEqual(len(scales), 2, f"need >1 scale, got {scales}")

    def test_still_photo_fallback_present(self):
        # The escape hatch when live scanning cannot reach a distant projector.
        self.assertIn('type="file"', self.html)
        self.assertIn('capture="environment"', self.html)

    def test_decoder_is_self_hosted(self):
        # unpkg is unreliable from the mainland; the decoder must ship with us.
        self.assertIn('src="/static/jsQR.js"', self.html)
        self.assertNotIn("unpkg.com", self.html)

    def test_focus_is_not_requested_only_as_advanced(self):
        # An `advanced` constraint may be dropped silently: a HarmonyOS Mate 60
        # stayed on focusMode "manual" through one while advertising "continuous".
        # A basic constraint has to be tried first so failure is observable.
        basic = self.html.find('{ focusMode: "continuous" }')
        advanced = self.html.find('advanced: [{ focusMode: "continuous" }]')
        self.assertNotEqual(basic, -1, "no plain focusMode constraint at all")
        if advanced != -1:
            self.assertLess(basic, advanced, "advanced focus constraint is tried first")

    def test_zoom_slider_stays_responsive(self):
        # jsQR is synchronous and owns the main thread for 50-200ms a frame on the
        # phones measured, which is the thread the slider's touch events use. The
        # decode loop has to stand down while a drag is in progress, and the slider
        # must not be quantised to whatever step the camera reports.
        self.assertIn("ZOOM_QUIET_MS", self.html)
        self.assertIn('zoomEl.step = "any"', self.html)

    def test_zoom_constraints_are_not_applied_per_event(self):
        # `input` fires per pixel of a drag; one applyConstraints each queues dozens
        # of camera reconfigurations and the preview trails the finger by seconds.
        self.assertIn("function pump()", self.html)

    def test_zoom_has_a_fallback_when_the_camera_has_none(self):
        # Same device reports no zoom capability at all, so the slider has to fall
        # back to cropping instead of disappearing.
        self.assertIn("useDigitalZoom", self.html)
        self.assertIn("digitalZoom", self.html)


if __name__ == "__main__":
    unittest.main()
