/**
 * Memory Observatory (Three.js) retired — the page now renders the starmap
 * canvas (js/starmap.js, the same component as the Hermes desktop pane).
 *
 * This stub keeps app.js's observatory call-sites harmless: it defines the
 * globals app.js reads/writes (sceneInitialized, initGraph, updateGraph,
 * showObsSeed/hideObsSeed, computeObservatoryFitZoom, obs* state) as no-ops.
 */
let sceneInitialized = false;
let obsCurrentScope = 500;
let obsAnim = null;
let obsPan = { x: 0, y: 0 };
let obsZoom = 0.6;

function showObsSeed() {}
function hideObsSeed() {}
function initGraph() {}
function updateGraph() {}
function computeObservatoryFitZoom() {
  return 0.6;
}
