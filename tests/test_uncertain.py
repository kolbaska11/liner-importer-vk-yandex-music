"""Tests for the separate user-curated review playlist(s)."""
import argparse
import csv
import json
import tempfile
import unittest
from pathlib import Path
from database import Store
from app import uncertain_upload
from matcher import Entry, Spec

class FakeYT:
    def __init__(self):
        self.created=[]
        self.plists={}
        self.calls=0
        self.fail_after_write=False
        self.fail_before_write=False
        self.force_bad_read=False

    def create_playlist(self, name, description, privacy_status='PRIVATE'):
        pid=f'PL{len(self.plists)+1:04}'
        self.plists[pid]=[]
        self.created.append((name, privacy_status))
        return pid

    def get_playlist(self, pid, limit=None):
        if self.force_bad_read:
            raise KeyError("Unable to find 'contents' using path")
        return {'tracks':[{'videoId':v} for v in self.plists[pid]],
                'trackCount':len(self.plists[pid])}

    def add_playlist_items(self, pid, videoIds, duplicates=False):
        self.calls+=1
        if self.fail_before_write:
            self.fail_before_write=False
            raise ConnectionError('fail before actual write')
        for vid in videoIds:
            if vid not in self.plists[pid]:
                self.plists[pid].append(vid)
        if self.fail_after_write:
            self.fail_after_write=False
            raise ConnectionError('response lost')
        return 'STATUS_SUCCEEDED'

def args(**extra):
    params=dict(name='Любимые песенки',max_per_playlist=900,batch_size=20,
                delay=0,yes=True,dry_run=False)
    params.update(extra)
    return argparse.Namespace(**params)

class UncertainTests(unittest.TestCase):
    def setupstore(self, location, statuses):
        root=Path(location)
        source=root/'songs.txt'
        source.write_text('\n'.join('Example Artist — Track '+str(i) for i in range(len(statuses))), encoding='utf-8')
        entries=[Entry(i+1,f'Example Artist — Track {i}',(Spec(f'Track {i}','Example Artist'),)) for i in range(len(statuses))]
        store=Store(source,entries,root)
        for i, status in enumerate(statuses):
            candidate={'videoId':f'A{i:010}', 'title':f'Track {i}', 'artists':['Example Artist'], 'score':49}
            store.set_track(i,status,candidate,alternatives=[candidate])
        store.set_meta('playlist_ids',{'1':'OLD1'})
        store.set_meta('liner_playlist_ids',{'1':'CONFIRMED1','2':'CONFIRMED2'})
        return store

    def test_separate_playlist_and_manual_deletion_not_restored(self):
        with tempfile.TemporaryDirectory() as td:
            store=self.setupstore(td,['UPLOADED','MATCHED','REVIEW','MISSING','REVIEW','SKIPPED','PENDING'])
            fake=FakeYT()
            try:
                uncertain_upload(args(),store,fake)
                self.assertEqual(len(fake.plists),1)
                self.assertEqual(len(fake.plists['PL0001']),3)
                self.assertIn('На проверку',fake.created[0][0])
                self.assertEqual(fake.created[0][1], 'UNLISTED')
                self.assertEqual([r['idx'] for r in store.uncertain_rows()],[2,3,4])
                self.assertEqual(store.get_meta('playlist_ids'),{'1':'OLD1'})
                self.assertEqual(store.get_meta('liner_playlist_ids'),{'1':'CONFIRMED1','2':'CONFIRMED2'})
                before=fake.calls
                fake.plists['PL0001'].pop(1)  # user prunes wrong track
                uncertain_upload(args(),store,fake)
                self.assertEqual(fake.calls,before)
                self.assertEqual(len(fake.plists['PL0001']),2)
            finally: store.close()

    def test_unknown_fail_after_write_is_reconciled(self):
        with tempfile.TemporaryDirectory() as td:
            store=self.setupstore(td,['REVIEW','REVIEW'])
            fake=FakeYT()
            fake.fail_after_write=True
            try:
                with self.assertRaisesRegex(RuntimeError,'UNKNOWN'):
                    uncertain_upload(args(batch_size=2),store,fake)
                self.assertEqual([r['state'] for r in store.uncertain_rows()],['ADDED','ADDED'])
                before=fake.calls
                uncertain_upload(args(batch_size=2),store,fake)
                self.assertEqual(fake.calls,before)
            finally: store.close()

    def test_unknown_fail_before_write_not_blindly_retried(self):
        with tempfile.TemporaryDirectory() as td:
            store=self.setupstore(td,['REVIEW','REVIEW'])
            fake=FakeYT()
            fake.fail_before_write=True
            try:
                with self.assertRaisesRegex(RuntimeError,'UNKNOWN'):
                    uncertain_upload(args(batch_size=2),store,fake)
                self.assertEqual([r['state'] for r in store.uncertain_rows()],['UNKNOWN','UNKNOWN'])
                before=fake.calls
                uncertain_upload(args(batch_size=2),store,fake)
                self.assertEqual(fake.calls,before)
            finally: store.close()

    def test_split_and_limit_900(self):
        with tempfile.TemporaryDirectory() as td:
            store=self.setupstore(td,['REVIEW']*901)
            fake=FakeYT()
            try:
                uncertain_upload(args(batch_size=50),store,fake)
                self.assertEqual([len(fake.plists[p]) for p in ('PL0001','PL0002')],[900,1])
                self.assertEqual(len(fake.created),2)
                self.assertEqual(len(store.uncertain_rows()),901)
            finally: store.close()

    def test_skip_duplicate_with_accepted_and_unfound(self):
        with tempfile.TemporaryDirectory() as td:
            store=self.setupstore(td,['MATCHED','REVIEW','MISSING','REVIEW','REVIEW'])
            # Duplicate the matched ID and among uncertain ID.
            shared=json.dumps({'videoId':'A0000000000', 'title':'same'})
            store.con.execute('UPDATE tracks SET candidate=? WHERE idx=1',(shared,))
            store.con.execute('UPDATE tracks SET candidate=NULL, alternatives=? WHERE idx=2',('[]',))
            shared_uncertain=json.dumps({'videoId':'A0000000003','title':'same'})
            store.con.execute('UPDATE tracks SET candidate=?, alternatives=? WHERE idx=4',(shared_uncertain,'[]'))
            store.con.commit()
            fake=FakeYT()
            try:
                uncertain_upload(args(),store,fake)
                self.assertEqual([r['idx'] for r in store.uncertain_rows()], [3])
                report=store.uncertain_report(Path(td))
                with report.open(encoding='utf-8-sig',newline='') as f:
                    rows=list(csv.DictReader(f))
                self.assertEqual(len(rows),4)
                self.assertEqual(sum(x['in_review_playlist']=='да' for x in rows),1)
            finally: store.close()

    def test_dry_run_zero_state_and_no_clients(self):
        with tempfile.TemporaryDirectory() as td:
            store=self.setupstore(td,['REVIEW']*3)
            try:
                uncertain_upload(args(dry_run=True),store)
                self.assertEqual(store.uncertain_rows(),[])
                self.assertIsNone(store.get_meta('uncertain_limit'))
            finally: store.close()

if __name__=='__main__': unittest.main()
