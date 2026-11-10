/**
 * graphLayout.js — left-to-right layered layout for pipeline graphs.
 *
 * A small Sugiyama-style layout, enough for pipelines (tens of nodes):
 *   1. layers: each node one column right of its furthest input (longest
 *      path from the source image), then pulled right towards its consumers
 *      when it has slack, so short side branches sit next to where they're used;
 *   2. order within a column: barycenter sweeps (left->right, right->left) to
 *      cut edge crossings, keeping the best ordering seen;
 *   3. coordinates: columns as wide as their widest node; nodes stacked in
 *      their column, then nudged towards the mean height of their neighbours
 *      (no overlaps) so edges run as flat as possible.
 *
 * layoutGraph(nodes, edges, size) -> {id: {x, y}}
 *   nodes: [id, ...]; edges: [[srcId, dstId], ...]; size(id) -> {w, h}
 */
(function (root) {
  function layoutGraph(nodes, edges, size, opts = {}) {
    const gapX = opts.gapX ?? 90, gapY = opts.gapY ?? 36;
    const ids = [...new Set(nodes)];
    const preds = Object.fromEntries(ids.map(i => [i, []]));
    const succs = Object.fromEntries(ids.map(i => [i, []]));
    for (const [a, b] of edges) {
      if (a in preds && b in preds && a !== b) { succs[a].push(b); preds[b].push(a); }
    }

    // 1. layers (longest path). Kahn order; anything left in a cycle goes last.
    const indeg = Object.fromEntries(ids.map(i => [i, preds[i].length]));
    const queue = ids.filter(i => indeg[i] === 0);
    const topo = [];
    while (queue.length) {
      const n = queue.shift();
      topo.push(n);
      for (const s of succs[n]) if (--indeg[s] === 0) queue.push(s);
    }
    ids.forEach(i => { if (!topo.includes(i)) topo.push(i); });
    const layer = {};
    for (const n of topo) layer[n] = Math.max(0, ...preds[n].map(p => (layer[p] ?? -1) + 1));
    // Pull nodes with slack right, next to their first consumer (sources stay put).
    for (const n of [...topo].reverse()) {
      if (!succs[n].length || !preds[n].length) continue;
      const lim = Math.min(...succs[n].map(s => layer[s])) - 1;
      if (lim > layer[n]) layer[n] = lim;
    }
    const nLayers = Math.max(0, ...ids.map(i => layer[i])) + 1;
    let cols = Array.from({length: nLayers}, () => []);
    topo.forEach(n => cols[layer[n]].push(n));

    // 2. crossing reduction
    const pos = () => {
      const p = {};
      cols.forEach(c => c.forEach((n, i) => { p[n] = i; }));
      return p;
    };
    const crossings = () => {
      const p = pos();
      let c = 0;
      const es = [];
      for (const [a, b] of edges) if (a in layer && b in layer) es.push([a, b]);
      for (let i = 0; i < es.length; i++) {
        for (let j = i + 1; j < es.length; j++) {
          const [a1, b1] = es[i], [a2, b2] = es[j];
          if (layer[a1] !== layer[a2] || layer[b1] !== layer[b2]) continue;
          if ((p[a1] - p[a2]) * (p[b1] - p[b2]) < 0) c++;
        }
      }
      return c;
    };
    const sweep = (down) => {
      const p = pos();
      const order = down ? cols.keys() : [...cols.keys()].reverse();
      for (const li of order) {
        const nbr = down ? preds : succs;
        const bc = n => {
          const ns = nbr[n].filter(m => m in p);
          return ns.length ? ns.reduce((s, m) => s + p[m], 0) / ns.length : p[n];
        };
        cols[li] = cols[li].map((n, i) => [bc(n), i, n]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(x => x[2]);
        cols[li].forEach((n, i) => { p[n] = i; });
      }
    };
    let best = cols.map(c => [...c]), bestC = crossings();
    for (let it = 0; it < 8 && bestC > 0; it++) {
      sweep(it % 2 === 0);
      const c = crossings();
      if (c < bestC) { bestC = c; best = cols.map(c2 => [...c2]); }
    }
    cols = best;

    // 3. coordinates
    const sz = Object.fromEntries(ids.map(i => [i, size(i)]));
    const xs = [];
    let x = 0;
    cols.forEach((c, li) => { xs[li] = x; x += Math.max(0, ...c.map(n => sz[n].w)) + gapX; });
    const y = {};
    cols.forEach(c => { let yy = 0; c.forEach(n => { y[n] = yy; yy += sz[n].h + gapY; }); });
    const center = n => y[n] + sz[n].h / 2;
    for (let it = 0; it < 6; it++) {
      const down = it % 2 === 0;
      for (const li of (down ? cols.keys() : [...cols.keys()].reverse())) {
        const c = cols[li];
        const want = c.map(n => {
          const ns = (down ? preds[n] : succs[n]).concat(down ? succs[n] : preds[n]).filter(m => m in y);
          return ns.length ? ns.reduce((s, m) => s + center(m), 0) / ns.length - sz[n].h / 2 : y[n];
        });
        // place in order at the wanted height without overlapping, then move
        // the whole column back up by the average push-down it received
        let floor = -Infinity;
        c.forEach((n, i) => { y[n] = Math.max(want[i], floor); floor = y[n] + sz[n].h + gapY; });
        const push = c.reduce((s, n, i) => s + (y[n] - want[i]), 0) / Math.max(1, c.length);
        if (push > 0) c.forEach(n => { y[n] -= push; });
      }
    }
    const minY = Math.min(...ids.map(i => y[i]));
    const out = {};
    ids.forEach(i => { out[i] = {x: xs[layer[i]], y: y[i] - minY}; });
    return out;
  }

  root.layoutGraph = layoutGraph;
  if (typeof module !== 'undefined') module.exports = {layoutGraph};
})(typeof window !== 'undefined' ? window : globalThis);
