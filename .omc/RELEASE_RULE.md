# Release Rules
<!-- last-analyzed: 2026-05-09T05:10:25Z -->

## Version Sources

Two primary version files (must stay in lockstep):

- `frontend/package.json` — `"version": "X.Y.Z"` (line 4)
- `agent/pyproject.toml` — `version = "X.Y.Z"` (line 3)

Two lock files mirror the primary versions and must be bumped alongside:

- `frontend/package-lock.json` — top-level `"version"` field
- `agent/uv.lock` — the `flowboard-agent` package entry's `version` field

No automated bump tool (`bump2version`, `release-it`, `semantic-release`, `changesets`) is configured. Bumps are manual edits.

## Release Trigger

Manual: edit version files → commit `chore(release): X.Y.Z` → annotated tag `vX.Y.Z` → `git push origin <branch> && git push origin vX.Y.Z`.

No CI automation runs on tag push (no `.github/workflows/`). The tag is the canonical record; users self-update by pulling main.

## Test Gate

- Backend: `cd agent && uv run pytest` (339 tests as of 1.2.3)
- Frontend type-check: `cd frontend && npx tsc --noEmit` (used as the lint gate per `package.json`'s `"lint": "tsc -b --noEmit"`)

No CI enforces these — they're a local pre-release discipline.

## Registry / Distribution

None. This is a self-hosted dev tool. Users clone the repo and run `make install` + `make agent` + `make frontend`. No npm/PyPI/Docker publish step.

## Release Notes Strategy

- No `CHANGELOG.md` file. History lives in git log + GitHub releases (annotated tag messages).
- Commit convention: **Conventional Commits** — `feat(scope):`, `fix(scope):`, `chore(scope):`, `docs(scope):`. Scopes seen in history: `release`, `llm`, `ai-providers`, `activity`, `toolbar`, `sponsor`, `llm-gemini`.
- Release commits use `chore(release): X.Y.Z` (no `v` prefix in commit message; `v` prefix only on the tag).
- Annotated tag message convention: short, e.g. `vX.Y.Z` or a one-line summary.

## CI Workflow Files

None. Only `.github/FUNDING.yml` exists.

## First-Time Setup Gaps

- **No release CI workflow.** Tags don't trigger anything automated. If/when distribution is added, scaffold `.github/workflows/release.yml` to run tests + create the GitHub Release.
- **No `CHANGELOG.md`.** History is reconstructed from `git log` + tag messages. Acceptable for a small self-hosted tool but worth adding if external contributors grow.
- **Skipped tag `v1.1.3`** — historical artifact, not a current concern.
