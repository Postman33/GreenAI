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

Blender starts with factory settings in the background. Its output is written
to `renders/blender_render.log`; the launcher prints progress every 15 seconds.
A stalled renderer is stopped after 180 seconds, while the completed DXF and
PDF remain available. For a large scene the direct renderer accepts
`--timeout-seconds` to increase this limit. These settings apply to newly
started renderers.

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

The overview camera frames the planted bed from the road side. The pedestrian
camera favours views where proposed tree crowns do not obscure one another.
Building context comes from whole normalized footprints, including the parts
outside the work boundary. Near-zero-area fragments are excluded from the 3D
illustration. This does not change planting constraints. Building heights remain
illustrative: footprints smaller than 100 m² use 3 m; larger ones use 12 m.
`scene.json` records these assumptions under `building_context` and each
building's `height_source`. Terrain outside extracted surfaces is a schematic
ground plane; it does not reconstruct the surrounding city or its relief.

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
continuous mature shrub mass. The AFTER request uses the Blender AFTER view,
plant mask and matching BEFORE photograph as references. The Blender view
defines geometry; the BEFORE photograph supplies lighting and material
continuity. The BEFORE request uses the Blender BEFORE view. The prompt forbids
inventing buildings or changing their silhouettes, but image generation can
still introduce inconsistencies: inspect both photographs before presenting
the pair. A moved gallery uses its local scene and renders even if the index
still contains paths to its previous location. The cost limit stops *subsequent* requests based on reported
usage; it cannot guarantee a hard cap on a request already in flight. Use
`--views both --max-images 4` for both camera angles. These images are
presentation visuals; their exact boundaries and species appearance remain
conceptual, so validate placement against the Blender render and DXF. Old
galleries without `*_plant_mask.png` must be rerendered first.

The photo cache compares decoded pixels, prompts, model and color-space
metadata. Blender's PNG timestamps and render duration do not invalidate it.
An actual reference change creates a new file with a hash suffix; previous
photos and their costs remain in `generation_report.json`. Its `current_images`
mapping identifies the matching versions used by the latest invocation.
Legacy records can migrate without another request only when their original
reference bytes still match. Otherwise one regeneration is required because
the older report did not store a pixel fingerprint.

The expense ledger includes all previous versions in the selected places.
At the cost or image-count limit, the script saves `photorealistic_status.json`
beside `gallery_index.json` and returns code 2. The interactive launcher reports
this as a paused photo stage; the planting DXF and PDF remain ready. The budget
does not reset on a cache miss. To explicitly authorize additional spending,
rerun only `render_photorealistic_gallery.py` with a larger **cumulative**
`--max-cost-usd`; there is no need to recalculate planting or rerender Blender.
