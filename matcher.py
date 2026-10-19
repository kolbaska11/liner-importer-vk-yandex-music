"""Plain-text parser and conservative YouTube Music result matching."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz
from unidecode import unidecode

SEPARATOR = re.compile(r"\s+(?:[-–—−]|::|\|)\s+")
DURATION = re.compile(r"\s*[|;]\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")
NOISE = re.compile(r"[^\w\s]+", re.UNICODE)
SPACES = re.compile(r"\s+")
BAD_VERSIONS = {
    "live": ("live", "концерт", "концертная"),
    "remix": ("remix", "rmx", "ремикс"),
    "cover": ("cover", "кавер"),
    "instrumental": ("instrumental", "инструментал", "karaoke", "караоке"),
    "sped": ("sped up", "speed up", "ускоренн", "slowed", "nightcore", "reverb"),
    "demo": ("demo", "демо"),
}


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).lower().replace("ё", "е")
    value = value.replace("&", " and ")
    value = NOISE.sub(" ", value)
    return SPACES.sub(" ", value).strip()


def key(value: str) -> str:
    return norm(unidecode(value))


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    a, b = key(left), key(right)
    if a == b:
        return 100.0
    return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))


def word_recall(query: str, candidate: str) -> float:
    qs = set(key(query).split())
    cs = set(key(candidate).split())
    return 100.0 * len(qs & cs) / len(qs) if qs else 0.0


@dataclass(frozen=True)
class Spec:
    title: str
    artist: str = ""
    duration_seconds: int | None = None


@dataclass(frozen=True)
class Entry:
    line: int
    raw: str
    specs: tuple[Spec, ...]
    duration_seconds: int | None = None
    parse_uncertain: bool = False


def parse_duration(raw: str) -> tuple[str, int | None]:
    m = DURATION.search(raw)
    if not m:
        return raw, None
    a, b, c = m.groups()
    duration = (int(a) * 60 + int(b)) if c is None else (int(a) * 3600 + int(b) * 60 + int(c))
    return raw[:m.start()].strip(), duration


def infer_artists(lines: list[str], known: list[str] | None = None) -> list[str]:
    """Repeated suffix/prefix patterns can identify artists even without separators."""
    counts: Counter[str] = Counter()
    for raw in lines:
        content, _ = parse_duration(raw)
        words = content.split()
        if not (2 <= len(words) <= 14):
            continue
        for size in range(1, min(4, len(words) - 1) + 1):
            if size == 1 and len(words[-1]) < 5:
                continue
            suffix = " ".join(words[-size:])
            prefix = " ".join(words[:size])
            if len(suffix) >= 4:
                counts[norm(suffix)] += 1
            if len(prefix) >= 4:
                counts[norm(prefix)] += 1
    suggestions = [value for value, count in counts.items() if count >= 3 and len(value) >= 5]
    suggestions.extend(a.strip() for a in (known or []) if a.strip())
    # Specific multi-word names take precedence over generic fragments.
    return sorted(set(suggestions), key=lambda s: (len(s.split()), len(s)), reverse=True)


def parse_entry(raw: str, line: int, artists: list[str], order: str = "auto") -> Entry:
    clean, duration = parse_duration(raw.strip())
    specs: list[Spec] = []

    parts = SEPARATOR.split(clean, maxsplit=1)
    if len(parts) == 2 and all(parts):
        a, b = (v.strip() for v in parts)
        if order == "title-artist":
            specs.append(Spec(a, b, duration))
        elif order == "artist-title":
            specs.append(Spec(b, a, duration))
        else:
            specs.extend((Spec(a, b, duration), Spec(b, a, duration)))
    else:
        normalized = norm(clean)
        for artist in artists:
            artist_key = norm(artist)
            if not artist_key:
                continue
            for start in (False, True):
                if start and normalized.startswith(artist_key + " "):
                    remainder = clean[len(artist):].strip(" –—-|:;")
                    if remainder:
                        specs.append(Spec(remainder, artist, duration))
                elif not start and normalized.endswith(" " + artist_key):
                    remainder = clean[:-len(artist)].strip(" –—-|:;")
                    if remainder:
                        specs.append(Spec(remainder, artist, duration))
            if specs:
                break
    if not specs:
        specs.append(Spec(clean, "", duration))
    return Entry(line, raw.strip(), tuple(specs), duration)


def cleaned_title(title: str) -> str:
    # Preserve meaning of remix/live, but remove ordinary suffix metadata.
    value = re.sub(r"\s*\((official (music )?video|official audio|lyrics?|audio)\)\s*$", "", title, flags=re.I)
    return value.strip()


def score_one(spec: Spec, candidate: dict[str, Any], raw: str) -> tuple[float, dict[str, Any]]:
    title = cleaned_title(str(candidate.get("title", "")))
    artist = ", ".join(x.get("name", "") if isinstance(x, dict) else str(x)
                       for x in (candidate.get("artists") or []))
    if not title or not candidate.get("videoId"):
        return 0.0, {}
    title_sim = similarity(spec.title, title)
    title_recall = word_recall(spec.title, title)
    if spec.artist:
        listed = [x.get("name", "") if isinstance(x, dict) else str(x) for x in (candidate.get("artists") or [])]
        wanted = re.split(r"\s+(?:feat\.|ft\.)\s+|,\s*", spec.artist, flags=re.I)
        per_artist = [max((similarity(expected, actual) for actual in listed), default=0) for expected in wanted if expected.strip()]
        artist_sim = max([similarity(spec.artist, name) for name in listed] + [similarity(spec.artist, artist)]
                         + ([sum(per_artist)/len(per_artist)] if per_artist else []))
        score = (0.68 * title_sim + 0.26 * artist_sim + 0.06 * title_recall)
        if artist_sim < 60:
            score = min(score, 79.0)
        if title_sim < 65:
            score = min(score, 79.0)
    else:
        # Full source line may be "song artist" or "artist song".
        all_text = f"{title} {artist}"
        full_match = max(similarity(raw, all_text), fuzz.token_sort_ratio(key(raw), key(all_text)))
        recall = word_recall(raw, all_text)
        score = 0.62 * full_match + 0.38 * recall
        # No reliable artist/title parsing -> request human confirmation.
        score = min(score, 89.0)
    source_text = key(f"{spec.title} {spec.artist}")
    target_text = key(f"{title} {artist}")
    penalty = 0
    for markers in BAD_VERSIONS.values():
        # Match whole words/phrases, not substrings ("live" must not match "alive").
        source_has = any(re.search(r"(?<!\w)" + re.escape(key(m)) + r"(?!\w)", source_text) for m in markers)
        target_has = any(re.search(r"(?<!\w)" + re.escape(key(m)) + r"(?!\w)", target_text) for m in markers)
        if target_has and not source_has:
            penalty += 24
        elif source_has and not target_has:
            penalty += 12
    score -= min(45, penalty)
    if candidate.get("resultType") == "video":
        score -= 3.0
    dur = spec.duration_seconds
    duration = candidate.get("duration_seconds")
    if duration is None:
        display = str(candidate.get("duration", ""))
        if display and re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", display):
            parts = [int(p) for p in display.split(":")]
            duration = parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2]
    if dur and duration:
        delta = abs(int(duration) - int(dur))
        if delta > 45:
            score -= 25
        elif delta > 12:
            score -= 10
        elif delta < 5:
            score += 2
    return round(max(0.0, min(100.0, score)), 1), {"matched_title": spec.title, "matched_artist": spec.artist}


def score_results(entry: Entry, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        vid = candidate.get("videoId")
        if not vid or candidate.get("resultType") not in ("song", "video"):
            continue
        choices = [score_one(spec, candidate, entry.raw) for spec in entry.specs]
        score, evidence = max(choices, key=lambda pair: pair[0])
        prev = results.get(vid)
        if prev is None or score > prev["score"]:
            results[vid] = {
                "videoId": vid,
                "title": candidate.get("title", ""),
                "artists": [x.get("name", "") if isinstance(x, dict) else str(x) for x in (candidate.get("artists") or [])],
                "duration": candidate.get("duration", ""),
                "resultType": candidate.get("resultType", ""),
                "score": score,
                **evidence,
            }
    return sorted(results.values(), key=lambda x: (-x["score"], x["title"]))


def search_queries(entry: Entry, max_queries: int) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    for spec in entry.specs:
        if spec.artist:
            ordered.extend([
                (f"{spec.artist} {spec.title}", "songs"),
                (f"{spec.title} {spec.artist}", "songs"),
                (f"{spec.artist} {spec.title}", "all"),
                (spec.title, "all"),
                (f"{spec.artist} {spec.title}", "videos"),
                (spec.title, "videos"),
                (spec.title, "songs"),
            ])
        else:
            ordered.extend([(spec.title, "all"), (spec.title, "songs"), (spec.title, "videos")])
        if any(ord(c) > 127 for c in spec.title + spec.artist):
            ordered.append((unidecode(f"{spec.artist} {spec.title}".strip()), "all"))
    out = []
    seen = set()
    for pair in ordered:
        identifier = (key(pair[0]), pair[1])
        if identifier not in seen:
            out.append(pair)
            seen.add(identifier)
    return out[:max_queries]


def verdict(results: list[dict[str, Any]], accept: float = 91.0, review: float = 65.0, gap: float = 6.0) -> str:
    if not results or results[0]["score"] < review:
        return "MISSING"
    if results[0]["score"] >= accept:
        if len(results) == 1 or results[0]["score"] - results[1]["score"] >= gap:
            return "MATCHED"
        # Different YouTube IDs often correspond to the same metadata. Don't
        # demand confirmation of two identical titles from the same artist.
        first, second = results[:2]
        if (key(first.get("title", "")) == key(second.get("title", ""))
            and {key(a) for a in first.get("artists", [])} == {key(a) for a in second.get("artists", [])}
            and second["score"] >= accept):
            return "MATCHED"
    return "REVIEW"
