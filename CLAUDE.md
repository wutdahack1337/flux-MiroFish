## Run commands
- requirements: marketdata/2026-04-05/ohlcv.csv, news/2026-04-05T01.md
- tweets: python3 backend/scripts/get_tweets.py --query bitcoin
- ohlcv: python3 backend/scripts/get_ohlcv.py
- gen real time seed: python3 research/gen_realtime_seed.py --ohlcv research/data/ohlcv/2026-04-16-07-00.json --tweets research/data/tweets/2026-04-16.json
- gen seed data: python3 backend/scripts/gen_seed.py --end-hour 2026-04-05T03 --hours 4 --count 3
- run backend: cd backend && source .venv/bin/activate && python run.py
- run prediction: ./run.sh 2026-04-05T01 10
- calculate metrics: python3 backend/scripts/calc_metrics.py price_predict/2026-04-10-12-39-06.csv

## Architecture
- This is a monorepo with a Vue frontend in frontend/ and a Flask backend in backend/.
- Backend entrypoint: backend/run.py. Flask app wiring and blueprint registration are in backend/app/__init__.py.
- API boundaries are split by blueprint:
  - backend/app/api/graph.py for graph and project workflows.
  - backend/app/api/simulation.py for simulation setup and execution.
  - backend/app/api/report.py for report generation and report chat.
- Business logic is in backend/app/services/. Keep route handlers thin and delegate non-trivial logic to services.
- Shared backend helpers are in backend/app/utils/.
- Frontend HTTP contracts are in frontend/src/api/. Keep API schema changes synchronized with backend endpoints.

## Conventions
- Preserve task/project state patterns in backend/app/models/task.py and backend/app/models/project.py (status enums + manager classes).
- Keep JSON and logs UTF-8 safe. Do not remove existing encoding safeguards in backend/run.py, backend/app/__init__.py, and backend/app/utils/logger.py.
- For transient external API failures, reuse retry helpers in backend/app/utils/retry.py instead of ad-hoc retry loops.
- For uploaded content and generated artifacts, keep paths under backend/uploads/ and maintain existing project-scoped directory layout.

## Pitfalls
- Simulation subprocess cleanup is important; preserve cleanup hooks and termination logic in simulation manager/runner paths.

## Build And Test
- Prerequisites: Node.js 18+, Python 3.11+, uv.
- Install JS dependencies: npm run setup.
- Install backend Python dependencies: npm run setup:backend.
- Run full dev stack (frontend + backend): npm run dev.
- Run only backend: npm run backend.
- Run only frontend: npm run frontend.
- Build frontend: npm run build.
- Run backend script tests: source backend/.venv/bin/activate && pytest backend/scripts/test_*.py.