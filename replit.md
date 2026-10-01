# Подбор релевантных контрагентов

FastAPI backend для ранжирования поставщиков, производителей и дистрибьюторов по закупкам Санкт-Петербурга.

## Run & Operate

- API запускается workflow `artifacts/api-server` на FastAPI и доступен под `/api`
- `/api/docs` — Swagger UI; `/api/healthz` — проверка
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Optional env: `DATABASE_PATH` — path to SQLite; `ADMIN_TOKEN` — guard for registry refresh

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Python 3.13, FastAPI, Uvicorn
- DB: SQLite (local, file-backed)
- The workspace retains generated TypeScript API/database packages; the running backend contract is FastAPI's `/api/openapi.json`
- Python dependencies are declared in the root `pyproject.toml`

## Where things live

- `artifacts/api-server/app/` — FastAPI routes, import pipeline, ranking engine, batch processing, and exports
- `artifacts/api-server/data/source/` — bundled source CSV files
- `artifacts/api-server/README.md` — API reference and ranking notes

## Architecture decisions

- Only the backend is in scope; do not add a frontend unless requested.
- Ranking must not depend on external neural-network APIs.
- Registry enrichment is offline: administrators upload source CSV files.
- SQLite with a local token index is the self-contained first implementation; the source specification's PostgreSQL/Elasticsearch target is a future scale-up path.

## Product

Analysts can import procurement CSVs, request explained supplier recommendations, inspect supplier and lot histories, process a batch, export CSV/XLSX, and load MSP/RNP enrichment snapshots.

## User preferences

- Use Python + FastAPI.
- Build backend only; no frontend.

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
