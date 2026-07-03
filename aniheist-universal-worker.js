// ===== ANIHEIST API — Universal Cloudflare Worker =====
// Single-file deploy to Cloudflare Workers Dashboard
// 10 providers with auto-fallback chain
//
// Endpoints:
//   /search?q=Death+Note        — Search across providers
//   /episodes/{anilistId}        — Episode list (Miruro)
//   /watch/{provider}/{id}/{ep}  — Stream from specific provider
//   /auto/{id}/{ep}              — Auto-fallback across all providers
//   /health                      — Status

const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, OPTIONS", "Content-Type": "application/json" };
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36";

function json(d, s = 200) { return new Response(JSON.stringify(d, null, 2), { status: s, headers: CORS }); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ===== UTILITY: AniList Title Resolution =====
async function anilistTitle(id) {
  const r = await fetch("https://graphql.anilist.co", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ query: "query($id:Int){Media(id:$id,type:ANIME){title{english romaji}}}", variables: { id } }),
  });
  const d = await r.json();
  return d?.data?.Media?.title?.english || d?.data?.Media?.title?.romaji || "";
}

// ===== PROVIDER 1: MIRURO PIPE (kuhi) =====
const MIRURO_DOMAINS = ["www.miruro.tv","www.miruro.to","www.miruro.bz","www.miruro.ru"];
const MIRURO_RANKING = ["zoro","bee","telli","arc","yugen","jet","neo","kiwi","hop","ally","pewe","moo","bonk"];

async function tryPipe(enc) {
  for (const base of MIRURO_DOMAINS) {
    try {
      const r = await fetch(`https://${base}/api/secure/pipe?e=${enc}`, { headers: { "User-Agent": UA, Referer: `https://${base}/`, Accept: "*/*" } });
      if (!r.ok) continue;
      const text = await r.text();
      try {
        const ds = new DecompressionStream("gzip");
        const w = ds.writable.getWriter();
        w.write(new Uint8Array(await new Response(text).arrayBuffer()));
        w.close();
        const reader = ds.readable.getReader();
        const chunks = []; while (true) { const { done, value } = await reader.read(); if (done) break; chunks.push(value); }
        const total = chunks.reduce((a, c) => a + c.length, 0);
        const merged = new Uint8Array(total); let off = 0;
        for (const c of chunks) { merged.set(c, off); off += c.length; }
        return JSON.parse(new TextDecoder().decode(merged));
      } catch { try { return JSON.parse(text); } catch { continue; } }
    } catch {}
  }
  return null;
}

function encPipe(p) { return btoa(JSON.stringify(p)).replace(/=+$/, ""); }

async function miruroEpisodes(aid) {
  return tryPipe(encPipe({ path: "episodes", method: "GET", query: { anilistId: aid }, body: null, version: "0.1.0" }));
}

async function miruroSources(epId, prov, cat, aid) {
  const eid = btoa(epId).replace(/=+$/, "");
  const d = await tryPipe(encPipe({ path: "sources", method: "GET", query: { episodeId: eid, provider: prov, category: cat, anilistId: aid }, body: null, version: "0.1.0" }));
  if (!d) throw new Error("miruro: pipe failed");
  const srcs = Array.isArray(d) ? d : d.sources || [];
  if (!srcs.length) throw new Error("miruro: no sources");
  const best = srcs[0];
  const url = best.url || best.file || "";
  if (!url) throw new Error("miruro: no URL");
  return { url, format: url.includes(".m3u8") ? "hls" : "mp4", source: `miruro/${prov}`, headers: { Referer: best.referer || "https://allmanga.to/", Origin: best.referer || "https://allmanga.to" } };
}

async function providerMiruro(aid, ep) {
  const data = await miruroEpisodes(aid);
  if (!data?.providers) throw new Error("miruro: no data");
  const providers = data.providers;
  for (const prov of MIRURO_RANKING) {
    if (!providers[prov]) continue;
    const eps = providers[prov].episodes || {};
    for (const cat of ["sub", "dub"]) {
      if (!eps[cat]) continue;
      for (const e of (Array.isArray(eps[cat]) ? eps[cat] : [])) {
        if (e.number === ep) {
          try { return await miruroSources(e.id, prov, cat, aid); } catch {}
        }
      }
    }
  }
  throw new Error("miruro: all failed");
}

