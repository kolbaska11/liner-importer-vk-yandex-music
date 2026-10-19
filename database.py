"""Durable progress/checkpoints. One SQLite database for each immutable source TXT."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from matcher import Entry


class Store:
    def __init__(self, source: Path, entries: list[Entry], root: Path):
        identity = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
        prefix = re.sub(r"[^\w-]+", "_", source.stem, flags=re.U)[:48] or "playlist"
        self.slug = f"{prefix}-{identity}"
        self.path = root / "work" / self.slug / "progress.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("""CREATE TABLE IF NOT EXISTS tracks (
            idx INTEGER PRIMARY KEY, line INTEGER NOT NULL, raw TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            candidate TEXT, alternatives TEXT NOT NULL DEFAULT '[]',
            score REAL, part INTEGER, note TEXT NOT NULL DEFAULT ''
        )""")
        self.con.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)")
        # Separate, non-destructive export for Liner. Do not reuse tracks.part/status:
        # an old YouTube Music import may already occupy one large playlist.
        self.con.execute("""CREATE TABLE IF NOT EXISTS liner_items (
            idx INTEGER PRIMARY KEY REFERENCES tracks(idx),
            part INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            uploaded INTEGER NOT NULL DEFAULT 0
        )""")
        # Independent, frozen queue for human review in YouTube Music.
        # It deliberately does not change the confirmed Liner export, statuses,
        # or the original bulk-upload playlist.
        self.con.execute("""CREATE TABLE IF NOT EXISTS uncertain_items (
            idx INTEGER PRIMARY KEY REFERENCES tracks(idx),
            part INTEGER NOT NULL,
            video_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (state IN ('PENDING', 'ADDED', 'UNKNOWN'))
        )""")
        self.con.execute("""CREATE TABLE IF NOT EXISTS search_cache (
            query TEXT NOT NULL, filter TEXT NOT NULL, results TEXT NOT NULL,
            PRIMARY KEY (query, filter)
        )""")
        if not self.con.execute("SELECT 1 FROM tracks LIMIT 1").fetchone():
            self.con.executemany("INSERT INTO tracks(idx, line, raw) VALUES (?, ?, ?)",
                                 [(index, entry.line, entry.raw) for index, entry in enumerate(entries)])
        else:
            old = self.con.execute("SELECT idx, line, raw FROM tracks ORDER BY idx").fetchall()
            if len(old) != len(entries) or any((row['line'], row['raw']) != (item.line, item.raw)
                                               for row, item in zip(old, entries)):
                raise ValueError("Источник отличается от сохранённого состояния. Не меняйте TXT в процессе импорта.")
        self.con.commit()

    def close(self):
        self.con.close()

    def rows(self, statuses: tuple[str, ...] | None = None):
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            return self.con.execute(f"SELECT * FROM tracks WHERE status IN ({placeholders}) ORDER BY idx", statuses).fetchall()
        return self.con.execute("SELECT * FROM tracks ORDER BY idx").fetchall()

    def set_track(self, idx: int, status: str, candidate: dict | None = None,
                  alternatives: list[dict] | None = None, note: str = ""):
        self.con.execute("""UPDATE tracks SET status=?, candidate=?, alternatives=?, score=?, note=? WHERE idx=?""",
                         (status, json.dumps(candidate, ensure_ascii=False) if candidate else None,
                          json.dumps(alternatives or [], ensure_ascii=False),
                          candidate.get("score") if candidate else None, note, idx))
        self.con.commit()

    def set_status(self, idx: int, status: str, note: str = ""):
        self.con.execute("UPDATE tracks SET status=?, note=? WHERE idx=?", (status, note, idx))
        self.con.commit()

    def assign_part(self, idx: int, part: int):
        self.con.execute("UPDATE tracks SET part=? WHERE idx=?", (part, idx))
        self.con.commit()

    def get_meta(self, name: str, default: Any = None):
        item = self.con.execute("SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
        return json.loads(item[0]) if item else default

    def set_meta(self, name: str, value: Any):
        self.con.execute("INSERT OR REPLACE INTO metadata(name,value) VALUES (?,?)", (name, json.dumps(value, ensure_ascii=False)))
        self.con.commit()

    def liner_rows(self, part: int | None = None):
        if part is None:
            return self.con.execute("SELECT * FROM liner_items ORDER BY idx").fetchall()
        return self.con.execute("SELECT * FROM liner_items WHERE part=? ORDER BY idx", (part,)).fetchall()

    def liner_candidates(self) -> list[tuple[int, str]]:
        # Both MATCHED and UPLOADED represent approved tracks. Source order preserved.
        seen = set()
        selected = []
        for row in self.rows(("MATCHED", "UPLOADED")):
            if not row["candidate"]:
                continue
            try:
                video_id = json.loads(row["candidate"]).get("videoId")
            except (TypeError, ValueError):
                continue
            if video_id and video_id not in seen:
                selected.append((row["idx"], video_id))
                seen.add(video_id)
        return selected

    def liner_plan(self, max_per_playlist: int) -> list[sqlite3.Row]:
        """Freeze independent Liner plan once; never change prior YouTube imports."""
        previous = self.get_meta("liner_max_per_playlist")
        if previous is not None and previous != max_per_playlist:
            raise ValueError(
                f"План Liner уже создан с лимитом {previous}. "
                f"Повтори с --max-per-playlist {previous}, иначе разбиение изменится."
            )
        existing = self.liner_rows()
        if existing:
            return existing
        tracks = self.liner_candidates()
        if not tracks:
            raise ValueError("Нет подтверждённых песен в сохранённой базе. Сначала выполни поиск и проверку.")
        if not 1 <= max_per_playlist <= 1000:
            raise ValueError("Допустимый лимит: от 1 до 1000 песен.")
        self.con.executemany(
            "INSERT INTO liner_items(idx, part, video_id) VALUES (?, ?, ?)",
            [(idx, i // max_per_playlist + 1, video_id) for i, (idx, video_id) in enumerate(tracks)]
        )
        self.set_meta("liner_max_per_playlist", max_per_playlist)
        self.con.commit()
        return self.liner_rows()

    def liner_mark_uploaded(self, idx: int):
        self.con.execute("UPDATE liner_items SET uploaded=1 WHERE idx=?", (idx,))
        self.con.commit()

    def liner_mark_pending(self, idx: int):
        self.con.execute("UPDATE liner_items SET uploaded=0 WHERE idx=?", (idx,))
        self.con.commit()

    def uncertain_rows(self, part: int | None = None):
        if part is None:
            return self.con.execute("SELECT * FROM uncertain_items ORDER BY idx").fetchall()
        return self.con.execute("SELECT * FROM uncertain_items WHERE part=? ORDER BY idx", (part,)).fetchall()

    @staticmethod
    def candidate_video_id(row) -> str | None:
        # The search result may be empty even if there is a REVIEW/MISSING row.
        # Only IDs can be placed in a YouTube Music playlist.
        for field in ("candidate", "alternatives"):
            try:
                value = json.loads(row[field]) if row[field] else None
            except (ValueError, TypeError):
                continue
            values = value if isinstance(value, list) else [value]
            for candidate in values:
                if isinstance(candidate, dict):
                    video_id = candidate.get("videoId")
                    if isinstance(video_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                        return video_id
        return None

    def uncertain_candidates(self):
        """Best saved guess for each unreviewed track, in source order.

        Exclude guesses identical to confidently accepted tracks, repeats,
        deliberate SKIPPED tracks and unsolved PENDING rows.
        """
        confirmed = {
            vid for row in self.rows(("MATCHED", "UPLOADED"))
            if (vid := self.candidate_video_id(row))
        }
        used = set(confirmed)
        selected = []
        skipped = {"no_candidate": 0, "duplicate": 0, "pending": len(self.rows(("PENDING",)))}
        for row in self.rows(("REVIEW", "MISSING")):
            video_id = self.candidate_video_id(row)
            if not video_id:
                skipped["no_candidate"] += 1
            elif video_id in used:
                skipped["duplicate"] += 1
            else:
                selected.append((row["idx"], video_id))
                used.add(video_id)
        return selected, skipped

    def uncertain_plan(self, max_per_playlist: int):
        """Freeze the human-review batch. Never refill after manual deletion."""
        if not 1 <= max_per_playlist <= 1000:
            raise ValueError("Максимальный размер проверочного плейлиста: от 1 до 1000.")
        previous = self.get_meta("uncertain_limit")
        if previous is not None and previous != max_per_playlist:
            raise ValueError(f"План проверки уже создан с лимитом {previous}. Используй этот же лимит.")
        existing = self.uncertain_rows()
        if existing:
            return existing
        selected, _ = self.uncertain_candidates()
        if not selected:
            raise ValueError("Нет непроверенных песен с найденными кандидатами. Сначала запусти поиск.")
        self.con.executemany(
            "INSERT INTO uncertain_items(idx,part,video_id) VALUES (?,?,?)",
            [(idx, i // max_per_playlist + 1, vid) for i, (idx, vid) in enumerate(selected)]
        )
        self.set_meta("uncertain_limit", max_per_playlist)
        self.con.commit()
        return self.uncertain_rows()

    def uncertain_mark(self, idx: int, state: str):
        if state not in ("PENDING", "ADDED", "UNKNOWN"):
            raise ValueError("Недопустимое состояние проверочного импорта")
        self.con.execute("UPDATE uncertain_items SET state=? WHERE idx=?", (state, idx))
        self.con.commit()

    def uncertain_report(self, root: Path):
        dest = root / "reports" / self.slug
        dest.mkdir(parents=True, exist_ok=True)
        selected = {row["idx"]: row for row in self.uncertain_rows()}
        rows = []
        for row in self.rows(("REVIEW", "MISSING")):
            planned = selected.get(row["idx"])
            video_id = self.candidate_video_id(row)
            best = {}
            if row["candidate"]:
                try:
                    best = json.loads(row["candidate"])
                except (ValueError, TypeError):
                    pass
            rows.append({
                "source_line": row["line"], "source": row["raw"], "status": row["status"],
                "score": row["score"] if row["score"] is not None else "",
                "guess_title": best.get("title", "") if isinstance(best, dict) else "",
                "guess_artists": ", ".join(best.get("artists", [])) if isinstance(best, dict) else "",
                "video_url": f"https://music.youtube.com/watch?v={video_id}" if video_id else "",
                "in_review_playlist": "да" if planned else "нет",
                "review_part": planned["part"] if planned else "",
                "upload_state": planned["state"] if planned else "",
            })
        with (dest / "СОМНИТЕЛЬНЫЕ_КАНДИДАТЫ.csv").open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=["source_line", "source", "status", "score",
                "guess_title", "guess_artists", "video_url", "in_review_playlist", "review_part", "upload_state"])
            writer.writeheader()
            writer.writerows(rows)
        return dest / "СОМНИТЕЛЬНЫЕ_КАНДИДАТЫ.csv"

    def upgrade_search_engine(self, version: int) -> tuple[int, int]:
        """One-time correction of poisoned negative cache in older releases.

        UPLOADED, MATCHED, REVIEW, DUPLICATE and SKIPPED are left untouched.
        """
        previous = self.get_meta("search_engine_version", 0)
        if previous >= version:
            return 0, 0
        n_cache = self.con.execute("DELETE FROM search_cache WHERE TRIM(results) = '[]'").rowcount
        n_missing = self.con.execute("""UPDATE tracks SET status='PENDING', note='Повторный поиск после исправления поиска YouTube Music'
                                          WHERE status='MISSING'""").rowcount
        self.set_meta("search_engine_version", version)
        self.con.commit()
        return n_missing, n_cache

    def cache_get(self, query: str, kind: str) -> list[dict] | None:
        row = self.con.execute("SELECT results FROM search_cache WHERE query=? AND filter=?", (query, kind)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, query: str, kind: str, results: list[dict]):
        self.con.execute("INSERT OR REPLACE INTO search_cache(query,filter,results) VALUES (?,?,?)",
                         (query, kind, json.dumps(results, ensure_ascii=False)))
        self.con.commit()

    def report(self, root: Path):
        dest = root / "reports" / self.slug
        dest.mkdir(parents=True, exist_ok=True)
        data = []
        parsed = self.get_meta("parsed_lines", [])
        for row in self.rows():
            specs = parsed[row["idx"]].get("specs", []) if row["idx"] < len(parsed) else []
            declared = specs[0] if specs else {}
            uncertain = parsed[row["idx"]].get("parse_uncertain", False) if row["idx"] < len(parsed) else False
            best = json.loads(row["candidate"]) if row["candidate"] else {}
            alternatives = json.loads(row["alternatives"])
            data.append({
                "line": row["line"], "source": row["raw"],
                "source_artist": declared.get("artist", ""),
                "source_title": declared.get("title", ""),
                "parsing_uncertain": "да" if uncertain else "нет",
                "status": row["status"],
                "score": row["score"] if row["score"] is not None else "",
                "found_artist": ", ".join(best.get("artists", [])),
                "found_title": best.get("title", ""),
                "youtube_link": "https://music.youtube.com/watch?v=" + best["videoId"] if best.get("videoId") else "",
                "playlist_part": row["part"] or "", "note": row["note"],
                "alternatives": alternatives,
            })
        with (dest / "results.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[x for x in data[0] if x != "alternatives"] if data else ["line","source","status"])
            writer.writeheader()
            for item in data:
                writer.writerow({x: item.get(x, "") for x in writer.fieldnames})
        with (dest / "results.json").open("w", encoding="utf-8") as f:
            json.dump({"songs": data, "playlists": self.get_meta("playlist_ids", {})}, f, ensure_ascii=False, indent=2)
        with (dest / "need_review.txt").open("w", encoding="utf-8") as f:
            for item in data:
                if item["status"] in ("REVIEW", "MISSING"):
                    f.write(f'{item["line"]}: {item["source"]} [{item["status"]}]\n')
        return dest
