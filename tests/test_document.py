"""Grounding parsing, content association, and prepared-image crop fidelity."""

from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import Mock

from PIL import Image

from app.document import build_document, parse_document
from app.errors import OCROutputError
from app.export import render_markdown
from app.processing import prepare_image
from app.service import OCRService


FIXTURE = Path(__file__).parent / "fixtures" / "double_drawer.txt"


def color_image(size=(100, 60), exif=None):
    with Image.new("RGB", size, "red") as image, BytesIO() as output:
        image.paste("blue", (size[0] // 2, 0, size[0], size[1]))
        image.save(output, format="PNG", **({"exif": exif} if exif else {}))
        return output.getvalue()


class DocumentTests(unittest.TestCase):
    def test_supplied_fixture_has_six_blocks_and_one_binary_crop(self):
        document = build_document(FIXTURE.read_text(), color_image((1000, 1200)))
        self.assertEqual((document.width, document.height), (1000, 1200))
        self.assertEqual([block.label for block in document.blocks], [
            "sub_title", "text", "image", "image_caption", "text", "text",
        ])
        self.assertIn("## Mobile: Double Drawer", document.blocks[0].content)
        self.assertIn("<center>Figure 7-2:", document.blocks[3].content)
        self.assertIn("Notes  1. Tidwell, 2006", document.blocks[5].content)
        block = document.blocks[2]
        self.assertEqual(block.content, "\n")
        self.assertEqual(block.bounding_boxes[0].normalized, (94, 220, 849, 579))
        self.assertEqual(block.bounding_boxes[0].pixels, (94, 264, 849, 695))
        self.assertEqual(len(block.images), 1)
        crop = block.images[0]
        self.assertIsInstance(crop.data, bytes)
        self.assertTrue(crop.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual((crop.bounding_box_index, crop.filename), (0, "image-001.png"))
        with Image.open(BytesIO(crop.data)) as image:
            self.assertEqual(image.size, (755, 431))
        markdown = render_markdown(document)
        self.assertEqual(markdown.count("![Image](image-001.png)"), 1)
        self.assertNotIn("[[", markdown)
        self.assertLess(markdown.index("![Image]"), markdown.index("<center>"))
        for text_block in document.blocks:
            self.assertIn(text_block.content, markdown)

    def test_native_and_simplified_markers_preserve_unknown_labels_and_content(self):
        native = "<|ref|>novel_label<|/ref|><|det|>[[0, 10, 999, 500], [1, 20, 888, 600]]<|/det|>"
        simplified = "novel_label[[0, 10, 999, 500], [1, 20, 888, 600]]"
        content = '\n## café  \n<center>caption</center>\r\n\n'
        for marker in (native, simplified):
            with self.subTest(marker=marker):
                document = parse_document("Preamble\n" + marker + content, 200, 100)
                self.assertEqual([block.label for block in document.blocks], ["text", "novel_label"])
                self.assertEqual(document.blocks[0].content, "Preamble\n")
                self.assertEqual(document.blocks[1].content, content)
                self.assertEqual(len(document.blocks[1].bounding_boxes), 2)

    def test_adjacent_native_empty_blocks_and_mixed_marker_styles(self):
        text = (
            "<|ref|>image<|/ref|><|det|>[[0,0,499,999]]<|/det|>"
            "<|ref|>image<|/ref|><|det|>[[500,0,999,999]]<|/det|>\n"
            "text[[0,0,999,999]]\nAfter"
        )
        document = build_document(text, color_image())
        self.assertEqual([block.content for block in document.blocks], ["", "\n", "\nAfter"])
        self.assertEqual([block.images[0].filename for block in document.blocks[:2]], ["image-001.png", "image-002.png"])

    def test_plain_text_and_empty_output(self):
        for text in ("", "  café\r\n\n", "## Header\n\nbody", "![external](figure.png)"):
            with self.subTest(text=text):
                document = parse_document(text, 20, 10)
                self.assertEqual(render_markdown(document), text)
                self.assertEqual(len(document.blocks), 1 if text else 0)
                if text:
                    self.assertEqual(document.blocks[0].bounding_boxes, [])

    def test_multiple_image_boxes_scale_independent_axes_and_preserve_pixels(self):
        document = build_document("image[[0,0,499,999],[500,0,999,999]]", color_image())
        block = document.blocks[0]
        self.assertEqual([box.pixels for box in block.bounding_boxes], [(0, 0, 49, 60), (50, 0, 100, 60)])
        for index, (asset, color) in enumerate(zip(block.images, [(255, 0, 0), (0, 0, 255)])):
            self.assertEqual(asset.bounding_box_index, index)
            with Image.open(BytesIO(asset.data)) as crop:
                self.assertEqual(crop.size, ((49, 50)[index], 60))
                self.assertEqual(crop.getpixel((0, 0)), color)
                self.assertEqual(crop.getpixel((crop.width - 1, crop.height - 1)), color)

    def test_only_exact_image_label_is_cropped(self):
        text = "image_caption[[0,0,999,999]]\ncaption\nImage[[0,0,999,999]]\nother"
        document = build_document(text, color_image())
        self.assertTrue(all(not block.images for block in document.blocks))

    def test_service_crops_exif_corrected_inference_image(self):
        exif = Image.Exif()
        exif[274] = 6
        original = color_image((100, 60), exif)
        prepared = prepare_image(BytesIO(original), "input.png")
        client = Mock()
        client.recognize.return_value = "image[[0,0,999,499]]"
        document = OCRService(client).ocr(BytesIO(original), "input.png")
        client.recognize.assert_called_once_with(prepared)
        self.assertEqual((document.width, document.height), (60, 100))
        with Image.open(BytesIO(document.blocks[0].images[0].data)) as crop:
            self.assertEqual(crop.size, (60, 49))
            self.assertEqual(crop.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(crop.getpixel((59, 48)), (255, 0, 0))

    def test_invalid_grounding_fails_clearly(self):
        invalid = (
            "image[[0,0,1000,999]]", "image[[-1,0,999,999]]",
            "image[[0,0,999.0,999]]", "image[[false,0,999,999]]",
            'image[[0,0,"999",999]]', "image[[0,0,999]]",
            "image[[0,0,0,999]]", "image[[999,0,0,999]]",
            "image[[0,999,999,0]]", "image[[0,0,999,0]]",
            "image[[broken]]", "image[[0,0,999,999]", "image[[]]",
            "<|ref|>image<|/ref|><|det|>[]<|/det|>",
            "<|ref|>image<|/ref|><|det|>[[0,0,999,999]]",
            "<|ref|>image<|det|>[[0,0,999,999]]<|/det|>",
            "<|ref|><|/ref|><|det|>[[0,0,999,999]]<|/det|>",
            "orphan <|det|>[[0,0,999,999]]<|/det|>",
            "text[[0,0,999,999]]\n<|/ref|>",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(OCROutputError):
                parse_document(text, 100, 60)

    def test_image_box_that_collapses_to_zero_pixels_fails(self):
        with self.assertRaisesRegex(OCROutputError, "empty pixel crop"):
            build_document("image[[0,0,1,1]]", color_image())
        # Small text boxes still describe valid normalized layout metadata.
        document = build_document("text[[0,0,1,1]]\ntiny", color_image())
        self.assertEqual(document.blocks[0].bounding_boxes[0].pixels, (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
