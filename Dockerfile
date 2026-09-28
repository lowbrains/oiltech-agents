FROM node:22-alpine AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/tsconfig.json ./tsconfig.json
COPY frontend/tsconfig.node.json ./tsconfig.node.json
COPY frontend/vite.config.ts ./vite.config.ts
COPY frontend/index.html ./index.html
COPY frontend/src ./src

RUN npm ci
RUN npm exec vite build


FROM python:3.13-slim

WORKDIR /app

# Зависимости (отдельным слоем для кэша)
COPY requirements.txt .
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements.txt
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi

# Chromium для PDF-экспорта дайджеста (Playwright). Тяжёлый слой (~400 МБ): собирать
# при остановленном стеке (`docker compose down` сначала) — на 1.9 ГБ RAM иначе OOM.
RUN python -m playwright install --with-deps chromium

# Код приложения и статика админки
COPY oiltech_digest ./oiltech_digest
COPY BACKLOG.md ./BACKLOG.md
COPY web ./web
COPY --from=frontend-build /frontend/dist ./frontend/dist
COPY scripts ./scripts
COPY data/seed ./data/seed

RUN mkdir -p /app/exports \
    && chmod +x ./scripts/docker-scheduler.sh

# Сборка кода (git SHA) — воркер NL сообщает её ядру вместе с номером контракта
# (contract.py). Последним слоем: смена SHA не пересобирает зависимости и Chromium.
ARG GIT_SHA=unknown
ENV OILTECH_BUILD=$GIT_SHA

EXPOSE 8000

CMD ["uvicorn", "oiltech_digest.api:app", "--host", "0.0.0.0", "--port", "8000"]
