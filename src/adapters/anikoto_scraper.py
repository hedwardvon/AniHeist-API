import httpx
import re
from typing import Optional
from src.utils.logger import get_logger
from src.models.stream import StreamResult, ParserError

log = get_logger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

SITES = {
    "anikoto": {"base": "https://anikototv.to", "name": "Anikoto"},
    "aniwaves": {"base": "https://aniwaves.ru", "name": "Aniwave"},
}


async def _get_anilist_title(anilist_id: int) -> str:
    async with httpx.AsyncClient(timeout=10) as c:
        q = """
        query ($id: Int) {
            Media(id: $id, type: ANIME) {
                title { romaji english }
            }
        }
        """
        r = await c.post(
            "https://graphql.anilist.co",
            json={"query": q, "variables": {"id": anilist_id}},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if r.status_code == 200:
            m = r.json().get("data", {}).get("Media", {})
            t = m.get("title", {})
            return t.get("english") or t.get("romaji") or ""
    return ""


def _ajax_headers(base: str, referer: Optional[str] = None) -> dict[str, str]:
    return {
        **HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": referer or f"{base}/",
    }


async def _json_response(client: httpx.AsyncClient, url: str, *, params=None, headers=None) -> dict:
    r = await client.get(url, params=params, headers=headers)
    if r.status_code != 200:
        raise ParserError(f"HTTP {r.status_code} from {r.url}")
    try:
        data = r.json()
    except ValueError as exc:
        content_type = r.headers.get("content-type", "unknown")
        preview = " ".join(r.text[:160].split())
        raise ParserError(
            f"Expected JSON from {r.url}; got {content_type}: {preview}"
        ) from exc
    if not isinstance(data, dict):
        raise ParserError(f"Unexpected response shape from {r.url}")
    return data


async def _search_site(base: str, keyword: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        data = await _json_response(
            c,
            f"{base}/ajax/anime/search",
            params={"keyword": keyword},
            headers=_ajax_headers(base),
        )
        result = data.get("result") or {}
        html = result.get("html", "") if isinstance(result, dict) else ""
        results = []
        for m in re.finditer(r'href="[^"]*/watch/([^"]+)"', html):
            path = m.group(1).split("/")[0]
            parts = path.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                slug = parts[0]
                show_id = parts[1]
            else:
                slug = path
                show_id = ""
            results.append({"slug": slug, "show_id": show_id, "path": path})
        return results


async def _get_show_id(base: str, slug: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        r = await c.get(f"{base}/watch/{slug}", headers=HEADERS)
        if r.status_code != 200:
            return None
        ids = re.findall(r'data-id=["\'](\d+)', r.text)
        return ids[0] if ids else None


async def _get_episode_list(base: str, show_id: str) -> list[dict]:
    """Legacy Anikoto episode-list parser."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        data = await _json_response(
            c,
            f"{base}/ajax/episode/list/{show_id}",
            headers=_ajax_headers(base),
        )
        html = data.get("result", "")
        eps = []
        nums = re.findall(r'data-num=["\'](\d+)', html)
        ids = re.findall(r'data-ids=["\']([^"\']+)', html)
        for i, value in enumerate(nums):
            eps.append({
                "num": int(value),
                "ids": ids[i] if i < len(ids) else "",
            })
        return eps


async def _get_servers(base: str, server_ids: str, audio: str = "sub") -> list[dict]:
    """Legacy Anikoto server-list parser."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        data = await _json_response(
            c,
            f"{base}/ajax/server/list",
            params={"servers": server_ids},
            headers=_ajax_headers(base),
        )
        html = data.get("result", "")
        servers = []
        sections = re.split(r'<div class="type" data-type="(sub|dub)">', html)
        in_target = False
        for piece in sections:
            if piece in ("sub", "dub"):
                in_target = piece == audio
            elif in_target:
                for m in re.finditer(r'data-link-id="([^"]+)"', piece):
                    servers.append({"link_id": m.group(1)})
                break
        return servers


async def _resolve_server(base: str, link_id: str) -> Optional[str]:
    """Legacy Anikoto source resolver."""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        data = await _json_response(
            c,
            f"{base}/ajax/server",
            params={"get": link_id},
            headers=_ajax_headers(base),
        )
        result = data.get("result") or {}
        return result.get("url", "") if isinstance(result, dict) else ""


async def _scrape_site(base: str, site_name: str, anilist_id: int, episode: int, dub: bool = False) -> StreamResult:
    """Legacy flow retained for Anikoto."""
    title = await _get_anilist_title(anilist_id)
    if not title:
        raise ParserError(f"{site_name}: Could not resolve AniList title")

    results = await _search_site(base, title)
    if not results:
        raise ParserError(f"{site_name}: No results for '{title}'")

    slug = results[0]["slug"]
    show_id = results[0].get("show_id", "")
    if not show_id:
        show_id = await _get_show_id(base, results[0]["path"])
    if not show_id:
        raise ParserError(f"{site_name}: Could not get show ID for {slug}")

    ep_list = await _get_episode_list(base, show_id)
    target = next((e for e in ep_list if e["num"] == episode), None)
    if not target or not target["ids"]:
        raise ParserError(f"{site_name}: Episode {episode} not found")

    audio = "dub" if dub else "sub"
    servers = await _get_servers(base, target["ids"], audio=audio)
    if not servers:
        raise ParserError(f"{site_name}: No servers available")

    video_url = ""
    for server in servers:
        video_url = await _resolve_server(base, server["link_id"])
        if video_url:
            break

    if not video_url:
        raise ParserError(f"{site_name}: Could not resolve any server")

    fmt = "embed" if any(d in video_url for d in ["vidtube", "megaplay", "vidwish"]) else ("hls" if ".m3u8" in video_url else "mp4")
    return StreamResult(
        url=video_url.replace("\\/", "/"),
        source=site_name,
        format=fmt,
        headers={"Referer": f"{base}/", "Origin": base},
    )


async def _aniwaves_episode_numbers(base: str, series_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        data = await _json_response(
            c,
            f"{base}/ajax/episode/list/{series_id}",
            headers=_ajax_headers(base),
        )
    html = data.get("result", "")
    rows = []
    pattern = re.compile(
        r'<a[^>]*data-num=["\'](\d+)["\'][^>]*data-sub=["\'](\d*)["\'][^>]*data-dub=["\'](\d*)["\'][^>]*>',
        re.S,
    )
    for number, has_sub, has_dub in pattern.findall(html):
        rows.append({
            "num": int(number),
            "has_sub": has_sub == "1",
            "has_dub": has_dub == "1",
        })
    return rows


async def _aniwaves_servers(
    base: str,
    series_id: str,
    episode: int,
    audio: str,
    referer: str,
) -> list[dict]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        data = await _json_response(
            c,
            f"{base}/ajax/server/list",
            params={"servers": series_id, "eps": episode},
            headers=_ajax_headers(base, referer),
        )
    if data.get("status") != 200:
        return []
    html = data.get("result", "")
    blocks = re.findall(
        r'<div class="type" data-type="([a-z]+)">.*?<ul>(.*?)</ul>',
        html,
        re.S,
    )
    for kind, block in blocks:
        if kind != audio:
            continue
        return [
            {"server_id": server_id, "link_id": link_id, "name": name.strip() or "Server"}
            for server_id, link_id, name in re.findall(
                r'<li[^>]*data-sv-id="(\d+)"[^>]*data-link-id="([^"]+)"[^>]*>([^<]*)</li>',
                block,
            )
        ]
    return []


async def _aniwaves_resolve(base: str, link_id: str, referer: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
        data = await _json_response(
            c,
            f"{base}/ajax/sources",
            params={"id": link_id, "asi": 0, "autoPlay": 1},
            headers=_ajax_headers(base, referer),
        )
    if data.get("status") != 200:
        return None
    result = data.get("result") or {}
    return result.get("url", "") if isinstance(result, dict) else ""


async def _scrape_aniwaves(anilist_id: int, episode: int, dub: bool = False) -> StreamResult:
    base = SITES["aniwaves"]["base"]
    title = await _get_anilist_title(anilist_id)
    if not title:
        raise ParserError("aniwaves: Could not resolve AniList title")

    results = await _search_site(base, title)
    if not results:
        raise ParserError(f"aniwaves: No results for '{title}'")

    match = next((item for item in results if item.get("show_id")), None)
    if not match:
        raise ParserError("aniwaves: Search result did not contain a numeric series ID")

    series_id = match["show_id"]
    watch_path = match["path"]
    referer = f"{base}/watch/{watch_path}/ep-{episode}"

    episode_rows = await _aniwaves_episode_numbers(base, series_id)
    target = next((item for item in episode_rows if item["num"] == episode), None)
    if not target:
        raise ParserError(f"aniwaves: Episode {episode} not found")

    audio = "dub" if dub else "sub"
    if audio == "dub" and not target["has_dub"]:
        raise ParserError(f"aniwaves: Episode {episode} has no dub")
    if audio == "sub" and not target["has_sub"]:
        raise ParserError(f"aniwaves: Episode {episode} has no sub")

    servers = await _aniwaves_servers(base, series_id, episode, audio, referer)
    if not servers:
        raise ParserError(f"aniwaves: No {audio} servers available for episode {episode}")

    for server in servers:
        video_url = await _aniwaves_resolve(base, server["link_id"], referer)
        if not video_url:
            continue
        video_url = video_url.replace("\\/", "/")
        lower = video_url.lower()
        fmt = "hls" if ".m3u8" in lower else "mp4" if ".mp4" in lower else "embed"
        return StreamResult(
            url=video_url,
            source=f"aniwaves/{server['name']}",
            format=fmt,
            headers={"Referer": f"{base}/", "Origin": base},
        )

    raise ParserError("aniwaves: Could not resolve any server")


async def get_anikoto_stream(anilist_id: int, episode: int, dub: bool = False) -> StreamResult:
    return await _scrape_site(SITES["anikoto"]["base"], "anikoto", anilist_id, episode, dub)


async def get_aniwaves_stream(anilist_id: int, episode: int, dub: bool = False) -> StreamResult:
    return await _scrape_aniwaves(anilist_id, episode, dub)
