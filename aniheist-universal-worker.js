// ===== ANIHEIST UNIVERSAL SCRAPER WORKER =====
// Single-file Cloudflare Worker — 7+ providers, auto-fallback
// Deploy: Copy this entire file into Cloudflare Dashboard → Workers → Create Worker
//
// Endpoints:
//   /search?q=Death+Note          — Search all providers
//   /episodes/1535                 — Get episode list from Miruro pipe
//   /watch/{provider}/{id}/{ep}    — Get stream URL from specific provider
//   /watch/miruro/1535/1           — Miruro pipe (auto fallback)
//   /watch/anikoto/1535/1          — Anikoto (vidtube embed)
//   /watch/aniwaves/1535/1         — Aniwaves (same engine)
//   /watch/pahe/1535/1             — AnimePahe
//   /watch/rea/1535/1              — ReAnime
//   /health                        — Status check

const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Content-Type": "application/json" };
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36";

// ===== UTILITY FUNCTIONS =====
function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), { status, headers: CORS });
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function btoa64(s) { return btoa(encodeURIComponent(s)).replace(/=+$/, ""); }

// ===== 1. MIRURO PIPE PROVIDER =====
const MIRURO_DOMAINS = [
  "https://www.miruro.tv", "https://www.miruro.to",
  "https://www.miruro.bz", "https://www.miruro.ru",
];
const MIRURO_RANKING = ["zoro", "bee", "telli", "arc", "yugen", "jet", "neo", "kiwi", "hop", "ally", "pewe", "moo", "bonk"];

async function tryPipe(encodedReq) {
  for (const base of MIRURO_DOMAINS) {
    try {
      const res = await fetch(`${base}/api/secure/pipe?e=${encodedReq}`, {
        headers: { "User-Agent": UA, Referer: `${base}/`, Accept: "*/*" },
      });
      if (!res.ok) continue;
      const text = await res.text();
      // Try gzip decompression (Cloudflare Workers have DecompressionStream)
      try {
        const ds = new DecompressionStream("gzip");
        const writer = ds.writable.getWriter();
        writer.write(new Uint8Array(await new Response(text).arrayBuffer()));
        writer.close();
        const reader = ds.readable.getReader();
        const chunks = [];
        while (true) { const { done, value } = await reader.read(); if (done) break; chunks.push(value); }
        const total = chunks.reduce((a, c) => a + c.length, 0);
        const merged = new Uint8Array(total);
        let offset = 0;
        for (const c of chunks) { merged.set(c, offset); offset += c.length; }
        return JSON.parse(new TextDecoder().decode(merged));
      } catch {
        // Maybe not compressed
        try { return JSON.parse(text); } catch { continue; }
      }
    } catch { continue; }
  }
  return null;
}

function encodePipe(payload) {
  return btoa(JSON.stringify(payload)).replace(/=+$/, "");
}

async function miruroEpisodes(anilistId) {
  const payload = { path: "episodes", method: "GET", query: { anilistId }, body: null, version: "0.1.0" };
  return tryPipe(encodePipe(payload));
}

async function miruroSources(episodeId, provider, category, anilistId) {
  const encId = btoa(episodeId).replace(/=+$/, "");
  const payload = { path: "sources", method: "GET", query: { episodeId: encId, provider, category, anilistId }, body: null, version: "0.1.0" };
  const data = await tryPipe(encodePipe(payload));
  if (!data) throw new Error("Miruro pipe failed");
  // Extract best stream URL
  const sources = Array.isArray(data) ? data : data.sources || [];
  if (sources.length === 0) throw new Error("No Miruro sources");
  const best = sources[0]; // First is usually best
  const url = best.url || best.file || "";
  if (!url) throw new Error("No URL in Miruro response");
  const fmt = url.includes(".m3u8") ? "hls" : url.includes(".mp4") ? "mp4" : "hls";
  return { url, format: fmt, source: `miruro/${provider}`, headers: { Referer: best.referer || "https://allmanga.to/", Origin: best.referer || "https://allmanga.to" } };
}

