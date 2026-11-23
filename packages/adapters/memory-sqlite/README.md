# dengcode-adapter-memory-sqlite

MemoryStore padrão do DengCode: um arquivo SQLite por workspace
(`~/.dengcode/workspaces/<ws>/memory.db`) com busca híbrida — FTS5 (bm25) +
vetores (`sqlite-vec`) combinados por Reciprocal Rank Fusion — e um grafo de
lineage (`edges`) que expande o filtro por entidades.

Sem `sqlite-vec` (ou sem adapter de embedding na stack) a busca cai para só
FTS5, com um aviso no log.

```yaml
memory_store:
  adapter: sqlite
  path: ~/.dengcode/workspaces/trabalho-1/memory.db  # opcional; ":memory:" só em testes
  vectors: true          # false força só FTS5
  min_similarity: 0.3    # similaridade (cosseno) mínima para um acerto vetorial
```
