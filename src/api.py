# path: src/api.py
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.config import settings
from src.orchestrator import Orchestrator
from src.middleware.error_handler import setup_error_handlers
from src.middleware.rate_limit import RateLimitMiddleware, rate_limiter
from src.models.stream import (
    StreamResult,
    ValidationError,
    AllSourcesExhaustedError,
    FallbackAttempt,
)
from src.utils.logger import setup_logging, get_logger
from src.utils.anilist import (
    get_anime_metadata,
    search_anime,
    get_episodes,
    get_trending,
    get_popular,
    get_newest,
    get_recent,
    get_top_rated,
)

setup_logging(log_level=settings.log_level, json_output=settings.json_logging)
log = get_logger(__name__)

orchestrator = Orchestrator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "Starting Anime Stream Scraper API",
        version=settings.app_version,
        max_browsers=settings.max_browsers,
    )
    await orchestrator.initialize()
    yield
    log.info("Shutting down API")
    await orchestrator.shutdown()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url=f"{settings.api_prefix}/docs",
    openapi_url=f"{settings.api_prefix}/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

setup_error_handlers(app)

ratelimit_middleware = RateLimitMiddleware(app, limit_per_minute=settings.rate_limit_per_minute)


@app.get(f"{settings.api_prefix}/stream")
@rate_limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def get_stream(
    request: Request,
    anime_id: int = Query(..., description="AniList anime ID", ge=1),
    episode: int = Query(..., description="Episode number (1-indexed)", ge=1),
    dub: bool = Query(False, description="Prefer dubbed version"),
    quality: Optional[str] = Query(None, description="Preferred quality (e.g., 720p, 1080p)"),
    provider: Optional[str] = Query(None, description="Specific Miruro provider (ally,pewe,bonk,kiwi,bee) or comma-separated priority list"),
    source: Optional[str] = Query(None, description="Source backend: miruro, reanime, or playwright"),
):
    start_time = time.monotonic()

    log.info(
        "Stream request received",
        anime_id=anime_id,
        episode=episode,
        dub=dub,
        quality=quality,
        provider=provider,
        source=source,
        ip=request.client.host if request.client else "unknown",
    )

    result = await orchestrator.get_stream(
        anime_id=anime_id,
        episode=episode,
        dub=dub,
        quality=quality,
        provider=provider,
        source=source,
    )

    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)

    log.info(
        "Stream response sent",
        anime_id=anime_id,
        episode=episode,
        source=result.source,
        response_time_ms=response_time_ms,
        cached=False,
    )

    return {
        "status": "success",
        "data": {
            "video_url": result.url,
            "format": result.format,
            "source": result.source,
            "subtitles": result.subtitles or [],
            "thumbnails": result.thumbnails,
            "headers": result.headers or {},
            "fallback_used": result.fallback_used,
            "fallback_attempts": (
                [
                    {"source": a.source, "error": a.error, "latency_ms": a.latency_ms}
                    for a in result.fallback_attempts
                ]
                if result.fallback_attempts
                else []
            ),
        },
        "meta": {
            "response_time_ms": response_time_ms,
            "cached": result.from_cache,
        },
    }


@app.get(f"{settings.api_prefix}/health")
async def health_check(request: Request):
    health_data = await orchestrator.get_health()
    return {
        "status": "ok" if health_data.get("status") == "healthy" else "degraded",
        "version": settings.app_version,
        "sources": health_data.get("sources", {}),
        "fallback_manager": health_data.get("fallback_manager", {}),
        "cache_enabled": health_data.get("cache_enabled", False),
    }


@app.get(f"{settings.api_prefix}/search")
@rate_limiter.limit("60/minute")
async def search_anime_endpoint(
    request: Request,
    q: str = Query(..., min_length=2, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
):
    start_time = time.monotonic()
    log.info("Search request", query=q, ip=request.client.host if request.client else "unknown")

    results = await search_anime(q, page)

    if not results:
        results = await orchestrator.search(q)

    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)

    return {
        "status": "success",
        "data": results,
        "meta": {
            "query": q,
            "count": len(results),
            "response_time_ms": response_time_ms,
        },
    }


@app.get(f"{settings.api_prefix}/anime/{{anime_id}}")
@rate_limiter.limit("60/minute")
async def get_anime_meta(
    request: Request,
    anime_id: int = Path(..., description="AniList anime ID", ge=1),
):
    start_time = time.monotonic()
    log.info("Metadata request", anime_id=anime_id)

    data = await get_anime_metadata(anime_id)
    if not data:
        return JSONResponse(status_code=404, content={
            "status": "error",
            "error": {"code": "ANIME_NOT_FOUND", "message": f"Anime ID {anime_id} not found"},
        })

    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {
        "status": "success",
        "data": data,
        "meta": {"response_time_ms": response_time_ms},
    }


