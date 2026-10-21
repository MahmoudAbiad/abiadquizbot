"""Keep Runner updates fresh without changing caching for other bot WebApps."""
from starlette.staticfiles import StaticFiles

RUNNER_ASSET_VERSION = 'flight-choice-20261020-2'


class RunnerAwareStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.replace('\\', '/').startswith('runner/'):
            response.headers['Cache-Control'] = 'no-store, max-age=0'
        return response