async function miruroSmartExtract(anilistId, episode) {
  const data = await miruroEpisodes(anilistId);
  if (!data || !data.providers) throw new Error("No Miruro data");
  const providers = data.providers;
  let attempts = [];
  for (const prov of MIRURO_RANKING) {
    if (!providers[prov]) continue;
    const eps = providers[prov].episodes || {};
    for (const cat of ["sub", "dub", "raw"]) {
      if (!eps[cat]) continue;
      for (const ep of (Array.isArray(eps[cat]) ? eps[cat] : [])) {
        if (ep.number === episode) attempts.push({ provider: prov, cat, id: ep.id });
      }
    }
  }
  for (const t of attempts) {
    try { return await miruroSources(t.id, t.provider, t.cat, anilistId); } catch {}
  }
  // Fallback: try all providers for any episode match
  for (const prov of MIRURO_RANKING) {
    if (!providers[prov]) continue;
    const eps = providers[prov].episodes || {};
    for (const cat of ["sub", "dub"]) {
      if (!eps[cat]) continue;
      for (const ep of (Array.isArray(eps[cat]) ? eps[cat] : [])) {
        if (ep.number === episode) {
          try { return await miruroSources(ep.id, prov, cat, anilistId); } catch {}
        }
      }
    }
  }
  throw new Error("Miruro: all providers failed");
}

