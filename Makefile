.PHONY: install backend frontend build dev

install:
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[all]'
	cd frontend && npm install

# Standalone: one process serves API + built UI at http://127.0.0.1:5173.
# (Run `make build` first so it serves the latest frontend.)
backend:
	cd backend && . .venv/bin/activate && sirius serve

frontend:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

# Dev (HMR): Vite serves the UI at :5173 and proxies /api + /ws to the backend
# on :8010 (see frontend/vite.config.ts). Open http://localhost:5173.
dev:
	@echo "Starting backend (:8010) and frontend dev server (:5173)… open http://localhost:5173"
	@(cd backend && . .venv/bin/activate && sirius serve --port 8010 & \
	  cd frontend && npm run dev & wait)
