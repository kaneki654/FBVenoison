# FBVenoison fusion assets

Created with the built-in `image_gen.imagegen` tool for this project on
2026-11-16. No external image sources or stock artwork were used.

- `venoison-emblem.png`: generated RGBA image with genuine transparent alpha;
  used by the hub and access gate.
- `venoison-atmosphere.png`: generated landscape background texture. Qt scales
  and animates this image along with light and droplets.
- `venoison.ico`: application-icon sizes derived from the generated emblem
  using Pillow (format conversion only).

## Emblem prompt

Use case: logo-brand
Asset type: production desktop application emblem for FBVenoison (venom + poison combined).
Primary request: Create one premium, beautifully crafted emblem combining an angular venomous serpent head with a poison droplet. The snake curls around and merges into the droplet, an integrated single silhouette with striking sharp eyes and fangs. Original identity, futuristic precision, elegant rather than cartoonish. Designed to read at 48px and as a large application icon.
Style: dimensional polished dark obsidian and luminous emerald green on the left, electric amethyst violet on the right; subtle metallic edges, restrained emission, crisp outline. Centered square composition with ample transparent margin, high-end game-launcher branding.
Text: none. No letters or wordmark.
Constraints: genuinely transparent background, isolated emblem only, no rectangular plaque, no mockup, no watermark. Save the result as an image asset; intended for a real Qt desktop interface.

Tool parameter: `transparent_background: true`.

## Atmosphere prompt

Use case: stylized-concept
Asset type: a production background image for FBVenoison, a desktop device-management control center combining venom and poison.
Primary request: Cinematic ultra-wide dark abstract atmosphere of flowing emerald venom and amethyst poison, liquid ribbons and vapor meeting in the middle. Macro liquid textures and subtle serpentine scales along the outer edges, tiny suspended droplets, premium futuristic laboratory mood. Beautiful light tracing, deep obsidian black with emerald on the left and violet on the right.
Composition: panoramic landscape 16:9, extremely dark calm center and lower two-thirds so overlaying interface text and data remain legible; richer fluid forms at the upper right and left edges. More negative space than detail. Elegant motion-friendly seamless-feeling atmosphere for slow drift, no hard focal object.
Constraints: no UI, no buttons, no screens, no text, no logo, no watermark, no people. Finished image texture, not a UI mockup.

Tool parameter: `transparent_background: false`.
