"""Content-based image validation and conversion checks."""

from io import BytesIO
import unittest

from PIL import Image

from app.errors import OCRInputError
from app.processing import convert_image, prepare_image


def encoded_image(format="PNG", mode="RGB", color="black", **save_options):
    output = BytesIO()
    with Image.new(mode, (3, 2), color) as image:
        image.save(output, format=format, **save_options)
    return output.getvalue()


class ProcessingTests(unittest.TestCase):
    def test_supported_bytes_are_preserved_regardless_of_filename(self):
        for format in ("PNG", "JPEG", "WEBP"):
            with self.subTest(format=format):
                data = encoded_image(format)
                self.assertEqual(prepare_image(BytesIO(data), "misleading.pdf"), data)

    def test_reads_from_current_position_and_keeps_caller_stream_open(self):
        data = encoded_image()
        stream = BytesIO(b"prefix" + data)
        stream.seek(6)
        self.assertEqual(prepare_image(stream, "input.png"), data)
        self.assertFalse(stream.closed)

    def test_fallback_formats_become_rgb_png(self):
        for format in ("BMP", "TIFF", "GIF"):
            with self.subTest(format=format):
                data = encoded_image(format)
                converted = prepare_image(BytesIO(data), "input.jpg")
                with Image.open(BytesIO(converted)) as image:
                    self.assertEqual((image.format, image.mode, image.size), ("PNG", "RGB", (3, 2)))
                    self.assertEqual(image.getpixel((0, 0)), (0, 0, 0))

    def test_alpha_is_flattened_onto_white(self):
        for color, expected in (
            ((255, 0, 0, 0), (255, 255, 255)),
            ((255, 0, 0, 128), (255, 127, 127)),
        ):
            with self.subTest(color=color):
                data = encoded_image(mode="RGBA", color=color)
                converted = prepare_image(BytesIO(data), "input.png")
                with Image.open(BytesIO(converted)) as image:
                    self.assertEqual(image.mode, "RGB")
                    self.assertEqual(image.getpixel((0, 0)), expected)

    def test_palette_transparency_is_flattened(self):
        data = encoded_image("GIF", color="black", transparency=0)
        converted = prepare_image(BytesIO(data), "input.gif")
        with Image.open(BytesIO(converted)) as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_exif_orientation_is_corrected(self):
        exif = Image.Exif()
        exif[274] = 6
        data = encoded_image("JPEG", exif=exif)
        converted = prepare_image(BytesIO(data), "input.jpg")
        with Image.open(BytesIO(converted)) as image:
            self.assertEqual(image.size, (2, 3))
            self.assertEqual(image.getexif().get(274, 1), 1)

    def test_heif_decoder_is_registered(self):
        data = encoded_image("HEIF")
        converted = prepare_image(BytesIO(data), "input.heic")
        with Image.open(BytesIO(converted)) as image:
            self.assertEqual((image.format, image.mode), ("PNG", "RGB"))

    def test_invalid_empty_pdf_svg_and_truncated_inputs_are_rejected(self):
        inputs = (b"", b"not an image", b"%PDF-1.7", b"<svg/>", encoded_image()[:45])
        for data in inputs:
            for entry in (
                lambda: prepare_image(BytesIO(data), "input.png"),
                lambda: convert_image(data, "input.png"),
            ):
                with self.subTest(data=data[:12]), self.assertRaises(OCRInputError):
                    entry()

    def test_multiframe_input_is_rejected(self):
        with Image.new("RGB", (3, 2), "red") as first:
            with Image.new("RGB", (3, 2), "blue") as second:
                for format in ("GIF", "TIFF", "PNG", "WEBP"):
                    output = BytesIO()
                    first.save(output, format=format, save_all=True, append_images=[second])
                    with self.subTest(format=format), self.assertRaisesRegex(OCRInputError, "Multi-frame"):
                        prepare_image(BytesIO(output.getvalue()), "input." + format.lower())


if __name__ == "__main__":
    unittest.main()