// ===== 2. ANIKOTO PROVIDER =====
async function anikotoSearch(keyword) {
  const res = await fetch(`https://anikotv.to/ajax/anime/search?keyword=${encodeURIComponent(keyword)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://anikotv.to/" },
  });
  if (!res.ok) return [];
  const data = await res.json();
  const html = data?.result?.html || "";
  const results = [];
  const re = /href="https:\/\/anikotv\.to\/watch\/([^"]+)"[^>]*>\s*([^<]+)/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    results.push({ slug: m[1].split("/")[0], title: m[2].trim() });
  }
  return results;
}

async function anikotoResolve(anilistId, episode) {
  // First get the title from AniList
  const titleRes = await fetch("https://graphql.anilist.co", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ query: "query($id:Int){Media(id:$id,type:ANIME){title{english romaji}}}", variables: { id: anilistId } }),
  });
  const titleData = await titleRes.json();
  const title = titleData?.data?.Media?.title?.english || titleData?.data?.Media?.title?.romaji || "";
  if (!title) throw new Error("Cannot resolve title");

  // Search
  const results = await anikotoSearch(title);
  if (!results.length) throw new Error("No results found");
  const slug = results[0].slug;

  // Get show ID from watch page
  const watchRes = await fetch(`https://anikotv.to/watch/${slug}`, { headers: { "User-Agent": UA } });
  const watchHtml = await watchRes.text();
  const idMatch = watchHtml.match(/data-id=["'](\d+)["']/);
  const showId = idMatch ? idMatch[1] : null;
  if (!showId) throw new Error("No show ID");

  // Get episode list
  const epRes = await fetch(`https://anikotv.to/ajax/episode/list/${showId}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: `https://anikotv.to/watch/${slug}` },
  });
  const epData = await epRes.json();
  const epHtml = epData?.result || "";
  const nums = epHtml.match(/data-num=["'](\d+)["']/g) || [];
  const ids = epHtml.match(/data-ids=["']([^"']+)["']/g) || [];
  let targetIds = "";
  for (let i = 0; i < nums.length; i++) {
    const n = nums[i].match(/data-num=["'](\d+)["']/)[1];
    if (parseInt(n) === episode) {
      targetIds = ids[i].match(/data-ids=["']([^"']+)["']/)[1];
      break;
    }
  }
  if (!targetIds) throw new Error(`Episode ${episode} not found`);

  // Get servers
  const srvRes = await fetch(`https://anikotv.to/ajax/server/list?servers=${encodeURIComponent(targetIds)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://anikotv.to/" },
  });
  const srvData = await srvRes.json();
  const srvHtml = srvData?.result || "";
  const sections = srvHtml.split(/<div class="type" data-type="(sub|dub)">/);
  let linkId = "";
  for (let i = 0; i < sections.length; i++) {
    if (sections[i] === "sub") {
      const ids = sections[i + 1]?.match(/data-link-id="([^"]+)"/);
      if (ids) { linkId = ids[1]; break; }
    }
  }
  if (!linkId) throw new Error("No server found");

  // Resolve server
  const res = await fetch(`https://anikotv.to/ajax/server?get=${encodeURIComponent(linkId)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://anikotv.to/" },
  });
  const data = await res.json();
  const videoUrl = data?.result?.url?.replace(/\\\//g, "/") || "";
  if (!videoUrl) throw new Error("No video URL");
  const fmt = videoUrl.includes("vidtube") || videoUrl.includes("megaplay") || videoUrl.includes("vidwish") ? "embed" : videoUrl.includes(".m3u8") ? "hls" : "mp4";
  return { url: videoUrl, format: fmt, source: "anikoto", headers: { Referer: "https://anikotv.to/", Origin: "https://anikotv.to" } };
}

// ===== 3. ANIWAVES PROVIDER (same engine as anikoto) =====
async function aniwavesResolve(anilistId, episode) {
  // Get title
  const titleRes = await fetch("https://graphql.anilist.co", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ query: "query($id:Int){Media(id:$id,type:ANIME){title{english romaji}}}", variables: { id: anilistId } }),
  });
  const titleData = await titleRes.json();
  const title = titleData?.data?.Media?.title?.english || titleData?.data?.Media?.title?.romaji || "";
  if (!title) throw new Error("Cannot resolve title");

  // Search on aniwaves
  const searchRes = await fetch(`https://aniwaves.ru/ajax/anime/search?keyword=${encodeURIComponent(title)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://aniwaves.ru/" },
  });
  const searchData = await searchRes.json();
  const searchHtml = searchData?.result?.html || "";
  const pathMatch = searchHtml.match(/href="[^"]*\/watch\/([^"]+)"/);
  if (!pathMatch) throw new Error("No results on aniwaves");
  const path = pathMatch[1].split("/")[0];
  const parts = path.rsplit("-", 1);
  const showId = parts[1]?.match(/^\d+$/) ? parts[1] : null;
  if (!showId) throw new Error("No show ID");

  // Episode list
  const epRes = await fetch(`https://aniwaves.ru/ajax/episode/list/${showId}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://aniwaves.ru/" },
  });
  const epData = await epRes.json();
  const epHtml = epData?.result || "";
  const ids = epHtml.match(/data-ids=["']([^"']+)["']/g) || [];
  const nums = epHtml.match(/data-num=["'](\d+)["']/g) || [];
  let targetIds = "";
  for (let i = 0; i < nums.length; i++) {
    const n = nums[i].match(/data-num=["'](\d+)["']/)[1];
    if (parseInt(n) === episode) {
      targetIds = ids[i].match(/data-ids=["']([^"']+)["']/)[1];
      break;
    }
  }
  if (!targetIds) throw new Error(`Episode ${episode} not found`);

  // Servers
  const srvRes = await fetch(`https://aniwaves.ru/ajax/server/list?servers=${encodeURIComponent(targetIds)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://aniwaves.ru/" },
  });
  const srvData = await srvRes.json();
  const srvHtml = srvData?.result || "";
  const sections = srvHtml.split(/<div class="type" data-type="(sub|dub)">/);
  let linkId = "";
  for (let i = 0; i < sections.length; i++) {
    if (sections[i] === "sub") {
      const ids = sections[i + 1]?.match(/data-link-id="([^"]+)"/);
      if (ids) { linkId = ids[1]; break; }
    }
  }
  if (!linkId) throw new Error("No server");

  const res = await fetch(`https://aniwaves.ru/ajax/server?get=${encodeURIComponent(linkId)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://aniwaves.ru/" },
  });
  const vidData = await res.json();
  const videoUrl = vidData?.result?.url?.replace(/\\\//g, "/") || "";
  if (!videoUrl) throw new Error("No URL");
  const fmt = videoUrl.includes("vidtube") || videoUrl.includes("megaplay") || videoUrl.includes("vidwish") ? "embed" : videoUrl.includes(".m3u8") ? "hls" : "mp4";
  return { url: videoUrl, format: fmt, source: "aniwaves", headers: { Referer: "https://aniwaves.ru/", Origin: "https://aniwaves.ru" } };
}

// ===== 4. ANIMEKIUH (kuhi provider from scraper-v2) =====
async function kuhiResolve(anilistId, episode, dub) {
  // Uses same Miruro backend
  return miruroSmartExtract(anilistId, episode);
}

// ===== 5. ANIMEPAHE PROVIDER =====
async function paheResolve(anilistId, episode) {
  const titleRes = await fetch("https://graphql.anilist.co", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ query: "query($id:Int){Media(id:$id,type:ANIME){title{english romaji}}}", variables: { id: anilistId } }),
  });
  const titleData = await titleRes.json();
  const title = titleData?.data?.Media?.title?.english || titleData?.data?.Media?.title?.romaji || "";
  if (!title) throw new Error("Cannot resolve title");

  // Search on animepahe
  const searchRes = await fetch(`https://animepahe.com/api?m=search&q=${encodeURIComponent(title)}`, {
    headers: { "User-Agent": UA },
  });
  if (!searchRes.ok) throw new Error(`AnimePahe search failed: ${searchRes.status}`);
  const searchData = await searchRes.json();
  const results = searchData?.data || [];
  if (!results.length) throw new Error("No results on AnimePahe");
  const session = results[0].session;
  const id = results[0].id;

  // Get episodes
  const epRes = await fetch(`https://animepahe.com/api?m=release&id=${id}&sort=episode_asc&page=1`, {
    headers: { "User-Agent": UA },
  });
  const epData = await epRes.json();
  const episodes = epData?.data || [];
  let targetEp = null;
  for (const ep of episodes) {
    if (ep.episode === episode) { targetEp = ep; break; }
  }
  if (!targetEp) throw new Error(`Episode ${episode} not found`);

  // Get stream URL
  const playRes = await fetch(`https://animepahe.com/play/${session}/${targetEp.session}`, {
    headers: { "User-Agent": UA },
  });
  const playHtml = await playRes.text();
  // Look for audio and video URLs in the page
  const audioMatch = playHtml.match(/data-audio=["']([^"']+)["']/);
  const srcMatch = playHtml.match(/data-src=["']([^"']+)["']/);
  const url = srcMatch ? srcMatch[1] : "";
  if (!url) throw new Error("No video URL found");
  return { url, format: "mp4", source: "animepahe", headers: { Referer: "https://kwik.cx/" } };
}

// ===== ROUTER =====
async function handleRequest(request) {
  const url = new URL(request.url);
  const path = url.pathname;

  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

  try {
    // Health
    if (path === "/health") {
      return json({ status: "ok", worker: "aniheist-universal", providers: ["miruro", "anikoto", "aniwaves", "animepahe", "kuhi"] });
    }

    // Search
    if (path === "/search") {
      const q = url.searchParams.get("q");
      if (!q) return json({ error: "Missing ?q=" }, 400);
      let results = [];
      try { results = await anikotoSearch(q); } catch {}
      return json({ success: true, results, source: results.length ? "anikoto" : "none" });
    }

    // Episodes (Miruro pipe)
    if (path.startsWith("/episodes/")) {
      const id = parseInt(path.split("/")[2]);
      const data = await miruroEpisodes(id);
      return json({ success: true, data });
    }

    // Watch: /watch/{provider}/{id}/{ep}
    const watchMatch = path.match(/^\/watch\/(\w+)\/(\d+)\/?(\d+)?$/);
    if (watchMatch) {
      const provider = watchMatch[1];
      const aid = parseInt(watchMatch[2]);
      const ep = parseInt(watchMatch[3] || "1");
      const dub = url.searchParams.get("dub") === "true";

      let result;
      switch (provider) {
        case "miruro": result = await miruroSmartExtract(aid, ep); break;
        case "anikoto": result = await anikotoResolve(aid, ep); break;
        case "aniwaves": result = await aniwavesResolve(aid, ep); break;
        case "pahe": result = await paheResolve(aid, ep); break;
        case "kuhi": result = await kuhiResolve(aid, ep, dub); break;
        default: return json({ error: `Unknown provider: ${provider}` }, 404);
      }
      return json({ success: true, data: result });
    }

    // Auto: /auto/{id}/{ep} — tries all providers in order
    if (path.startsWith("/auto/")) {
      const parts = path.split("/");
      const aid = parseInt(parts[2]);
      const ep = parseInt(parts[3] || "1");
      const providers = ["miruro", "anikoto", "aniwaves", "pahe"];
      for (const prov of providers) {
        try {
          let result;
          switch (prov) {
            case "miruro": result = await miruroSmartExtract(aid, ep); break;
            case "anikoto": result = await anikotoResolve(aid, ep); break;
            case "aniwaves": result = await aniwavesResolve(aid, ep); break;
            case "pahe": result = await paheResolve(aid, ep); break;
          }
          if (result) return json({ success: true, data: result, fallback_used: prov !== "miruro" });
        } catch {}
      }
      return json({ error: "All providers failed" }, 502);
    }

    return json({ error: "Not found. Try /health, /search?q=, /watch/miruro/{id}/{ep}, /auto/{id}/{ep}" }, 404);
  } catch (e) {
    return json({ error: e.message }, 500);
  }
}

export default { fetch: handleRequest };
