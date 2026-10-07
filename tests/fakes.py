"""An in-memory stand-in for the parts of the Kubernetes API the bot uses."""
import copy


class FakeKube:
    namespace = 'don-ju'

    def __init__(self):
        self.objects = {}
        self.version = 0
        self.fail = False  # make every request time out

    async def request(self, method, path, body=None, content_type='application/json'):
        if self.fail:
            raise TimeoutError()
        if method == 'GET':
            obj = self.objects.get(path)
            return (200, copy.deepcopy(obj)) if obj else (404, {})
        if method == 'POST':
            path = f"{path}/{body['metadata']['name']}"
            if path in self.objects:
                return 409, {}
            return 201, self._store(path, body)
        current = self.objects.get(path)
        if current is None:
            return 404, {}
        if method == 'PUT':
            if body['metadata'].get('resourceVersion') != current['metadata']['resourceVersion']:
                return 409, {}
            return 200, self._store(path, body)
        if method == 'PATCH':
            merged = copy.deepcopy(current)
            merged.setdefault('data', {}).update(body['data'])
            return 200, self._store(path, merged)
        raise ValueError(method)

    def _store(self, path, body):
        self.version += 1
        obj = copy.deepcopy(body)
        obj.setdefault('metadata', {})['resourceVersion'] = str(self.version)
        self.objects[path] = obj
        return copy.deepcopy(obj)
