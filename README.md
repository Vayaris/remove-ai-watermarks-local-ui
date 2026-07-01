# Remove AI Watermarks Local UI

Windows local web UI for scanning and cleaning AI image provenance markers using a local GPU.

Built on top of [`remove-ai-watermarks`](https://github.com/wiltodelta/remove-ai-watermarks) by `wiltodelta`, Apache-2.0.

## What It Does

- Drag-and-drop local image UI at `http://127.0.0.1:7868`
- Scan first with `remove-ai-watermarks identify --json`
- Then clean with a selected profile:
  - auto after scan
  - Gemini / Google
  - ChatGPT / OpenAI
  - Stable Diffusion / FLUX
  - metadata only
  - visible mark only
- Shows progress from 0 to 100 percent using CLI stages and denoising logs
- Uses CUDA when GPU processing is needed

## Download

Use the GitHub release asset:

`RemoveAIWatermarksLocal-v1.0.0-windows-portable.zip`

If the release is split into `.001`, `.002`, etc., download all parts into the same folder, run `JOIN_ZIP_PARTS.bat`, then extract the rebuilt `.zip`.

## Run

Extract the portable folder, then double-click:

`Start-RemoveAIWatermarks.bat`

The app opens locally at:

`http://127.0.0.1:7868`

## Notes

- NVIDIA GPU with a recent driver is recommended.
- The first GPU cleanup can be slow because Hugging Face models download into `models-cache/`.
- The release does not include Hugging Face model caches to keep downloads manageable.
- Only use this on images you own or have the right to modify.

## From Source

Run PowerShell from the repo root:

```powershell
.\scripts\build-portable.ps1
```

Then create the archive:

```powershell
.\scripts\make-release.ps1
```

## License

Apache-2.0. See [LICENSE](LICENSE).
