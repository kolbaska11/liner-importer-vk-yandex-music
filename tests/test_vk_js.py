from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class VKSecurityTests(unittest.TestCase):
    def test_no_hardcoded_personal_playlists(self):
        script = (ROOT / 'exporters/vk_mobile.js').read_text(encoding='utf-8')
        self.assertIn("const PLAYLISTS = (input || '')", script)
        self.assertNotIn('const PLAYLISTS = [', script)
        self.assertIn("location.hostname !== 'm.vk.ru'", script)
        self.assertIn("fetch('/audio'", script)

    def test_private_directories_ignored(self):
        gi = (ROOT / '.gitignore').read_text(encoding='utf-8')
        for pattern in ('auth/*', 'data/*', 'work/*', 'reports/*', '.env'):
            self.assertIn(pattern, gi)

if __name__ == '__main__': unittest.main()
