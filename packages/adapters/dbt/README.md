# dengcode-adapter-dbt

TransformPort sobre dbt-core. Cada comando (`parse`, `ls`, `compile`, `run`,
`test`) roda num subprocesso com `cwd` no projeto dbt: o dbt guarda estado
global (logging, adapters, diretório atual) e não é seguro chamá-lo de forma
concorrente dentro de um app assíncrono. O subprocesso é isolado e cancelável.

Configuração na stack:

```yaml
transform:
  adapter: dbt_core
  config:
    project_dir: examples/ecommerce/dbt   # relativo à raiz do projeto
    profiles_dir: examples/ecommerce/dbt  # opcional (padrão: project_dir)
    target: dev
    timeout_s: 600
    dbt_executable: null                  # opcional (padrão: dbt ao lado do Python)
```
