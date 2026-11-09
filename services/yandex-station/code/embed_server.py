"""Local embedding sidecar for assistant memory (loopback only).

Serves multilingual-e5-small ONNX embeddings so the adapter stays
dependency-light: one POST /embed {"texts": [...]} -> {"vectors": [[...]]}.
The model is warm-resident; CPU-only, no GPU contention with the desktop.
"""
from __future__ import annotations

import logging
import os

from aiohttp import web

LOG = logging.getLogger('aisis.yandex.embed')

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
class Embedder:
    def __init__(self):
        # fastembed pulls onnxruntime + tokenizers only; no torch.
        from fastembed import TextEmbedding
        self.model = TextEmbedding(MODEL)
        self.dim = len(next(iter(self.model.embed(['warmup']))))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self.model.embed(texts)]


def make_app(embedder: Embedder) -> web.Application:
    async def health(_request):
        return web.json_response({'service': 'aisis-station-embed', 'model': MODEL, 'dim': embedder.dim})

    async def embed(request):
        try:
            body = await request.json()
            texts = body.get('texts')
        except ValueError:
            raise web.HTTPBadRequest(text='invalid JSON')
        if not isinstance(texts, list) or not texts or len(texts) > 64 \
                or not all(isinstance(t, str) and 0 < len(t) <= 8000 for t in texts):
            raise web.HTTPBadRequest(text='texts must be 1..64 non-empty strings')
        return web.json_response({'vectors': embedder.embed(texts), 'dim': embedder.dim})

    app = web.Application(client_max_size=65536)
    app.router.add_get('/healthz', health)
    app.router.add_post('/embed', embed)
    return app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    app = make_app(Embedder())
    web.run_app(app, host='127.0.0.1', port=int(os.environ.get('AISIS_EMBED_PORT', '31123')), access_log=None)
