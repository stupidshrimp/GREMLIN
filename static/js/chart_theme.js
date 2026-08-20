/* The chart palette, read out of the stylesheet.
 *
 * Everything else in the application is themed by naming a token in CSS and
 * letting the cascade do the rest. A canvas cannot be: it is a bitmap, not a
 * tree, so it inherits nothing, and the pixels an axis label was drawn in stay
 * that colour until something draws over them. Both halves of that are handled
 * here -- the palette is read back out of :root so the values still live in
 * theme.css and nowhere else, and a repaint is asked for whenever the theme
 * changes.
 *
 * Loaded by the two pages that draw charts, before the file that draws them. */
(function () {
  const root = document.documentElement;

  // getComputedStyle is a layout read and these are wanted inside drawing loops
  // -- once per bar, in the worst case -- so the whole palette is resolved once
  // and handed out until something invalidates it.
  let cache = null;

  function read(name, fallback) {
    const value = getComputedStyle(root).getPropertyValue(name).trim();
    // A token that is missing (or a browser that will not give it up) resolves
    // to the empty string, which as a fillStyle is silently ignored and leaves
    // the canvas painting in whatever colour it last used. The fallback is what
    // keeps that from turning into an invisible chart.
    return value || fallback;
  }

  function build() {
    const series = [];
    const types = [];
    for (let i = 1; i <= 7; i += 1) {
      series.push(read("--chart-series-" + i, "#3f5e77"));
      types.push(read("--chart-type-" + i, "#5b7f8c"));
    }

    return {
      // Chrome.
      grid: read("--chart-grid", "#eef2f6"),
      axis: read("--chart-axis", "#c4d2dd"),
      label: read("--chart-label", "#5e7082"),
      ink: read("--chart-ink", "#2a3f50"),
      band: read("--chart-band", "#dceee4"),
      pointFill: read("--chart-point-fill", "#ffffff"),

      // Series.
      bar: read("--chart-bar", "#3f5e77"),
      barAlt: read("--chart-bar-alt", "#7fa6c0"),
      average: read("--chart-average", "#1f4d33"),
      brand: read("--chart-brand", "#0a3a27"),
      danger: read("--chart-danger", "#c0392b"),
      dangerDeep: read("--chart-danger-deep", "#7d2b2b"),
      accent: read("--chart-accent", "#2f8f5b"),
      highlight: read("--chart-highlight", "#c2723b"),
      goal: read("--chart-goal", "#d6a700"),
      series: series,
      types: types,
    };
  }

  window.gremlinChartPalette = function () {
    if (!cache) cache = build();
    return cache;
  };

  // Each page registers the one function that redraws whatever it currently has
  // on screen. Kept as a list rather than a single slot so a page with more than
  // one chart module does not have to pick which of them gets to repaint.
  const repainters = [];

  window.gremlinOnThemeChange = function (repaint) {
    if (typeof repaint === "function") repainters.push(repaint);
  };

  document.addEventListener("gremlin:themechange", function () {
    cache = null;

    // After the next frame. The event is dispatched as the attribute is set, and
    // inside a view transition that happens while the browser is holding the old
    // page as a photograph -- so a repaint fired synchronously would be drawn
    // with the new palette but captured into the *old* snapshot, and the wipe
    // would reveal a chart that had already changed. Waiting a frame puts the
    // redraw safely after the snapshot has been taken.
    requestAnimationFrame(function () {
      repainters.forEach(function (repaint) {
        try {
          repaint();
        } catch (e) {
          // One chart module failing to redraw must not stop the others; the
          // page is still usable with a stale chart on it, and it is not worth
          // taking the rest of the page down over.
          if (window.console && console.error) console.error(e);
        }
      });
    });
  });
})();