// ===== PROVIDER 2: ANIKOTO (koto) =====
async function anikotoSearch(q) {
  const r = await fetch(`https://anikotv.to/ajax/anime/search?keyword=${encodeURIComponent(q)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://anikotv.to/" },
  });
  if (!r.ok) return [];
  const d = await r.json();
  const html = d?.result?.html || "";
  const results = [];
  const re = /href="https:\/\/anikotv\.to\/watch\/([^"]+)"[^>]*>\s*([^<]+)/g;
  let m; while ((m = re.exec(html)) !== null) results.push({ slug: m[1].split("/")[0], title: m[2].trim() });
  return results;
}

async function extractAnikoto(slug, ep) {
  const wr = await fetch(`https://anikotv.to/watch/${slug}`, { headers: { "User-Agent": UA } });
  const wh = await wr.text();
  const idM = wh.match(/data-id=["'](\d+)["']/);
  if (!idM) throw new Error("koto: no ID");
  const showId = idM[1];
  const er = await fetch(`https://anikotv.to/ajax/episode/list/${showId}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: `https://anikotv.to/watch/${slug}` },
  });
  const ed = await er.json();
  const eh = ed?.result || "";
  const nums = [...eh.matchAll(/data-num=["'](\d+)["']/g)];
  const ids = [...eh.matchAll(/data-ids=["']([^"']+)["']/g)];
  let tid = "";
  for (let i = 0; i < nums.length; i++) {
    if (parseInt(nums[i][1]) === ep) { tid = ids[i][1]; break; }
  }
  if (!tid) throw new Error(`koto: ep ${ep} not found`);
  const sr = await fetch(`https://anikotv.to/ajax/server/list?servers=${encodeURIComponent(tid)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://anikotv.to/" },
  });
  const sd = await sr.json();
  const sh = sd?.result || "";
  const secs = sh.split(/<div class="type" data-type="(sub|dub)">/);
  let lid = "";
  for (let i = 0; i < secs.length; i++) {
    if (secs[i] === "sub") { const m = secs[i+1]?.match(/data-link-id="([^"]+)"/); if (m) { lid = m[1]; break; } }
  }
  if (!lid) throw new Error("koto: no server");
  const rr = await fetch(`https://anikotv.to/ajax/server?get=${encodeURIComponent(lid)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://anikotv.to/" },
  });
  const rd = await rr.json();
  const vu = rd?.result?.url?.replace(/\\\//g, "/") || "";
  if (!vu) throw new Error("koto: no URL");
  const isEmb = vu.includes("vidtube") || vu.includes("megaplay") || vu.includes("vidwish");
  return { url: vu, format: isEmb ? "embed" : vu.includes(".m3u8") ? "hls" : "mp4", source: "anikoto", headers: { Referer: "https://anikotv.to/" } };
}

async function providerKoto(aid, ep) {
  const title = await anilistTitle(aid);
  if (!title) throw new Error("koto: no title");
  const results = await anikotoSearch(title);
  if (!results.length) throw new Error("koto: no results");
  return extractAnikoto(results[0].slug, ep);
}

// ===== PROVIDER 3: ANIWAVES =====
async function providerAniwaves(aid, ep) {
  const title = await anilistTitle(aid);
  if (!title) throw new Error("aniwaves: no title");
  const sr = await fetch(`https://aniwaves.ru/ajax/anime/search?keyword=${encodeURIComponent(title)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://aniwaves.ru/" },
  });
  const sd = await sr.json();
  const sh = sd?.result?.html || "";
  const pm = sh.match(/href="[^"]*\/watch\/([^"]+)"/);
  if (!pm) throw new Error("aniwaves: no results");
  const path = pm[1].split("/")[0];
  const parts = path.rsplit("-", 1);
  const sid = parts[1]?.match(/^\d+$/) ? parts[1] : null;
  if (!sid) throw new Error("aniwaves: no ID");
  const er = await fetch(`https://aniwaves.ru/ajax/episode/list/${sid}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://aniwaves.ru/" },
  });
  const ed = await er.json();
  const eh = ed?.result || "";
  const ids = [...eh.matchAll(/data-ids=["']([^"']+)["']/g)];
  const nums = [...eh.matchAll(/data-num=["'](\d+)["']/g)];
  let tid = "";
  for (let i = 0; i < nums.length; i++) { if (parseInt(nums[i][1]) === ep) { tid = ids[i][1]; break; } }
  if (!tid) throw new Error(`aniwaves: ep ${ep} not found`);
  const svr = await fetch(`https://aniwaves.ru/ajax/server/list?servers=${encodeURIComponent(tid)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://aniwaves.ru/" },
  });
  const svd = await svr.json();
  const svh = svd?.result || "";
  const secs = svh.split(/<div class="type" data-type="(sub|dub)">/);
  let lid = "";
  for (let i = 0; i < secs.length; i++) { if (secs[i] === "sub") { const m = secs[i+1]?.match(/data-link-id="([^"]+)"/); if (m) { lid = m[1]; break; } } }
  if (!lid) throw new Error("aniwaves: no server");
  const rr = await fetch(`https://aniwaves.ru/ajax/server?get=${encodeURIComponent(lid)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://aniwaves.ru/" },
  });
  const rd = await rr.json();
  const vu = rd?.result?.url?.replace(/\\\//g, "/") || "";
  if (!vu) throw new Error("aniwaves: no URL");
  const isEmb = vu.includes("vidtube") || vu.includes("megaplay") || vu.includes("vidwish");
  return { url: vu, format: isEmb ? "embed" : vu.includes(".m3u8") ? "hls" : "mp4", source: "aniwaves", headers: { Referer: "https://aniwaves.ru/" } };
}

// ===== PROVIDER 4: ANIMEPAHE (pahe) =====
async function providerPahe(aid, ep) {
  const title = await anilistTitle(aid);
  if (!title) throw new Error("pahe: no title");
  const sr = await fetch(`https://animepahe.com/api?m=search&q=${encodeURIComponent(title)}`, { headers: { "User-Agent": UA } });
  if (!sr.ok) throw new Error(`pahe: search failed ${sr.status}`);
  const sd = await sr.json();
  const results = sd?.data || [];
  if (!results.length) throw new Error("pahe: no results");
  const session = results[0].session, pid = results[0].id;
  const er = await fetch(`https://animepahe.com/api?m=release&id=${pid}&sort=episode_asc&page=1`, { headers: { "User-Agent": UA } });
  const ed = await er.json();
  const eps = ed?.data || [];
  let target = null;
  for (const e of eps) { if (e.episode === ep) { target = e; break; } }
  if (!target) throw new Error(`pahe: ep ${ep} not found`);
  const pr = await fetch(`https://animepahe.com/play/${session}/${target.session}`, { headers: { "User-Agent": UA } });
  const ph = await pr.text();
  const sm = ph.match(/data-src=["']([^"']+)["']/);
  const vu = sm ? sm[1] : "";
  if (!vu) throw new Error("pahe: no URL");
  return { url: vu, format: "mp4", source: "animepahe", headers: { Referer: "https://kwik.cx/" } };
}

// ===== PROVIDER 5: REANIME (rea) =====
async function providerReanime(aid, ep) {
  const r = await fetch(`https://reanime.to/api/anime/${aid}`, { headers: { "User-Agent": UA } });
  if (!r.ok) throw new Error(`rea: ${r.status}`);
  const d = await r.json();
  const eps = d?.episodes || [];
  let target = null;
  for (const e of eps) { if (e.episode === ep) { target = e; break; } }
  if (!target) throw new Error(`rea: ep ${ep} not found`);
  const sr = await fetch(`https://reanime.to/api/sources/${target.id}`, { headers: { "User-Agent": UA, Referer: "https://reanime.to/" } });
  const sd = await sr.json();
  const vu = sd?.sources?.[0]?.url || sd?.url || "";
  if (!vu) throw new Error("rea: no URL");
  return { url: vu, format: vu.includes(".m3u8") ? "hls" : "mp4", source: "reanime", headers: { Referer: "https://reanime.to/" } };
}

// ===== PROVIDER 6: ANIMEGG (egg) =====
async function providerEgg(aid, ep) {
  const r = await fetch(`https://www.animegg.org/anime/${aid}`, { headers: { "User-Agent": UA } });
  if (!r.ok) throw new Error(`egg: ${r.status}`);
  const html = await r.text();
  // Try to find episode links and extract video URL
  const epLinks = html.match(new RegExp(`/watch/${aid}/ep-${ep}[^"']*`, "i"));
  if (!epLinks) throw new Error(`egg: ep ${ep} not found`);
  const wr = await fetch(`https://www.animegg.org${epLinks[0]}`, { headers: { "User-Agent": UA, Referer: "https://www.animegg.org/" } });
  const wh = await wr.text();
  const vm = wh.match(/<video[^>]+src=["']([^"']+)["']/);
  const im = wh.match(/<iframe[^>]+src=["']([^"']+)["']/);
  const vu = vm ? vm[1] : im ? im[1] : "";
  if (!vu) throw new Error("egg: no URL");
  return { url: vu, format: "mp4", source: "animegg", headers: { Referer: "https://www.animegg.org/" } };
}

// ===== PROVIDER 7: ANINEKO (neko) =====
async function providerNeko(aid, ep) {
  const title = await anilistTitle(aid);
  if (!title) throw new Error("neko: no title");
  const sr = await fetch(`https://anineko.to/ajax/search?q=${encodeURIComponent(title)}`, {
    headers: { "User-Agent": UA, "X-Requested-With": "XMLHttpRequest", Referer: "https://anineko.to/" },
  });
  if (!sr.ok) throw new Error("neko: search failed");
  const sd = await sr.json();
  const results = sd?.results || [];
  if (!results.length) throw new Error("neko: no results");
  const slug = results[0].url;
  const wr = await fetch(`https://anineko.to${slug}`, { headers: { "User-Agent": UA } });
  const wh = await wr.text();
  const vm = wh.match(/<iframe[^>]+src=["']([^"']+)["']/);
  const ds = wh.match(/data-src=["']([^"']+)["']/);
  const vu = vm ? vm[1] : ds ? ds[1] : "";
  if (!vu) throw new Error("neko: no URL");
  return { url: vu, format: "embed", source: "anineko", headers: { Referer: "https://anineko.to/" } };
}

// ===== PROVIDER 8: ANIDB APP (anidb) =====
async function providerAnidb(aid, ep) {
  const r = await fetch(`https://anidb.app/anime/${aid}`, { headers: { "User-Agent": UA } });
  if (!r.ok) throw new Error(`anidb: ${r.status}`);
  const html = await r.text();
  const em = html.match(/<iframe[^>]+src=["']([^"']+)["']/);
  const vu = em ? em[1] : "";
  if (!vu) throw new Error("anidb: no embed");
  return { url: vu, format: "embed", source: "anidb", headers: { Referer: "https://anidb.app/" } };
}

// ===== PROVIDER 9: ALLANIME =====
async function providerAllanime(aid, ep) {
  const title = await anilistTitle(aid);
  if (!title) throw new Error("allanime: no title");
  const q = `query($s:String!){shows(search:$s,limit:5,page:1){edges{_id name availableEpisodes}}}`;
  const r = await fetch(`https://api.allanime.day/api`, {
    method: "POST", headers: { "Content-Type": "application/json", Origin: "https://allanime.day" },
    body: JSON.stringify({ query: q, variables: { s: title } }),
  });
  if (!r.ok) throw new Error(`allanime: ${r.status}`);
  const d = await r.json();
  const edges = d?.data?.shows?.edges || [];
  if (!edges.length) throw new Error("allanime: no results");
  const sid = edges[0]._id;
  const eq = `query($sid:String!,$ep:Int!){episode(showId:$sid,episodeNumber:$ep){episodeString sourceUrls{sourceUrl quality}}}`;
  const er = await fetch(`https://api.allanime.day/api`, {
    method: "POST", headers: { "Content-Type": "application/json", Origin: "https://allanime.day" },
    body: JSON.stringify({ query: eq, variables: { sid, ep } }),
  });
  const ed = await er.json();
  const epData = ed?.data?.episode;
  if (!epData) throw new Error("allanime: ep not found");
  const epStr = epData.episodeString || `${sid}?ep=${ep}`;
  // Get stream via embed
  const streamEp = epData.sourceUrls?.[0]?.sourceUrl || "";
  if (streamEp) {
    const str = await fetch(streamEp, { headers: { "User-Agent": UA, Referer: "https://allanime.day/" } });
    const sth = await str.text();
    const m3u8 = sth.match(/https?://[^"'\s<>]+\.m3u8[^"'\s<>]*/);
    if (m3u8) return { url: m3u8[0], format: "hls", source: "allanime", headers: { Referer: "https://allanime.day/" } };
  }
  throw new Error("allanime: no stream");
}

// ===== PROVIDER 10: KISSANIME =====
async function providerKissanime(aid, ep) {
  const title = await anilistTitle(aid);
  if (!title) throw new Error("kiss: no title");
  const sr = await fetch(`https://kissanime.com.ru/Search?q=${encodeURIComponent(title)}`, { headers: { "User-Agent": UA } });
  if (!sr.ok) throw new Error(`kiss: ${sr.status}`);
  const sh = await sr.text();
  const links = sh.match(/href="\/(?:Anime|anime)\/([^"]+)"/g);
  if (!links) throw new Error("kiss: no results");
  const slug = links[0].replace(/href="\/(?:Anime|anime)\//, "").replace(/"/, "");
  const wr = await fetch(`https://kissanime.com.ru/Anime/${slug}`, { headers: { "User-Agent": UA } });
  const wh = await wr.text();
  // KissAnime uses iframe embeds
  const im = wh.match(/<iframe[^>]+src=["']([^"']+)["']/);
  const vu = im ? im[1] : "";
  if (!vu) throw new Error("kiss: no embed");
  return { url: vu, format: "embed", source: "kissanime", headers: { Referer: "https://kissanime.com.ru/" } };
}

// ===== PROVIDER LIST & AUTO-FALLBACK =====
const PROVIDERS = [
  { name: "miruro", fn: providerMiruro },
  { name: "anikoto", fn: providerKoto },
  { name: "aniwaves", fn: providerAniwaves },
  { name: "animepahe", fn: providerPahe },
  { name: "reanime", fn: providerReanime },
  { name: "animegg", fn: providerEgg },
  { name: "anineko", fn: providerNeko },
  { name: "anidb", fn: providerAnidb },
  { name: "allanime", fn: providerAllanime },
  { name: "kissanime", fn: providerKissanime },
];

// ===== ROUTER =====
async function handleRequest(request) {
  const url = new URL(request.url);
  const path = url.pathname;
  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });

  try {
    // Health
    if (path === "/health" || path === "/") {
      return json({ name: "AniHeistAPI", version: "2.0", providers: PROVIDERS.map(p => p.name), routes: [
        "/search?q=", "/episodes/{id}", "/watch/{provider}/{id}/{ep}", "/auto/{id}/{ep}", "/health"
      ]});
    }

    // Search
    if (path === "/search") {
      const q = url.searchParams.get("q") || url.searchParams.get("query");
      if (!q) return json({ error: "Missing ?q=" }, 400);
      let results = [];
      try { results = await anikotoSearch(q); } catch {}
      return json({ success: true, results, count: results.length, source: "anikoto" });
    }

    // Episodes via Miruro pipe
    if (path.startsWith("/episodes/")) {
      const id = parseInt(path.split("/")[2]);
      const data = await miruroEpisodes(id);
      return json({ success: true, data });
    }

    // Watch specific provider: /watch/{name}/{id}/{ep}
    const wm = path.match(/^\/watch\/(\w+)\/(\d+)\/?(\d+)?$/);
    if (wm) {
      const pname = wm[1], aid = parseInt(wm[2]), ep = parseInt(wm[3] || "1");
      const prov = PROVIDERS.find(p => p.name === pname);
      if (!prov) return json({ error: `Unknown provider: ${pname}. Available: ${PROVIDERS.map(p=>p.name).join(", ")}` }, 404);
      const result = await prov.fn(aid, ep);
      return json({ success: true, data: result });
    }

    // Auto-fallback: /auto/{id}/{ep}
    const am = path.match(/^\/auto\/(\d+)\/?(\d+)?$/);
    if (am) {
      const aid = parseInt(am[1]), ep = parseInt(am[2] || "1");
      const errors = [];
      for (const prov of PROVIDERS) {
        try {
          const result = await prov.fn(aid, ep);
          return json({ success: true, data: result, fallback: errors.length > 0, attempts: errors });
        } catch (e) { errors.push(`${prov.name}: ${e.message}`); }
      }
      return json({ error: "All providers failed", attempts: errors }, 502);
    }

    return json({ error: "Not found. See /health for routes." }, 404);
  } catch (e) {
    return json({ error: e.message, stack: e.stack }, 500);
  }
}

export default { fetch: handleRequest };
