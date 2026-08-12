"""FPL VORTEX card rendering.

One renderer, one canvas, four categories. The approved slide backgrounds in
``assets/frames/`` carry the branding — lion mark, category banner, Premier
League crest, and the source/follow footer are all baked into the artwork, so
the code never redraws them and cannot drift away from the design.

Everything the renderer adds sits inside :data:`CONTENT_BOX`, the clear region
of the slide. Nothing is ever drawn over the header or footer bars.
"""

from .background import (
    CANVAS,
    CATEGORIES,
    CONTENT_BOX,
    Category,
    MissingFrameError,
    content_box_px,
    load_background,
    render_background,
)

__all__ = [
    "CANVAS",
    "CATEGORIES",
    "CONTENT_BOX",
    "Category",
    "MissingFrameError",
    "content_box_px",
    "load_background",
    "render_background",
]
