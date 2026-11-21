"""
Builds lists.json: the Movies & TV lists the apps can't cheaply make themselves (each needs dozens of lookups).
Run every 6 hours by .github/workflows/lists.yml; the apps read the small file this writes.

    movies.topNew    what Australians are renting/buying (Apple's AU iTunes chart), matched to IMDb
    movies.cinemas   in Australian cinemas now (TMDB)
    movies.soon      coming to Australian cinemas (TMDB)
    movies.topRated  all-time best (TMDB)
    tv.newSeasons    returning shows whose new season started in the last fortnight (TVmaze)
    tv.soon          new seasons and new series starting in the next 45 days (TVmaze)
    tv.topRated      all-time best (TMDB)

Every item: {"id": IMDb id, "kind": "movie"|"series", "name", "year", "poster", "note", "date"} with date the
Australian (Sydney) day it lands, where there is one. The TMDB lists need TMDB_TOKEN (the API read access token);
without it they're left out and the apps show those tiles as not set up yet.
"""
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lists.json')
SYD = ZoneInfo('Australia/Sydney')
TODAY = datetime.datetime.now(SYD).date()
TOKEN = os.environ.get('TMDB_TOKEN', '').strip()
UA = 'GrandStand lists (github.com/gstandtv)'


def get(url, headers=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            if e.code == 404:
                return None
            raise
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2)


def norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def item(id, kind, name, year=None, poster=None, note=None, date=None):
    return {k: v for k, v in dict(id=id, kind=kind, name=name, year=year, poster=poster, note=note,
                                   date=date.isoformat() if date else None).items() if v}


def day(d):
    return f"{d:%a} {d.day} {d:%b}"


# ---------------------------------------------------------------- Apple's chart

def top_new_movies():
    feed = get('https://itunes.apple.com/au/rss/topmovies/limit=50/json')['feed']['entry']
    out = []
    for e in feed:
        name = re.sub(r'\s*\(\d{4}\)\s*$', '', e['im:name']['label']).strip()
        year = e.get('im:releaseDate', {}).get('label', '')[:4]
        q = urllib.parse.quote(name)
        metas = (get(f'https://v3-cinemeta.strem.io/catalog/movie/top/search={q}.json') or {}).get('metas', [])
        match = None
        for m in metas:
            y = re.match(r'\d{4}', m.get('releaseInfo') or m.get('year') or '')
            if norm(m['name']) == norm(name) and (not year or not y or abs(int(y.group()) - int(year)) <= 1):
                match = m
                break
        if not match or not str(match.get('id', '')).startswith('tt'):
            continue
        # The chart has old favourites too (Harry Potter, The Hunger Games): "new" is this year's and last year's
        y = re.match(r'\d{4}', match.get('releaseInfo') or match.get('year') or year or '')
        if not y or int(y.group()) < TODAY.year - 1:
            continue
        art = e.get('im:image', [{}])[-1].get('label')
        out.append(item(match['id'], 'movie', match['name'], year or None, match.get('poster') or art))
    return out


# ---------------------------------------------------------------- TVmaze

KEEP_TYPES = {'Scripted', 'Animation', 'Reality', 'Documentary'}


def schedule(first, last):
    """Every English season premiere (episode 1) aired between the two days, once per show and season."""
    found = {}
    d = first
    while d <= last:
        for url in (f'https://api.tvmaze.com/schedule/web?date={d}', f'https://api.tvmaze.com/schedule?country=US&date={d}',
                    f'https://api.tvmaze.com/schedule?country=GB&date={d}', f'https://api.tvmaze.com/schedule?country=AU&date={d}'):
            for e in get(url) or []:
                show = e.get('show') or e.get('_embedded', {}).get('show') or {}
                imdb = (show.get('externals') or {}).get('imdb')
                if e.get('number') != 1 or not imdb or show.get('language') != 'English' or show.get('type') not in KEEP_TYPES:
                    continue
                try:
                    when = datetime.datetime.fromisoformat(e['airstamp'].replace('Z', '+00:00')).astimezone(SYD).date()
                except Exception:
                    continue
                key = (show['id'], e.get('season'))
                if key in found and found[key]['when'] <= when:
                    continue
                found[key] = dict(when=when, season=e.get('season') or 1, imdb=imdb, name=show['name'], weight=show.get('weight') or 0,
                                  type=show.get('type'),
                                  where=(show.get('webChannel') or show.get('network') or {}).get('name'),
                                  poster=(show.get('image') or {}).get('medium'), year=(show.get('premiered') or '')[:4])
            time.sleep(0.6)  # TVmaze allows about 20 calls in 10 seconds
        d += datetime.timedelta(days=1)
    return list(found.values())


def tv_item(s, upcoming):
    what = 'New series' if s['season'] == 1 else f"Season {s['season']}"
    note = ' · '.join(x for x in (what, s['where']) if x)
    return item(s['imdb'], 'series', s['name'], s['year'] or None, s['poster'], note, s['when'])


def popular_shows(limit=1500):
    """IMDb's popularity order (Cinemeta's Popular list): {IMDb id: rank}."""
    rank, skip = {}, 0
    while skip < limit:
        metas = (get(f'https://v3-cinemeta.strem.io/catalog/series/top/skip={skip}.json') or {}).get('metas', [])
        if not metas:
            break
        for m in metas:
            rank.setdefault(m['id'], len(rank) + 1)
        skip += len(metas)
    return rank


def worth_listing(s, rank):
    """TVmaze's own score barely separates Storage Wars from Only Murders, so a returning show has to be one people
    watch (in IMDb's top 1,500); a brand-new drama isn't ranked yet, so it gets in on TVmaze's score instead."""
    if s['imdb'] in rank:
        return True
    return s['season'] == 1 and s['type'] in ('Scripted', 'Animation') and s['weight'] >= 95


