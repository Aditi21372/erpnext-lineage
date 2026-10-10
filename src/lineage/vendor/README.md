# Vendored JavaScript

Loaded in this order (see `viewer.VENDOR_FILES`):

| file | package | license |
|---|---|---|
| `graphology.umd.min.js` | graphology 0.25.4 | MIT |
| `sigma.min.js` | sigma 3.0.2 | MIT |
| `d3-dispatch.min.js` | d3-dispatch 3.0.1 | ISC |
| `d3-quadtree.min.js` | d3-quadtree 3.0.1 | ISC |
| `d3-timer.min.js` | d3-timer 3.0.1 | ISC |
| `d3-force.min.js` | d3-force 3.0.0 | ISC |

fetched from `https://cdn.jsdelivr.net/npm/<package>@<version>/<file>`.
Vendored (not CDN-loaded) so pages work offline and pin exact versions.
Sigma.js renders WebGL; d3-force computes the one-shot layout.
