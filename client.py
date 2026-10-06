"""IGDB v4 provider, using Twitch client-credentials authentication."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from playlite.metadata import MetadataError, link_name
from playlite.sorting_name import sorting_name

CREDENTIALS = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'playlite/igdb.json'


def load_credentials():
    try:
        values = json.loads(CREDENTIALS.read_text()) if CREDENTIALS.exists() else {}
    except (OSError, ValueError):
        values = {}
    values = values if isinstance(values, dict) else {}
    values = {key: value for key, value in values.items() if isinstance(value, str)}
    return (os.environ.get('PLAYLITE_IGDB_CLIENT_ID', values.get('client_id', '')),
            os.environ.get('PLAYLITE_IGDB_CLIENT_SECRET', values.get('client_secret', '')))


def save_credentials(client_id, client_secret):
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True)
    temporary = CREDENTIALS.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(temporary, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'client_id': client_id.strip(), 'client_secret': client_secret.strip()}, stream)
    temporary.replace(CREDENTIALS)


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=body.encode(), headers=headers or {}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = response.read(4 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        messages = {401: 'IGDB authentication failed. Check your Twitch credentials.',
                    403: 'IGDB access denied. Check your Twitch application settings.',
                    429: 'IGDB rate limit reached. Try again shortly.'}
        raise MetadataError(messages.get(error.code, f'IGDB request failed (HTTP {error.code}).')) from None
    except (OSError, ValueError):
        raise MetadataError('Could not connect to IGDB. Check your network connection.') from None
    if len(data) > 4 * 1024 * 1024:
        raise MetadataError('IGDB response is too large.')
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise MetadataError('IGDB returned an invalid response.') from None


class IGDBProvider:
    def __init__(self, client_id, client_secret):
        self.client_id, self.client_secret = client_id.strip(), client_secret.strip()
        self.token = None
        self.expires = 0

    def games(self, query):
        if not self.client_id or not self.client_secret:
            raise MetadataError('Configure your IGDB Twitch Client ID and Client Secret first.')
        if not self.token or time.monotonic() >= self.expires:
            response = post('https://id.twitch.tv/oauth2/token', urllib.parse.urlencode(
                {'client_id': self.client_id, 'client_secret': self.client_secret, 'grant_type': 'client_credentials'}),
                {'Content-Type': 'application/x-www-form-urlencoded'})
            if not isinstance(response, dict) or not response.get('access_token'):
                raise MetadataError('Twitch did not return an access token.')
            self.token = response['access_token']
            self.expires = time.monotonic() + max(0, int(response.get('expires_in', 0)) - 60)
        try:
            response = post('https://api.igdb.com/v4/games', query,
                            {'Client-ID': self.client_id, 'Authorization': 'Bearer ' + self.token,
                             'Content-Type': 'text/plain', 'Accept': 'application/json'})
        except MetadataError:
            self.token = None
            raise
        if not isinstance(response, list):
            raise MetadataError('IGDB returned an unexpected game response.')
        return response

    def search_games(self, query):
        query = query.strip()
        if not query:
            raise MetadataError('Enter a game name, IGDB game ID, or IGDB URL.')
        if query.isascii() and query.isdigit():
            condition = f'where id = {int(query)};'
        else:
            parsed = urllib.parse.urlsplit(query)
            if parsed.hostname in ('www.igdb.com', 'igdb.com'):
                match = re.fullmatch(r'/games/([a-zA-Z0-9-]+)/?', parsed.path)
                if not match:
                    raise MetadataError('Use an IGDB game page URL.')
                condition = 'where slug = ' + json.dumps(match[1]) + ';'
            else:
                condition = 'search ' + json.dumps(query) + ';'
        response = self.games('fields name,first_release_date; ' + condition + ' limit 30;')
        return [{'id': item['id'], 'name': item['name']} for item in response
                if isinstance(item, dict) and isinstance(item.get('id'), int) and isinstance(item.get('name'), str)]

    def fetch_metadata(self, game_id):
        fields = ('name,summary,storyline,url,first_release_date,genres.name,platforms.name,'
                  'involved_companies.company.name,involved_companies.developer,involved_companies.publisher,'
                  'game_modes.name,keywords.name,themes.name,collections.name,franchises.name,'
                  'aggregated_rating,rating,websites.url,websites.type.type,cover.image_id,artworks.image_id,screenshots.image_id')
        games = self.games(f'fields {fields}; where id = {int(game_id)}; limit 1;')
        if not games:
            raise MetadataError('IGDB could not find this game.')
        return normalize(games[0])


def normalize(game):
    result, images = {}, {}
    if game.get('name'):
        result['Name'] = game['name']
        result['SortingName'] = sorting_name(game['name'])
    description = '\n\n'.join(game[key] for key in ('summary', 'storyline') if game.get(key))
    if description:
        result['Description'] = description
    for key, source in [('Genres', 'genres'), ('Platforms', 'platforms'), ('Features', 'game_modes'),
                        ('Tags', 'keywords'), ('Categories', 'themes'), ('Series', 'collections')]:
        values = [item['name'] for item in game.get(source, []) if isinstance(item, dict) and item.get('name')]
        if key == 'Series':
            values += [item['name'] for item in game.get('franchises', []) if isinstance(item, dict) and item.get('name')]
        if values:
            result[key] = list(dict.fromkeys(values))
    for key, role in [('Developers', 'developer'), ('Publishers', 'publisher')]:
        values = [item['company']['name'] for item in game.get('involved_companies', [])
                  if item.get(role) and isinstance(item.get('company'), dict) and item['company'].get('name')]
        if values:
            result[key] = list(dict.fromkeys(values))
    if game.get('first_release_date') is not None:
        try:
            result['ReleaseDate'] = {'ReleaseDate': datetime.fromtimestamp(game['first_release_date'], timezone.utc).date().isoformat()}
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    for key, source in [('CriticScore', 'aggregated_rating'), ('CommunityScore', 'rating')]:
        score = game.get(source)
        if isinstance(score, (int, float)) and 0 <= score <= 100:
            result[key] = round(score)
    links = []
    if game.get('url'):
        links.append({'Name': 'IGDB', 'Url': game['url']})
    for item in game.get('websites', []):
        url = item.get('url', '')
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme in ('https', 'http') and parsed.hostname:
            website_type = item.get('type')
            category = website_type.get('type', '').casefold() if isinstance(website_type, dict) else item.get('category')
            name = link_name(url, category=category)
            links.append({'Name': name, 'Url': url})
    if links:
        result['Links'] = links
    def image(item, size):
        image_id = item.get('image_id', '') if isinstance(item, dict) else ''
        return f'https://images.igdb.com/igdb/image/upload/t_{size}/{image_id}.jpg' if re.fullmatch(r'[a-zA-Z0-9_-]+', image_id) else None
    cover = image(game.get('cover'), 'cover_big')
    if cover:
        images['CoverImage'] = cover
    backgrounds = game.get('artworks') or game.get('screenshots') or []
    if backgrounds and (background := image(backgrounds[0], 'screenshot_huge')):
        images['HeaderImage'] = background
        images['BackgroundImage'] = background
    return {'fields': result, 'images': images, 'name': game.get('name', 'IGDB game'), 'id': game['id'], 'provider': 'IGDB'}
