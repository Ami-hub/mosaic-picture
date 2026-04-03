FROM python:3.13.12-slim AS builder

WORKDIR /app
 
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.13.12-slim AS runner
 
WORKDIR /app

COPY --from=builder /app/.venv .venv
COPY app.py app.py
COPY photomosaic.py photomosaic.py
COPY templates templates
COPY static static

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
 
EXPOSE 8000
 
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "300", "app:app"]
