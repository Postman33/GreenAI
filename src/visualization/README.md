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
`scene_preview.png`.  Rendering requires Blender 4.x. It can be located through
`PATH`, `BLENDER_EXE`, or the `-Blender` parameter.

Outputs:

- `overview_before.png` / `overview_after.png`;
- `pedestrian_before.png` / `pedestrian_after.png`;
- `top_after.png`;
- `greenai_scene.blend`;
- `render_manifest.json`.

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
