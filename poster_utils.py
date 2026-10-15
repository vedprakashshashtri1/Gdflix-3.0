import re
import aiohttp
import logging
import urllib.parse
from config import SPIDY_API_KEY, POSTER_API_URL, TMDB_API_KEY

LOGGER = logging.getLogger(__name__)


def to_hd_poster_url(url: str | None) -> str | None:
    """HD Poster Converter — strip CDN downscale params before Telegram upload.

    - TMDB (image.tmdb.org): /t/p/w300|w500|w780/... -> /t/p/original/...
    - IMDb/Amazon (media-amazon.com): ..._V1_UX182_CR0,0,182,268_AL_.jpg
      -> ..._V1_.jpg (drops the resize/crop segment, keeps raw original).
    - Anything else: returned unchanged. Never raises.
    """
    if not url or not isinstance(url, str):
        return url
    try:
        u = url.strip()
        if "image.tmdb.org" in u:
            u = re.sub(r"/w(300|500|780)/", "/original/", u)
            return u
        host = urllib.parse.urlparse(u).netloc.lower()
        if "media-amazon" in host or "imdb" in host:
            # ._V1_<resize/crop params>.jpg[?...] -> ._V1_.jpg
            u2 = re.sub(r"(\._V1_)[^/?]*?(\.jpe?g)(\?.*)?$", r"\1.jpg", u, flags=re.IGNORECASE)
            return u2
        return u
    except Exception:
        return url


