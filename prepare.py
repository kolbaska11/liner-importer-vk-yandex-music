"""Convert VK browser TXT / generic Artist — Title TXT to importer-ready CSV.

No built-in music lists, account IDs, or fixed number of playlists.
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
FIELDS = ['source_line', 'artist', 'title', 'raw', 'parse_status', 'source_section']
SEP = re.compile(r'^={8,}\s*$')
COUNT = re.compile(r'^(?:Количество треков|Треков|Tracks)\s*:\s*\d+', re.I)
META = re.compile(r'^(?:https?://|VK playlist ID\s*:|Плейлист ID\s*:|ОШИБКА\s*:)', re.I)


def slug(value: str) -> str:
    value = unicodedata.normalize('NFKC', value)
    value = re.sub(r'[^\w\-.]+', '_', value, flags=re.UNICODE).strip('._')
    return (value or 'playlist')[:65]


def entry(line: str, number: int, section: str) -> dict:
    raw = line.strip()
    m = re.split(r'\s+[—–]\s+|\s+-\s+', raw, maxsplit=1)
    if len(m) == 2 and all(x.strip() for x in m):
        artist, title = (x.strip() for x in m)
        status = 'точно'
    else:
        artist, title, status = '', raw, 'проверить'
    return dict(source_line=number, artist=artist, title=title, raw=raw,
                parse_status=status, source_section=section)


def parse_sections(text: str) -> dict[str, list[dict]]:
    lines = text.lstrip('\ufeff').splitlines()
    sections: dict[str, list[dict]] = {}
    section = 'Весь список'
    marker = False
    for idx, original in enumerate(lines, 1):
        line = original.strip()
        if not line:
            continue
        if SEP.fullmatch(line):
            marker = True
            continue
        if marker:
            marker = False
            # First delimiter opens a header. The SECOND delimiter closes it;
            # the next line is a SONG (Artist — Title), not a new section.
            looks_like_song = bool(re.search(r'\s+[—–-]\s+', line))
            if not looks_like_song and not COUNT.match(line) and not META.match(line):
                section = re.sub(r'^\d+\.\s+', '', line).strip()
                sections.setdefault(section, [])
                continue
        if COUNT.match(line) or META.match(line):
            continue
        # A leading metadata line before the first section does not contain a song.
        sections.setdefault(section, []).append(entry(line, idx, section))
    return {name: rows for name, rows in sections.items() if rows}


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def prepare(path: str | Path, output_dir: str | Path = DATA):
    source = Path(path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    content = source.read_text(encoding='utf-8-sig')
    sections = parse_sections(content)
    if not sections:
        raise ValueError('Не найдено ни одной песни. Проверь TXT.')
    # Prefer the VK deduplicated section if available, so we do not combine it with
    # the six source playlists and accidentally multiply entries.
    unique = next((rows for key, rows in sections.items() if 'УНИКАЛЬНЫЙ СПИСОК' in key.upper()), None)
    source_rows = unique if unique is not None else [r for rows in sections.values() for r in rows]
    all_rows, seen = [], set()
    for r in source_rows:
        identity = (r['artist'].casefold().strip(), r['title'].casefold().strip())
        if identity not in seen:
            seen.add(identity)
            all_rows.append(r)
    output_files = {'00_all_unique.csv': 'Все треки (без повторов)'}
    write_csv(output / '00_all_unique.csv', all_rows)
    for index, (name, rows) in enumerate(sections.items(), 1):
        filename = f'{index:02d}_{slug(name)}.csv'
        write_csv(output / filename, rows)
        output_files[filename] = name
    uncertain = [r for r in all_rows if r['parse_status'] == 'проверить']
    write_csv(output / '00_review_parse.csv', uncertain)
    stats = {'source': str(source.name), 'sections': {k: len(v) for k, v in sections.items()},
             'unique_entries': len(all_rows), 'uncertain': len(uncertain),
             'playlists': output_files}
    (output / 'manifest.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    return stats


def main(argv=None):
    p = argparse.ArgumentParser(description='Подготовка экспортированного TXT к импорту в YouTube Music')
    p.add_argument('file', help='TXT из VK либо строки вида Исполнитель — Название')
    p.add_argument('--output-dir', default=str(DATA))
    args = p.parse_args(argv)
    result = prepare(args.file, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('Подготовлено. Импортируй data/00_all_unique.csv или отдельный CSV.')

if __name__ == '__main__':
    main()
