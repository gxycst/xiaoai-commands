"""Private portrait and static SVG routes, without network or real NAS files."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

class BrandingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = server.CommandsStore(str(self.folder))
        self.original = b'[]\n'
        (self.folder / 'commands.json').write_bytes(self.original)

    def tearDown(self):
        self.assertEqual((self.folder / 'commands.json').read_bytes(), self.original)
        self.temp.cleanup()

    def test_private_png_serves_exact_bytes(self):
        data = b'\x89PNG\r\n\x1a\n' + b'private-photo-fixture'
        (self.folder / 'niuniu.png').write_bytes(data)
        self.assertEqual(self.store.read_portrait(), data)

    def test_missing_portrait_does_not_create_file(self):
        with self.assertRaises(server.APIError) as ctx:
            self.store.read_portrait()
        self.assertEqual(ctx.exception.status, 404)
        self.assertFalse((self.folder / 'niuniu.png').exists())

    def test_invalid_or_oversized_png_rejected(self):
        for data in (b'<svg>not PNG</svg>', b'\x89PNG\r\n\x1a\n' + b'x'*(8*1024*1024)):
            (self.folder / 'niuniu.png').write_bytes(data)
            with self.assertRaises(server.APIError) as ctx:
                self.store.read_portrait()
            self.assertEqual(ctx.exception.status, 422)

    def test_symbolic_link_portrait_rejected(self):
        target = self.folder / 'outside.png'
        target.write_bytes(b'\x89PNG\r\n\x1a\nprivate')
        try:
            (self.folder / 'niuniu.png').symlink_to(target)
        except OSError:
            self.skipTest('Creating symlinks is unavailable')
        with self.assertRaises(server.APIError):
            self.store.read_portrait()

    def route(self, path):
        handler = object.__new__(server.Handler)
        app = server.Application('TestPassword-9528', str(self.folder), '127.0.0.0/8')
        handler.server = SimpleNamespace(app=app)
        handler.client_address = ('127.0.0.1', 12345)
        handler.path, handler.command = path, 'GET'
        captured = []
        handler._send = lambda *args, **kwargs: captured.append((args, kwargs))
        handler._dispatch()
        return captured[0][0]

    def test_svg_has_correct_content_type(self):
        status, data, content_type = self.route('/speaker.svg')
        self.assertEqual(status, 200)
        self.assertEqual(content_type, 'image/svg+xml')
        self.assertIn(b'<svg', data)

    def test_photo_route_uses_mount_not_public_static(self):
        data = b'\x89PNG\r\n\x1a\nfrom-private-mount'
        (self.folder / 'niuniu.png').write_bytes(data)
        status, body, content_type = self.route('/niuniu.png')
        self.assertEqual((status, body, content_type), (200, data, 'image/png'))

    def test_other_private_files_not_served(self):
        for path in ('/commands.json', '/niuniu.png/../commands.json', '/%2e%2e/niuniu.png'):
            with self.assertRaises(server.APIError) as ctx:
                self.route(path)
            self.assertEqual(ctx.exception.status, 404)

if __name__ == '__main__':
    unittest.main()