def format_clean_poster_title(
    raw_title: str
) -> tuple[str, str | None, str | None]:

    title = re.sub(r"\.\w{2,4}$", "", raw_title)
    title = re.sub(r"[-_.]", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    season = None
    year = None

    sxx_exx = re.search(
        r"(?<!\w)S0*(\d{1,2})E\d{1,2}(?!\w)",
        title,
        re.IGNORECASE
    )

    if sxx_exx:
        season = f"Season {int(sxx_exx.group(1))}"

        title = title[:sxx_exx.start()].strip()

        return _final_clean(title), season, None

    season_match = re.search(
        r"\bSeason\s+(\d{1,2})\b",
        title,
        re.IGNORECASE
    )

    if season_match:

        season = f"Season {int(season_match.group(1))}"

        title = title[:season_match.start()].strip()

        return _final_clean(title), season, None

    s_simple = re.search(
        r"(?<!\w)S0*(\d{1,2})(?!\w)",
        title,
        re.IGNORECASE
    )

    if s_simple:

        season = f"Season {int(s_simple.group(1))}"

        title = title[:s_simple.start()].strip()

        return _final_clean(title), season, None

    all_years = list(
        re.finditer(r"\b(19|20)\d{2}\b", title)
    )

    if all_years:
        first_year_match = all_years[0]
        year = first_year_match.group(0)
        title = title[:first_year_match.start()].strip()
        return _final_clean(title), None, year

    return _final_clean(title), None, None


def _final_clean(title: str) -> str:
    title = re.sub(r"[\[\](){}]", "", title)
    title = re.sub(r"\b(19|20)\d{2}\b\s*$", "", title).strip()
    title = re.sub(r"\s+", " ", title).strip()
    return title


async def scrape_tmdb_poster_async(
    q_title: str,
    is_tv: bool,
    q_year: str | None = None
) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    url = f"https://www.themoviedb.org/search?query={urllib.parse.quote_plus(q_title)}"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
                
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        rows = soup.find_all('div', class_=lambda x: x and 'flex-nowrap' in x)
        
        results = []
        for row in rows:
            link = row.find('a', href=True)
            if not link:
                continue
            
            media_type = link.get('data-media-type')
            href = link.get('href', '')
            if not media_type:
                media_type = 'tv' if '/tv/' in href else 'movie'
                
            img = row.find('img')
            poster_url = None
            if img:
                src = img.get('src') or img.get('data-src') or ''
                match = re.search(r'/t/p/.*?/([^/\s]+)', src)
                if match:
                    poster_url = f"https://image.tmdb.org/t/p/w780/{match.group(1)}"
            
            if not poster_url:
                continue
                
            title_span = row.select_one('.flex-wrap h2 span')
            title = title_span.text.strip() if title_span else img.get('alt', '') if img else ''
            
            parenthesis = row.select_one('.flex-wrap h2 span.font-light')
            original_title = parenthesis.text.strip().strip('()') if parenthesis else ''
            
            date_span = row.select_one('span.release_date')
            date_text = date_span.text.strip() if date_span else ''
            
            year_match = re.search(r'\b(19|20)\d{2}\b', date_text)
            year = year_match.group(0) if year_match else None
            
            # Calculate matching score
            score = 0
            
            # Exact title match (case insensitive)
            clean_q = q_title.lower().strip()
            clean_title = title.lower().strip()
            clean_orig = original_title.lower().strip()
            
            if clean_q == clean_title or clean_q == clean_orig:
                score += 15
            elif clean_q in clean_title or clean_q in clean_orig or clean_title in clean_q or clean_orig in clean_q:
                score += 5
                
            # Media type match
            target_type = 'tv' if is_tv else 'movie'
            if media_type == target_type:
                score += 10
            
            # Year match
            if q_year:
                if year == q_year:
                    score += 15
                elif year is not None:
                    score -= 5
            
            results.append({
                'poster': poster_url,
                'href': href,
                'score': score
            })
            
        if not results:
            return None
            
        # Sort by score descending
        results.sort(key=lambda x: x['score'], reverse=True)
        best_match = results[0]
        
        # Detail path se detail page load karke landscape backdrop fetch karo
        detail_path = best_match.get('href')
        if detail_path:
            detail_url = f"https://www.themoviedb.org{detail_path}"
            try:
                async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                    async with session.get(detail_url) as resp:
                        if resp.status == 200:
                            detail_html = await resp.text()
                            detail_soup = BeautifulSoup(detail_html, 'html.parser')
                            
                            backdrop_url = None
                            portrait_url = None
                            
                            for meta in detail_soup.find_all('meta'):
                                prop = meta.get('property') or meta.get('name')
                                if prop == 'og:image':
                                    val = meta.get('content', '')
                                    if '/w780/' in val:
                                        backdrop_url = val
                                    elif '/w500/' in val:
                                        portrait_url = val
                                        
                            final_url = backdrop_url or portrait_url
                            if final_url:
                                return final_url
            except Exception as e:
                LOGGER.warning(f"Failed to fetch TMDB detail page backdrop: {e}")
                
        return best_match['poster']
        
    except Exception as e:
        LOGGER.exception(f"Scrape error for {q_title}: {e}")
        return None


async def fetch_ott_from_justwatch(title: str, year: str | int | None = None) -> str:
    """Fetch OTT providers from JustWatch GraphQL API"""
    try:
        gql_query = """query GetSearchTitles($first: Int!, $searchQuery: String!, $country: Country!, $language: Language!) {
            popularTitles(country: $country, first: $first, filter: {searchQuery: $searchQuery}) {
                edges {
                    node {
                        content(country: $country, language: $language) {
                            title
                            originalReleaseYear
                        }
                        offers(country: $country, platform: WEB) {
                            monetizationType
                            package { clearName }
                        }
                    }
                }
            }
        }"""
        payload = {
            "query": gql_query,
            "variables": {
                "first": 5,
                "searchQuery": title,
                "country": "IN",
                "language": "en"
            }
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://apis.justwatch.com/graphql", json=payload, headers=headers) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                edges = data.get("data", {}).get("popularTitles", {}).get("edges", [])

                    # Match year first
                year_int = None
                if year:
                    try:
                        year_int = int(str(year)[:4])
                    except (ValueError, TypeError):
                        pass

                best = None
                for e in edges:
                    node = e.get("node", {})
                    content = node.get("content", {}) or {}
                    jw_year = content.get("originalReleaseYear")
                    if year_int and jw_year and jw_year == year_int:
                        best = node
                        break
                    if best is None:
                        best = node

                if not best:
                    return ""

                offers = best.get("offers", []) or []
                seen = set()
                platforms = []
                for o in offers:
                    mtype = o.get("monetizationType", "")
                    name = o.get("package", {}).get("clearName", "")
                    if mtype in ("FLATRATE", "FLATRATE_AND_BUY") and name and name not in seen:
                        seen.add(name)
                        platforms.append(name)
                return ", ".join(platforms[:4])
    except Exception as e:
        LOGGER.warning(f"JustWatch OTT error for {title}: {e}")
        return ""


async def fetch_ott(title: str, year: str | int | None = None, is_tv: bool = False) -> str:
    """Try JustWatch + TMDB for OTT — JustWatch first, TMDB fallback"""
    jw = await fetch_ott_from_justwatch(title, year)
    if jw:
        return jw
    y_str = str(year)[:4] if year else None
    return await fetch_ott_from_tmdb(title, y_str, is_tv)


async def fetch_ott_from_tmdb(title: str, year: str | None = None, is_tv: bool = False) -> str:
    """Fetch OTT/Watch Providers from TMDB"""
    if not TMDB_API_KEY:
        return ""
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        search_type = "tv" if is_tv else "movie"
        params = {"api_key": TMDB_API_KEY, "query": title}
        if year:
            if is_tv:
                params["first_air_date_year"] = year
            else:
                params["year"] = year

        search_url = f"https://api.themoviedb.org/3/search/{search_type}"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(search_url, params=params) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    return ""

                tmdb_id = results[0]["id"]

                    # Fetch Watch Providers
                provider_url = f"https://api.themoviedb.org/3/{search_type}/{tmdb_id}/watch/providers"
                async with session.get(provider_url, params={"api_key": TMDB_API_KEY}) as presp:
                    if presp.status != 200:
                        return ""
                    pdata = await presp.json()
                    results_list = pdata.get("results", {})

                    # IN (India) pehle dekho, phir US
                    for cc in ["IN", "US"]:
                        country_data = results_list.get(cc, {})
                        providers = country_data.get("flatrate", []) or country_data.get("buy", []) or country_data.get("rent", [])
                        if providers:
                            names = [p["provider_name"] for p in providers[:3]]
                            return ", ".join(names)
                    return ""
    except Exception as e:
        LOGGER.warning(f"TMDB OTT fetch error for {title}: {e}")
        return ""


def _title_relevance(query: str, result_title: str) -> bool:
    """Check how well result title matches the query"""
    q = query.lower().strip()
    t = (result_title or "").lower().strip()
    if not q or not t:
        return False
    # Exact match ya contained
    if q in t or t in q:
        return True
    # Word-level match — query ke kam se kam 1 word result mein hona chahiye
    q_words = set(q.split())
    t_words = set(t.split())
    if not q_words:
        return False
    overlap = q_words & t_words
    return len(overlap) >= 1


async def _fetch_posters_tmdb(title: str, year: str | int | None = None, is_tv: bool = False) -> list[dict]:
    """Search posters from TMDB API"""
    if not TMDB_API_KEY:
        return []
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        search_type = "tv" if is_tv else "movie"
        params = {"api_key": TMDB_API_KEY, "query": title, "include_adult": "false"}
        if year:
            y = int(str(year)[:4]) if year else None
            if is_tv:
                params["first_air_date_year"] = y
            else:
                params["year"] = y

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://api.themoviedb.org/3/search/{search_type}", params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                results = data.get("results", [])

                posters = []
                seen = set()
                for item in results[:10]:
                    backdrop = item.get("backdrop_path")
                    if not backdrop:
                        continue
                    poster_url = f"https://image.tmdb.org/t/p/w780{backdrop}"
                    if poster_url in seen:
                        continue
                    seen.add(poster_url)
                    t = item.get("title") or item.get("name") or title
                    y = item.get("release_date", "")[:4] or item.get("first_air_date", "")[:4] or None
                    if _title_relevance(title, t):
                        posters.append({
                            "title": t,
                            "year": int(y) if y and y.isdigit() else None,
                            "poster": poster_url
                        })
                return posters
    except Exception as e:
        LOGGER.warning(f"TMDB poster search error for {title}: {e}")
        return []


async def _fetch_posters_spidey(title: str, year: str | int | None = None, season: str | None = None) -> list[dict]:
    """Search posters from Spidey poster API + title filter"""
    timeout = aiohttp.ClientTimeout(total=10)
    params = {"title": title}
    if season:
        # Extract season number if available, e.g. 'Season 1' -> '1'
        s_num = re.search(r'\d+', str(season))
        params["season"] = s_num.group(0) if s_num else season
    elif year:
        params["year"] = str(year)[:4]

    query_string = urllib.parse.urlencode(params)
    url = f"{POSTER_API_URL}/v1/fetch?{query_string}&api_key={SPIDY_API_KEY}"

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                results = data.get("results", [])

                posters = []
                seen = set()
                for item in results:
                    poster = item.get("landscape") or item.get("poster")
                    if not poster or poster in seen:
                        continue
                    t = item.get("title") or title
                    if not _title_relevance(title, t):
                        continue
                    seen.add(poster)
                    posters.append({
                        "title": t,
                        "year": item.get("year"),
                        "poster": poster
                    })
                return posters
    except Exception as e:
        LOGGER.warning(f"Spidey poster fetch error: {e}")
        return []


async def get_multiple_posters(
    raw_filename: str
) -> list[dict]:

    title, season, year = format_clean_poster_title(raw_filename)
    is_tv = season is not None

    # Spidey API — full title se search
    spidey_results = await _fetch_posters_spidey(title, year, season)

    # If Spidey results are low, try partial queries
    if len(spidey_results) < 3:
        words = title.split()
        if len(words) > 1:
            partial = " ".join(words[:2])
            extra = await _fetch_posters_spidey(partial, year, season)
            seen = {p["poster"] for p in spidey_results}
            for e in extra:
                if e["poster"] not in seen:
                    spidey_results.append(e)
                    seen.add(e["poster"])

    # Also search posters from TMDB
    tmdb_results = await _fetch_posters_tmdb(title, year, is_tv)

    # Merge + deduplicate (Spidey pehle, phir TMDB)
    merged = []
    seen = set()
    for p in spidey_results + tmdb_results:
        if p["poster"] not in seen:
            seen.add(p["poster"])
            merged.append(p)

    if not merged:
        # Last resort — try Spidey without title filter
        try:
            merged = await _fetch_posters_spidey(title, year, season)
        except Exception:
            pass

    if not merged:
        # Still no results — try partial query without filter
        words = title.split()
        if len(words) > 1:
            partial = " ".join(words[:2])
            try:
                merged = await _fetch_posters_spidey(partial, year, season)
            except Exception:
                pass

    if not merged:
        # Last last resort — TMDB scrape fallback
        try:
            poster_url = await scrape_tmdb_poster_async(title, is_tv, year)
            if poster_url:
                merged = [{"title": title, "year": year, "poster": poster_url}]
        except Exception:
            pass

    # HD normalize + dedupe: same image at different CDN sizes collapses to one
    hd_merged = []
    seen_hd = set()
    for u in merged:
        u["poster"] = to_hd_poster_url(u.get("poster") or "")
        if u["poster"] and u["poster"] not in seen_hd:
            seen_hd.add(u["poster"])
            hd_merged.append(u)
    merged = hd_merged

    # Fetch OTT for each result
    for u in merged:
        r_title = u.get("title") or title
        r_year = u.get("year")
        if isinstance(r_year, str):
            r_year = r_year[:4] if r_year else None
        u["ott"] = await fetch_ott(r_title, r_year, is_tv)

    return merged


def _score_poster_item(item: dict, target_year: str | None, target_ott: str | None) -> int:
    """Score poster candidate: exact year match (+50), OTT platform match (+100), landscape (+10)"""
    score = 0
    u = (item.get("poster") or item.get("landscape") or "").lower()
    y = str(item.get("year") or "")
    if target_year and y:
        if target_year in y:
            score += 50
        else:
            score -= 35
    if target_ott:
        if target_ott == "amazon" and ("amazon" in u or "pv-target-images" in u):
            score += 100
        elif target_ott == "hotstar" and "hotstar" in u:
            score += 100
        elif target_ott == "netflix" and "nflx" in u:
            score += 100
        elif target_ott == "zee5" and "zee5" in u:
            score += 100
        elif target_ott == "sonyliv" and "sonyliv" in u:
            score += 100
        elif target_ott == "jio" and "jio" in u:
            score += 100
    if item.get("landscape") or "target-images" in u or "/list/" in u or "_hm_" in u:
        score += 10
    return score


async def get_best_poster(raw_filename: str) -> str | None:
    """Fetch the single best poster URL for raw filename from poster API with OTT and Year matching"""
    try:
        title, season, year = format_clean_poster_title(raw_filename)
        fn_upper = raw_filename.upper()
        target_ott = None
        if any(k in fn_upper for k in ("AMZN", "AMAZON", "PRIME")):
            target_ott = "amazon"
        elif any(k in fn_upper for k in ("DSNP", "HOTSTAR", "HS")):
            target_ott = "hotstar"
        elif any(k in fn_upper for k in ("NF", "NETFLIX")):
            target_ott = "netflix"
        elif "ZEE" in fn_upper:
            target_ott = "zee5"
        elif "SONY" in fn_upper:
            target_ott = "sonyliv"
        elif "JIO" in fn_upper:
            target_ott = "jio"

        posters = await get_multiple_posters(raw_filename)
        if posters:
            sorted_posters = sorted(
                posters,
                key=lambda p: _score_poster_item(p, year, target_ott),
                reverse=True
            )
            return sorted_posters[0].get("poster")
            
        # Direct query search fallback on SPIDY Poster API
        search_query = title or raw_filename
        timeout = aiohttp.ClientTimeout(total=8)
        query_url = f"{POSTER_API_URL}/v1/fetch?api_key={SPIDY_API_KEY}&query={urllib.parse.quote(search_query)}"
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(query_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    if results:
                        sorted_res = sorted(
                            results,
                            key=lambda itm: _score_poster_item(
                                {"poster": itm.get("landscape") or itm.get("poster"), "year": itm.get("year")},
                                year,
                                target_ott
                            ),
                            reverse=True
                        )
                        return to_hd_poster_url(
                            sorted_res[0].get("landscape") or sorted_res[0].get("poster")
                        )
    except Exception as e:
        LOGGER.warning(f"get_best_poster error for {raw_filename}: {e}")
    return None
