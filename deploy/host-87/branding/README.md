# Branding assets for the chat UI

`cistup_iisc_logo.svg` is the CiSTUP / IISc lockup, 1402x578.

Open WebUI serves **PNG**, so the SVG is the source and the PNGs beside it are
generated from it. Regenerate rather than hand-editing them:

```bash
docker run --rm -v "$PWD:/w" -w /w minidocks/librsvg \
  rsvg-convert -w 512 -h 211 cistup_iisc_logo.svg -o splash.png
```

The lockup is **wide**, which is the whole difficulty. It reads well on the splash
and login screen and becomes an illegible smudge at favicon size, so the square
assets are cropped to the mark alone rather than scaled down from the full
lockup. A 32px-wide version of a 1402px-wide image is not a small logo, it is a
grey rectangle.

## Which files Open WebUI actually reads

Verify against the pinned tag before mounting anything -- these paths have moved
between releases, and a bind-mount onto a path that does not exist creates a
DIRECTORY there, which breaks the container in a way that does not mention
mounts:

```bash
docker compose exec open-webui sh -c \
  'find /app -maxdepth 4 \( -name "*.png" -o -name "*.svg" \) | grep -iv node_modules'
```

The same file usually has to be replaced in **two** trees -- the built frontend
and the backend's static directory -- or you get the new logo on the browser tab
and the old one on the splash screen.

## Trademark

This is institutional branding, not project code. The repository's Apache-2.0
licence covers the code and does not grant any right to the CiSTUP or IISc marks.
Keep it that way: do not use these assets on anything that is not this Centre's
own deployment.
