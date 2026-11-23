# Fase 8 — Distribuição e lançamento

**Objetivo:** qualquer engenheiro de dados instala e usa em menos de 10 minutos.

**Antes de começar:** as decisões em aberto de licença e nome precisam estar
resolvidas (ver `PROGRESSO.md`).

## Tarefas

1. **Versionamento** — SemVer; versão única do motor; adapters com
   compatibilidade declarada (`dengcode-core>=X,<Y`). Changelog automático
   (ex.: `git-cliff`) a partir dos commits.
2. **PyPI** — publicação via GitHub Actions com Trusted Publishing; meta-pacote
   `dengcode` com extras `[azure]`, `[aws]`, `[google]`, `[oss]`, `[all]`.
3. **Executável único** — PyInstaller ou Nuitka (decisão registrada em ADR);
   builds por SO/arquitetura (win-x64, linux-x64, linux-arm64, macos-arm64,
   macos-x64); variantes por preset se o tamanho exigir.
4. **Assinatura** — Windows (Authenticode) e macOS (notarização); checksums
   SHA-256 publicados.
5. **Canais** — GitHub Releases, winget, Homebrew tap, imagem Docker multi-arch
   no GHCR.
6. **Extensão** — publicar no VS Code Marketplace e no Open VSX; download do
   motor apontando para os GitHub Releases.
7. **Atualização** — `dengcode update` (detecta canal de instalação e orienta ou atualiza).
8. **Telemetria** — opt-in explícito na primeira execução; documentar o que é coletado.
9. **Documentação** (`docs/site/`, MkDocs Material): instalação por canal,
   primeiros passos por preset, configuração de stacks, segurança e permissões,
   guia "como criar um adapter" (com template via `uv init`/cookiecutter),
   referência de comandos.
10. **Template de adapter** — `dengcode adapter new <nome> --port query_engine`
    gera pacote com contrato já ligado.

## Critério de pronto

- [ ] Uma pessoa que nunca viu o projeto instala (Windows e Linux), configura o
      preset `opensource` e conclui a primeira tarefa em menos de 10 minutos,
      seguindo só a documentação.
- [ ] Release automatizada a partir de uma tag `vX.Y.Z`.
- [ ] Extensão publicada e instalável.
