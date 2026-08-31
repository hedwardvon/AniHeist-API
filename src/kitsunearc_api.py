# path: src/kitsunearc_api.py
from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

import src.adapters as adapters_package
from src.adapters.base import BaseAdapter
from src.models.stream import StreamResult
from src.utils.anilist import get_episodes
from src.utils.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/kitsunearc", tags=["KitsuneArc"])
ANILIST_API = "https://graphql.anilist.co"
TIMEOUT = 15
_orchestrator = None
_candidate_cache: dict[str, "AdapterCandidate"] | None = None


@dataclass
class AdapterCandidate:
    candidate_id: str
    label: str
    resolve: Callable[[int, int, bool, Optional[str]], Awaitable[StreamResult]]


def bind_orchestrator(orchestrator) -> None:
    global _orchestrator, _candidate_cache
    _orchestrator = orchestrator
    _candidate_cache = None


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


def _display_label(value: str) -> str:
    parts = [part for part in value.replace("-", "_").split("_") if part]
    return " ".join(part.capitalize() for part in parts) or "Provider"


def _class_candidate_id(module_name: str, cls: type[BaseAdapter]) -> str:
    stem = module_name.rsplit(".", 1)[-1].strip().lower()
    if stem:
        return stem
    name = cls.__name__
    if name.lower().endswith("adapter"):
        name = name[:-7]
    return name.lower()


def _constructor_kwargs(cls: type[BaseAdapter]) -> dict[str, Any] | None:
    if _orchestrator is None:
        return None
    kwargs: dict[str, Any] = {}
    try:
        signature = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return None
    for name, parameter in signature.parameters.items():
        if name == "self" or parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if name == "browser_pool" and hasattr(_orchestrator, "browser_pool"):
            kwargs[name] = _orchestrator.browser_pool
            continue
        if name == "http_pool" and hasattr(_orchestrator, "http_pool"):
            kwargs[name] = _orchestrator.http_pool
            continue
        if parameter.default is inspect.Parameter.empty:
            return None
    return kwargs


def _class_resolver(adapter: BaseAdapter):
    async def resolve(anilist_id: int, episode: int, dub: bool, quality: Optional[str]) -> StreamResult:
        kwargs: dict[str, Any] = {"dub": dub}
        if quality:
            kwargs["quality"] = quality
        return await adapter.get_video_url(str(anilist_id), episode, **kwargs)

    return resolve


def _function_resolver(function):
    async def resolve(anilist_id: int, episode: int, dub: bool, quality: Optional[str]) -> StreamResult:
        kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            signature = None
        if signature and "dub" in signature.parameters:
            kwargs["dub"] = dub
        if signature and quality and "quality" in signature.parameters:
            kwargs["quality"] = quality
        return await function(anilist_id, episode, **kwargs)

    return resolve


def _discover_candidates() -> dict[str, AdapterCandidate]:
    global _candidate_cache
    if _candidate_cache is not None:
        return _candidate_cache

    discovered: dict[str, AdapterCandidate] = {}
    for module_info in pkgutil.iter_modules(adapters_package.__path__):
        if module_info.name.startswith("_") or module_info.name == "base":
            continue
        module_name = f"{adapters_package.__name__}.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            log.warning("KitsuneArc adapter import failed", adapter=module_info.name, error=str(exc))
            continue
        if getattr(module, "KITSUNEARC_EXPOSE", True) is False:
            continue

        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls is BaseAdapter or cls.__module__ != module.__name__:
                continue
            try:
                is_adapter = issubclass(cls, BaseAdapter)
            except TypeError:
                is_adapter = False
            if not is_adapter or inspect.isabstract(cls):
                continue
            kwargs = _constructor_kwargs(cls)
            if kwargs is None:
                continue
            try:
                instance = cls(**kwargs)
            except Exception as exc:
                log.warning(
                    "KitsuneArc adapter initialization failed",
                    adapter=cls.__name__,
                    error=str(exc),
                )
                continue
            candidate_id = _class_candidate_id(module.__name__, cls)
            discovered.setdefault(
                candidate_id,
                AdapterCandidate(
                    candidate_id=candidate_id,
                    label=_display_label(candidate_id),
                    resolve=_class_resolver(instance),
                ),
            )

        for name, function in inspect.getmembers(module, inspect.iscoroutinefunction):
            if function.__module__ != module.__name__:
                continue
            if not (name.startswith("get_") and name.endswith("_stream")):
                continue
            candidate_id = name[4:-7].strip("_").lower()
            if not candidate_id:
                continue
            discovered.setdefault(
                candidate_id,
                AdapterCandidate(
                    candidate_id=candidate_id,
                    label=_display_label(candidate_id),
                    resolve=_function_resolver(function),
                ),
            )

    _candidate_cache = dict(sorted(discovered.items()))
    log.info("KitsuneArc adapters discovered", adapters=list(_candidate_cache))
    return _candidate_cache


@router.get("/identity")
async def kitsunearc_identity(mal_id: int = Query(..., ge=1)):
    media = await _mal_to_anilist(mal_id)
    if not media:
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "error": {
                    "code": "MAL_ID_NOT_FOUND",
                    "message": f"No AniList mapping found for MAL ID {mal_id}",
                },
            },
        )
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
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "error": {
                    "code": "MAL_ID_NOT_FOUND",
                    "message": f"No AniList mapping found for MAL ID {mal_id}",
                },
            },
        )
    anilist_id = int(media["id"])
    episode_data = await get_episodes(anilist_id)
    episode_numbers = [
        row.get("number")
        for row in episode_data.get("episodes") or []
        if isinstance(row.get("number"), int) and row.get("number") > 0
    ]

    providers: dict[str, dict[str, Any]] = {}
    for candidate_id, candidate in _discover_candidates().items():
        providers[candidate_id] = {
            "name": candidate.label,
            "episodes": {
                "sub": [
                    {"number": number, "id": candidate_id}
                    for number in episode_numbers
                ],
                "dub": [
                    {"number": number, "id": candidate_id}
                    for number in episode_numbers
                ],
            },
        }

    return {
        "mal_id": mal_id,
        "provider_anime_id": str(anilist_id),
        "providers": providers,
    }


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
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": {
                    "code": "ORCHESTRATOR_NOT_BOUND",
                    "message": "AniHeist orchestrator is not available",
                },
            },
        )
    media = await _mal_to_anilist(mal_id)
    if not media:
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "error": {
                    "code": "MAL_ID_NOT_FOUND",
                    "message": f"No AniList mapping found for MAL ID {mal_id}",
                },
            },
        )
    anilist_id = int(media["id"])

    try:
        if source:
            candidate = _discover_candidates().get(source.lower())
            if candidate is None:
                return JSONResponse(
                    status_code=404,
                    content={
                        "status": "error",
                        "error": {
                            "code": "ADAPTER_NOT_FOUND",
                            "message": f"AniHeist adapter '{source}' is not available",
                        },
                    },
                )
            result = await candidate.resolve(
                anilist_id,
                episode,
                track == "dub",
                quality,
            )
        else:
            result = await _orchestrator.get_stream(
                anime_id=anilist_id,
                episode=episode,
                dub=(track == "dub"),
                quality=quality,
                provider=provider,
                source=None,
            )
    except Exception as exc:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 502),
            content={
                "status": "error",
                "error": {"code": exc.__class__.__name__, "message": str(exc)},
                "mal_id": mal_id,
                "provider_anime_id": str(anilist_id),
            },
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
        "adapters": [
            {"id": item.candidate_id, "name": item.label}
            for item in _discover_candidates().values()
        ],
    }
