# Video and audit report sources

Everything needed to rebuild the silent walkthrough (`docs/demo/demo.mp4`) and the audit report
(`docs/demo/AUDIT_REPORT.*`) from this repository alone. Paths below are relative to the repo root.

## What is here

| Path | What it is |
|---|---|
| `index.html` | Root of the video: theme (colours, type), and the timing of every slide |
| `compositions/bg.html`, `compositions/chrome.html` | Background grid, and the persistent project label, slide counter and progress bar |
| `compositions/s01-context.html` | Slide 01, the industry and four everyday problems |
| `compositions/s02-hook.html` | Slide 02, the project in one sentence |
| `compositions/explainer.html` | Slides 03-04, the white "how it works" chapter (edit its `CONFIG` block; the fork's breakdown ticks are the `LOOP` block) |
| `compositions/s03-architecture.html` | Slide 05, the architecture diagram |
| `compositions/s04-running.html` | Slide 06, one machine's vibration trace and work order, then the closed-loop year (`RUN` block) |
| `compositions/value.html` | Slide 07, the week's work orders and the plant-floor scene (`SHEET` block) |
| `compositions/s05-results.html`, `s06-twist.html`, `s07-end.html` | Slides 08-10, results, lesson and fix, end card |
| `assets/plant_run.json` | Work orders of the first two weeks plus their latent truth, daily cumulative failures per policy (fork seed 101) and one vibration trace, exported by `tools/export_work_orders.py` |
| `audit/facts.yaml` | Every figure (F-xx), assumption (A-xx), slide text and check expression used by the report |
| `tools/build_audit_report.py` | Builds the slide stills, re-checks every figure against the repo outputs, writes MD, DOCX and PDF |
| `tools/export_work_orders.py` | Re-runs the repo's own models, scheduler and plant for fork 1 and exports `assets/plant_run.json` (asserts the work orders equal `outputs/work_orders.json` and the failure totals equal `outputs/closed_loop_runs.csv`) |
| `tools/inject_sheet.py` | Inlines that JSON into `value.html`, `explainer.html` and `s04-running.html` (renders cannot fetch files) |
| `package.json`, `hyperframes.json`, `meta.json` | HyperFrames project files (CLI pinned to 0.8.156) |

The repo saves no plots, so the slide 06 charts are drawn from the exported numbers rather than
from matplotlib images. Fonts (Inter, JetBrains Mono, Montserrat) are referenced by name and
embedded by the HyperFrames renderer; all three are SIL Open Font License.

## Requirements

- Node 22 and `npx` (HyperFrames 0.8.156 is fetched by `npx`), plus Chrome/Chromium for rendering.
- `ffmpeg` for compressing the video and extracting slide stills.
- LibreOffice (`soffice`) to convert the report to PDF.
- The repo's own environment (`uv sync`) for `make run` and `export_work_orders.py`.
- For `build_audit_report.py` and `inject_sheet.py` (any Python 3.11+, not added to the repo's
  `pyproject.toml`): `pip install pyyaml pandas python-docx pillow numpy matplotlib`.

## Rebuild the numbers, the video and the report

```bash
# 1. repo outputs (about 2 minutes)
make run

# 2. inputs for the video (about 25 seconds)
.venv/bin/python docs/demo/source/tools/export_work_orders.py docs/demo/source/assets/plant_run.json
python3 docs/demo/source/tools/inject_sheet.py docs/demo/source/assets/plant_run.json \
  docs/demo/source/compositions --rows 0,1,2,3,4,5

# 3. render (89 s, 1920x1080, 30 fps, silent) and compress
cd docs/demo/source
npx --yes hyperframes@0.8.156 check
npx --yes hyperframes@0.8.156 render -f 30 -q looks -o renders/predictive-maintenance-master.mp4
cd -
ffmpeg -y -i docs/demo/source/renders/predictive-maintenance-master.mp4 -c:v libx264 -preset slow -crf 25 \
  -pix_fmt yuv420p -movflags +faststart -an docs/demo/demo.mp4

# 4. audit report: slide stills, automated checks, AUDIT_REPORT.md/.docx/.pdf
python3 docs/demo/source/tools/build_audit_report.py docs/demo/source/audit/facts.yaml
```

`build_audit_report.py` exits with code 1 if any figure on screen no longer matches the repo outputs.
Everything is seeded (data seed 7, closed-loop forks 101-105), so a fresh `make run` reproduces the
same numbers; the report lists which figures and slides each input affects (change-impact matrix).

The `renders/` folder (about 12 MB) is not committed; step 3 recreates it.
