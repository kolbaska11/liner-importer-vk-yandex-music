#!/usr/bin/env python3
"""Import a large TXT playlist into YouTube Music with human-in-the-loop matching.

Usage:
    python main.py auth browser
    python main.py scan playlist.txt
    python main.py review playlist.txt
    python main.py upload playlist.txt --name "Моя музыка"
    python main.py status playlist.txt
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from uuid import uuid4
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from database import Store
from matcher import Entry, Spec, infer_artists, parse_entry, score_results, search_queries, verdict

ROOT = Path(__file__).resolve().parent
# Search results filtered by type can be empty in non-English ytmusicapi locales.
# This is strictly an API parser setting: Cyrillic song titles remain untouched.
SEARCH_ENGINE_VERSION = 2
SMOKE_TEST_QUERY = "Nirvana Smells Like Teen Spirit"
load_dotenv(ROOT / ".env")


def log(message: str) -> None:
    print(message, flush=True)


def read_playlist(path: Path, order: str) -> list[Entry]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        required = {"source_line", "artist", "title", "raw", "parse_status"}
        if not rows or not required <= set(rows[0]):
            raise ValueError("Неверный формат подготовленного CSV. Запусти prepare.py.")
        result = []
        for row in rows:
            title, artist = row["title"].strip(), row["artist"].strip()
            if not title:
                raise ValueError(f"Пустое название у строки {row['source_line']}")
            result.append(Entry(int(row["source_line"]), row["raw"].strip(),
                                (Spec(title, artist),), parse_uncertain=row["parse_status"] == "проверить"))
        log(f"Подготовлено песен: {len(result)}; неопределённых названий: {sum(x.parse_uncertain for x in result)}")
        return result
    content = path.read_bytes()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1251")
    lines = [(number, x.strip()) for number, x in enumerate(text.splitlines(), 1)
             if x.strip() and not x.lstrip().startswith("#")]
    hints_file = ROOT / "artists.txt"
    hints = [x.strip() for x in hints_file.read_text(encoding="utf-8-sig").splitlines()
             if x.strip() and not x.lstrip().startswith("#")] if hints_file.exists() else []
    artists = infer_artists([x for _, x in lines], hints)
    return [parse_entry(raw, number, artists, order) for number, raw in lines]


def storage(args: argparse.Namespace) -> tuple[Store, list[Entry]]:
    source = Path(args.file).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Файл не найден: {source}\nСоздайте playlist.txt на основе playlist_example.txt.")
    entries = read_playlist(source, args.order)
    if not entries:
        raise ValueError("В TXT нет композиций.")
    store = Store(source, entries, ROOT)
    # On resume we deliberately lock the parsing mode and artist hints: otherwise
    # the same source could be silently interpreted differently by a different run.
    parsed = [{"line": e.line, "raw": e.raw,
               "parse_uncertain": e.parse_uncertain,
               "specs": [{"title": s.title, "artist": s.artist, "duration_seconds": s.duration_seconds} for s in e.specs]}
              for e in entries]
    locked = store.get_meta("parsed_lines")
    if locked is None:
        store.set_meta("parsed_lines", parsed)
    elif locked != parsed:
        raise ValueError("Изменился режим --order или artists.txt после начала работы. "
                         "Верните прежние настройки или используйте новый TXT-файл с другим именем/содержимым.")
    return store, entries


def auth_location(require_auth: bool) -> tuple[str, Path | None]:
    """Resolve the exact credentials selected for *all* operations.

    Browser credentials are preferred when both browser.json and oauth.json exist,
    because the older menu could silently select a stale OAuth session.
    Override explicitly with YTM_AUTH_MODE=oauth if needed.
    """
    folder = ROOT / "auth"
    browser = folder / "browser.json"
    oauth = folder / "oauth.json"
    preference = os.getenv("YTM_AUTH_MODE", "auto").strip().lower()
    if preference not in ("auto", "browser", "oauth"):
        raise RuntimeError("YTM_AUTH_MODE должен быть auto, browser или oauth")
    if preference == "browser":
        if not browser.exists():
            raise RuntimeError("Выбран browser, но файл auth/browser.json отсутствует. Выполни пункт 2.")
        return "browser", browser
    if preference == "oauth":
        if not oauth.exists():
            raise RuntimeError("Выбран oauth, но файл auth/oauth.json отсутствует. Выполни пункт 11.")
        return "oauth", oauth
    if browser.exists():
        return "browser", browser
    if oauth.exists():
        return "oauth", oauth
    if require_auth:
        raise RuntimeError("Нет авторизации. Выполни пункт 2: подключение через браузер.")
    return "none", None


def browser_auth_warnings(path: Path) -> list[str]:
    """Only return high-level validation findings; NEVER print secret values."""
    try:
        headers = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(headers, dict):
            return ["Файл browser.json не содержит объект заголовков."]
    except (OSError, ValueError):
        return ["Файл browser.json не читается как JSON."]
    values = {str(k).lower(): str(v) for k, v in headers.items()}
    warnings = []
    if "sapisidhash" not in values.get("authorization", "").lower():
        warnings.append("Отсутствует браузерный Authorization (SAPISIDHASH). Повтори пункт 2 с POST browse.")
    if "__Secure-3PAPISID" not in values.get("cookie", ""):
        warnings.append("В Cookie нет __Secure-3PAPISID. Вероятно, скопирован запрос без входа в аккаунт.")
    if not values.get("x-goog-authuser", "").isdigit():
        warnings.append("Некорректный x-goog-authuser. Выбери запрос нужного Google-аккаунта.")
    return warnings


def client(require_auth: bool):
    try:
        from ytmusicapi import OAuthCredentials, YTMusic
    except ImportError as exc:
        raise RuntimeError("Установи зависимости через пункт 1 в меню.") from exc
    method, auth = auth_location(require_auth)
    if method == "browser":
        return YTMusic(str(auth), language="en")
    if method == "oauth":
        client_id = os.getenv("YTM_CLIENT_ID", "").strip()
        client_secret = os.getenv("YTM_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise RuntimeError("Для OAuth нужны YTM_CLIENT_ID и YTM_CLIENT_SECRET в .env")
        return YTMusic(str(auth), oauth_credentials=OAuthCredentials(client_id=client_id, client_secret=client_secret), language="en")
    return YTMusic(language="en")


def write_error(exc: Exception, operation: str) -> RuntimeError:
    """Error messages contain no credentials; say exactly which operation failed."""
    reason = str(exc)
    if "401" in reason or "You must be signed in" in reason or "Unauthorized" in reason:
        return RuntimeError(
            f"Операция '{operation}' отклонена YouTube Music (HTTP 401). "
            "Проверка чтения в пункте 3 не подтверждает доступ к записи. "
            "Запусти пункт 13 для проверки создания плейлиста. "
            "Если не проходит — заново скопируй POST browse в пункте 2; "
            "убедись, что вход выполнен в нужный Google/YouTube-канал. "
            "Уже найденные песни и база work сохраняются."
        )
    return RuntimeError(f"Ошибка операции '{operation}': {type(exc).__name__}: {reason[:350]}")


def auth_write_test(ytm=None, *, confirmed: bool = False) -> bool:
    """Opt-in creation/deletion of ONE temporary empty PRIVATE playlist.

    Never touches previously created/imported playlists. Returns success only
    when creation and cleanup both succeed.
    """
    if not confirmed:
        confirm = input("Будет создан и затем удалён ОДИН пустой приватный тестовый плейлист. Продолжить? [да/нет]: ").strip().lower()
        if confirm not in ("да", "д", "yes", "y"):
            log("Тест записи отменён. Аккаунт не изменён.")
            return False
    ytm = ytm or client(require_auth=True)
    name = "YTMusic импорт — временная проверка " + uuid4().hex[:8]
    ident = None
    try:
        try:
            ident = ytm.create_playlist(name, "Временный тест возможности записи; будет удалён", privacy_status="PRIVATE")
        except Exception as exc:
            raise write_error(exc, "создание приватного плейлиста") from exc
        if not isinstance(ident, str) or not ident or ident.startswith("STATUS_"):
            raise RuntimeError("YouTube Music не вернул ID созданного плейлиста. Проверь аккаунт перед повтором.")
        log("Создание плейлистов РАБОТАЕТ. Удаляю временный пустой плейлист…")
    finally:
        if isinstance(ident, str) and ident and not ident.startswith("STATUS_"):
            try:
                result = ytm.delete_playlist(ident)
                if result is not None and not successful(result):
                    raise RuntimeError("Сервис не подтвердил удаление")
            except Exception as exc:
                raise RuntimeError(
                    "Временный тестовый плейлист создан, но не удалось удалить автоматически. "
                    f"Удали его вручную: https://music.youtube.com/playlist?list={ident}. "
                    f"Причина: {type(exc).__name__}: {str(exc)[:160]}"
                ) from exc
    log("Тест записи ПРОЙДЕН: плейлист создан и удалён. Можно запускать пункт 7.")
    return True


def auth_command(args: argparse.Namespace) -> None:
    auth_dir = ROOT / "auth"
    auth_dir.mkdir(exist_ok=True)
    if args.method == "clipboard":
        from auth_clipboard import setup_from_clipboard
        log("Читаю запрос из буфера обмена. В CMD ничего вставлять не нужно.")
        setup_from_clipboard(auth_dir / "browser.json")
        log("Сохранено: auth/browser.json. Пункт 3 проверит выбранный аккаунт.")
    elif args.method == "browser":
        import ytmusicapi
        log("Вставляй заголовки только локально; Cookie и Authorization — секреты.")
        ytmusicapi.setup(filepath=str(auth_dir / "browser.json"))
        log("Сохранено: auth/browser.json")
    elif args.method == "oauth":
        import ytmusicapi
        client_id = os.getenv("YTM_CLIENT_ID", "").strip()
        client_secret = os.getenv("YTM_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise RuntimeError("Сначала настрой YTM_CLIENT_ID и YTM_CLIENT_SECRET в .env (см. README).")
        ytmusicapi.setup_oauth(client_id=client_id, client_secret=client_secret,
                               filepath=str(auth_dir / "oauth.json"), open_browser=True)
        log("Сохранено: auth/oauth.json. Для выбора OAuth вместо браузера пропиши YTM_AUTH_MODE=oauth в .env")
    elif args.method in ("check", "write-test"):
        method, path = auth_location(require_auth=True)
        log(f"Способ авторизации: {method} (источник: auth/{path.name})")
        if method == "browser":
            warnings = browser_auth_warnings(path)
            for warning in warnings:
                log("ПРЕДУПРЕЖДЕНИЕ: " + warning)
            if warnings and args.method == "write-test":
                raise RuntimeError("Заголовки в browser.json неполные. Прежде чем тестировать запись, повтори пункт 2.")
        ytm = client(require_auth=True)
        if args.method == "check":
            ytm.get_library_playlists(limit=1)
            log("✓ Чтение библиотеки работает.")
            try:
                info = ytm.get_account_info()
                if isinstance(info, dict):
                    name = info.get("accountName") or info.get("channelHandle")
                    if name:
                        log(f"Аккаунт YouTube Music: {name}")
                    else:
                        log("Имя аккаунта не указано API; проверь нужный канал в браузере.")
            except Exception as exc:
                log(f"⚠ Информацию об аккаунте получить не удалось: {type(exc).__name__}. Это НЕ проверка записи.")
            search_probe(ytm)
            log("Чтение и поиск проверены. Создание плейлистов проверяется ОТДЕЛЬНО в пункте 13.")
        else:
            auth_write_test(ytm)


def diagnose() -> None:
    """Non-mutating diagnostic for the actual user playlist, no secrets printed."""
    ytm = client(require_auth=False)
    prefer_unfiltered = search_probe(ytm)
    search_kind = None if prefer_unfiltered else "songs"
    example = "Nirvana Smells Like Teen Spirit"
    log(f"Проверяю пример поиска: {example}")
    results = ytm.search(example, filter=search_kind, limit=20)
    if not isinstance(results, list):
        raise RuntimeError("Не удалось разобрать ответ поиска первого трека")
    log(f"Получено {len(results)} результатов. Показываю до трёх музыкальных записей:")
    choices = [x for x in results if isinstance(x,dict) and x.get("videoId") and x.get("resultType") in ("song","video")]
    for row in choices[:3]:
        artists = ', '.join((a.get('name','') if isinstance(a,dict) else str(a)) for a in row.get('artists',[]))
        log(f"  {row.get('title','')} — {artists} [{row.get('resultType')}]")
    if not choices:
        log("Этот конкретный трек не появился в выдаче. Это не обязательно проблема программы: "
            "возможно, песни нет на YouTube Music или название отличается.")


def search_probe(ytm, *, verbose: bool = True) -> bool:
    """Confirm the catalogue works before declaring real songs missing.

    Returns True when filtered results are broken but unfiltered results work.
    Never puts credentials, cookies or raw HTTP data in diagnostic output.
    """
    try:
        filtered = ytm.search(SMOKE_TEST_QUERY, filter="songs", limit=20)
        if not isinstance(filtered, list):
            raise RuntimeError("неожиданный формат ответа songs")
        # The fallback is verified even when filtered results are present. If the
        # filter breaks with the user's local YouTube response, don't lose tracks.
        unfiltered = ytm.search(SMOKE_TEST_QUERY, limit=20)
        if not isinstance(unfiltered, list):
            raise RuntimeError("неожиданный формат ответа без фильтра")
    except Exception as exc:
        raise RuntimeError(
            "Диагностика YouTube Music не прошла. Проверь доступ к music.youtube.com "
            "в Python, VPN/прокси и авторизацию. Никакие песни не помечены как ненайденные. "
            f"Техническая причина: {type(exc).__name__}: {str(exc)[:240]}"
        ) from exc
    def music_count(data):
        return sum(1 for x in data if isinstance(x,dict) and x.get("videoId")
                   and x.get("resultType") in ("song", "video"))
    nf, nu = music_count(filtered), music_count(unfiltered)
    if verbose:
        log(f"Диагностика каталога: отфильтрованных результатов={nf}, обычных={nu}.")
    if nf == 0 and nu == 0:
        raise RuntimeError(
            "YouTube Music не находит даже Nirvana — Smells Like Teen Spirit. "
            "Это проблема поиска/API/сети, а не пропущенные песни. "
            "Импорт остановлен до устранения проблемы. "
            "Проверь, доступен ли music.youtube.com без блокировок, и попробуй обновить ytmusicapi."
        )
    if nf == 0 and nu > 0:
        if verbose:
            log("⚠ Фильтр songs возвращает пустые результаты. Переключаюсь на поиск без фильтров.")
        return True
    return False


def resilient_search(ytm, store: Store, query: str, kind: str,
                     delay: float, prefer_unfiltered: bool = False) -> list[dict]:
    """Search with a robust unfiltered fallback. Never cache empty results."""
    if prefer_unfiltered:
        kind = "all"
    cached = store.cache_get(query, kind)
    if cached:  # Empty entries from the previous release are never trusted.
        return cached
    for attempt in range(4):
        try:
            result = ytm.search(query, filter=None if kind == "all" else kind, limit=20)
            if not isinstance(result, list):
                raise RuntimeError("YouTube Music вернул ответ, который не является списком")
            if result:
                store.cache_put(query, kind, result)
            if delay:
                time.sleep(delay)
            if not result and kind != "all":
                # Sometimes the library's locale-specific parser silently
                # produces [], even though the music exists in unfiltered search.
                return resilient_search(ytm, store, query, "all", delay)
            return result
        except Exception as exc:
            if attempt == 3:
                raise RuntimeError(
                    f"Поиск {query!r} ({kind}) завершился ошибкой. "
                    "Никакие песни не отмечены как пропущенные. "
                    f"Причина: {type(exc).__name__}: {str(exc)[:280]}"
                ) from exc
            wait = 2 ** (attempt + 1)
            log(f"  ⚠ Ошибка обращения к YouTube Music ({type(exc).__name__}). Повтор через {wait} с...")
            time.sleep(wait)
    raise RuntimeError("Неожиданный выход из поискового цикла")


def scan(args: argparse.Namespace, store: Store, entries: list[Entry], ytm=None) -> None:
    ytm = ytm or client(require_auth=False)
    statuses = ("PENDING", "REVIEW", "MISSING") if args.retry else ("PENDING",)
    # Check the service before changing any existing progress or cached search.
    prefer_unfiltered = search_probe(ytm)
    upgraded_missing, upgraded_cache = store.upgrade_search_engine(SEARCH_ENGINE_VERSION)
    if upgraded_missing or upgraded_cache:
        log(f"Исправление старой базы: на повторный поиск возвращено {upgraded_missing} песен; "
            f"удалено {upgraded_cache} пустых ответов из кэша.")
    pending = store.rows(statuses)
    if args.limit is not None:
        pending = pending[:args.limit]
    log(f"Начинаю поиск: {len(pending)} строк. Порог автопринятия: {args.accept:.0f}/100.")
    try:
        for num, row in enumerate(pending, 1):
            entry = entries[row["idx"]]
            unique: dict[str, dict] = {}
            attempts = set()
            raw_results = 0
            for query, kind in search_queries(entry, args.max_queries):
                effective_kind = "all" if prefer_unfiltered else kind
                if (query, effective_kind) in attempts:
                    continue
                attempts.add((query, effective_kind))
                batch = resilient_search(ytm, store, query, kind, args.delay, prefer_unfiltered)
                raw_results += len(batch)
                for candidate in batch:
                    vid = candidate.get("videoId")
                    if vid and vid not in unique:
                        unique[vid] = candidate
                results = score_results(entry, list(unique.values()))
                # Fast path only for very confident titles + known artists, otherwise
                # gather alternatives first so we do not rush into the wrong remix.
                if (results and len(entry.specs) > 0 and any(x.artist for x in entry.specs)
                    and results[0]["score"] >= 98 and verdict(results, accept=98) == "MATCHED"):
                    break
            ranked = score_results(entry, list(unique.values()))
            classification = verdict(ranked, accept=args.accept, review=args.review)
            if entry.parse_uncertain and classification == "MATCHED":
                classification = "REVIEW"  # Never auto-approve a guessed song name.
            best = ranked[0] if ranked else None
            store.set_track(row["idx"], classification, best, ranked[:8],
                            "Подтверждено алгоритмом" if classification == "MATCHED" else "Требуется проверка" if classification == "REVIEW" else "Нет подходящих результатов")
            marker = {"MATCHED":"OK", "REVIEW":"?", "MISSING":"X"}[classification]
            preview = (f'{best["title"]} — {", ".join(best["artists"])} ({best["score"]:.0f})') if best else "не найдено"
            if not best and raw_results:
                log(f"  ⚠ API вернул {raw_results} объектов, но ни один не содержит подходящий videoId/тип song или video.")
            log(f"{num}/{len(pending)} {marker} {entry.raw[:57]} → {preview[:74]}")
    finally:
        dest = store.report(ROOT)
        log(f"Промежуточные отчёты: {dest}")


def review(args: argparse.Namespace, store: Store, entries: list[Entry]) -> None:
    remaining = store.rows(("REVIEW", "MISSING"))
    if args.limit is not None:
        remaining = remaining[:args.limit]
    log(f"Неоднозначных/ненайденных строк: {len(remaining)}")
    try:
        for i, row in enumerate(remaining, 1):
            print(f"\n[{i}/{len(remaining)}] строка {row['line']}: {row['raw']} ({row['status']})")
            alternatives = json.loads(row["alternatives"])
            for number, candidate in enumerate(alternatives, 1):
                print(f" {number}) [{candidate['score']:.0f}] {candidate['title']} — "
                      f"{', '.join(candidate['artists'])} [{candidate['resultType']}] "
                      f"https://music.youtube.com/watch?v={candidate['videoId']}")
            print("Номер — подтвердить, m ИД — вручную указать videoId, "
                  "s — пропустить, Enter — позже, q — закончить")
            choice = input("Выбор: ").strip()
            if choice.lower() == "q":
                break
            if choice.lower() == "s":
                store.set_status(row["idx"], "SKIPPED", "Пропущено пользователем")
                continue
            if choice.lower().startswith("m "):
                value = choice[2:].strip()
                match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", value)
                vid = match.group(1) if match else value
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                    log("Некорректный videoId: нужна 11-символьная строка или ссылка YouTube.")
                    continue
                manual = {"videoId":vid, "title":"Вручную выбранный трек", "artists":[],
                          "score":100.0, "duration":"", "resultType":"video"}
                store.set_track(row["idx"], "MATCHED", manual, alternatives, "Указан вручную")
                continue
            if choice.isdecimal() and 1 <= int(choice) <= len(alternatives):
                selected = alternatives[int(choice)-1]
                store.set_track(row["idx"], "MATCHED", selected, alternatives, "Подтверждено пользователем")
                continue
            if choice:
                log("Неверная команда. Эта строка оставлена для проверки.")
    finally:
        log(f"Отчёты: {store.report(ROOT)}")


def successful(response: Any) -> bool:
    if isinstance(response, str):
        return response in ("STATUS_SUCCEEDED", "STATUS_SUCCESS", "SUCCEEDED")
    return isinstance(response, dict) and response.get("status") in ("STATUS_SUCCEEDED", "STATUS_SUCCESS", "SUCCEEDED")


def playlist_contents(ytm, playlist_id: str) -> tuple[set[str], bool]:
    """IDs and whether we have a complete playlist view.

    Some versions of ytmusicapi return at most 100 tracks despite limit=None.
    It is unsafe to mark omitted songs missing when such a partial view arrives.
    """
    playlist = ytm.get_playlist(playlist_id, limit=None)
    if not isinstance(playlist, dict) or not isinstance(playlist.get("tracks"), list):
        raise RuntimeError("Не удалось получить содержимое плейлиста для проверки дублей/сбоев.")
    tracks = playlist["tracks"]
    found = {x["videoId"] for x in tracks if isinstance(x, dict) and x.get("videoId")}
    count = playlist.get("trackCount")
    if isinstance(count, str) and re.fullmatch(r"[\d, .]+", count):
        count = int(re.sub(r"\D", "", count))
    if isinstance(count, bool) or not isinstance(count, int):
        count = None
    complete = (count <= len(tracks)) if count is not None else len(tracks) < 100
    return found, complete


def playlist_confirmed_empty_in_library(ytm, playlist_id: str) -> bool:
    """Trust an *owned* playlist with an explicitly zero track count only.

    Do not interpret an unreadable playlist as empty. This protects reruns
    from adding duplicate songs when the API response changes.
    """
    try:
        playlists = ytm.get_library_playlists(limit=None)
    except Exception:
        return False
    if not isinstance(playlists, list):
        return False
    for playlist in playlists:
        if not isinstance(playlist, dict) or playlist.get("playlistId") != playlist_id:
            continue
        if playlist.get("owned") is not True:
            continue
        count = playlist.get("count")
        if isinstance(count, (int, float)) and not isinstance(count, bool):
            return count == 0
        return isinstance(count, str) and bool(re.fullmatch(r"\s*0\s*(?:songs?|tracks?|пес(?:ен|ни)|трек(?:ов|а)?)?\s*", count, re.I))
    return False


def remote_for_upload(ytm, playlist_id: str, part: int, *, just_created: bool, has_uploaded: bool) -> tuple[set[str], bool]:
    """Read remote IDs or explicitly establish that the playlist is empty.

    For a newly created playlist, the create_playlist() response itself
    establishes that its initial contents are empty. For an older playlist,
    require a live zero-count proof or a separate interactive confirmation.
    """
    if just_created:
        log("Новый плейлист только что создан — пока он пуст. Первую проверку содержимого пропускаю.")
        return set(), True
    try:
        return playlist_contents(ytm, playlist_id)
    except KeyError as exc:
        # This known library failure usually means that the playlist page
        # has no renderer. It does NOT prove that the playlist is empty.
        if "contents" not in str(exc) and "Unable to find" not in str(exc):
            raise
        if has_uploaded:
            raise RuntimeError(
                "YouTube Music не отдаёт содержимое ранее заполненного плейлиста. "
                "Загрузка остановлена, чтобы не повторить песни. "
                "Все ранее подтверждённые добавления сохранены в work. "
                f"Проверь список вручную: https://music.youtube.com/playlist?list={playlist_id}. "
                "Попробуй обновить ytmusicapi (пункт 1) и перезапустить пункт 7."
            ) from exc
        if playlist_confirmed_empty_in_library(ytm, playlist_id):
            log("Список треков API недоступен, но библиотека подтверждает: плейлист пустой (0).")
            return set(), True
        log("Не удалось прочитать ранее созданный плейлист; считать его пустым автоматически ОПАСНО.")
        log(f"Открой ссылку: https://music.youtube.com/playlist?list={playlist_id}")
        log("Если в нём действительно НОЛЬ треков, можешь разрешить первую загрузку.")
        answer = input('Введи слово ПУСТОЙ для подтверждения (иначе импорт остановится): ').strip().casefold()
        if answer != "пустой":
            raise RuntimeError("Импорт остановлен без добавления музыки. Плейлист и прогресс сохранены.") from exc
        log("Подтверждено вручную: импорт начинаю в пустой плейлист.")
        return set(), True


def upload(args: argparse.Namespace, store: Store, ytm=None) -> None:
    chosen = store.rows(("MATCHED", "UPLOADED"))
    if not chosen:
        log("Нет подтверждённых треков. Сначала выполни scan и review.")
        return
    if not (1 <= args.per_playlist <= 4800):
        raise ValueError("Размер части должен быть от 1 до 4800 треков.")
    if not args.yes and not args.dry_run:
        log(f"Импортировать {len(chosen)} подтверждённых записей в YouTube Music?")
        answer = input("Будут созданы/изменены плейлисты в ТВОЁМ аккаунте. Продолжить? [да/нет]: ").strip().lower()
        if answer not in ("да", "д", "yes", "y"):
            log("Загрузка отменена. Название не зафиксировано и данные не изменены.")
            return
    old_limit = store.get_meta("per_playlist")
    if old_limit is not None and old_limit != args.per_playlist:
        raise ValueError(f"Части уже сформированы по {old_limit} песен. "
                         "Продолжай с тем же --per-playlist, чтобы не нарушить распределение.")
    saved_title = store.get_meta("playlist_title")
    title = saved_title or args.name
    if saved_title and args.name != saved_title:
        log(f"Продолжаю ранее созданный проект с названием: {saved_title!r} (новое название проигнорировано).")
    if not saved_title and not args.dry_run:
        store.set_meta("playlist_title", title)
    if old_limit is None and not args.dry_run:
        store.set_meta("per_playlist", args.per_playlist)
    # Deduplicate by videoId; avoid API rejecting whole batches when an identical
    # video is present twice in a playlist.
    seen = set()
    for row in chosen:
        chosen_obj = json.loads(row["candidate"]) if row["candidate"] else {}
        vid = chosen_obj.get("videoId")
        if vid and vid in seen and row["status"] == "MATCHED":
            if not args.dry_run:
                store.set_status(row["idx"], "DUPLICATE", "Этот videoId уже встречался в исходном списке")
        elif vid:
            seen.add(vid)
    chosen = [r for r in store.rows(("MATCHED", "UPLOADED")) if r["candidate"]]
    if args.dry_run:
        log(f"Предварительный просмотр: уникальных подтверждённых треков {len(chosen)}; "
            f"плейлистов до {(len(chosen) + args.per_playlist - 1) // args.per_playlist}.")
        return
    # Persist playlist part per track. Stable assignments survive restarts/reviews.
    counts: dict[int, int] = {}
    for row in store.rows():
        if row["part"] and row["status"] in ("MATCHED", "UPLOADED"):
            counts[row["part"]] = counts.get(row["part"], 0) + 1
    for row in chosen:
        if row["part"] is not None:
            continue
        part = 1
        while counts.get(part, 0) >= args.per_playlist:
            part += 1
        store.assign_part(row["idx"], part)
        counts[part] = counts.get(part, 0) + 1
    if len(counts) > 1:
        log("Большой список разделяется на части: " + ", ".join(f"часть {p}: {n}" for p,n in sorted(counts.items())))
    ytm = ytm or client(require_auth=True)
    playlist_ids = store.get_meta("playlist_ids", {})
    for part in sorted(counts):
        part_key = str(part)
        just_created = False
        if part_key not in playlist_ids:
            new_name = f"{title} · Часть {part:02d}"
            log(f"Создаю приватный плейлист: {new_name}" if args.privacy == "PRIVATE" else f"Создаю плейлист: {new_name}")
            try:
                created = ytm.create_playlist(new_name, "Импортировано из личного текстового списка", privacy_status=args.privacy)
            except Exception as exc:
                raise write_error(exc, "создание плейлиста") from exc
            if not isinstance(created, str) or not created or created.startswith("STATUS_"):
                raise RuntimeError(f"Не удалось создать плейлист. Ответ: {str(created)[:250]}")
            playlist_ids[part_key] = created
            store.set_meta("playlist_ids", playlist_ids)
            just_created = True
            log(f"Создан: https://music.youtube.com/playlist?list={created}")
        playlist_id = playlist_ids[part_key]
        these = [row for row in store.rows(("MATCHED", "UPLOADED")) if row["part"] == part]
        log(f"Сверяю список с существующим плейлистом (часть {part})...")
        try:
            remote_ids, remote_complete = remote_for_upload(
                ytm, playlist_id, part, just_created=just_created,
                has_uploaded=any(row["status"] == "UPLOADED" for row in these),
            )
        except Exception as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise write_error(exc, "чтение ранее созданного плейлиста") from exc
        if not remote_complete:
            log("⚠ YouTube Music вернул только ЧАСТЬ треков плейлиста (ограничение API). "
                "Ранее подтверждённые добавления сохраняю по локальной базе, без повторной отправки.")
        pending = []
        for row in these:
            obj = json.loads(row["candidate"])
            vid = obj["videoId"]
            if vid in remote_ids:
                if row["status"] != "UPLOADED":
                    store.set_status(row["idx"], "UPLOADED", "Подтверждено наличием на YouTube Music")
            else:
                if row["status"] == "UPLOADED" and not remote_complete:
                    # An incomplete server list cannot disprove a successful
                    # acknowledged write from an earlier batch.
                    remote_ids.add(vid)
                    continue
                if row["status"] == "UPLOADED":
                    store.set_status(row["idx"], "MATCHED", "Отсутствовал при повторной сверке")
                pending.append((row["idx"], vid))
        log(f"Часть {part}: уже на сервере {len(remote_ids)}, осталось добавить {len(pending)}")
        batch_size = args.batch_size
        try:
            for offset in range(0, len(pending), batch_size):
                batch = pending[offset:offset+batch_size]
                vids = [vid for _,vid in batch]
                try:
                    result = ytm.add_playlist_items(playlist_id, videoIds=vids, duplicates=False)
                    if not successful(result):
                        raise RuntimeError(f"Неожиданный ответ: {str(result)[:300]}")
                except Exception as exc:
                    log("ВНИМАНИЕ: запрос мог выполниться на сервере до обрыва соединения.")
                    # Read the remote state; do not retry blindly.
                    try:
                        reconciled, _ = playlist_contents(ytm, playlist_id)
                        for idx, vid in batch:
                            if vid in reconciled:
                                store.set_status(idx, "UPLOADED", "Подтверждено после ошибки отправки")
                    except Exception as reconciliation_error:
                        log(f"Не удалось проверить удалённое состояние: {reconciliation_error}")
                    raise RuntimeError("Партия остановлена для безопасной сверки. "
                                       "Запусти upload ещё раз — он проверит сервер и продолжит. "
                                       f"{write_error(exc, 'добавление песен')}") from exc
                for idx, vid in batch:
                    store.set_status(idx, "UPLOADED", "Успешно добавлено")
                    remote_ids.add(vid)
                log(f"  ✓ Добавлено {min(offset+len(batch), len(pending))}/{len(pending)}")
                if args.delay:
                    time.sleep(args.delay)
            # Final check is intentional: a successful write acknowledgement does
            # not guarantee the entire requested list is now visible on the server.
            try:
                verified, verification_complete = playlist_contents(ytm, playlist_id)
            except KeyError as exc:
                # The write response may be valid even if the library parser
                # cannot read the playlist. Preserve successful acknowledgements.
                if "contents" not in str(exc) and "Unable to find" not in str(exc):
                    raise
                log("  ⚠ YouTube Music принял запросы, но API не дал проверить содержимое. "
                    "Статусы UPLOADED сохранены; в браузере проверь количество песен. "
                    "Не запускай повторную загрузку вслепую.")
            else:
                absent = 0
                if not verification_complete:
                    log("  ⚠ Список от API неполный (возможно ограничен 100 треками). "
                        "Не буду ошибочно помечать остальные песни как отсутствующие.")
                for row in store.rows(("UPLOADED",)) if verification_complete else []:
                    if row["part"] == part:
                        song = json.loads(row["candidate"])
                        if song["videoId"] not in verified:
                            store.set_status(row["idx"], "REVIEW", "Не обнаружен после загрузки. Проверь доступность видео и выбери альтернативу")
                            absent += 1
                if verification_complete:
                    log(f"  Проверено на сервере: {len(verified)} уникальных видео. "
                        + (f"Отсутствуют {absent}; отправлены на ручную проверку." if absent else "Отсутствующих среди добавленных нет."))
                else:
                    log(f"  API показал {len(verified)} треков (неполный список). "
                        "Статусы успешно добавленных оставлены без изменения.")
        finally:
            log(f"Отчёты: {store.report(ROOT)}")
    log("Готово. Плейлисты:")
    for p, identifier in sorted(playlist_ids.items(), key=lambda item: int(item[0])):
        log(f"  Часть {p}: https://music.youtube.com/playlist?list={identifier}")



def liner_upload(args: argparse.Namespace, store: Store, ytm=None) -> None:
    """Export approved YouTube Music IDs to NEW <=limit-sized playlists for Liner.

    This intentionally does NOT modify an existing YouTube Music import,
    tracks.part, tracks.status, or original playlist_ids. Tracks can be copied
    to two new playlists even if already uploaded to one large original.
    """
    limit = args.max_per_playlist
    if not 1 <= limit <= 1000:
        raise ValueError("Лимит одного плейлиста для Liner: от 1 до 1000.")
    if args.dry_run:
        planned = store.liner_rows()
        if planned:
            amount = len(planned)
            limit = store.get_meta("liner_max_per_playlist", limit)
        else:
            amount = len(store.liner_candidates())
        n_parts = (amount + limit - 1) // limit
        log(f"Для Liner подтверждено уникальных песен: {amount}. "
            f"При лимите {limit} получится {n_parts} частей.")
        if n_parts:
            log("Распределение: " + ", ".join(
                f"часть {i+1}: {min(limit, amount-i*limit)}"
                for i in range(n_parts)
            ))
        return

    previous_limit = store.get_meta("liner_max_per_playlist")
    if previous_limit is not None and previous_limit != limit:
        # Menu always remembers prior selection; enforce same on CLI as well.
        raise ValueError(f"Режим Liner уже запускался с лимитом {previous_limit}. "
                         f"Запусти с --max-per-playlist {previous_limit}.")
    saved_title = store.get_meta("liner_title")
    title = saved_title or args.name
    if saved_title and args.name != saved_title:
        log(f"Сохранено имя режима Liner: {saved_title!r}; новое имя не применяется.")
    selected = store.liner_rows()
    if not selected:
        preview = store.liner_candidates()
        if not preview:
            log("Подтверждённых песен пока нет. Запусти пункты 5 и 6.")
            return
        amount = len(preview)
    else:
        amount = len(selected)
    parts = (amount + limit - 1) // limit
    log(f"Режим Liner: {amount} уникальных песен, {parts} новых плейлистов, "
        f"не более {limit} песен в каждом.")
    if parts != 2:
        raise ValueError(
            f"Подтверждено {amount} треков: при лимите {limit} получится {parts} частей, а нужны 2. "
            "Скорректируй лимит (не более 1000) или число песен. Ничего не изменено."
        )
    log("Существующий плейлист YouTube Music и результаты старого импорта не изменятся.")
    if not args.yes:
        confirm = input("Создать/продолжить ОТДЕЛЬНЫЕ плейлисты для Liner? [да/нет]: ").strip().casefold()
        if confirm not in ("да", "д", "yes", "y"):
            log("Отменено. Ничего не изменено.")
            return
    selected = store.liner_plan(limit)
    if not saved_title:
        store.set_meta("liner_title", title)
    ytm = ytm or client(require_auth=True)
    playlist_ids = store.get_meta("liner_playlist_ids", {})
    for part in sorted(set(row["part"] for row in selected)):
        rows = store.liner_rows(part)
        key = str(part)
        just_created = False
        if key not in playlist_ids:
            display = f"{title} — Liner {part:02d}"
            log(f"Создаю отдельный приватный плейлист {display!r}")
            try:
                pid = ytm.create_playlist(display,
                    f"Перенос из ВК для Liner, часть {part}", privacy_status="UNLISTED")
            except Exception as exc:
                raise write_error(exc, f"создание плейлиста Liner {part}") from exc
            if not isinstance(pid, str) or not pid or pid.startswith("STATUS_"):
                raise RuntimeError("YouTube Music не вернул ID созданного плейлиста Liner.")
            playlist_ids[key] = pid
            store.set_meta("liner_playlist_ids", playlist_ids)
            just_created = True
            log(f"Создан: https://music.youtube.com/playlist?list={pid}")
        pid = playlist_ids[key]
        # For previously created playlists, protect against unsafe duplicate writes.
        remote_ids, remote_complete = remote_for_upload(
            ytm, pid, part, just_created=just_created,
            has_uploaded=any(row["uploaded"] for row in rows),
        )
        pending = []
        for row in rows:
            video_id = row["video_id"]
            if video_id in remote_ids:
                if not row["uploaded"]:
                    store.liner_mark_uploaded(row["idx"])
            elif row["uploaded"] and not remote_complete:
                # Preserve acknowledged adds when server returns a truncated list.
                continue
            elif row["uploaded"]:
                # Never automatically re-send an acknowledged video. An API
                # parser may under-report even if it declares the list complete.
                log(f"⚠ Ранее добавленный трек {video_id} не виден через API. "
                    "Не повторяю его автоматически, проверь список вручную.")
                continue
            else:
                pending.append((row["idx"], video_id))
        log(f"Liner {part}: {len(rows)} в плане; осталось добавить {len(pending)}")
        try:
            for offset in range(0, len(pending), args.batch_size):
                batch = pending[offset:offset + args.batch_size]
                vids = [video_id for _, video_id in batch]
                try:
                    result = ytm.add_playlist_items(pid, videoIds=vids, duplicates=False)
                    if not successful(result):
                        raise RuntimeError(f"Неожиданный ответ YouTube Music: {str(result)[:150]}")
                except Exception as exc:
                    # Network may fail after write; reconcile once, then stop.
                    try:
                        seen_remote, _ = playlist_contents(ytm, pid)
                        for idx, vid in batch:
                            if vid in seen_remote:
                                store.liner_mark_uploaded(idx)
                    except Exception:
                        pass
                    raise RuntimeError(
                        "Загрузка Liner остановлена после неясного ответа YouTube Music. "
                        "Не повторяю партию автоматически. "
                        f"Проверь https://music.youtube.com/playlist?list={pid}, "
                        "затем запусти пункт 14 ещё раз. "
                        f"Детали: {type(exc).__name__}: {str(exc)[:160]}"
                    ) from exc
                for idx, _ in batch:
                    store.liner_mark_uploaded(idx)
                log(f"  Liner {part}: добавлено {min(offset+len(batch),len(pending))}/{len(pending)}")
                if args.delay:
                    time.sleep(args.delay)
        finally:
            log("Прогресс Liner сохранён в базе work. Старый импорт не затронут.")
        # Avoid a misleading claim when get_playlist() is broken; local ack is saved.
        try:
            remote_now, complete = playlist_contents(ytm, pid)
            if complete:
                missing = [r for r in store.liner_rows(part) if r["video_id"] not in remote_now]
                if missing:
                    log(f"⚠ API не показывает {len(missing)} песен в части {part}; "
                        "проверь на сайте YouTube Music перед импортом в Liner.")
            else:
                log("⚠ YouTube Music отдал неполный список; проверь число песен в браузере.")
        except Exception:
            log("⚠ Проверка содержимого через API недоступна; проверь число песен в браузере.")
    log("Готово. Для переноса в Liner используй ТОЛЬКО эти отдельные плейлисты:")
    for part, pid in sorted(playlist_ids.items(), key=lambda x:int(x[0])):
        log(f"  {title} — Liner {int(part):02d}: https://music.youtube.com/playlist?list={pid}")
    log("Плейлисты созданы с доступом ПО ССЫЛКЕ (UNLISTED), чтобы их мог прочитать Liner.")
    log("Старый плейлист оставлен без изменений.")


def uncertain_upload(args: argparse.Namespace, store: Store, ytm=None) -> None:
    """One or more review-only playlists of best *unconfirmed* candidates.

    This is deliberately separate from confirmed Liner playlists and the old
    project. Users may remove wrong songs directly in YouTube Music. The plan
    is frozen, and acknowledged additions are NEVER automatically refilled.
    """
    limit = args.max_per_playlist
    if not 1 <= limit <= 1000:
        raise ValueError("Для Liner размер проверочного плейлиста должен быть от 1 до 1000.")
    existing = store.uncertain_rows()
    if existing:
        previous = store.get_meta("uncertain_limit", limit)
        if previous != limit:
            raise ValueError(f"Проверочный плейлист ранее создан с лимитом {previous}; используй его же.")
        total = len(existing)
        skipped = None
    else:
        selected, skipped = store.uncertain_candidates()
        total = len(selected)
    if not total:
        log("Нет сомнительных песен, для которых найден хоть один видео-кандидат. ")
        log("Сначала выполни поиск (пункт 5). Песни без videoId нельзя добавить в плейлист.")
        return
    parts = (total + limit - 1) // limit
    log(f"Проверочный экспорт: {total} разных видео, {parts} отдельный(ых) плейлист(ов), "
        f"не более {limit} в каждом.")
    if skipped is not None:
        log(f"Без найденного видео: {skipped['no_candidate']}; уже есть среди "
            f"подтверждённых/повторов: {skipped['duplicate']}; ещё не искали: {skipped['pending']}.")
    log("Включаю кандидатов со статусом REVIEW и MISSING, если у них есть videoId, "
        "даже при низком рейтинге. Они НЕ считаются подтверждёнными.")
    log("Старые плейлисты и результаты поиска не меняются.")
    if args.dry_run:
        log("Только предварительный расчёт: аккаунт и база не изменены.")
        return
    if not args.yes:
        confirm = input("Создать/продолжить плейлист с СОМНИТЕЛЬНЫМИ версиями для удаления вручную? [да/нет]: ").strip().casefold()
        if confirm not in ("да", "д", "yes", "y"):
            log("Отменено. Ничего не изменено.")
            return
    rows = store.uncertain_plan(limit)
    title = store.get_meta("uncertain_title") or args.name
    if store.get_meta("uncertain_title") is None:
        store.set_meta("uncertain_title", title)
    elif title != args.name:
        log(f"Для проверочного плейлиста уже сохранено название {title!r}; не изменяю его.")
    report_path = store.uncertain_report(ROOT)
    log(f"Отчёт со всеми сомнительными кандидатами: {report_path}")
    ytm = ytm or client(require_auth=True)
    identifiers = store.get_meta("uncertain_playlist_ids", {})
    for part in sorted({row["part"] for row in rows}):
        group = store.uncertain_rows(part)
        key = str(part)
        just_created = False
        if key not in identifiers:
            name = f"{title} — На проверку {part:02d}" if parts > 1 else f"{title} — На проверку"
            try:
                pid = ytm.create_playlist(name,
                    "Сомнительные совпадения из ВК: удали неправильные записи вручную; "
                    "скрипт не восстановит удалённые треки.", privacy_status="UNLISTED")
            except Exception as exc:
                raise write_error(exc, f"создание проверочного плейлиста {part}") from exc
            if not isinstance(pid, str) or not pid or pid.startswith("STATUS_"):
                raise RuntimeError("YouTube Music не вернул ID проверочного плейлиста. Повторную попытку "
                                   "делай только после проверки библиотеки, чтобы не создать дубли.")
            identifiers[key] = pid
            store.set_meta("uncertain_playlist_ids", identifiers)
            just_created = True
            log(f"Создан плейлист: https://music.youtube.com/playlist?list={pid}")
        pid = identifiers[key]
        if all(row["state"] == "ADDED" for row in group):
            log(f"На проверку {part}: отправка уже завершена. Ранее удалённые вручную песни НЕ восстанавливаю.")
            continue
        remote_ids, _ = remote_for_upload(ytm, pid, part, just_created=just_created,
                                          has_uploaded=any(row["state"] != "PENDING" for row in group))
        pending = []
        unresolved = 0
        for row in group:
            if row["video_id"] in remote_ids and row["state"] != "ADDED":
                store.uncertain_mark(row["idx"], "ADDED")
            elif row["state"] == "UNKNOWN":
                # A network timeout may have happened AFTER YouTube received
                # the batch. Never re-send ambiguous writes without inspection.
                unresolved += 1
            elif row["state"] == "PENDING":
                pending.append((row["idx"], row["video_id"]))
            # ADDED is immutable from this code path: manual pruning respected.
        log(f"На проверку {part}: в плане {len(group)}, к отправке {len(pending)}, "
            f"требуют ручной сверки после сбоя {unresolved}.")
        try:
            for offset in range(0, len(pending), args.batch_size):
                batch = pending[offset:offset + args.batch_size]
                ids = [video_id for _, video_id in batch]
                try:
                    result = ytm.add_playlist_items(pid, videoIds=ids, duplicates=False)
                    if not successful(result):
                        raise RuntimeError(f"Неожиданный ответ YouTube Music: {str(result)[:120]}")
                except Exception as exc:
                    # A failed network response does not prove the write failed.
                    # Freeze unresolved rows as UNKNOWN to avoid duplicates.
                    for idx, _ in batch:
                        store.uncertain_mark(idx, "UNKNOWN")
                    try:
                        seen, _ = playlist_contents(ytm, pid)
                        for idx, vid in batch:
                            if vid in seen:
                                store.uncertain_mark(idx, "ADDED")
                    except Exception:
                        pass
                    raise RuntimeError(
                        "Отправка сомнительных треков остановлена. Эта партия помечена UNKNOWN и "
                        "НЕ будет отправлена повторно вслепую. Проверь плейлист: "
                        f"https://music.youtube.com/playlist?list={pid}. "
                        f"Причина: {type(exc).__name__}: {str(exc)[:160]}") from exc
                for idx, _ in batch:
                    store.uncertain_mark(idx, "ADDED")
                log(f"  На проверку {part}: добавлено {min(offset+len(batch), len(pending))}/{len(pending)}")
                if args.delay:
                    time.sleep(args.delay)
        finally:
            store.uncertain_report(ROOT)
    log("Проверочные плейлисты (можешь удалить из них все неправильные версии):")
    for part, pid in sorted(identifiers.items(), key=lambda x: int(x[0])):
        log(f"  На проверку {int(part):02d}: https://music.youtube.com/playlist?list={pid}")
    log("Уже отправленные и вручную удалённые песни не возвращаются при повторном запуске.")
    log("Песни, вообще не найденные поиском, перечислены в отчёте CSV и в YouTube не добавляются.")


def show_status(store: Store):
    counts = {}
    for r in store.rows():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log("Состояние: " + ", ".join(f"{status}={count}" for status,count in sorted(counts.items())))
    log(f"База прогресса: {store.path}")
    log(f"Отчёты: {store.report(ROOT)}")
    for part, ident in store.get_meta("playlist_ids", {}).items():
        log(f"Часть {part}: https://music.youtube.com/playlist?list={ident}")
    uncertain_rows = store.uncertain_rows()
    if uncertain_rows:
        log(f"На проверку: {sum(r['state']=='ADDED' for r in uncertain_rows)}/{len(uncertain_rows)} "
            f"отправлено; неопределённых после ошибки: {sum(r['state']=='UNKNOWN' for r in uncertain_rows)}")
        for part, ident in store.get_meta("uncertain_playlist_ids", {}).items():
            log(f"На проверку {part}: https://music.youtube.com/playlist?list={ident}")
    liner_rows = store.liner_rows()
    if liner_rows:
        log(f"Liner: {sum(bool(r['uploaded']) for r in liner_rows)}/{len(liner_rows)} "
            "песен подтверждено в отдельных плейлистах")
        for part, ident in store.get_meta("liner_playlist_ids", {}).items():
            log(f"Liner {part}: https://music.youtube.com/playlist?list={ident}")


def parser_build() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Точный импорт TXT-плейлистов в YouTube Music (без Premium)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("diagnose", help="Проверка поиска и первого трека без записи в базу")
    auth = sub.add_parser("auth", help="Авторизация Google / проверка подключения")
    auth.add_argument("method", choices=("clipboard", "browser", "oauth", "check", "write-test"))
    for name in ("scan", "review", "upload", "status", "liner", "uncertain"):
        cmd = sub.add_parser(name)
        cmd.add_argument("file", help="Путь к TXT, например playlist.txt")
        cmd.add_argument("--order", choices=("auto", "title-artist", "artist-title"), default="auto",
                         help="Как разбирать строки с разделителем —")
        if name == "scan":
            cmd.add_argument("--retry", action="store_true", help="Повторить REVIEW/MISSING, используя кэш")
            cmd.add_argument("--max-queries", type=int, default=5, choices=range(1, 11), metavar="1..10")
            cmd.add_argument("--accept", type=float, default=94.0)
            cmd.add_argument("--review", type=float, default=65.0)
            cmd.add_argument("--delay", type=float, default=0.7, help="Пауза между реальными запросами поиска")
            cmd.add_argument("--limit", type=int, help="Ограничить число строк, удобно для тестов")
        if name == "uncertain":
            cmd.add_argument("--name", default="Любимые песенки")
            cmd.add_argument("--max-per-playlist", type=int, default=900)
            cmd.add_argument("--batch-size", type=int, default=25, choices=range(1, 51))
            cmd.add_argument("--delay", type=float, default=0.7)
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--yes", action="store_true")
        if name == "review":
            cmd.add_argument("--limit", type=int)
        if name == "liner":
            cmd.add_argument("--name", default="Любимые песенки", help="Имя новых плейлистов для Liner")
            cmd.add_argument("--max-per-playlist", type=int, default=900,
                             help="Максимум 1000, по умолчанию 900 для запаса")
            cmd.add_argument("--batch-size", type=int, default=25, choices=range(1, 51))
            cmd.add_argument("--delay", type=float, default=0.7)
            cmd.add_argument("--dry-run", action="store_true", help="Только предварительный расчёт")
            cmd.add_argument("--yes", action="store_true", help="Не задавать вопрос-подтверждение")
        if name == "upload":
            cmd.add_argument("--name", default="Вся музыка из ВК", help="Название группы плейлистов")
            cmd.add_argument("--privacy", choices=("PRIVATE", "UNLISTED", "PUBLIC"), default="PRIVATE")
            cmd.add_argument("--per-playlist", type=int, default=4500, help="Не больше 4800")
            cmd.add_argument("--batch-size", type=int, choices=range(1, 51), default=25, metavar="1..50")
            cmd.add_argument("--delay", type=float, default=0.7)
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--yes", action="store_true", help="Подтвердить изменение плейлистов без вопроса")
    return parser


def main(argv=None):
    args = parser_build().parse_args(argv)
    if args.command == "auth":
        auth_command(args)
        return
    if args.command == "diagnose":
        diagnose()
        return
    store = None
    try:
        store, entries = storage(args)
        if args.command == "scan":
            scan(args, store, entries)
        elif args.command == "review":
            review(args, store, entries)
        elif args.command == "upload":
            upload(args, store)
        elif args.command == "liner":
            liner_upload(args, store)
        elif args.command == "uncertain":
            uncertain_upload(args, store)
        elif args.command == "status":
            show_status(store)
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nОстановлено пользователем. Прогресс сохраняется после каждого трека/партии.")
        sys.exit(130)
    except Exception as exc:
        log(f"\nОШИБКА: {exc}")
        sys.exit(1)
