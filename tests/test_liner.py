"""Regression tests for Liner splitting without altering legacy playlists."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path

from app import liner_upload
from database import Store
from matcher import Entry, Spec


class YoutubeFake:
    def __init__(self):
        self.playlists = {}
        self.created = 0
        self.calls = 0
        self.fail_once = False
        self.plist_names = []

    def create_playlist(self, name, description, privacy_status="PRIVATE"):
        self.created += 1
        ident = f"YT{self.created:03}"
        self.plist_names.append((name, privacy_status))
        self.playlists[ident] = []
        return ident

    def get_playlist(self, pid, limit=None):
        return {"tracks": [{"videoId": vid} for vid in self.playlists[pid]],
                "trackCount": len(self.playlists[pid])}

    def add_playlist_items(self, pid, videoIds, duplicates=False):
        self.calls += 1
        self.playlists[pid].extend(v for v in videoIds if v not in self.playlists[pid])
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("Response lost after server saved playlist")
        return "STATUS_SUCCEEDED"


class LinerTests(unittest.TestCase):
    def fixture(self, directory, n=1303):
        source = Path(directory) / "songs.txt"
        source.write_text("\n".join(f"Example Artist — Track {i}" for i in range(n)), encoding="utf8")
        entries = [Entry(i+1, f"Example Artist — Track {i}", (Spec(f"Track {i}", "Example Artist"),)) for i in range(n)]
        store = Store(source, entries, Path(directory))
        rows = []
        for i in range(n):
            status = "UPLOADED" if i < 400 else "MATCHED"
            rows.append((status, json.dumps({"videoId": f"V{i:010}", "title": f"Track {i}"}), 1, i))
        store.con.executemany("UPDATE tracks SET status=?, candidate=?, part=? WHERE idx=?", rows)
        store.con.commit()
        store.set_meta("playlist_ids", {"1": "ORIGINAL_OLD"})
        store.set_meta("playlist_title", "Original Playlist")
        store.set_meta("per_playlist", 4500)
        args = argparse.Namespace(max_per_playlist=900, name="Любимые песенки", yes=True, dry_run=False, batch_size=50, delay=0)
        return store, args

    def test_1303_splits_900_403_old_unchanged_and_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            store, args = self.fixture(temp)
            fake = YoutubeFake()
            try:
                liner_upload(args, store, fake)
                self.assertEqual(fake.created, 2)
                self.assertEqual([len(fake.playlists[k]) for k in ('YT001', 'YT002')], [900,403])
                self.assertEqual([x[1] for x in fake.plist_names], ['UNLISTED','UNLISTED'])
                self.assertEqual(sum(r['uploaded'] for r in store.liner_rows()), 1303)
                self.assertEqual(store.get_meta('playlist_ids'), {'1':'ORIGINAL_OLD'})
                self.assertEqual(store.get_meta('per_playlist'), 4500)
                self.assertEqual(len([r for r in store.rows() if r['part']==1]), 1303)
                self.assertEqual(len(store.rows(('UPLOADED',))), 400)
                calls = fake.calls
                liner_upload(args, store, fake)
                self.assertEqual(fake.created, 2)
                self.assertEqual(fake.calls, calls)
            finally:
                store.close()

    def test_resumes_after_ambiguous_write_without_repeating(self):
        with tempfile.TemporaryDirectory() as temp:
            store, args = self.fixture(temp, 3)
            args.max_per_playlist = 2
            fake = YoutubeFake()
            fake.fail_once = True
            try:
                with self.assertRaisesRegex(RuntimeError, 'остановлена'):
                    liner_upload(args, store, fake)
                self.assertEqual(fake.created,1)
                self.assertEqual(len(fake.playlists['YT001']),2)
                liner_upload(args, store, fake)
                self.assertEqual(fake.created,2)
                self.assertEqual(len(fake.playlists['YT001']),2)
                self.assertEqual(len(fake.playlists['YT002']),1)
                self.assertEqual(fake.calls,2)
            finally:
                store.close()

    def test_dry_run_never_changes_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store, args = self.fixture(temp)
            try:
                args.dry_run = True
                liner_upload(args, store)
                self.assertEqual(store.liner_rows(), [])
                self.assertIsNone(store.get_meta('liner_max_per_playlist'))
            finally:
                store.close()

    def test_three_parts_refused_before_creating_anything(self):
        with tempfile.TemporaryDirectory() as temp:
            store, args = self.fixture(temp, 1801)
            fake = YoutubeFake()
            try:
                with self.assertRaisesRegex(ValueError, 'нужны 2'):
                    liner_upload(args, store, fake)
                self.assertEqual(fake.created, 0)
                self.assertEqual(store.liner_rows(), [])
            finally:
                store.close()

    def test_known_imported_track_not_resent_if_remote_inconsistent(self):
        with tempfile.TemporaryDirectory() as temp:
            store, args = self.fixture(temp, 3)
            args.max_per_playlist=2
            fake = YoutubeFake()
            try:
                liner_upload(args, store, fake)
                fake.playlists['YT001'] = []  # example of a deceptive API view
                previous_calls = fake.calls
                liner_upload(args, store, fake)
                self.assertEqual(fake.calls, previous_calls)
                self.assertEqual(sum(r['uploaded'] for r in store.liner_rows()), 3)
            finally:
                store.close()


if __name__=='__main__':
    unittest.main()
