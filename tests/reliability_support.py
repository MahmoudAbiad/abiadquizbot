"""Execute production functions with explicit dependency injection, no SDK imports."""
import ast
from pathlib import Path
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]


def load_functions(path, names, **env):
    module = ast.parse((ROOT / path).read_text())
    module.body = [node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]
    for node in module.body:
        node.decorator_list = []
    env.setdefault('__name__', 'isolated_production')
    exec(compile(module, path, 'exec', flags=__import__('__future__').annotations.compiler_flag), env)
    return env


class HTTPError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.time = 0
        self.expires = {}
        self.fail = False

    async def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError('Redis unavailable')
        if self.expires.get(key, float('inf')) <= self.time:
            self.values.pop(key, None)
        if nx and key in self.values:
            return False
        self.values[key] = value
        if ex:
            self.expires[key] = self.time + ex
        return True

    async def eval(self, script, count, *args):
        key, token, *rest = args
        if self.values.get(key) != token:
            return 0
        if 'release_owned' in script:
            del self.values[key]
        elif 'renew_owned' in script:
            self.expires[key] = self.time + int(rest[0])
        else:
            raise AssertionError('Unexpected script')
        return 1
