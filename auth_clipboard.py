"""Import locally copied Chrome/Edge/Firefox request headers safely.

Supported clipboard formats: Copy as fetch (Node.js), Copy as cURL (bash),
Copy request headers, and JSON header objects.

Never logs, uploads, or returns credentials to the terminal.
"""
from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
from pathlib import Path


class ClipboardAuthError(ValueError):
    pass


def read_clipboard() -> str:
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        try:
            value = root.clipboard_get()
        finally:
            root.destroy()
        return value
    except Exception:
        pass
    # Python may lack tkinter on some installations. PowerShell is bundled with Windows.
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            check=True,
        )
        return result.stdout
    except Exception as exc:
        raise ClipboardAuthError(
            "Не могу прочитать буфер обмена. Убедись, что текст скопирован, "
            "а Windows разрешает программе читать буфер обмена."
        ) from exc


def _balanced_object(text: str, start: int) -> str:
    """Return first JS object; respect quotes and escapes (without executing JS)."""
    start = text.find("{", start)
    if start == -1:
        raise ClipboardAuthError("Не найден объект заголовков.")
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'", "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ClipboardAuthError("Объект заголовков скопирован не полностью.")


def _object_headers(text: str) -> dict[str, str]:
    # In fetch(...) the only object we care about is `headers: {...}`.
    marker = re.search(r"(?:[\"']?headers[\"']?)\s*:\s*\{", text, re.I)
    if marker:
        chunk = _balanced_object(text, marker.start())
    elif text.lstrip().startswith("{"):
        chunk = _balanced_object(text, 0)
    else:
        return {}
    try:
        obj = json.loads(chunk)
    except (ValueError, TypeError):
        try:
            obj = ast.literal_eval(chunk)
        except (SyntaxError, ValueError) as exc:
            raise ClipboardAuthError(
                "Заголовки в скопированном тексте не распознаны. "
                "Скопируй весь запрос через Copy as fetch (Node.js), не его содержимое."
            ) from exc
    if not isinstance(obj, dict):
        return {}
    return {str(k).lower().strip(): str(v) for k, v in obj.items() if isinstance(v, (str, int, float))}


def _curl_headers(text: str) -> dict[str, str]:
    """Read headers from POSIX/Firefox, Chrome, or Windows cURL commands.

    We intentionally do not parse request bodies: Firefox may put multiline
    JSON into --data-raw using quoting that Python's shlex doesn't support.
    Header arguments appear before the body in browser-generated commands.
    """
    if not re.match(r"\s*curl(?:\.exe)?\s", text, re.I):
        return {}

    # Bash and CMD/PowerShell line continuations, including Windows CRLF.
    clean = re.sub(r"\\\r?\n[ \t]*", " ", text.strip())
    clean = re.sub(r"\^\r?\n[ \t]*", " ", clean)
    clean = re.sub(r"`\r?\n[ \t]*", " ", clean)
    # Most browsers put all headers BEFORE --data. Never ask shlex to parse
    # any following body. Without this, unmatched quoting in the JSON body
    # can prevent reading valid cookie and authorization headers.
    clean = re.split(r"\s+(?=--(?:data|json)(?:[-= ]|$))", clean, maxsplit=1)[0]

    try:
        tokens = shlex.split(clean, posix=True)
    except ValueError:
        # Salvage valid header arguments that precede an unfinished quote.
        # No command is executed, and secrets are never printed.
        tokens = []
        lexer = shlex.shlex(clean, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            for token in lexer:
                tokens.append(token)
        except ValueError:
            pass

    result: dict[str, str] = {}
    for i, token in enumerate(tokens):
        content = None
        if token in ("-H", "--header", "-b", "--cookie") and i + 1 < len(tokens):
            content = tokens[i + 1]
            if token in ("-b", "--cookie"):
                content = "cookie: " + content
        elif token.startswith("--header="):
            content = token[len("--header="):]
        elif token.startswith("--cookie="):
            content = "cookie: " + token.split("=", 1)[1]

        if content:
            # shlex does not natively consume Bash ANSI-C quoting $'...'.
            # In most Firefox requests only ordinary values are used, but
            # this lets simple ANSI-C quoted headers still be recognized.
            if content.startswith("$") and ":" in content[1:]:
                content = content[1:]
            if ":" in content:
                name, value = content.split(":", 1)
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name.strip()):
                    result[name.lower().strip()] = value.strip()
    return result


