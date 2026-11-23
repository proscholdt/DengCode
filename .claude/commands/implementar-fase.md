---
description: Implementa uma fase do plano (ex.: /implementar-fase 2)
argument-hint: <número da fase>
---

Implemente a **fase $ARGUMENTS** do projeto.

1. Leia `CLAUDE.md`, `PROGRESSO.md`, `docs/ARQUITETURA.md` e o arquivo
   `docs/fases/fase-$ARGUMENTS-*.md`, além das especificações e ADRs que ele cita.
2. Confirme que as fases anteriores estão concluídas no `PROGRESSO.md`.
   Se não estiverem, pare e me avise.
3. Antes de escrever código, mostre um plano curto: tarefas na ordem, arquivos
   que vai criar ou alterar e decisões que precisa tomar. Se alguma decisão
   for de arquitetura, proponha um ADR.
4. Implemente em fatias pequenas, cada uma com testes. Rode `make lint` e
   `make test` ao fim de cada fatia e corrija até passar.
5. Faça commits pequenos no formato `tipo(escopo): descrição` em português.
6. No fim, verifique o critério de pronto da fase item por item, mostrando a
   evidência de cada um (comando rodado e resultado).
7. Atualize o `PROGRESSO.md`: tarefas marcadas, decisões tomadas e pendências.