@app.get(f"{settings.api_prefix}/anime/{{anime_id}}/episodes")
@rate_limiter.limit("60/minute")
async def get_anime_episodes(
    request: Request,
    anime_id: int = Path(..., description="AniList anime ID", ge=1),
):
    start_time = time.monotonic()
    log.info("Episodes request", anime_id=anime_id)

    data = await get_episodes(anime_id)
    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)

    return {
        "status": "success",
        "data": data,
        "meta": {"response_time_ms": response_time_ms},
    }


@app.get(f"{settings.api_prefix}/trending")
@rate_limiter.limit("60/minute")
async def trending_anime(
    request: Request,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=50, description="Results per page"),
):
    start_time = time.monotonic()
    data = await get_trending(page, per_page)
    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {
        "status": "success",
        "data": data,
        "meta": {"page": page, "count": len(data), "response_time_ms": response_time_ms},
    }


@app.get(f"{settings.api_prefix}/newest")
@rate_limiter.limit("60/minute")
async def newest_anime(
    request: Request,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=50, description="Results per page"),
):
    start_time = time.monotonic()
    data = await get_newest(page, per_page)
    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {
        "status": "success",
        "data": data,
        "meta": {"page": page, "count": len(data), "response_time_ms": response_time_ms},
    }


@app.get(f"{settings.api_prefix}/recent")
@rate_limiter.limit("60/minute")
async def recent_anime(
    request: Request,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=50, description="Results per page"),
):
    start_time = time.monotonic()
    data = await get_recent(page, per_page)
    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {
        "status": "success",
        "data": data,
        "meta": {"page": page, "count": len(data), "response_time_ms": response_time_ms},
    }


@app.get(f"{settings.api_prefix}/top-rated")
@rate_limiter.limit("60/minute")
async def top_rated_anime(
    request: Request,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=50, description="Results per page"),
):
    start_time = time.monotonic()
    data = await get_top_rated(page, per_page)
    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {
        "status": "success",
        "data": data,
        "meta": {"page": page, "count": len(data), "response_time_ms": response_time_ms},
    }


@app.get(f"{settings.api_prefix}/popular")
@rate_limiter.limit("60/minute")
async def popular_anime(
    request: Request,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=50, description="Results per page"),
):
    start_time = time.monotonic()
    data = await get_popular(page, per_page)
    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {
        "status": "success",
        "data": data,
        "meta": {"page": page, "count": len(data), "response_time_ms": response_time_ms},
    }


@app.get(f"{settings.api_prefix}/anime/{{anime_id}}/providers")
@rate_limiter.limit("60/minute")
async def get_providers(
    request: Request,
    anime_id: int = Path(..., description="AniList anime ID", ge=1),
):
    start_time = time.monotonic()
    log.info("Providers request", anime_id=anime_id)

    try:
        from consumet_api.miruro_pipe import MiruroPipe
        mp = MiruroPipe()
        providers = await mp.get_providers_for_anime(anime_id)
        response_time_ms = round((time.monotonic() - start_time) * 1000, 1)
        return {
            "status": "success",
            "data": {
                "anime_id": anime_id,
                "providers": providers,
            },
            "meta": {"response_time_ms": response_time_ms},
        }
    except Exception as e:
        return JSONResponse(status_code=404, content={
            "status": "error",
            "error": {"code": "PROVIDERS_NOT_FOUND", "message": str(e)},
        })


@app.get(f"{settings.api_prefix}/anime/{{anime_id}}/sources")
@rate_limiter.limit("60/minute")
async def get_sources(
    request: Request,
    anime_id: int = Path(..., description="AniList anime ID", ge=1),
):
    start_time = time.monotonic()
    log.info("Sources request", anime_id=anime_id)

    sub = False
    dub = False
    sources = []

    # Check Miruro pipe
    try:
        from consumet_api.miruro_pipe import MiruroPipe
        mp = MiruroPipe()
        providers = await mp.get_providers_for_anime(anime_id)
        has_sub = any(p.get("episodes", {}).get("sub", 0) > 0 for p in providers)
        has_dub = any(p.get("episodes", {}).get("dub", 0) > 0 for p in providers)
        if has_sub:
            sub = True
            sources.append({"name": "miruro", "type": "sub", "providers": [p["name"] for p in providers if p.get("episodes", {}).get("sub", 0) > 0]})
        if has_dub:
            dub = True
            sources.append({"name": "miruro", "type": "dub", "providers": [p["name"] for p in providers if p.get("episodes", {}).get("dub", 0) > 0]})
    except Exception:
        pass

    # Check Anikoto
    try:
        from src.adapters.anikoto_scraper import _get_anilist_title, _search_anikoto, _get_show_id, _get_episode_list
        title = await _get_anilist_title(anime_id)
        if title:
            results = await _search_anikoto(title)
            if results:
                show_id = await _get_show_id(results[0]["slug"])
                if show_id:
                    ep_list = await _get_episode_list(show_id)
                    if ep_list:
                        sub = True
                        sources.append({"name": "anikoto", "type": "sub"})
                        # Check if any episode has dub servers by looking at the first ep's server list
                        from src.adapters.anikoto_scraper import _get_servers
                        servers = await _get_servers(ep_list[0]["ids"], audio="dub")
                        if servers:
                            dub = True
                            sources.append({"name": "anikoto", "type": "dub"})
    except Exception:
        pass

    response_time_ms = round((time.monotonic() - start_time) * 1000, 1)
    return {
        "status": "success",
        "data": {
            "anime_id": anime_id,
            "sub_available": sub,
            "dub_available": dub,
            "sources": sources,
        },
        "meta": {"response_time_ms": response_time_ms},
    }


