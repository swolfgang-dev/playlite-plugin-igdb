from plugin_test_support import require_plugin
require_plugin('IGDB')
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication
from playlite_plugins.igdb.client import IGDBProvider, normalize, save_credentials
from playlite.metadata import MetadataError
from playlite.metadata_dialog import MetadataDownloader
from playlite.providers import discover_providers

APP = QApplication.instance() or QApplication([])


class IGDBTests(unittest.TestCase):
    def setUp(self):
        fixture = discover_providers(include_disabled=True)
        patcher = patch('playlite.providers.discover_providers', return_value=fixture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_normalization_and_missing_values(self):
        payload = normalize({'id': 42, 'name': 'Example', 'summary': 'Summary', 'storyline': 'Story',
                             'involved_companies': [{'developer': True, 'company': {'name': 'Studio'}},
                                                    {'publisher': True, 'company': {'name': 'Publisher'}}],
                             'genres': [{'name': 'Adventure'}], 'game_modes': [{'name': 'Single player'}],
                             'collections': [{'name': 'Series'}], 'first_release_date': 1704067200,
                             'aggregated_rating': 89.7, 'rating': 88.3,
                             'cover': {'image_id': 'co123'}, 'screenshots': [{'image_id': 'sc123'}],
                             'websites': [{'url': 'https://store.steampowered.com/app/1234/'}]})
        self.assertEqual(payload['fields']['Developers'], ['Studio'])
        self.assertEqual(payload['fields']['Publishers'], ['Publisher'])
        self.assertEqual(payload['fields']['Description'], 'Summary\n\nStory')
        self.assertEqual(payload['fields']['ReleaseDate'], {'ReleaseDate': '2024-01-01'})
        self.assertEqual(payload['fields']['CriticScore'], 90)
        self.assertEqual(payload['fields']['Links'][0]['Name'], 'Steam')
        self.assertIn('t_cover_big/co123.jpg', payload['images']['CoverImage'])
        self.assertNotIn('Description', normalize({'id': 1})['fields'])

    def test_authentication_search_escaping_and_token_reuse(self):
        client = IGDBProvider('client', 'secret')
        with patch('playlite_plugins.igdb.client.post', side_effect=[{'access_token': 'token', 'expires_in': 3600},
                                                    [{'id': 42, 'name': 'Example'}], []]) as post:
            self.assertEqual(client.search_games('Example')[0]['id'], 42)
            client.search_games('"; limit 500;')
            self.assertEqual(post.call_count, 3)
            self.assertIn('search "\\\"; limit 500;";', post.call_args.args[1])
            self.assertEqual(post.call_args.args[2]['Authorization'], 'Bearer token')
            self.assertNotIn('secret', post.call_args.args[0])

    def test_credentials_permissions_and_missing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'config/igdb.json'
            with patch('playlite_plugins.igdb.client.CREDENTIALS', filename):
                save_credentials('client', 'secret')
            self.assertEqual(filename.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(filename.read_text())['client_id'], 'client')
        with patch('playlite_plugins.igdb.client.post') as network:
            with self.assertRaises(MetadataError):
                IGDBProvider('', '').search_games('Example')
            network.assert_not_called()

    def test_igdb_selection_uses_igdb_match_and_keeps_download_modes_separate(self):
        for mode in ('metadata', 'images'):
            dialog = MetadataDownloader({'Name': 'Example', 'Links': [{'Name': 'Steam', 'Url': 'https://store.steampowered.com/app/1234/'}]}, mode=mode)
            dialog.source.setCurrentText('IGDB')
            self.assertEqual(dialog.query.text(), 'Example')
            with patch.object(dialog.providers['IGDB'].client, 'search_games', return_value=[]) as search, patch.object(dialog, 'run_task', side_effect=lambda function, completed, message: completed(function())):
                dialog.search()
                search.assert_called_once_with('Example')
            keys = {item.data(256) for item in dialog.field_items()}
            if mode == 'metadata':
                self.assertIn('CoverImage', keys)
            else:
                self.assertIn('CoverImage', keys)
            for table in (dialog.fields, dialog.artwork_fields):
                for row in range(table.rowCount()):
                    if mode == 'images':
                        self.assertEqual(table.cellWidget(row, 1).currentText(), 'IGDB')
                    else:
                        self.assertEqual(table.columnCount(), 3)
            dialog.reject()
            dialog.cache.cleanup()
