# Infographic sources

The PNGs in `docs/images` (banner.png, how-it-works.png, whats-new-5.png) are rendered
from these HTML files with headless Chrome at 2x:

```
chrome --headless=new --hide-scrollbars --force-device-scale-factor=2 --window-size=1200,600 --screenshot=banner.png banner.html
```

Window sizes: banner 1200x600, how 1200x612, new 1200x488. `mascot.png` is cropped from the
original banner. Keep the text in these files true to the code when a release changes it.
