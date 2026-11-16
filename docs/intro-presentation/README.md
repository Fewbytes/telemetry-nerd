# Telemetry Nerd — DevOpsDays TLV 2026 talk

Slidev deck for the 90-minute hands-on session "Telemetry Nerd: a power tool for power telemetry users".

```bash
cd docs/intro-presentation
npm install
npm run dev      # http://localhost:3030, presenter view at /presenter
npm run build    # static SPA in dist/
npm run export   # PDF (needs playwright-chromium)
```

Attendee setup handout: `PREREQS.md` (send before the session).

Slides live in `slides.md`; speaker notes are the `<!-- ... -->` blocks at the end of each slide.
Images go in `public/`. Lab data: `data/` (regenerate with `uv run data/make_release_latency.py`).
