import csv
import tempfile
import unittest
from pathlib import Path

from prepare import prepare, entry, parse_sections
from exporters.yandex_music import resolve_rows, export_rows
from app import read_playlist
from matcher import Entry, Spec, score_results, verdict


class PrepareTests(unittest.TestCase):
    def test_single_playlist(self):
        rows = parse_sections('Nirvana — Smells Like Teen Spirit\nКишлак — Музыка\n')
        self.assertEqual(len(rows['Весь список']), 2)
        self.assertEqual(rows['Весь список'][0]['artist'], 'Nirvana')

    def test_vk_export_multi_section(self):
        exported = ('========================================\nПЛЕЙЛИСТ 1\n'
                    'VK playlist ID: 10\nКоличество треков: 2\n'
                    '========================================\n'
                    'Example Artist — Example Song\nQueen — Bohemian Rhapsody\n'
                    '========================================\nПЛЕЙЛИСТ 2\n'
                    'VK playlist ID: 20\nКоличество треков: 1\n'
                    '========================================\nQueen — Bohemian Rhapsody\n')
        result = parse_sections(exported)
        self.assertEqual([len(r) for r in result.values()], [2, 1])
        with tempfile.TemporaryDirectory() as temp:
            infile = Path(temp) / 'export.txt'
            infile.write_text(exported, encoding='utf-8')
            meta = prepare(infile, Path(temp) / 'out')
            self.assertEqual(meta['unique_entries'], 2)
            self.assertEqual(len(meta['playlists']), 3)
            records = read_playlist(Path(temp) / 'out' / '00_all_unique.csv', 'auto')
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].specs[0].artist, 'Example Artist')

    def test_unique_overrides_repeated_sections(self):
        exported = ('========================================\nПЛЕЙЛИСТ 1\n'
                    'Количество треков: 1\n========================================\nA — One\n'
                    '========================================\nОБЩИЙ УНИКАЛЬНЫЙ СПИСОК\n'
                    'Количество треков: 2\n========================================\nA — One\nB — Two\n')
        with tempfile.TemporaryDirectory() as temp:
            infile = Path(temp) / 'src.txt'
            infile.write_text(exported, encoding='utf-8')
            meta = prepare(infile, Path(temp) / 'out')
            self.assertEqual(meta['unique_entries'], 2)

    def test_uncertain_not_autoconfirmed(self):
        r = entry('unknown broken song title', 3, 'Test')
        self.assertEqual(r['parse_status'], 'проверить')
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'm.txt'; file.write_text('unknown broken song title', encoding='utf-8')
            prepare(file, Path(temp) / 'out')
            records = read_playlist(Path(temp) / 'out' / '00_all_unique.csv', 'auto')
            self.assertTrue(records[0].parse_uncertain)

    def test_windows_utf8_bom(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'b.txt'
            file.write_text('\ufeffПошлая Молли — Любимая песня\n', encoding='utf-8')
            prepare(file, Path(temp) / 'out')
            csv_data = (Path(temp) / 'out' / '00_all_unique.csv').read_text(encoding='utf-8-sig')
            self.assertIn('Пошлая Молли', csv_data)


class FakeArtist:
    def __init__(self, name): self.name = name


class FakeTrack:
    def __init__(self, artist='Nirvana', title='Smells Like Teen Spirit'):
        self.artists = [FakeArtist(artist)]
        self.title = title


class FakeShort:
    def __init__(self, track=None): self.track = track
    def fetch_track(self): return self.track


class YandexTests(unittest.TestCase):
    def test_full_track_metadata(self):
        rows, problems = resolve_rows([FakeShort(FakeTrack())], 'likes', delay=0)
        self.assertEqual(len(rows), 1)
        self.assertFalse(problems)
        self.assertEqual(rows[0]['artist'], 'Nirvana')

    def test_unavailable_track_report(self):
        with tempfile.TemporaryDirectory() as temp:
            path, errors = export_rows([FakeShort(FakeTrack()), FakeShort(None)], 'test', temp, delay=0)
            self.assertTrue(path.exists())
            self.assertEqual(len(errors), 1)
            self.assertTrue(path.with_suffix('.errors.csv').exists())


class MatchingTests(unittest.TestCase):
    def test_correct_match(self):
        s = Entry(1, 'Nirvana — Smells Like Teen Spirit', (Spec('Smells Like Teen Spirit','Nirvana'),))
        candidates = score_results(s, [{'videoId':'123', 'title':'Smells Like Teen Spirit',
                       'artists':[{'name':'Nirvana'}], 'resultType':'song'}])
        self.assertEqual(verdict(candidates, accept=94), 'MATCHED')

    def test_unrequested_remix_not_accepted(self):
        s = Entry(1, 'Nirvana — Smells Like Teen Spirit', (Spec('Smells Like Teen Spirit','Nirvana'),))
        scored = score_results(s, [{'videoId':'123', 'title':'Smells Like Teen Spirit (Remix)',
                       'artists':[{'name':'Nirvana'}], 'resultType':'song'}])
        self.assertLess(scored[0]['score'], 94)


if __name__ == '__main__': unittest.main()
