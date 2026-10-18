# Infographic sources

The PNGs are rendered from these HTML files with headless Chrome at 2x:

- `hero.html` → `assets/hyatlas-v5.0.0-hero.png` (README top).
- `banner.html` → `docs/images/modes.png` (the three modes) and, downscaled to exactly 1200x600,
  `docs/images/banner.png` (the Hermes catalog `image`, which must be 1200x600).
- `how.html` → `docs/images/how-it-works.png`; `new.html` → `docs/images/whats-new-5.png`.

```
chrome --headless=new --hide-scrollbars --force-device-scale-factor=2 --window-size=1200,600 --screenshot=banner.png banner.html
```

Window sizes: hero and banner 1200x600, how 1200x612, new 1200x488. `mascot.png` is cropped from the
original banner. Keep the text in these files true to the code when a release changes it.

`hero-art.png` was generated with GPT Image 2.5 (Flare) through the Nous Portal, via Hermes's
image tool, with this prompt (the title and text are real text added in hero.html, not part of
the image):

> Wide cinematic hero illustration for an open-source AI memory project. A cute chibi anime
> scholar with fluffy dark brown hair, gold-trimmed dark hooded robe, writing with a golden
> quill in a glowing open book, sitting on the left third. Around and behind him, a dark navy
> starry sky with a luminous orrery: eight thin concentric glowing rings in soft blue, violet
> and amber, with small constellation nodes and fine lines connecting them like a knowledge
> graph. Small golden sparkles. Deep near-black background (#0b0b0d) with subtle violet and
> teal glow. The right half is calm, dark, empty negative space for a title. Polished vector
> anime style, crisp lines, high contrast. Absolutely no text, letters, numbers, logos or
> watermarks anywhere.