def tv_lists():
    rank = popular_shows()
    past = schedule(TODAY - datetime.timedelta(days=14), TODAY)
    ahead = schedule(TODAY + datetime.timedelta(days=1), TODAY + datetime.timedelta(days=45))
    best = lambda s: (rank.get(s['imdb'], 9999), -s['weight'])
    new_seasons = sorted((s for s in past if s['season'] >= 2 and worth_listing(s, rank)), key=best)[:40]
    new_seasons.sort(key=lambda s: s['when'], reverse=True)
    soon = sorted((s for s in ahead if worth_listing(s, rank)), key=best)[:40]
    soon.sort(key=lambda s: (s['when'], best(s)))
    return [tv_item(s, False) for s in new_seasons], [tv_item(s, True) for s in soon]


# ---------------------------------------------------------------- TMDB

def tmdb(path, **params):
    q = urllib.parse.urlencode(params)
    return get(f'https://api.themoviedb.org/3{path}?{q}', {'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/json'})


def poster(p):
    return f'https://image.tmdb.org/t/p/w185{p}' if p else None


def au_cinema_date(release_dates):
    """The first Australian cinema release (theatrical or limited), else None."""
    for r in (release_dates or {}).get('results', []):
        if r.get('iso_3166_1') == 'AU':
            days = sorted(x['release_date'][:10] for x in r.get('release_dates', []) if x.get('type') in (2, 3))
            if days:
                return datetime.date.fromisoformat(days[0])
    return None


def movie_pages(path, pages, **params):
    seen, out = set(), []
    for p in range(1, pages + 1):
        for m in (tmdb(path, page=p, **params) or {}).get('results', []):
            if m['id'] not in seen and m.get('original_language') == 'en':
                seen.add(m['id'])
                out.append(m)
    return out


def movie_full(m):
    return tmdb(f"/movie/{m['id']}", append_to_response='external_ids,release_dates') or {}


def cinemas_and_soon():
    now, soon = [], []
    for m in movie_pages('/movie/now_playing', 3, region='AU'):
        f = movie_full(m)
        imdb = (f.get('external_ids') or {}).get('imdb_id')
        when = au_cinema_date(f.get('release_dates'))
        if imdb and when and TODAY - datetime.timedelta(days=60) <= when <= TODAY:
            now.append(item(imdb, 'movie', m['title'], str(when.year), poster(m.get('poster_path')), f"In cinemas {when.day} {when:%b}", when))
    for m in movie_pages('/movie/upcoming', 4, region='AU'):
        f = movie_full(m)
        imdb = (f.get('external_ids') or {}).get('imdb_id')
        when = au_cinema_date(f.get('release_dates'))
        if imdb and when and when > TODAY:
            soon.append(item(imdb, 'movie', m['title'], str(when.year), poster(m.get('poster_path')), "Cinema release", when))
    soon.sort(key=lambda i: i['date'])
    return now[:40], soon[:40]


def top_rated():
    movies, shows = [], []
    # TMDB's own top_rated lets in brand-new titles on a few hundred early votes; the all-time best need lots of votes.
    for m in movie_pages('/discover/movie', 4, sort_by='vote_average.desc', **{'vote_count.gte': 5000, 'with_original_language': 'en'}):
        imdb = (tmdb(f"/movie/{m['id']}/external_ids") or {}).get('imdb_id')
        if imdb:
            movies.append(item(imdb, 'movie', m['title'], (m.get('release_date') or '')[:4] or None, poster(m.get('poster_path'))))
    seen = set()
    for p in range(1, 5):
        # No talk shows or news (10767, 10763).
        for s in (tmdb('/discover/tv', page=p, sort_by='vote_average.desc', with_original_language='en', without_genres='10767,10763',
                       **{'vote_count.gte': 1500}) or {}).get('results', []):
            if s['id'] in seen or s.get('original_language') != 'en':
                continue
            seen.add(s['id'])
            imdb = (tmdb(f"/tv/{s['id']}/external_ids") or {}).get('imdb_id')
            if imdb:
                shows.append(item(imdb, 'series', s['name'], (s.get('first_air_date') or '')[:4] or None, poster(s.get('poster_path'))))
    return movies[:60], shows[:60]


# ----------------------------------------------------------------

def main():
    old = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding='utf-8')).get('lists', {})
        except Exception:
            pass
    lists = {}

    def build(name, fn):
        """One list failing keeps its last good copy rather than emptying the tile."""
        try:
            return fn()
        except Exception as e:
            print(f'{name}: {e!r}', file=sys.stderr)
            return None

    r = build('movies.topNew', top_new_movies)
    lists['movies.topNew'] = r if r else old.get('movies.topNew', [])
    r = build('tv', tv_lists)
    lists['tv.newSeasons'], lists['tv.soon'] = r if r else (old.get('tv.newSeasons', []), old.get('tv.soon', []))
    if TOKEN:
        r = build('cinemas', cinemas_and_soon)
        lists['movies.cinemas'], lists['movies.soon'] = r if r else (old.get('movies.cinemas', []), old.get('movies.soon', []))
        r = build('topRated', top_rated)
        lists['movies.topRated'], lists['tv.topRated'] = r if r else (old.get('movies.topRated', []), old.get('tv.topRated', []))
    for k, v in lists.items():
        print(f'{k}: {len(v)}', ', '.join(i['name'] for i in v[:4]))
    if lists == old:
        print('no change')
        return
    data = {'at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'lists': lists}
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))


if __name__ == '__main__':
    main()
