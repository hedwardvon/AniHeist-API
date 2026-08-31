# path: src/kitsunearc_api.py
from typing import Optional
import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from src.utils.anilist import get_episodes
from src.utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/kitsunearc", tags=["KitsuneArc"])
ANILIST_API = "https://graphql.anilist.co"
TIMEOUT = 15
_orchestrator = None

def bind_orchestrator(orchestrator) -> None:
    global _orchestrator
    _orchestrator = orchestrator

MAL_LOOKUP_QUERY = """
query ($malId: Int) {
  Media(idMal: $malId, type: ANIME) {
    id
    idMal
    episodes
    status
    title { romaji english }
  }
}
"""

async def _mal_to_anilist(mal_id: int) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(
                ANILIST_API,
                json={"query": MAL_LOOKUP_QUERY, "variables": {"malId": mal_id}},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            if response.status_code != 200:
                return None
            return (response.json().get("data") or {}).get("Media")
    except Exception as exc:
        log.warning("MAL to AniList lookup failed", mal_id=mal_id, error=str(exc))
        return None

@router.get("/identity")
async def kitsunearc_identity(mal_id: int = Query(..., ge=1)):
    media = await _mal_to_anilist(mal_id)
    if not media:
        return JSONResponse(status_code=404, content={"status":"error","error":{"code":"MAL_ID_NOT_FOUND","message":f"No AniList mapping found for MAL ID {mal_id}"}})
    title = media.get("title") or {}
    return {
        "mal_id": mal_id,
        "provider_anime_id": str(media["id"]),
        "anilist_id": media["id"],
        "title": title.get("english") or title.get("romaji"),
    }

@router.get("/episodes")
async def kitsunearc_episodes(mal_id: int = Query(..., ge=1)):
    media = await _mal_to_anilist(mal_id)
    if not media:
        return JSONResponse(status_code=404, content={"status":"error","error":{"code":"MAL_ID_NOT_FOUND","message":f"No AniList mapping found for MAL ID {mal_id}"}})
    anilist_id = int(media["id"])
    episode_data = await get_episodes(anilist_id)
    episodes = []
    for row in episode_data.get("episodes") or []:
        number = row.get("number")
        if isinstance(number, int) and number > 0:
            episodes.append({"number": number, "track": "sub"})
            episodes.append({"number": number, "track": "dub"})
    return {"mal_id": mal_id, "provider_anime_id": str(anilist_id), "episodes": episodes}

@router.get("/watch")
async def kitsunearc_watch(
    mal_id: int = Query(..., ge=1),
    episode: int = Query(..., ge=1),
    track: str = Query("sub", pattern="^(sub|dub)$"),
    provider: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    quality: Optional[str] = Query(None),
):
    if _orchestrator is None:
        return JSONResponse(status_code=503, content={"status":"error","error":{"code":"ORCHESTRATOR_NOT_BOUND","message":"AniHeist orchestrator is not available"}})
    media = await _mal_to_anilist(mal_id)
    if not media:
        return JSONResponse(status_code=404, content={"status":"error","error":{"code":"MAL_ID_NOT_FOUND","message":f"No AniList mapping found for MAL ID {mal_id}"}})
    anilist_id = int(media["id"])
    try:
        result = await _orchestrator.get_stream(
            anime_id=anilist_id,
            episode=episode,
            dub=(track == "dub"),
            quality=quality,
            provider=provider,
            source=source,
        )
    except Exception as exc:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 502),
            content={"status":"error","error":{"code":exc.__class__.__name__,"message":str(exc)},"mal_id":mal_id,"provider_anime_id":str(anilist_id)},
        )
    result_format = str(result.format).lower()

    if result_format in {"hls", "m3u8"}:
        stream_type = "hls"
    elif result_format == "embed":
        stream_type = "embed"
    else:
        stream_type = "direct"

    item = {
        "type": stream_type,
        "url": result.url,
        "track": track,
        "source": result.source,
    }

    if result.headers:
        item["headers"] = result.headers

    return {
        "mal_id": mal_id,
        "provider_anime_id": str(anilist_id),
        "sources": [item],
        "subtitles": result.subtitles or [],
    }


@router.get("/health")
async def kitsunearc_health():
    return {
        "status": "healthy",
        "service": "aniheist",
        "contract": "kitsunearc-generic-resolver",
    }