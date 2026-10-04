import re
from urllib.parse import urlsplit
from playlite.providers import MetadataProvider
from .client import IGDBProvider, load_credentials, save_credentials


class Provider(MetadataProvider):
    query_hint = 'Game name, IGDB ID, or IGDB URL'

    def __init__(self):
        self.client = IGDBProvider(*load_credentials())

    def create_settings(self, parent=None):
        from PyQt6.QtWidgets import QWidget, QFormLayout, QLineEdit, QLabel
        widget = QWidget(parent)
        form = QFormLayout(widget)
        widget.original_credentials = load_credentials()
        widget.client_id = QLineEdit(widget.original_credentials[0])
        widget.secret = QLineEdit(widget.original_credentials[1])
        widget.secret.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow('Client ID', widget.client_id)
        form.addRow('Client secret', widget.secret)
        help_text = QLabel('Uses a Twitch developer application. <a href="https://api-docs.igdb.com/#account-creation">Setup instructions</a>.')
        help_text.setWordWrap(True)
        help_text.setOpenExternalLinks(True)
        form.addRow(help_text)
        return widget

    def save_settings(self, widget):
        credentials = (widget.client_id.text().strip(), widget.secret.text().strip())
        if bool(credentials[0]) != bool(credentials[1]):
            raise ValueError('Enter both IGDB credentials, or clear both.')
        if credentials != widget.original_credentials:
            save_credentials(*credentials)
            widget.original_credentials = credentials
            self.client = IGDBProvider(*credentials)

    def search(self, query):
        return self.client.search_games(query)

    def fetch(self, game_id, fields):
        return self.client.fetch_metadata(game_id)

    def linked_query(self, game):
        return next((link.get('Url') for link in game.get('Links') or []
                     if urlsplit(link.get('Url', '')).hostname in ('igdb.com', 'www.igdb.com')), None)

    def is_exact_query(self, query, result_id=None):
        query = query.strip()
        if query.isascii() and query.isdigit():
            return result_id is None or str(int(query)) == str(result_id)
        parsed = urlsplit(query)
        return parsed.hostname in ('igdb.com', 'www.igdb.com') and re.fullmatch(r'/games/[a-zA-Z0-9-]+/?', parsed.path) is not None

    image_types = frozenset(('CoverImage', 'HeaderImage', 'BackgroundImage'))

    def images(self, game_id, image_type):
        if image_type not in self.image_types:
            return []
        from time import monotonic
        from playlite.metadata import MetadataError
        game_id = int(game_id)
        cache = getattr(self, '_image_cache', {})
        cached = cache.get(game_id)
        if cached and monotonic() - cached[0] < 300:
            return cached[1][image_type]
        response = self.client.games(f'fields cover.image_id,artworks.image_id,screenshots.image_id; where id = {game_id}; limit 1;')
        if not response:
            raise MetadataError('IGDB could not find this game.')
        game = response[0]
        images = {kind: [] for kind in self.image_types}
        seen = set()
        for family, entries, types in [
                ('Cover', [game.get('cover')], ('CoverImage',)),
                ('Artwork', game.get('artworks') or [], ('HeaderImage', 'BackgroundImage')),
                ('Screenshot', game.get('screenshots') or [], ('HeaderImage', 'BackgroundImage'))]:
            for index, entry in enumerate(entries):
                image_id = entry.get('image_id') if isinstance(entry, dict) else None
                if not isinstance(image_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', image_id):
                    continue
                url = f'https://images.igdb.com/igdb/image/upload/t_1080p_2x/{image_id}.jpg'
                if url in seen:
                    continue
                seen.add(url)
                candidate = {'url': url, 'label': f'{family} {index + 1}'}
                for kind in types:
                    images[kind].append(candidate)
        cache[game_id] = (monotonic(), images)
        if len(cache) > 32:
            cache.pop(next(iter(cache)))
        self._image_cache = cache
        return images[image_type]
