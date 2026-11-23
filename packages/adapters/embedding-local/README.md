# dengcode-adapter-embedding-local

Embeddings locais com [fastembed](https://github.com/qdrant/fastembed) (ONNX,
sem torch). Modelo padrão: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
(384 dimensões, multilíngue). Ver `docs/adr/0007-embeddings-locais-fastembed.md`.

Na primeira chamada o modelo (~220 MB) é baixado para
`~/.dengcode/workspaces/<ws>/cache/models` (configurável).

```yaml
embedding:
  adapter: local
  model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2  # opcional
  dimensions: 384     # obrigatório só para modelos fora da tabela conhecida
  cache_dir: ~/modelos  # opcional
  threads: 2            # opcional
```
