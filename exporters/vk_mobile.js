(async () => {
    console.clear();

    // Вставь ссылки через всплывающее окно (каждая с новой строки),
    // например: https://vk.ru/music?z=audio_playlist123456_7/ACCESS_HASH
    // Данные никуда не отправляются, кроме самого сайта m.vk.ru.
    const input = prompt(
        'Вставь ссылки на плейлисты VK (по одной на строку):',
        ''
    );
    const PLAYLISTS = (input || '').split(/\s+/).map(s => s.trim()).filter(Boolean);
    if (!PLAYLISTS.length) {
        console.warn('Не указаны ссылки. Экспорт отменён.');
        return;
    }

    // ------------------------------------------------------------
    // Проверка страницы
    // ------------------------------------------------------------

    if (location.hostname !== 'm.vk.ru') {
        console.error(
            '❌ Этот скрипт нужно запускать именно на https://m.vk.ru/'
        );

        console.log(
            'Открой https://m.vk.ru/, войди в аккаунт и снова вставь код.'
        );

        return;
    }

    console.log('🎵 VK Playlist Exporter');
    console.log('Начинаю загрузку плейлистов...');
    console.log('');

    const sleep = ms =>
        new Promise(resolve => setTimeout(resolve, ms));

    // ------------------------------------------------------------
    // Очистка текста
    // ------------------------------------------------------------

    function cleanHTML(value) {
        if (value == null) {
            return '';
        }

        const div = document.createElement('div');

        div.innerHTML = String(value);

        return (div.textContent || div.innerText || '')
            .replace(/\s+/g, ' ')
            .trim();
    }

    // ------------------------------------------------------------
    // Разбираем ссылку VK
    // ------------------------------------------------------------

    function parsePlaylistUrl(urlString) {
        const url = new URL(urlString);

        let ownerId = null;
        let playlistId = null;
        let accessHash = null;

        // Формат:
        // vk.ru/music?z=audio_playlist123456789_8/HASH

        const z = url.searchParams.get('z');
        if (z) {
            const match = z.match(
                /audio_playlist(-?\d+)_(\d+)(?:\/|_)?([a-zA-Z0-9]+)?/
            );
            if (match) {
                ownerId = match[1];
                playlistId = match[2];
                accessHash = match[3] || '';
            }
        }

        // Формат:
        // vk.ru/music/playlist/123456789_8_HASH

        if (!ownerId) {
            const match = url.pathname.match(
                /playlist\/(-?\d+)_(\d+)(?:_([a-zA-Z0-9]+))?/
            );

            if (match) {
                ownerId = match[1];
                playlistId = match[2];
                accessHash =
                    match[3] ||
                    url.searchParams.get('access_key') ||
                    '';
            }
        }

        // Формат: vk.ru/audio_playlist123456_7 (без хеша для публичных)
        if (!ownerId) {
            const direct = url.pathname.match(/audio_playlist(-?\d+)_(\d+)(?:_([a-zA-Z0-9]+))?/);
            if (direct) {
                ownerId = direct[1];
                playlistId = direct[2];
                accessHash = direct[3] || '';
            }
        }

        if (!ownerId || !playlistId) {
            throw new Error(
                `Не удалось разобрать ссылку:\n${urlString}`
            );
        }

        return {
            ownerId,
            playlistId,
            accessHash: accessHash || ''
        };
    }

    // ------------------------------------------------------------
    // POST запрос к VK
    // ------------------------------------------------------------

    async function vkPost(params) {
        const response = await fetch('/audio', {
            method: 'POST',

            credentials: 'include',

            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type':
                    'application/x-www-form-urlencoded; charset=UTF-8'
            },

            body: new URLSearchParams(params).toString()
        });

        const raw = await response.text();

        if (!response.ok) {
            throw new Error(
                `HTTP ${response.status}\n${raw.slice(0, 500)}`
            );
        }

        try {
            // Некоторые ответы VK могут начинаться с <!--
            return JSON.parse(
                raw.replace(/^\s*<!--\s*/, '')
            );
        } catch (error) {
            console.error(
                'VK вернул не JSON:',
                raw.slice(0, 1000)
            );

            throw new Error(
                'VK вернул неожиданный ответ. Смотри Console.'
            );
        }
    }

    // ------------------------------------------------------------
    // Получаем ID трека для reload_audio
    // ------------------------------------------------------------

    function getReloadId(track) {
        try {
            const hashes = String(track[13] || '')
                .split('/');

            if (
                track[0] == null ||
                track[1] == null ||
                !hashes[2] ||
                !hashes[5]
            ) {
                return null;
            }

            return [
                track[1],
                track[0],
                hashes[2],
                hashes[5]
            ].join('_');

        } catch {
            return null;
        }
    }

    // ------------------------------------------------------------
    // Загружаем один плейлист
    // ------------------------------------------------------------

    async function loadPlaylist(url, number) {
        const info = parsePlaylistUrl(url);

        console.log(
            `📀 Плейлист ${number}`
        );

        console.log(
            `owner_id = ${info.ownerId}`
        );

        console.log(
            `playlist_id = ${info.playlistId}`
        );

        let offset = 0;

        const rawTracks = [];

        let page = 1;

        // Защита от бесконечного цикла
        for (let safety = 0; safety < 100; safety++) {

            console.log(
                `⬇️ Загружаю страницу ${page}, offset ${offset}...`
            );

            const response = await vkPost({
                act: 'load_section',

                owner_id: info.ownerId,

                playlist_id: info.playlistId,

                offset: String(offset),

                type: 'playlist',

                access_hash: info.accessHash,

                is_loading_all: '1'
            });

            const section =
                response &&
                response.data &&
                response.data[0];

            if (!section) {
                console.error(
                    'Полный ответ VK:',
                    response
                );

                throw new Error(
                    `VK не отдал данные плейлиста ${number}.`
                );
            }

            const list =
                Array.isArray(section.list)
                    ? section.list
                    : [];

            console.log(
                `Получено: ${list.length}`
            );

            rawTracks.push(...list);

            console.log(
                `Всего найдено: ${rawTracks.length}`
            );

            if (!section.hasMore) {
                break;
            }

            // VK использует шаг 2000 для плейлистов
            offset += 2000;

            page++;

            await sleep(700);
        }

        if (!rawTracks.length) {
            throw new Error(
                `Плейлист ${number}: VK вернул 0 треков.`
            );
        }

        // --------------------------------------------------------
        // Пробуем получить исполнителя и название сразу
        // --------------------------------------------------------

        const tracks = rawTracks.map((track, index) => ({
            index,

            id: getReloadId(track),

            owner:
                track && track[1] != null
                    ? String(track[1])
                    : '',

            audioId:
                track && track[0] != null
                    ? String(track[0])
                    : '',

            title: cleanHTML(
                track ? track[3] : ''
            ),

            artist: cleanHTML(
                track ? track[4] : ''
            )
        }));

        let missing = tracks.filter(
            track =>
                !track.title ||
                !track.artist
        );

        console.log(
            `📝 Метаданные сразу получены для ${
                tracks.length - missing.length
            }/${tracks.length}`
        );

        // --------------------------------------------------------
        // Если VK не прислал название сразу —
        // используем reload_audio
        // --------------------------------------------------------

        if (missing.length) {
            console.log(
                `🔄 Нужно дополнительно получить ${missing.length} треков`
            );

            const reloadable =
                missing.filter(track => track.id);

            for (
                let i = 0;
                i < reloadable.length;
                i += 10
            ) {
                const batch =
                    reloadable.slice(i, i + 10);

                console.log(
                    `🔄 Метаданные ${
                        Math.min(
                            i + batch.length,
                            reloadable.length
                        )
                    }/${reloadable.length}`
                );

                const response = await vkPost({
                    act: 'reload_audio',

                    ids: batch
                        .map(track => track.id)
                        .join(',')
                });

                const audios =
                    response &&
                    response.data &&
                    Array.isArray(response.data[0])
                        ? response.data[0]
                        : [];

                for (const audio of audios) {

                    const owner =
                        String(audio[1]);

                    const audioId =
                        String(audio[0]);

                    const original =
                        tracks.find(track =>
                            track.owner === owner &&
                            track.audioId === audioId
                        );

                    if (!original) {
                        continue;
                    }

                    original.title =
                        cleanHTML(audio[3]);

                    original.artist =
                        cleanHTML(audio[4]);
                }

                // VK ограничивает частоту reload_audio
                await sleep(1600);
            }
        }

        // --------------------------------------------------------
        // Формируем итог
        // --------------------------------------------------------

        const result = tracks
            .filter(track =>
                track.artist &&
                track.title
            )
            .sort(
                (a, b) =>
                    a.index - b.index
            )
            .map(
                track =>
                    `${track.artist} — ${track.title}`
            );

        console.log(
            `✅ Плейлист ${number}: ${result.length} треков`
        );

        console.log('');

        return {
            number,
            playlistId: info.playlistId,
            url,
            tracks: result,
            originalCount: rawTracks.length
        };
    }

    // ------------------------------------------------------------
    // Обрабатываем все плейлисты
    // ------------------------------------------------------------

    const results = [];

    for (
        let i = 0;
        i < PLAYLISTS.length;
        i++
    ) {
        try {
            const result =
                await loadPlaylist(
                    PLAYLISTS[i],
                    i + 1
                );

            results.push(result);

        } catch (error) {

            console.error(
                `❌ Ошибка плейлиста ${i + 1}:`,
                error
            );

            results.push({
                number: i + 1,
                playlistId: '?',
                url: PLAYLISTS[i],
                tracks: [],
                error: error.message
            });
        }
    }

    // ------------------------------------------------------------
    // TXT
    // ------------------------------------------------------------

    const sections =
        results.map(result => {

            const header = [
                '========================================',
                `ПЛЕЙЛИСТ ${result.number}`,
                `VK playlist ID: ${result.playlistId}`,
                `Количество треков: ${result.tracks.length}`,
                '========================================',
                ''
            ];

            if (result.error) {
                header.push(
                    `ОШИБКА: ${result.error}`
                );

                return header.join('\r\n');
            }

            return [
                ...header,
                ...result.tracks
            ].join('\r\n');
        });

    const finalText =
        sections.join(
            '\r\n\r\n\r\n'
        );

    // BOM для нормальной кириллицы в Windows
    const blob =
        new Blob(
            ['\uFEFF' + finalText],
            {
                type:
                    'text/plain;charset=utf-8'
            }
        );

    const blobUrl =
        URL.createObjectURL(blob);

    const link =
        document.createElement('a');

    link.href = blobUrl;

    link.download =
        'VK_playlists.txt';

    document.body.appendChild(link);

    link.click();

    link.remove();

    setTimeout(
        () =>
            URL.revokeObjectURL(blobUrl),
        2000
    );

    // ------------------------------------------------------------
    // Буфер обмена
    // ------------------------------------------------------------

    try {
        await navigator.clipboard.writeText(
            finalText
        );

        console.log(
            '📋 Текст также скопирован в буфер обмена.'
        );
    } catch {
        console.log(
            'ℹ️ В буфер обмена скопировать не удалось — это не проблема.'
        );
    }

    console.log('');
    console.log('==============================');
    console.log('✅ ГОТОВО');
    console.log('==============================');

    results.forEach(result => {
        console.log(
            `Плейлист ${result.number}: ${result.tracks.length} треков`
        );
    });

    console.log('');
    console.log(
        '💾 Файл VK_playlists.txt скачан.'
    );

})();