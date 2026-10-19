# 🚀 Публикация на GitHub

## 1. Создай репозиторий

Открой https://github.com/new и укажи:

- **Repository name:** `liner-importer-vk-yandex-music`
- **Description:** `Transfer playlists from VK and Yandex Music to YouTube Music, with smart matching and Liner-ready playlist splitting.`
- **Visibility:** Public, если хочешь открыть код всем.
- **Initialize this repository:** **НЕ** ставь галочки Add a README, Add .gitignore и Choose a license — всё уже в проекте.

## 2. Залей файлы — способ через Git

Открой папку проекта (ту, в которой лежит README.md), введи `cmd` в адресной строке проводника и выполни команды по очереди:

```bat
git init
git branch -M main
git add .
git status
git commit -m "Initial public release: VK and Yandex Music importer"
git remote add origin https://github.com/ТВОЙ_ЛОГИН/liner-importer-vk-yandex-music.git
git push -u origin main
```

Заменяй `ТВОЙ_ЛОГИН` своим логином GitHub. Если команды `git` нет, установи **Git for Windows** с https://git-scm.com/download/win или воспользуйся GitHub Desktop.

**Перед `git commit` внимательно изучи результат `git status`.** Среди файлов не должно быть Cookie, токенов, `.env`, личного списка музыки, папок `data/`, `auth/`, `work/` или `reports/` (кроме пустых `.gitkeep`).

## 3. Залей файлы — без терминала

Открой новый пустой репозиторий → **uploading an existing file** → перетащи распакованные исходники и папки. **Сам ZIP загружать вместо исходников не надо:** GitHub не распакует его как код репозитория. Проверь, что скрытые `.github`, `.gitignore` и `.env.example` также попали в список файлов перед коммитом.

## 4. Добавь Topics

**Settings не нужны:** на главной странице репозитория в About нажми значок шестерёнки и добавь:

`python`, `music`, `youtube-music`, `vk`, `yandex-music`, `playlist-importer`, `ytmusicapi`, `liner`

## 5. После публикации

- Проверь, что README отобразился красиво и ссылки открываются.
- Проверь раздел **Actions**: тесты должны запускаться при каждом push.
- Если обнаружен секрет в уже опубликованном коммите, одного удаления из следующего коммита недостаточно: немедленно отзови токен / сеанс, а затем очисти историю при необходимости.
- Для готовой версии можно открыть **Releases → Draft a new release**, тег `v1.0.0`, и приложить ZIP с исходниками без личных данных.
