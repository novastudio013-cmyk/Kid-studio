# Quality settings

Three presets. Measured on this 2-core sandbox; a GitHub runner (4 vCPU) is roughly **2× faster**.

| Preset | Renders at | Supersample | Samples | x264 | Mouth | Measured / still | 6-scene short |
|---|---|---|---|---|---|---|---|
| `draft` | 1080×1920 | 1.0× | 16 | crf 20, veryfast | 12 fps | 24 s | ~4 min |
| **`high`** ← default | 1080×1920 | **1.5×** (1620×2880) | 32–48 | crf 16, medium | 30 fps | 105 s | **~19 min** |
| `ultra` | 1080×1920 | **2.0×** (2160×3840) | 64 | crf 14, slow | 30 fps | ~240 s | ~45 min |

Delivery is always **1080×1920** (and 1920×1080 for long). Supersampling means Blender draws
*above* delivery size and the encoder downsamples with Lanczos — that is what kills jagged
edges, and it is far cheaper than raising the sample count.

## What actually changed

| | Before | Now (`high`) |
|---|---|---|
| Delivery | 864×1536 | **1080×1920** (true Full HD vertical) |
| Rendered at | 864×1536 (1.0×) | **1620×2880** (1.5×) then Lanczos down |
| Samples | 16 | 32–48 |
| Video codec | crf 19, veryfast, `Scale` | **crf 16, medium, `-tune animation`, aq-mode=3** |
| Audio | 160 kbps, 44.1 kHz | **192 kbps, 48 kHz** |
| Mouth | 12 fps | 30 fps |
| Video bitrate | ~1.4 Mbps | **~2.6 Mbps** |

The encoder changes matter as much as the render: `-tune animation` plus `aq-mode=3` stops
x264 smearing the flat colour areas that this art style is made of.

## Where the money goes

A 6-scene short at `high` costs about **19 minutes on 2 cores**, so **~10–12 min on a GitHub
runner**. Three videos a week = **~35 min/month** against a 2,000-minute free allowance.

`ultra` is roughly 2.5× that. Fine for shorts; for a 12-scene long video at `ultra` you are
looking at 90+ min and should raise `timeout-minutes` in the workflow.

## Denoising

`high` and `ultra` enable Blender's OIDN denoiser, which gives clean gradients at lower
sample counts — but it needs RAM. `resolve_quality()` checks free memory first and
**silently disables denoising below 3 GB**, compensating with more samples. On a 1.9 GB box
OIDN was OOM-killed and took the entire run down with it; that must not happen in CI.

## Pushing further

If `ultra` is still not enough:

1. **`supersample: 2.5`** — edit `QUALITY["ultra"]` in `kids.py`. Sharpness gains flatten out
   fast past 2×, so this is mostly a waste.
2. **Denoise + 128 samples** — removes almost all residual grain. Very slow on CPU.
3. **True frame-by-frame animation** — the real quality ceiling. Blender renders every frame
   instead of one still per scene, so motion is genuine 3D rather than camera drift. Needs a
   GPU and ~1,000 frames per short. This is the only change that makes a *visible* leap.
4. **Better art, not more pixels.** Right now the biggest quality limit is the character
   itself — more facial expression, props with real detail, better composition. No amount of
   resolution fixes an under-detailed model.

Honest summary: `high` is what I would ship. `ultra` is a marginal gain for 2.5× the time;
the next real step up is animation, not resolution.
