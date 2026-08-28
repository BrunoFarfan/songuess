# Show available repository commands.
default:
  @just --list

# Run every application test and static build check.
check:
  cd backend && just lint && just test
  cd frontend && just lint && just test && just build

# Build the Astro assets consumed by the Worker.
build:
  cd frontend && just build

# Apply all schema migrations to Wrangler's local D1 database.
d1-migrate-local:
  cd backend && uv run pywrangler d1 migrations apply songuess-local --local

# Import a generated application-only catalog into local D1.
d1-import-local file:
  cd backend && uv run pywrangler d1 execute songuess-local --local --file ../{{file}}

# Export the enabled application catalog and deterministic manifest.
export-d1 output="release/catalog.sql":
  uv run --project backend python -m dataset.export_d1 --output {{output}}

# Run the complete Worker and static assets through workerd.
worker-dev:
  cd backend && uv run pywrangler dev

# Validate the Worker bundle without publishing it.
worker-dry-run:
  cd frontend && just build
  cd backend && uv run pywrangler deploy --dry-run --env ""
