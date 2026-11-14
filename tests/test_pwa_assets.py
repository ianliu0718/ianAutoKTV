import json
import struct
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PwaAssetsTests(unittest.TestCase):
    """確認 PWA manifest、圖示尺寸與各主要頁面的共用宣告一致。"""

    def test_manifest_icons_exist_with_declared_png_dimensions(self):
        manifest_path = PROJECT_ROOT / "static" / "manifest.webmanifest"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertTrue(any(icon["purpose"] == "any" for icon in manifest["icons"]))
        self.assertTrue(any(icon["purpose"] == "maskable" for icon in manifest["icons"]))

        for icon in manifest["icons"]:
            icon_path = PROJECT_ROOT.joinpath(*icon["src"].lstrip("/").split("/"))
            self.assertTrue(icon_path.is_file(), icon["src"])
            width, height = struct.unpack(">II", icon_path.read_bytes()[16:24])
            expected_size = tuple(int(value) for value in icon["sizes"].split("x"))
            self.assertEqual((width, height), expected_size, icon["src"])

    def test_ios_favicon_and_page_head_include_exist(self):
        icon_dir = PROJECT_ROOT / "static" / "icons"
        self.assertTrue((icon_dir / "ianautoktv-180.png").is_file())
        self.assertTrue((icon_dir / "ianautoktv.ico").is_file())

        templates_dir = PROJECT_ROOT / "templates"
        for template_name in (
            "room_entry.html",
            "remote.html",
            "player.html",
            "combo.html",
            "admin.html",
            "drive-prototype.html",
        ):
            template = (templates_dir / template_name).read_text(encoding="utf-8")
            self.assertIn("{% include '_pwa_head.html' %}", template)


if __name__ == "__main__":
    unittest.main()
