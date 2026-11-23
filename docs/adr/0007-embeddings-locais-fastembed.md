# ADR 0007 — Embeddings locais com fastembed (ONNX, sem torch)

- Status: aceito
- Data: 2026-11-23

## Contexto

A busca da memória (`docs/especificacoes/memoria.md`) combina FTS5 com busca
vetorial. Para o preset open source funcionar sem nenhuma conta em provedor,
precisamos de um `EmbeddingPort` que rode na máquina do usuário, em Windows,
Linux e macOS, instalado só com `uv sync`.

Requisitos:

- **Multilíngue**: o conteúdo da memória (títulos, incidentes, glossário) é
  escrito em português, e as entidades (tabelas, colunas) costumam estar em
  inglês. Modelos só de inglês (ex.: `bge-small-en`) aproximam mal os dois.
- **Leve e portátil**: sem compilador, sem CUDA, sem wheels gigantes; o
  DengCode é uma ferramenta de linha de comando, não um servidor de ML.
- **Sem rede no caminho de teste**: os testes unitários não podem baixar modelo.

Opções avaliadas:

| Opção | Prós | Contras |
| --- | --- | --- |
| `sentence-transformers` (PyTorch) | catálogo enorme, referência de qualidade | `torch` sozinho tem centenas de MB a GB por plataforma; instalação lenta e frágil no Windows; inicialização pesada |
| `fastembed` (ONNX Runtime) | sem torch; wheels prontas para Windows/Linux/macOS; modelos quantizados/otimizados; mesma família de modelos (`sentence-transformers/*`) | catálogo menor, fixo na versão da lib |
| Só embeddings de provedor (OpenAI, Ollama) | zero modelo local | exige conta ou um servidor Ollama rodando; não cobre o caso "instalei e já funciona" |

## Decisão

- Adapter `local` (`dengcode-adapter-embedding-local`) sobre **`fastembed`**.
- Modelo padrão: **`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`**
  (384 dimensões, 50+ idiomas incluindo português; ~220 MB).
- `model`, `dimensions`, `cache_dir` e `threads` configuráveis na stack; o
  cache padrão é `~/.dengcode/workspaces/<ws>/cache/models`.
- O modelo é carregado **sob demanda** no primeiro `embed`, numa thread
  (`asyncio.to_thread`); construir o adapter não toca a rede nem o disco.
- Os adapters `ollama` e `openai` também implementam `EmbeddingPort`
  (`/api/embed` e `embeddings.create`) para quem já usa esses provedores. O
  Anthropic não oferece API de embeddings, então não há adapter dele.
- Sem embedder (ou sem `sqlite-vec`), a memória continua funcionando só com
  FTS5 — o embedding é melhoria de recall, não requisito.

## Consequências

- (+) Instalação só com `uv sync`, igual nas três plataformas; sem torch.
- (+) Busca semântica em português pronta no preset open source, offline
  depois do primeiro download.
- (−) **Primeira execução baixa o modelo (~220 MB)**; sem internet nesse
  momento, o adapter levanta `AdapterError` com mensagem clara e a memória cai
  para FTS5 (o store registra um aviso e reindexa os vetores quando o modelo
  voltar a estar disponível).
- (−) Ocupa ~220 MB em disco por cache de workspace (pode-se apontar
  `cache_dir` para um diretório compartilhado) e algumas centenas de MB de RAM
  quando carregado.
- (−) O catálogo de modelos é o do `fastembed`; modelo fora dele exige outro
  adapter. Trocar de modelo muda a dimensão: o store SQLite detecta e
  reindexa os vetores automaticamente.
- (−) MiniLM multilíngue é um modelo pequeno: qualidade inferior a modelos
  grandes de provedor, compensada pela fusão com FTS5 (RRF) e pelo grafo de
  entidades.
