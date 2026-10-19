#!/usr/bin/env python3
"""Export Yandex Music likes and playlists as CSV metadata for the local importer.

Music is NOT downloaded. OAuth tokens are never printed or transmitted elsewhere.
Uses unofficial yandex-music library; API changes may require updates.
"""
from __future__ import annotations
import argparse
import csv
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from prepare import FIELDS, DATA, slug, write_csv
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

TOKEN_PATH = ROOT / 'auth' / 'yandex_token.txt'


def get_token():
    token = os.getenv('YANDEX_MUSIC_TOKEN', '').strip()
    if not token and TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text(encoding='utf-8').strip()
    return token


def authorize():
    from yandex_music import Client
    client = Client()
    if not hasattr(client, 'device_auth'):
        raise RuntimeError('Для Device Flow требуется yandex-music 3.0+. '
                           'Обнови пакет: python -m pip install -U yandex-music')
    def on_code(code):
        print('Открой в браузере:', code.verification_url)
        print('Код подтверждения:', code.user_code)
        print('После подтверждения программа продолжит работу автоматически.')
    token = client.device_auth(on_code=on_code)
    secret = token.access_token
    if not secret:
        raise RuntimeError('Яндекс не вернул access_token')
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(secret, encoding='utf-8')
    try: TOKEN_PATH.chmod(0o600)
    except OSError: pass
    print('Успешно! Токен сохранён только локально в auth/yandex_token.txt')
    print('Никому не отправляй содержимое этого файла, в GitHub его не загружай.')


def client_from_token():
    from yandex_music import Client
    token = get_token()
    if not token:
        raise RuntimeError('Нет токена Яндекс Музыки. Запусти: python exporters/yandex_music.py --auth')
    return Client(token).init()


def full_track(short):
    # TrackShort: .track may be present, otherwise .fetch_track().
    track = getattr(short, 'track', None)
    if track is not None and getattr(track, 'title', None):
        return track
    if getattr(short, 'title', None) and getattr(short, 'artists', None):
        return short
    if hasattr(short, 'fetch_track'):
        return short.fetch_track()
    return None


def resolve_rows(shorts, section, delay=0.08):
    """Return records plus unresolved items, preserving input order."""
    records, errors = [], []
    for ix, short in enumerate(shorts, 1):
        try:
            track = full_track(short)
            if track is None: raise ValueError('Нет метаданных (track == None)')
            title = str(getattr(track, 'title', '') or '').strip()
            artists = getattr(track, 'artists', None) or []
            artist = ', '.join(str(getattr(a, 'name', '') or '').strip() for a in artists)
            artist = ', '.join(x for x in artist.split(', ') if x)
            if not title or not artist:
                raise ValueError('Название/исполнитель отсутствует')
            records.append(dict(source_line=ix, artist=artist, title=title,
                                raw=f'{artist} — {title}', parse_status='точно',
                                source_section=section))
        except Exception as exc:
            ident = str(getattr(short, 'id', '') or getattr(short, 'track_id', '') or 'unknown')
            errors.append(dict(index=ix, id=ident, reason=f'{type(exc).__name__}: {str(exc)[:100]}'))
        if delay: time.sleep(delay)
    return records, errors


def export_rows(shorts, label, out_dir=DATA, delay=0.08):
    rows, problems = resolve_rows(shorts, label, delay)
    filename = slug(label) + '.csv'
    path = Path(out_dir) / filename
    write_csv(path, rows)
    print(f'{label}: выгружено {len(rows)} записей -> {path}')
    if problems:
        report = path.with_suffix('.errors.csv')
        with report.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['index','id','reason'])
            writer.writeheader()
            writer.writerows(problems)
        print(f'  Не удалось получить {len(problems)} записей; отчёт: {report}')
    return path, problems


def playlist_tracks(playlist):
    # Some list endpoints return playlists without all tracks loaded.
    if hasattr(playlist, 'fetch_tracks'):
        fetched = playlist.fetch_tracks()
        if fetched is not None: return fetched
    return getattr(playlist, 'tracks', []) or []


def main(argv=None):
    parser = argparse.ArgumentParser(description='Экспорт МЕТАДАННЫХ из Яндекс Музыки (без скачивания аудио)')
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--auth', action='store_true', help='Войти через OAuth Device Flow')
    actions.add_argument('--likes', action='store_true', help='Выгрузить «Мне нравится»')
    actions.add_argument('--all-playlists', action='store_true', help='Выгрузить свои плейлисты')
    actions.add_argument('--list', action='store_true', help='Показать свои плейлисты')
    actions.add_argument('--playlist-id', help='Выгрузить плейлист по ID (kind)')
    parser.add_argument('--output-dir', default=str(DATA))
    parser.add_argument('--delay', type=float, default=0.08)
    args = parser.parse_args(argv)
    if args.auth:
        authorize(); return
    client = client_from_token()
    if args.likes:
        likes = client.users_likes_tracks()
        export_rows(getattr(likes, 'tracks', likes) or [], 'Yandex_Мне_нравится', args.output_dir, args.delay)
        return
    lists = client.users_playlists_list() or []
    if args.list:
        for p in lists:
            print(f'{getattr(p, "kind", "?")}: {getattr(p, "title", "Без имени")}')
        return
    targets = lists if args.all_playlists else [p for p in lists if str(getattr(p, 'kind', '')) == str(args.playlist_id)]
    if not targets:
        raise RuntimeError('Подходящих плейлистов не найдено. Используй --list.')
    total_errors = 0
    for i, p in enumerate(targets, 1):
        name = f'Yandex_{i:02d}_{getattr(p, "title", "Плейлист")}'
        _, errors = export_rows(playlist_tracks(p), name, args.output_dir, args.delay)
        total_errors += len(errors)
    if total_errors:
        print('ВНИМАНИЕ: некоторые треки не выгружены, проверь файлы .errors.csv.')


if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: print('\nОстановлено. Уже сохранённые CSV остаются на диске.'); sys.exit(130)
    except ImportError as exc:
        print('Установи: python -m pip install -r requirements.txt')
        sys.exit(1)
    except Exception as exc:
        print('Ошибка экспорта Яндекс Музыки:', type(exc).__name__, str(exc)[:300]); sys.exit(1)