@app.get(f"{settings.api_prefix}/proxy/hls")
@rate_limiter.limit("120/minute")
async def proxy_hls(
    request: Request,
    url: str = Query(..., description="HLS URL to proxy"),
    referer: str = Query("https://www.miruro.tv/", description="Referer header"),
    origin: str = Query("https://www.miruro.tv", description="Origin header"),
):
    """Proxy HLS segments through our server, adding required Referer/Origin headers."""
    import httpx
    from fastapi.responses import Response
    
    if not url.startswith("http"):
        from src.models.stream import ValidationError
        raise ValidationError("Invalid URL")
    
    headers = {
        "Referer": referer,
        "Origin": origin,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
    }
    
    # Detect scheme from forwarded headers (nginx proxy sets these)
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    
    # Route through Cloudflare Worker if configured
    worker_url = settings.cloudflare_worker_url
    if worker_url:
        worker_url = worker_url.rstrip("/")
        params = urllib.parse.urlencode({"url": url, "referer": referer, "origin": origin})
        cf_proxy = f"{worker_url}?{params}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(cf_proxy, follow_redirects=True)
                content_type = resp.headers.get("content-type", "application/octet-stream")
                return Response(content=resp.content, media_type=content_type)
        except Exception as e:
            log.warning("Cloudflare Worker proxy failed, falling back to direct", error=str(e))

    proxy_url = settings.get_proxy()
    async with httpx.AsyncClient(timeout=30, proxy=proxy_url) as client:
        try:
            resp = await client.get(url, headers=headers, follow_redirects=True)
            content_type = resp.headers.get("content-type", "application/octet-stream")
            
            # If it's a playlist, rewrite URLs to go through our proxy
            if "m3u8" in content_type or url.endswith(".m3u8"):
                text = resp.text
                base_url = url.rsplit("/", 1)[0] + "/"
                proxy_base = f"{scheme}://{request.url.netloc}{settings.api_prefix}/proxy/hls"
                
                lines = []
                for line in text.split("\n"):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and not stripped.startswith("http"):
                        full_url = base_url + stripped
                        params = urllib.parse.urlencode({"url": full_url, "referer": referer, "origin": origin})
                        lines.append(f"{proxy_base}?{params}")
                    elif stripped.startswith("http"):
                        params = urllib.parse.urlencode({"url": stripped, "referer": referer, "origin": origin})
                        lines.append(f"{proxy_base}?{params}")
                    else:
                        lines.append(line)
                
                return Response(content="\n".join(lines), media_type=content_type)
            
            return Response(content=resp.content, media_type=content_type)
        except Exception as e:
            return Response(content=str(e), status_code=502)

@app.get(f"{settings.api_prefix}/proxy/status")
async def proxy_status():
    from src.proxy_pool import get_proxy_pool
    pool = await get_proxy_pool()
    config_proxy = settings.get_proxy() or None
    speedx_counts = {"http": 0, "socks4": 0, "socks5": 0}
    for node in pool.nodes:
        proto = node.protocol
        if proto in speedx_counts:
            speedx_counts[proto] += 1
    return {
        "brightdata_configured": bool(settings.brightdata_username and settings.brightdata_password),
        "proxy_list_size": len(settings.proxy_list),
        "proxy_pool_nodes": len(pool.nodes),
        "active_proxy": config_proxy or pool.get_proxy_url(),
        "speedx_enabled": True,
        "speedx_sources": speedx_counts,
    }

@app.post(f"{settings.api_prefix}/fallback/reset")
async def reset_fallback_state(source: Optional[str] = Query(None, description="Source name to reset (omit for all)")):
    await orchestrator.reset_fallback(source)
    return {
        "status": "success",
        "message": f"Fallback state reset for {'all sources' if not source else source}",
    }

# KitsuneArc MAL-native compatibility routes
from src.kitsunearc_api import router as kitsunearc_router, bind_orchestrator as bind_kitsunearc_orchestrator
bind_kitsunearc_orchestrator(orchestrator)
app.include_router(kitsunearc_router)
