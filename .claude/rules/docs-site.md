# Documentation site rules

When working in `docs/` or `mkdocs.yml`, read `.agents/rules/docs-site.md` for full detail.

## Key points

- `docs/` is the **human-facing** mkdocs-material site (published to GitHub Pages). It showcases the smart home system with polished content, screenshots, and architecture diagrams.
- `agent-docs/` is **agent-facing** internal reference (not published). Different audience, different purpose.
- Feature pages (`docs/features/`) explain end-to-end features across multiple apps and HA YAML — they are NOT copies of app READMEs.
- Every page must have a `nav:` entry in `mkdocs.yml`.
- Images live in `docs/img/`. Use descriptive filenames.
- Build check: `mkdocs build --strict` (CI gates PRs on this).
- Local preview: `./scripts/serve-docs.sh`

## When code changes affect the docs site

- New feature or app → consider adding/updating a feature page in `docs/features/`
- Changed architecture → update `docs/architecture/overview.md`
- New app → update `docs/apps/index.md`
- Always update the page map in `.agents/rules/docs-site.md` when adding/removing pages