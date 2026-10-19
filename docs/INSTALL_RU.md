# Установка для Windows — краткая шпаргалка

1. Установите Python 3.11+ с https://python.org/downloads/ (при установке отметьте **Add Python to PATH**).
2. Скачайте репозиторий через **Code → Download ZIP** и распакуйте.
3. Запустите `START_WINDOWS.bat` → **1**. Дождитесь завершения pip.
4. Источник VK: `m.vk.ru` → F12 → Console → `exporters/vk_mobile.js` → TXT → **10** для подготовки CSV.
5. Источник Яндекс: `START_WINDOWS.bat` → сначала `python exporters/yandex_music.py --auth`, затем **16** или **17**.
6. Выберите готовый CSV через **9**.
7. YouTube: Firefox → music.youtube.com → F12 → Network → POST browse → **Copy Request Headers**, далее меню **2**, **3**, **13**.
8. Поиск: **4**, **5**, **6**. Части Liner: **14**. Неуверенные варианты: **15**.

Папки **`auth/`** и **`work/`** не удаляйте при обновлении программы; `data/` хранит ваши CSV, `reports/` — отчёты.

Пока YouTube Music не подтвердит тест пункта 13, массовый импорт не запускайте.
