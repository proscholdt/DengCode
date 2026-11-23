# Imagem do motor DengCode (multi-stage com uv).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY . .
RUN uv sync --frozen --all-packages --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --create-home dengcode
COPY --from=build /src/.venv /opt/dengcode
ENV PATH="/opt/dengcode/bin:${PATH}" PYTHONUNBUFFERED=1
USER dengcode
WORKDIR /work
ENTRYPOINT ["dengcode"]
CMD ["--help"]
