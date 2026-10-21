from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from services.runner_static import RunnerAwareStaticFiles


class RunnerCacheTests(unittest.TestCase):
    def test_runner_revalidates_existing_cache_and_other_webapps_keep_their_policy(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);(root/'runner').mkdir();(root/'runner'/'game.js').write_text('new build');(root/'other.html').write_text('other app')
            app=FastAPI();app.mount('/webapp',RunnerAwareStaticFiles(directory=directory))
            with TestClient(app) as client:
                first=client.get('/webapp/runner/game.js?v=new')
                self.assertEqual(first.headers['cache-control'],'no-store, max-age=0')
                cached=client.get('/webapp/runner/game.js',headers={'If-None-Match':first.headers['etag']})
                self.assertEqual(cached.status_code,304)
                self.assertEqual(cached.headers['cache-control'],'no-store, max-age=0')
                self.assertNotIn('cache-control',client.get('/webapp/other.html').headers)
