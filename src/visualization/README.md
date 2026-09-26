# GreenAI visualization

The module converts the verified planting plan into a reproducible Blender
scene.  It never changes planting coordinates.  A representative 120 x 120 m
fragment is selected automatically; use explicit focus coordinates when a
specific fragment is required.

## Prepare and render

```powershell
.\scripts\run_visualization.ps1 `
  -PipelineOutput .\output\latest `
  -VisualizationOutput .\output\visualization `
  -Quality draft
```

The preparation stage works without Blender and creates `scene.json` and
`scene_preview.png`.  Rendering requires Blender. It can be located through
`PATH`, `BLENDER_EXE`, or the `-Blender` parameter.

For the current pipeline output, for example:

```powershell
.\scripts\run_visualization.ps1 `
  -PipelineOutput .\output\batch_10004141_genplan `
  -VisualizationOutput .\output\batch_10004141_genplan\blender_preview `
  -Quality draft
```

The Blender preview renders a representative 120 x 120 m fragment. Current
point-based shrub plantings are placed at their actual plan coordinates, not
scattered again across a shrub bed. The visualization uses each shrub's validated
planting-footprint radius rather than the smaller CAD symbol radius, so touching
shrub crowns read as a continuous mass. The scene includes existing trees, roads,
buildings, lawns, and proposed plantings. Heights and plant appearance are
illustrative when the DXF does not provide them.

Outputs:

- `overview_before.png` / `overview_after.png`;
- `pedestrian_before.png` / `pedestrian_after.png`;
- `top_after.png`;
- `overview_plant_mask.png` / `pedestrian_plant_mask.png` (false colors per proposed species);
- `greenai_scene.blend`;
- `render_manifest.json`.

To append the Blender views to an existing planting atlas without changing the
planting plan:

```powershell
.\.venv\Scripts\python.exe scripts\append_blender_preview.py `
  --atlas .\output\batch_10004141_genplan\planting_plan_atlas.pdf `
  --renders .\output\batch_10004141_genplan\blender_preview\renders `
  --output .\output\batch_10004141_genplan\planting_plan_atlas_with_blender.pdf
```

The appendix uses four PNG files from `renders`. They can later be replaced by
photorealistic views with the same cameras, while the CAD plan and atlas tables
remain unchanged.

## Several places with matched before/after cameras

Use the gallery command to choose three spatially separated planting locations
and render overview and pedestrian before/after pairs at each one:

```powershell
.\.venv\Scripts\python.exe scripts\render_visualization_gallery.py `
  --pipeline-output .\output\batch_10004141_genplan `
  --places 3 --radius 45 --quality draft
```

Each `place_XX/renders` directory contains four PNGs and a `.blend` scene.
`gallery_index.json` records the DXF focus coordinates, counts and both camera
transforms. For a given camera, only the proposed-planting collection is
switched between the two renders; the camera and existing surroundings stay
fixed.

Append one comparison sheet per place to the atlas:

```powershell
.\.venv\Scripts\python.exe scripts\append_blender_preview.py `
  --atlas .\output\batch_10004141_genplan\planting_plan_atlas.pdf `
  --gallery-index .\output\batch_10004141_genplan\blender_gallery\gallery_index.json `
  --output .\output\batch_10004141_genplan\planting_plan_atlas_gallery.pdf
```

No cloud account or API token is required for Blender. The deterministic
Blender images remain the source of geometry, perspective and before/after
comparison.

## Photorealistic enhancement

The OpenRouter command builds each prompt from `scene.json` and the plant catalog
in `plant_prompt.py`. The checked-in text under `prompts/` is for manual reference.
The Blender image remains the geometry reference. The enhanced image is a
conceptual visualization and must not replace the DXF or Blender validation.

OpenRouter enhancement of one matched before/after pair:

```powershell
.\.venv\Scripts\python.exe scripts\render_photorealistic_gallery.py `
  --gallery-index .\output\batch_10004141_genplan\blender_gallery\gallery_index.json `
  --places 1 --views overview --max-images 2 --max-cost-usd 0.10
```

The script reads `OPENROUTER_API_KEY` (or the older `OPENAI_API_KEY`) from the
environment or the ignored `config/openai.env`. By default it makes at most two
requests using `openai/gpt-image-2` at `quality=low`, and records actual API
cost in `place_XX/photorealistic/generation_report.json`. Existing successful
images are reused. The AFTER prompt is generated from `scene.json`: it names the
actual proposed species, checks hardiness against the project catalog when an
exact entry exists, and explains the Blender color-mask legend. It asks for a
continuous mature shrub mass. The AFTER request uses the Blender AFTER view and
plant mask as references; the BEFORE request uses the Blender BEFORE view. This
keeps the proposed geometry more influential than an AI interpretation of the
earlier photograph. The cost limit stops *subsequent* requests based on reported
usage; it cannot guarantee a hard cap on a request already in flight. Use
`--views both --max-images 4` for both camera angles. These images are
presentation visuals; their exact boundaries and species appearance remain
conceptual, so validate placement against the Blender render and DXF. Old
galleries without `*_plant_mask.png` must be rerendered first.
