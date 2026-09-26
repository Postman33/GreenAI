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
scattered again across a shrub bed. The scene includes existing trees, roads,
buildings, lawns, and proposed plantings. Heights and plant appearance are
illustrative when the DXF does not provide them.

Outputs:

- `overview_before.png` / `overview_after.png`;
- `pedestrian_before.png` / `pedestrian_after.png`;
- `top_after.png`;
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

No cloud account or API token is required.  A token is needed only for an
optional later AI relighting/inpainting step.  The deterministic Blender images
remain the source of geometry, perspective and before/after comparison.

## Photorealistic enhancement

The checked-in prompts under `prompts/` convert `overview_after.png` and
`pedestrian_after.png` into presentation images.  The Blender image is always
the geometry reference.  The enhanced image is a conceptual visualization and
must not replace the DXF or the Blender render during validation.

Codex/ChatGPT built-in image generation does not require a project API key. To
run the same enhancement automatically from a standalone backend, configure a
cloud image provider and its API key. Keep credentials in an environment
variable; never put them in the repository or `scene.json`.
