# Imagem do motor DengCode (multi-stage com uv).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
# O venv é criado no mesmo caminho em que roda na imagem final (shebangs válidos).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/dengcode
WORKDIR /src
COPY . .
RUN uv sync --frozen --all-packages --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --create-home dengcode
COPY --from=build /opt/dengcode /opt/dengcode
ENV PATH="/opt/dengcode/bin:${PATH}" PYTHONUNBUFFERED=1
USER dengcode
WORKDIR /work
ENTRYPOINT ["dengcode"]
CMD ["--help"]
