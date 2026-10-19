"""Public-friendly Windows console menu with no personal playlist data."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
SELECTED = ROOT / 'work' / 'selected_playlist.txt'
DEMO = ROOT / 'examples' / 'example_playlist.csv'


def candidates():
    result = sorted(DATA.glob('*.csv')) + sorted(DATA.glob('*.txt'))
    return [p for p in result if p.is_file() and not p.name.startswith('00_review_')]


def current():
    if SELECTED.exists():
        path = Path(SELECTED.read_text(encoding='utf-8').strip())
        if path.is_file():
            return path
    return candidates()[0] if candidates() else DEMO


def call(module, *args):
    command = [sys.executable, '-m', *map(str, args)] if module == '-m' else [sys.executable, str(ROOT / module), *map(str, args)]
    print('\n$ python', ' '.join(map(str, command[1:])), flush=True)
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def select_playlist():
    choices = candidates()
    if choices:
        print('\nФайлы в data/:')
        for idx, p in enumerate(choices, 1):
            print(f'{idx}. {p.name}')
    else:
        print('\nВ data/ пока нет импортируемых файлов.')
    user = input('Введи номер или путь к CSV/TXT: ').strip().strip('"')
    selected = choices[int(user) - 1] if user.isdecimal() and 1 <= int(user) <= len(choices) else Path(user)
    if not selected.is_file():
        print('Такого файла нет.')
        return
    SELECTED.parent.mkdir(exist_ok=True)
    SELECTED.write_text(str(selected.resolve()), encoding='utf-8')
    print('Выбран:', selected)


def menu():
    while True:
        src = current()
        print('\n' + '=' * 65)
        print(' Liner Importer | VK & Yandex Music → YouTube Music')
        print('=' * 65)
        print('Текущий файл:', src)
        print(' 1. Установить зависимости Python (первый запуск)')
        print(' 2. Авторизация YouTube Music из буфера Firefox')
        print(' 3. Проверить чтение и поиск')
        print(' 4. Поиск первых 20 треков')
        print(' 5. Поиск всех треков')
        print(' 6. Проверка сомнительных совпадений')
        print(' 7. Создать обычный плейлист YouTube Music')
        print(' 8. Состояние / отчёты')
        print(' 9. Выбрать CSV/TXT')
        print('10. Преобразовать VK TXT в CSV')
        print('11. Резервный вход через Google OAuth')
        print('12. Диагностика поиска')
        print('13. Проверка права создавать плейлисты')
        print('14. Плейлисты для Liner (не более 900 треков в каждом)')
        print('15. Отдельный плейлист «На проверку»')
        print('16. Экспорт «Мне нравится» из Яндекс Музыки')
        print('17. Экспорт плейлистов из Яндекс Музыки')
        print(' 0. Выход')
        choice = input('Номер: ').strip()
        if choice == '0': return
        if choice == '1':
            call('-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt'))
        elif choice == '2':
            print('Firefox → music.youtube.com → F12 → Network → POST browse → ПКМ → Copy Request Headers.')
            input('Скопируй заголовки; вернись сюда и нажми Enter: ')
            call('app.py', 'auth', 'clipboard')
        elif choice == '3': call('app.py', 'auth', 'check')
        elif choice == '4': call('app.py', 'scan', src, '--limit', '20')
        elif choice == '5': call('app.py', 'scan', src)
        elif choice == '6': call('app.py', 'review', src)
        elif choice == '7':
            name = input('Имя плейлиста [Моя музыка]: ').strip() or 'Моя музыка'
            call('app.py', 'upload', src, '--name', name)
        elif choice == '8': call('app.py', 'status', src)
        elif choice == '9': select_playlist()
        elif choice == '10':
            path = input('Путь к TXT экспорта VK: ').strip().strip('"')
            if path: call('prepare.py', path)
        elif choice == '11': call('app.py', 'auth', 'oauth')
        elif choice == '12': call('app.py', 'diagnose')
        elif choice == '13': call('app.py', 'auth', 'write-test')
        elif choice == '14':
            name = input('Название [Любимые песенки]: ').strip() or 'Любимые песенки'
            call('app.py', 'liner', src, '--name', name)
        elif choice == '15':
            name = input('Название [Любимые песенки]: ').strip() or 'Любимые песенки'
            call('app.py', 'uncertain', src, '--name', name)
        elif choice == '16': call('exporters/yandex_music.py', '--likes')
        elif choice == '17': call('exporters/yandex_music.py', '--all-playlists')
        else: print('Нет такого пункта')
        input('\nEnter — вернуться в меню...')

if __name__ == '__main__':
    try: menu()
    except KeyboardInterrupt: print('\nОстановлено.')
