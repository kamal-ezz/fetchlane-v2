"""Identify equivalent download requests without storing raw advanced options."""

import hashlib
import json
from urllib.parse import parse_qs, urlsplit


def canonical_url(url: str, command: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    youtube = host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
    query = parse_qs(parsed.query)
    if command == "playlist" and youtube and query.get("list"):
        return "youtube:playlist:" + query["list"][0]
    if command != "playlist":
        video_id = None
        if host in {"youtu.be", "www.youtu.be"}:
            video_id = parsed.path.strip("/").split("/")[0]
        elif youtube:
            if parsed.path == "/watch":
                video_id = query.get("v", [None])[0]
            elif parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
                video_id = parsed.path.split("/")[2]
        if video_id:
            return "youtube:video:" + video_id
    # Arbitrary site query strings and fragments may change the requested content.
    return url


def request_key(command: str, urls: list[str], options: dict, extra: list[str]) -> str:
    settings = {"output": options["output"]}
    if command == "audio":
        settings["format"] = options.get("format", "best")
    else:
        settings["quality"] = options.get("quality", "best")
        if options.get("format", "best") != "best":
            settings["video_format"] = options["format"]
    if command == "playlist":
        settings["items"] = options.get("items")
    # Advanced flags can use URL timestamps (e.g. --download-sections *from-url).
    identities = urls if extra else [canonical_url(url, command) for url in urls]
    payload = {"command": command, "urls": sorted(identities),
               "settings": settings, "extra": extra}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
