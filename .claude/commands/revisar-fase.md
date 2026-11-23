---
description: Revisa criticamente uma fase implementada (ex.: /revisar-fase 2)
argument-hint: <número da fase>
---

Faça uma revisão crítica da **fase $ARGUMENTS** já implementada, como um
engenheiro sênior que não escreveu o código.

Verifique e reporte, com arquivo e linha:

1. **Critério de pronto** de `docs/fases/fase-$ARGUMENTS-*.md`: cada item
   realmente atendido? Rode os comandos para provar.
2. **Arquitetura**: alguma violação das regras de dependência do `CLAUDE.md`?
   Algum nome de ferramenta/provedor dentro do `dengcode_core`?
3. **Segurança**: SQL que escapa do SqlGuard, segredo em log, PII que chega ao
   LLM, escrita fora do workspace, escopo de workspace ausente.
4. **Portabilidade**: caminhos fixos, comandos só de Unix, encoding.
5. **Testes**: caminhos de erro sem teste, testes que dependem de rede, testes frágeis.
6. **Simplicidade**: abstrações que a fase não pediu, código morto.

Liste os achados do mais grave para o menos grave. Não corrija nada ainda:
pergunte quais devo corrigir.