def _raw_headers(text: str) -> dict[str, str]:
    result = {}
    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith(("//", "#", "{", "}")):
            continue
        if ":" not in line or line.startswith(":"):
            continue
        key, value = line.split(":", 1)
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key.strip()):
            result[key.lower().strip()] = value.strip()
    return result


def extract_headers(clipboard_text: str) -> dict[str, str]:
    if not clipboard_text or not clipboard_text.strip():
        raise ClipboardAuthError("Буфер обмена пуст. Сначала скопируй запрос из вкладки Network.")
    if len(clipboard_text) > 2_000_000:
        raise ClipboardAuthError("Скопировано слишком много текста. Нужен один запрос POST /browse.")

    text = clipboard_text.strip()
    result = _curl_headers(text)
    if not result:
        result = _object_headers(text)
    if not result:
        result = _raw_headers(text)

    cookie = result.get("cookie", "").strip()
    if not cookie or "=" not in cookie:
        raise ClipboardAuthError(
            "Не вижу Cookie. Скорее всего, скопирован Payload/Response, а не сам запрос. "
            "Нужно: ПКМ на строке POST browse в Network → «Копировать заголовки запроса» "
            "(Firefox), или Copy as fetch (Node.js) / cURL (POSIX)."
        )

    allowed = (
        "accept", "authorization", "content-type", "cookie", "x-goog-authuser",
        "x-origin", "origin", "referer", "user-agent", "x-youtube-client-name", "x-youtube-client-version",
        "x-youtube-device", "x-youtube-page-cl", "x-youtube-page-label",
    )
    cleaned = {k: v for k, v in result.items() if k in allowed and v}
    for key, value in cleaned.items():
        if "\r" in value or "\n" in value:
            raise ClipboardAuthError("Один из заголовков содержит перенос строки; попробуй скопировать заново.")
    cleaned.setdefault("x-goog-authuser", "0")
    cleaned.setdefault("x-origin", "https://music.youtube.com")
    cleaned.setdefault("accept", "*/*")
    cleaned.setdefault("content-type", "application/json")
    return cleaned


def setup_from_clipboard(output: Path) -> None:
    try:
        from ytmusicapi import setup
    except ImportError as exc:
        raise ClipboardAuthError("Сначала выбери пункт 1 — Install Python dependencies.") from exc
    headers = extract_headers(read_clipboard())
    # Search can work without signed-in headers, but playlist modifications cannot.
    if "sapisidhash" not in headers.get("authorization", "").lower():
        raise ClipboardAuthError("Запрос не содержит Authorization: SAPISIDHASH. Выбери авторизованный POST browse (Status 200), а не запрос из гостевого режима.")
    if "__Secure-3PAPISID" not in headers.get("cookie", ""):
        raise ClipboardAuthError("Cookie не содержит __Secure-3PAPISID. Убедись, что вошёл в Google-аккаунт в YouTube Music, и скопируй запрос заново.")
    output.parent.mkdir(parents=True, exist_ok=True)
    # ytmusicapi.setup validates and fills browser headers. Our format is plain
    # 'Header: value' so it works even when Chrome copy formatting changes.
    raw = "\n".join(f"{key}: {value}" for key, value in headers.items())
    try:
        setup(filepath=str(output), headers_raw=raw)
    except Exception as exc:
        raise ClipboardAuthError("Библиотека не приняла заголовки. Проверь, что выбран POST browse и вход выполнен.") from exc
