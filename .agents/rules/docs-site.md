# Documentation site standards (mkdocs-material)

> **Applies to:** `docs/**`, `mkdocs.yml`

The `docs/` directory is a human-facing documentation site published to GitHub Pages via mkdocs-material. It showcases the smart home system, explains features end-to-end, and serves as a polished reference for anyone browsing the repo. This is NOT agent-internal documentation (that lives in `agent-docs/`).

## Audience and tone

- **Audience**: Humans — homeowners, smart home enthusiasts, developers exploring the repo
- **Tone**: Conversational but informative. Explain *why* things work the way they do, not just *what* they do
- **Visuals**: Use screenshots, architecture diagrams, and device photos wherever possible
- **Code**: Show config snippets and YAML examples inline when they help explain a concept

## Site structure

```
docs/
├── index.md                        # Landing page: highlights, system overview, AI journey
├── features/
│   ├── camera-notifications.md     # GenAI detection summaries + door alerts
│   ├── occupancy-lighting.md       # mmWave + Inovelli zone-based lighting
│   └── photo-frame.md             # Immich photo slideshow on wall displays
├── architecture/
│   └── overview.md                # System diagram, data flows, key concepts
├── apps/
│   └── index.md                   # AppDaemon app listing + dependency graph
├── setup/
│   └── getting-started.md         # Dev environment setup
└── img/                           # All images for the docs site
```

Navigation is defined in `mkdocs.yml` under `nav:`. Every page in `docs/` must have a `nav:` entry.

## Page map

Update this map when adding or removing pages.

| Page | Path | Covers |
|------|------|--------|
| Home | `docs/index.md` | Highlights, how it works, AI journey, explore links |
| GenAI Camera Notifications | `docs/features/camera-notifications.md` | detection_summary_app, detection_summary_viewer, door_notify |
| Occupancy-Based Lighting | `docs/features/occupancy-lighting.md` | HA automations, blueprints, Inovelli/Zooz switches |
| Immich Photo Frame | `docs/features/photo-frame.md` | immich_fetcher, photo_frame_viewer |
| Architecture Overview | `docs/architecture/overview.md` | System diagram, data flows, self-provisioning, relay pattern |
| AppDaemon Apps | `docs/apps/index.md` | App listing, provider listing, dependency graph |
| Getting Started | `docs/setup/getting-started.md` | Clone, venv, secrets, run, test, serve docs |

## Content rules

### Keep in sync with the codebase

Feature pages must reflect reality. When code changes affect a feature page:

- **New app or feature** → update or create the relevant feature page
- **Changed architecture** (new data flow, renamed app, new provider) → update `docs/architecture/overview.md`
- **New app added** → update `docs/apps/index.md` app listing and dependency graph
- **Changed config** → update any config snippets shown in feature pages
- **Removed feature** → remove or archive the feature page, update `nav:` in `mkdocs.yml`

### Relationship to app READMEs

Feature pages are **not** copies of app READMEs. They serve different purposes:

| | Feature page (`docs/features/`) | App README (`appdaemon/apps/*/README.md`) |
|---|---|---|
| **Audience** | End users, showcase readers | Agents and developers |
| **Scope** | End-to-end feature across multiple apps + HA YAML | Single app internals |
| **Depth** | How the feature works and why it's cool | Config keys, entity tables, provisioning details |
| **Images** | Screenshots, device photos, dashboards | None (or minimal) |

Feature pages should link to app READMEs for detailed config, not duplicate them.

### Images

- All images live in `docs/img/`
- Reference with relative paths: `![alt text](../img/filename.png)` or `![alt text](img/filename.png)` from `index.md`
- Use descriptive filenames: `garage-detection-notification.png` not `IMG_1234.jpg`
- Include screenshots of dashboards, push notifications, and physical devices to make pages engaging
- Optimize images for web (compress large screenshots)

### Home Assistant YAML references

When a feature involves HA YAML, include a table mapping to the relevant files:

```markdown
| Area | Path |
|------|------|
| Automations | `home-assistant/automations/occupancy-based-lighting/` |
| Scripts | `home-assistant/scripts/inovelli/` |
| Cards | `home-assistant/cards/basement/rumpus-room/` |
```

This helps readers navigate the repo and understand what lives where.

## Building and previewing

```bash
# Local preview (from repo root with venv active)
./scripts/serve-docs.sh

# Build check (used in CI)
mkdocs build --strict

# Deploy to GitHub Pages
mkdocs gh-deploy
```

The `docs-build.yml` workflow gates PRs on `mkdocs build --strict` passing.
The `deploy-docs.yml` workflow deploys to GitHub Pages on push to `main`.

## When creating a new feature page

1. Create the markdown file in `docs/features/`
2. Add it to the `nav:` section in `mkdocs.yml`
3. Add it to the page map in this rule
4. Include at minimum: overview, how it works, architecture/data flow, relevant HA YAML paths, links to app READMEs
5. Add TODO comments for screenshots and content to be filled in later
