/**
 * pipelineEditor.js — Drawflow-backed visual pipeline editor (v3 P3).
 *
 * Drawflow has no concept of typed ports, so type checking lives here: we
 * mirror the server's coercion table (shipped in GET /api/ops) and reject
 * incompatible connections on the `connectionCreated` event by immediately
 * removing them. runner.validate() on the server remains the source of truth
 * — Save and Run both re-validate there.
 *
 * Graph mapping:
 *   Drawflow node id (numeric) <-> our node id (stable string kept in node.data)
 *   Drawflow input_N / output_N (1-based) <-> our named ports (by index)
 * The implicit "input" source node is a real Drawflow node of op `__input__`
 * so the user can drag from it; it is stripped/re-added on export/import.
 */

const INPUT_OP = '__input__';

function pipelineEditor(pipelineId, publishedId = null, publishedVersion = null) {
  return {
    pipelineId,
    // Published mode (D-035): viewing an instance-wide published version.
    // Read-only like a preset; custom nodes come from the version's snapshot.
    publishedId,
    publishedVersion,
    published: null,        // GET /api/published/<id> payload in published mode
    publication: null,      // own pipeline's publish status (library mode)
    publishing: false,
    editor: null,
    catalog: {ops: [], coercions: [], custom_nodes: []},
    opsByName: {},
    customById: {},
    pipeline: null,
    title: '',
    isPreset: false,
    saving: false,
    running: false,
    dirty: false,
    issues: [],
    runError: null,
    nodePreviews: {},
    nodeValues: {},          // node id -> {port: value} for non-image outputs (D-041)
    paletteFilter: '',
    libraryResults: [],      // library ops matching paletteFilter (server search)
    libraryTotal: 0,
    _libSearchTimer: null,
    _libSearchSeq: 0,
    jsonDrafts: {},          // "nodeId:param" -> text while a JSON param doesn't parse
    sessions: [],
    selectedSessionId: '',
    selectedNodeId: null,
    loadFailed: false,

    // --- lifecycle -------------------------------------------------------

    subSpecs: {},         // "<published_id>@<version>" -> node interface (D-045)
    publishedList: [],
    savedModels: [],
    savedLabels: [],
    _assetsLoading: false,

    async init() {
      if (typeof Drawflow === 'undefined') {
        this.loadFailed = true;
        console.error('Drawflow failed to load from CDN');
        return;
      }

      const pipeUrl = this.publishedId
        ? `/api/published/${this.publishedId}` + (this.publishedVersion ? `?version=${this.publishedVersion}` : '')
        : `/api/pipelines/${this.pipelineId}`;
      const [catRes, pipeRes, sessRes] = await Promise.all([
        fetch('/api/ops'),
        fetch(pipeUrl),
        fetch('/api/sessions'),
      ]);

      if (!catRes.ok || !pipeRes.ok) {
        this.loadFailed = true;
        return;
      }
      this.catalog = await catRes.json();
      this.opsByName = Object.fromEntries(this.catalog.ops.map(o => [o.name, o]));
      this.customById = Object.fromEntries(
        (this.catalog.custom_nodes || []).map(c => [c.custom_node_id, c]));

      this.pipeline = await pipeRes.json();
      if (this.publishedId) {
        this.published = this.pipeline;
        this.publishedVersion = this.pipeline.version;
        this.title = this.pipeline.version_title;
        this.isPreset = true;   // read-only
        for (const [cid, cn] of Object.entries(this.pipeline.custom_nodes || {})) {
          this.customById[cid] = {custom_node_id: cid, ...cn};
        }
      } else {
        this.title = this.pipeline.title;
        this.isPreset = this.pipeline.is_preset;
        this.publication = this.pipeline.publication || null;
      }
      if (sessRes.ok) {
        this.sessions = await sessRes.json();
        if (this.sessions.length) this.selectedSessionId = this.sessions[0].session_id;
      }
      await this._loadLibrarySpecs((this.pipeline.graph?.nodes || []).map(n => n.op));
      await Promise.all((this.pipeline.graph?.nodes || []).filter(n => n.pipeline)
        .map(n => this._loadSubSpec(n.pipeline)));
      fetch('/api/published').then(r => r.ok ? r.json() : [])
        .then(list => { this.publishedList = list || []; });
      this.$watch('paletteFilter', () => this._searchLibrary());

      this._initDrawflow();
      this._importGraph(this.pipeline.graph);
      // Graphs from the chat / MCP / older saves have no positions: lay them
      // out. Either way, start with the whole pipeline in view.
      const nodes = this.pipeline.graph?.nodes || [];
      await this.$nextTick();
      if (nodes.length && nodes.some(n => !n.pos)) this.prettify({markDirty: false});
      else this.fitView();
    },

    _initDrawflow() {
      const container = this.$refs.canvas;
      this.editor = new Drawflow(container);
      this.editor.reroute = true;
      this.editor.start();

      this.editor.zoom_min = 0.15;
      this.editor.zoom_max = 2.5;
      // Wheel zooms around the cursor. Drawflow's own handler (Ctrl+wheel,
      // around the canvas centre) is skipped: this listener is on the wrapper,
      // in the capture phase, and stops the event before it reaches it.
      container.parentElement.addEventListener('wheel', (e) => {
        e.preventDefault();
        e.stopPropagation();
        this._zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0015)));
      }, {passive: false, capture: true});

      this.editor.on('connectionCreated', (info) => this._onConnection(info));
      this.editor.on('nodeSelected', (id) => { this.selectedNodeId = id; });
      this.editor.on('nodeUnselected', () => { this.selectedNodeId = null; });
      ['nodeCreated', 'nodeRemoved', 'nodeMoved', 'connectionRemoved', 'nodeDataChanged']
        .forEach(ev => this.editor.on(ev, () => { this.dirty = true; }));
    },

    // --- type checking ---------------------------------------------------

    _canConnect(srcType, dstType) {
      if (dstType === 'ANY' || srcType === 'ANY' || srcType === dstType) return true;
      return (this.catalog.coercions || []).some(
        c => c.src === srcType && c.dst === dstType);
    },

    _portsOf(dfNode) {
      const data = dfNode.data || {};
      if (data.op === INPUT_OP) {
        return {inputs: [], outputs: [{name: 'image', type: 'IMAGE_ANY'}]};
      }
      if (data.custom_node_id) {
        const cn = this.customById[data.custom_node_id];
        return cn ? cn.ports : {inputs: [], outputs: []};
      }
      if (data.pipeline) {
        const sp = this.subSpecs[this._subKey(data.pipeline)];
        return sp ? {inputs: sp.inputs, outputs: sp.outputs} : {inputs: [], outputs: []};
      }
      const spec = this.opsByName[data.op];
      return spec ? {inputs: spec.inputs, outputs: spec.outputs} : {inputs: [], outputs: []};
    },

    _onConnection(info) {
      const src = this.editor.getNodeFromId(info.output_id);
      const dst = this.editor.getNodeFromId(info.input_id);
      if (!src || !dst) return;

      const outIdx = parseInt(String(info.output_class).replace('output_', ''), 10) - 1;
      const inIdx = parseInt(String(info.input_class).replace('input_', ''), 10) - 1;
      const srcPort = this._portsOf(src).outputs[outIdx];
      const dstPort = this._portsOf(dst).inputs[inIdx];
      if (!srcPort || !dstPort) return;

      // The source image is a wildcard: its concrete type isn't known until
      // run time, so allow it into any image-ish port (server does the same).
      const srcIsInput = (src.data || {}).op === INPUT_OP;
      const ok = srcIsInput
        ? ['IMAGE_ANY', 'IMAGE_RGB', 'IMAGE_GRAY', 'FLOAT_MAP', 'ANY', 'ARRAY'].includes(dstPort.type)
        : this._canConnect(srcPort.type, dstPort.type);

      if (!ok) {
        this.editor.removeSingleConnection(
          info.output_id, info.input_id, info.output_class, info.input_class);
        let msg = `Can't connect ${srcPort.type} → ${dstPort.type}.`;
        if (srcPort.type === 'IMAGE_RGB' && dstPort.type === 'IMAGE_GRAY') {
          msg += ' Insert a "to_grayscale" node between them.';
        }
        this._toast(msg);
      }
    },

    _toast(message, kind = 'info') {
      window.ui.toast(message, kind);
    },

    // --- library ops (D-041) ---------------------------------------------

    async _loadLibrarySpecs(opNames) {
      // Library specs aren't in /api/ops (hundreds of them): fetch the ones a
      // graph uses before importing it.
      const want = [...new Set(opNames.filter(n => n && n.startsWith('lib:') && !this.opsByName[n]))];
      if (!want.length) return;
      const res = await fetch(`/api/ops/library?names=${encodeURIComponent(want.join(','))}`);
      if (!res.ok) return;
      for (const op of (await res.json()).ops || []) this.opsByName[op.name] = op;
    },

    _searchLibrary() {
      clearTimeout(this._libSearchTimer);
      const q = this.paletteFilter.trim();
      if (q.length < 2) { this.libraryResults = []; return; }
      const seq = ++this._libSearchSeq;
      this._libSearchTimer = setTimeout(async () => {
        const res = await fetch(`/api/ops/library?q=${encodeURIComponent(q)}&limit=40`);
        if (!res.ok || seq !== this._libSearchSeq) return;
        const data = await res.json();
        this.libraryResults = data.ops || [];
        this.libraryTotal = data.total || 0;
      }, 250);
    },

    shortName(name) {
      return name.startsWith('lib:') ? name.slice(name.lastIndexOf('.') + 1) : name;
    },

    // --- published pipelines as nodes (D-045) ------------------------------

    _subKey(ref) { return `${ref.published_id}@${ref.version}`; },

    async _loadSubSpec(ref) {
      const key = this._subKey(ref);
      if (this.subSpecs[key]) return this.subSpecs[key];
      const q = ref.version ? `?version=${ref.version}` : '';
      const res = await fetch(`/api/published/${ref.published_id}/node${q}`);
      if (!res.ok) return null;
      const itf = await res.json();
      const spec = {...itf, name: `pipeline: ${itf.title} v${itf.version}`, subpipeline: true,
                    library: {}, doc: itf.doc || `Published by ${itf.owner}`};
      this.subSpecs[this._subKey(itf)] = spec;
      if (!ref.version) this.subSpecs[key] = spec;
      return spec;
    },

    async addPipelineNode(pub) {
      const spec = await this._loadSubSpec({published_id: pub.published_id});
      if (!spec) { this._toast('Could not load that pipeline.', 'error'); return; }
      const nodeId = `${(pub.title || 'pipeline').toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 20)}_${Math.random().toString(36).slice(2, 6)}`;
      this._addNode({our_id: nodeId, pipeline: {published_id: spec.published_id, version: spec.version},
                     label: spec.name, params: {}},
                    spec.inputs.length, spec.outputs.length, 240 + Math.random() * 120, 80 + Math.random() * 220);
      this.dirty = true;
    },

    // --- palette ---------------------------------------------------------

    get paletteGroups() {
      const q = this.paletteFilter.trim().toLowerCase();
      const groups = {};
      for (const op of this.catalog.ops) {
        if (q && !op.name.includes(q) && !op.doc.toLowerCase().includes(q)
            && !op.category.includes(q)) continue;
        (groups[op.category] = groups[op.category] || []).push(op);
      }
      const customs = (this.catalog.custom_nodes || []).filter(
        c => !q || c.name.toLowerCase().includes(q));
      if (customs.length) groups['custom'] = customs.map(c => ({
        name: c.name, category: 'custom', doc: c.doc || 'Your custom node',
        inputs: c.ports.inputs, outputs: c.ports.outputs, params: c.params || [],
        custom_node_id: c.custom_node_id,
      }));
      const pubs = this.publishedList.filter(p => !p.unpublished_at && (!q
        || [p.title, p.description, p.owner].some(f => (f || '').toLowerCase().includes(q))));
      if (pubs.length) groups['published pipelines'] = pubs.slice(0, 30).map(p => ({
        name: p.title, category: 'published', doc: `${p.owner} · v${p.latest_version}${p.description ? ' · ' + p.description : ''}`,
        inputs: [], outputs: [], params: [], published_id: p.published_id,
      }));
      for (const op of this.libraryResults) {
        (groups[`library · ${op.category}`] = groups[`library · ${op.category}`] || []).push(op);
      }
      return groups;
    },

    addOpNode(op) {
      if (this.isPreset) return;
      if (op.published_id) { this.addPipelineNode(op); return; }
      if (op.library) this.opsByName[op.name] = op;
      const nodeId = `${this.shortName(op.name)}_${Math.random().toString(36).slice(2, 7)}`;
      const params = {};
      // Library ops start with no params set, so the function's own defaults apply.
      if (!op.library) (op.params || []).forEach(p => { params[p.name] = p.default; });
      this._addNode({
        our_id: nodeId,
        op: op.custom_node_id ? undefined : op.name,
        custom_node_id: op.custom_node_id,
        label: op.name,
        params,
      }, op.inputs.length, op.outputs.length, 240 + Math.random() * 120, 80 + Math.random() * 220);
      this.dirty = true;
    },

    addInputNode() {
      if (this.isPreset) return;
      if (this._findInputNode()) { this._toast('Source image node already exists.'); return; }
      this._addNode({our_id: 'input', op: INPUT_OP, label: 'source image', params: {}}, 0, 1, 40, 140);
      this.dirty = true;
    },

    _addNode(data, nIn, nOut, x, y) {
      const ports = data.op === INPUT_OP
        ? {inputs: [], outputs: [{name: 'image', type: 'IMAGE_ANY'}]}
        : (data.pipeline
            ? this._portsOf({data})
            : (data.custom_node_id
                ? this.customById[data.custom_node_id].ports
                : {inputs: this.opsByName[data.op].inputs, outputs: this.opsByName[data.op].outputs}));

      const esc = (t) => String(t).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
      const inLabels = ports.inputs.map(p =>
        `<div class="df-port-label${p.optional ? ' df-port-optional' : ''}" title="${p.optional ? 'optional' : ''}">${esc(p.name)}${p.optional ? '?' : ''}<span class="df-port-type">${p.type}</span></div>`).join('');
      const outLabels = ports.outputs.map(p =>
        `<div class="df-port-label df-out">${p.name}<span class="df-port-type">${p.type}</span></div>`).join('');
      const isInput = data.op === INPUT_OP;
      const html = `
        <div class="df-node ${isInput ? 'df-node-input' : ''} ${data.pipeline ? 'df-node-sub-pipeline' : ''}">
          <div class="df-node-title" title="${esc(data.label)}">${esc(data.label.startsWith('lib:') ? this.shortName(data.label) : data.label)}</div>
          ${data.label.startsWith('lib:') ? `<div class="df-node-sub">${esc(data.label.slice(4, data.label.lastIndexOf('.')))}</div>` : ''}
          <div class="df-node-ports">
            <div class="df-node-col">${inLabels}</div>
            <div class="df-node-col df-node-col-out">${outLabels}</div>
          </div>
        </div>`;
      this.editor.addNode(data.label, nIn, nOut, x, y, 'photon-node', data, html);
    },

    _findInputNode() {
      const nodes = this.editor.drawflow.drawflow.Home.data;
      return Object.values(nodes).find(n => (n.data || {}).op === INPUT_OP);
    },

    removeSelected() {
      if (this.isPreset || !this.selectedNodeId) return;
      this.editor.removeNodeId(`node-${this.selectedNodeId}`);
      this.selectedNodeId = null;
      this.dirty = true;
    },

    // --- selected node params -------------------------------------------

    get selectedNode() {
      if (!this.selectedNodeId || !this.editor) return null;
      return this.editor.getNodeFromId(this.selectedNodeId);
    },

    get selectedSpec() {
      const n = this.selectedNode;
      if (!n) return null;
      const d = n.data || {};
      if (d.op === INPUT_OP) return null;
      if (d.custom_node_id) {
        const cn = this.customById[d.custom_node_id];
        return cn ? {name: cn.name, params: cn.params || [], doc: cn.doc} : null;
      }
      if (d.pipeline) return this.subSpecs[this._subKey(d.pipeline)] || null;
      return this.opsByName[d.op] || null;
    },

    paramValue(name) {
      const n = this.selectedNode;
      return n ? (n.data.params || {})[name] : undefined;
    },

    isParamSet(name) {
      const n = this.selectedNode;
      return !!n && Object.prototype.hasOwnProperty.call(n.data.params || {}, name);
    },

    paramText(p) {
      // text-box view of a str/json param; drafts keep unparsable input visible
      const key = `${this.selectedNodeId}:${p.name}`;
      if (key in this.jsonDrafts) return this.jsonDrafts[key];
      if (!this.isParamSet(p.name)) return '';
      const v = this.paramValue(p.name);
      return p.kind === 'str' ? v : JSON.stringify(v);
    },

    paramPlaceholder(p) {
      if (p.required) return 'required';
      return p.default === null || p.default === undefined ? 'library default' : `default: ${JSON.stringify(p.default)}`;
    },

    jsonInvalid(p) {
      return `${this.selectedNodeId}:${p.name}` in this.jsonDrafts;
    },

    // Saved models / painted labels pickers (D-044)
    assetParam(p) {
      const op = this.selectedSpec && this.selectedSpec.name;
      if (op === 'ml_load_model' && p.name === 'model_id') { this._loadAssets(); return 'model'; }
      if (op === 'load_labels' && p.name === 'annotation_id') { this._loadAssets(); return 'labels'; }
      return null;
    },

    _loadAssets() {
      if (this._assetsLoading) return;
      this._assetsLoading = true;
      fetch('/api/models').then(r => r.ok ? r.json() : {models: []})
        .then(d => { this.savedModels = d.models || []; });
      fetch('/api/annotations').then(r => r.ok ? r.json() : {annotations: []})
        .then(d => { this.savedLabels = d.annotations || []; });
    },

    setParam(name, value, kind) {
      const n = this.selectedNode;
      if (!n || this.isPreset) return;
      const params = {...(n.data.params || {})};
      const key = `${this.selectedNodeId}:${name}`;
      const unset = value === '' || value === null || (typeof value === 'number' && Number.isNaN(value));
      delete this.jsonDrafts[key];
      if (unset && (kind === 'json' || kind === 'str' || this.opsByName[n.data.op]?.library)) {
        delete params[name];             // back to the function's default
      } else {
        let v = value;
        if (kind === 'int') v = parseInt(value, 10);
        else if (kind === 'float') v = parseFloat(value);
        else if (kind === 'bool') v = !!value;
        else if (kind === 'json') {
          try { v = JSON.parse(value); }
          catch { this.jsonDrafts = {...this.jsonDrafts, [key]: value}; return; }
        }
        params[name] = v;
      }
      this.jsonDrafts = {...this.jsonDrafts};
      this.editor.updateNodeDataFromId(n.id, {...n.data, params});
      this.dirty = true;
    },

    get selectedValues() {
      const n = this.selectedNode;
      return n ? this.nodeValues[(n.data || {}).our_id] || null : null;
    },

    formatValue(v) {
      const t = JSON.stringify(v);
      return t && t.length > 400 ? t.slice(0, 400) + '…' : t;
    },

    // --- graph <-> drawflow ---------------------------------------------

    _exportGraph() {
      const dfNodes = this.editor.drawflow.drawflow.Home.data;
      const nodes = [];
      const edges = [];
      const idOf = {};   // drawflow numeric id -> our string id

      for (const [dfId, n] of Object.entries(dfNodes)) {
        idOf[dfId] = (n.data || {}).our_id || `n${dfId}`;
      }

      for (const [dfId, n] of Object.entries(dfNodes)) {
        const d = n.data || {};
        if (d.op === INPUT_OP) continue;   // implicit on the server side
        const node = {id: idOf[dfId], params: d.params || {}};
        if (d.custom_node_id) node.custom_node_id = d.custom_node_id;
        else if (d.pipeline) node.pipeline = d.pipeline;
        else node.op = d.op;
        node.pos = {x: n.pos_x, y: n.pos_y};   // round-trips layout; server ignores it
        nodes.push(node);
      }

      for (const [dfId, n] of Object.entries(dfNodes)) {
        const dstPorts = this._portsOf(n).inputs;
        Object.entries(n.inputs || {}).forEach(([inputClass, conn]) => {
          const inIdx = parseInt(inputClass.replace('input_', ''), 10) - 1;
          const dstPort = dstPorts[inIdx];
          if (!dstPort) return;
          (conn.connections || []).forEach(c => {
            const srcNode = dfNodes[c.node];
            if (!srcNode) return;
            const outIdx = parseInt(String(c.input).replace('output_', ''), 10) - 1;
            const srcPort = this._portsOf(srcNode).outputs[outIdx];
            if (!srcPort) return;
            edges.push({
              from: [idOf[c.node], srcPort.name],
              to: [idOf[dfId], dstPort.name],
            });
          });
        });
      }

      return {nodes, edges};
    },

    _importGraph(graph) {
      this.editor.clear();
      const dfIdByOurId = {};

      // Implicit source node, positioned left of everything else.
      this._addNode({our_id: 'input', op: INPUT_OP, label: 'source image', params: {}}, 0, 1, 40, 160);
      dfIdByOurId['input'] = this.editor.nodeId - 1;

      (graph.nodes || []).forEach((node, i) => {
        const isCustom = !!node.custom_node_id;
        const isSub = !!node.pipeline;
        const spec = isSub ? this.subSpecs[this._subKey(node.pipeline)]
          : (isCustom ? this.customById[node.custom_node_id] : this.opsByName[node.op]);
        if (!spec) {
          this.issues.push({node_id: node.id,
            message: isSub ? `Published pipeline ${node.pipeline.published_id} v${node.pipeline.version} is not available (unpublished?) — skipped.`
              : `Unknown ${isCustom ? 'custom node' : 'op'} "${node.op || node.custom_node_id}" — skipped.`});
          return;
        }
        const ports = isCustom ? spec.ports : {inputs: spec.inputs, outputs: spec.outputs};
        const pos = node.pos || {x: 260 + (i % 4) * 210, y: 60 + Math.floor(i / 4) * 170};
        this._addNode({
          our_id: node.id,
          op: isCustom || isSub ? undefined : node.op,
          custom_node_id: node.custom_node_id,
          pipeline: node.pipeline,
          label: isSub ? spec.name : (isCustom ? `custom:${spec.name}` : node.op),
          params: node.params || {},
        }, ports.inputs.length, ports.outputs.length, pos.x, pos.y);
        dfIdByOurId[node.id] = this.editor.nodeId - 1;
      });

      (graph.edges || []).forEach(e => {
        const [srcId, srcPort] = e.from;
        const [dstId, dstPort] = e.to;
        const srcDf = dfIdByOurId[srcId];
        const dstDf = dfIdByOurId[dstId];
        if (srcDf === undefined || dstDf === undefined) return;

        const srcNode = this.editor.getNodeFromId(srcDf);
        const dstNode = this.editor.getNodeFromId(dstDf);
        const outIdx = this._portsOf(srcNode).outputs.findIndex(p => p.name === srcPort);
        const inIdx = this._portsOf(dstNode).inputs.findIndex(p => p.name === dstPort);
        if (outIdx < 0 || inIdx < 0) return;
        this.editor.addConnection(srcDf, dstDf, `output_${outIdx + 1}`, `input_${inIdx + 1}`);
      });

      this.dirty = false;
    },

    // --- actions ---------------------------------------------------------

    async validate() {
      // A published version was validated at publish time against its own
      // snapshot; the server re-validates on run. /api/pipelines/validate only
      // knows the viewer's library, so it would reject the author's custom nodes.
      if (this.publishedId) { this.issues = []; return true; }
      const graph = this._exportGraph();
      const res = await fetch('/api/pipelines/validate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({graph}),
      });
      const data = await res.json();
      this.issues = data.issues || [];
      return data.valid;
    },

    async save() {
      if (this.isPreset || this.saving) return;
      this.saving = true;
      try {
        const graph = this._exportGraph();
        const res = await fetch(`/api/pipelines/${this.pipelineId}`, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({title: this.title, graph}),
        });
        if (!res.ok) throw new Error((await res.json()).error || 'Save failed');
        this.dirty = false;
        await this.validate();   // surface issues without blocking the save
      } catch (e) {
        this._toast(e.message, 'error');
      } finally {
        this.saving = false;
      }
    },

    async cloneToMine() {
      const res = await fetch('/api/pipelines', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({clone_from: this.pipelineId}),
      });
      const data = await res.json();
      if (res.ok) window.location.href = `/pipelines/${data.pipeline_id}`;
      else this._toast(data.error || 'Clone failed', 'error');
    },

    async publish() {
      if (this.isPreset || this.publishing) return;
      if (this.dirty) {
        const ok = await window.ui.confirm({title: 'Unsaved changes', message: 'Publishing uses the saved pipeline. Save your changes and continue?', confirmText: 'Save and continue'});
        if (!ok) return;
        await this.save();
        if (this.dirty) return;
      }
      const pub = this.publication;
      const next = pub ? `v${pub.latest_version + 1}` : 'v1';
      const answer = await window.ui.prompt({
        title: `Publish ${next}`,
        message: 'Everyone on this Photon instance will be able to run and fork this version, including the code of any custom nodes it uses. Published versions never change.',
        label: pub ? 'What changed?' : 'Notes (optional)',
        placeholder: pub ? 'e.g. tuned the threshold for low-contrast scans' : '',
        multiline: true,
        confirmText: 'Publish',
        checkbox: {label: 'Show my latest output of this pipeline as the card image (visible to everyone)', checked: true},
      });
      if (answer === null) return;
      this.publishing = true;
      try {
        const res = await fetch(`/api/pipelines/${this.pipelineId}/publish`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({changelog: answer.value, thumbnail: answer.checked}),
        });
        const data = await res.json();
        if (!res.ok) {
          this.issues = data.issues || [];
          this._toast(data.error || 'Publish failed', 'error');
          return;
        }
        this.publication = {published_id: data.published_id, latest_version: data.version, unpublished: false};
        let note = data.created ? `Published v${data.version}.` : `No changes since v${data.version}; nothing new published.`;
        if (answer.checked && !data.thumbnail) note += ' No card image yet: run this pipeline once, then publish again.';
        else if (answer.checked && !data.created) note += ' Card image refreshed.';
        this._toast(note, data.created ? 'success' : 'info');
      } finally {
        this.publishing = false;
      }
    },

    async unpublish() {
      if (!this.publication) return;
      const ok = await window.ui.confirm({title: 'Unpublish pipeline?', message: 'Colleagues can no longer see or run it. Its versions are kept, and publishing again restores it.', confirmText: 'Unpublish', danger: true});
      if (!ok) return;
      const res = await fetch(`/api/published/${this.publication.published_id}/unpublish`, {method: 'POST'});
      if (res.ok) { this.publication = {...this.publication, unpublished: true}; this._toast('Unpublished.', 'success'); }
      else this._toast('Unpublish failed', 'error');
    },

    async fork() {
      const res = await fetch(`/api/published/${this.publishedId}/fork`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({version: this.publishedVersion}),
      });
      const data = await res.json();
      if (res.ok) window.location.href = `/pipelines/${data.pipeline_id}`;
      else this._toast(data.error || 'Fork failed', 'error');
    },

    switchVersion(v) {
      window.location.href = `/published/${this.publishedId}?version=${v}`;
    },

    async run() {
      if (this.running) return;
      if (!this.selectedSessionId) { this._toast('Pick a session to run against.'); return; }
      if (!(await this.validate())) return;

      this.running = true;
      this.runError = null;
      this.nodePreviews = {};
      this.nodeValues = {};
      const graph = this._exportGraph();

      try {
        const res = await fetch(`/api/sessions/${this.selectedSessionId}/run-pipeline`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(this.publishedId
            ? {published_id: this.publishedId, version: this.publishedVersion}
            : {graph, title: this.title}),
        });
        const data = await res.json();
        if (!res.ok) {
          this.issues = data.issues || [{node_id: null, message: data.error || 'Run failed'}];
          this.running = false;
          return;
        }
        this._listenToRun();
      } catch (e) {
        this.runError = e.message;
        this.running = false;
      }
    },

    _listenToRun() {
      const es = new EventSource(`/api/sessions/${this.selectedSessionId}/stream`);
      es.onmessage = (e) => {
        const ev = JSON.parse(e.data);
        if (ev.event === 'stream_end') { es.close(); this.running = false; return; }
        if (ev.event === 'node_previews') {
          this.nodePreviews = {...this.nodePreviews, ...ev.previews};
        }
        if (ev.event === 'node_values' && ev.image_slot === 0) {
          this.nodeValues = {...this.nodeValues, ...ev.values};
        }
        if (ev.event === 'pipeline_error') {
          this.runError = `${ev.failed_node || 'pipeline'}: ${ev.message}`;
        }
        if (ev.event === 'model_saved') {
          this.savedModels = [ev.model, ...this.savedModels];
          window.ui && window.ui.toast(`Saved model "${ev.model.name}" (${ev.model.estimator}).`, 'success');
        }
      };
      es.onerror = () => { es.close(); this.running = false; };
    },

    // --- layout ------------------------------------------------------------

    _nodeSize(dfId) {
      const el = document.getElementById(`node-${dfId}`);
      return el ? {w: el.offsetWidth || 180, h: el.offsetHeight || 80} : {w: 180, h: 80};
    },

    // Arrange every node left to right in dependency order (graphLayout.js).
    prettify({markDirty = true} = {}) {
      if (!this.editor || typeof layoutGraph !== 'function') return;
      const data = this.editor.drawflow.drawflow.Home.data;
      const ids = Object.keys(data);
      if (!ids.length) return;
      const edges = [];
      for (const [id, n] of Object.entries(data)) {
        for (const out of Object.values(n.outputs || {})) {
          for (const c of out.connections || []) edges.push([id, String(c.node)]);
        }
      }
      const pos = layoutGraph(ids, edges, id => this._nodeSize(id), {gapX: 70, gapY: 28});
      for (const id of ids) {
        const p = pos[id];
        data[id].pos_x = 40 + p.x;
        data[id].pos_y = 40 + p.y;
        const el = document.getElementById(`node-${id}`);
        if (el) { el.style.left = `${data[id].pos_x}px`; el.style.top = `${data[id].pos_y}px`; }
      }
      for (const id of ids) this.editor.updateConnectionNodes(`node-${id}`);
      if (markDirty && !this.isPreset) this.dirty = true;
      this.fitView();
    },

    // Zoom and pan so every node is visible (Drawflow scales around the
    // canvas centre: translate = (centre - bbox centre) * zoom).
    fitView() {
      if (!this.editor) return;
      const data = this.editor.drawflow.drawflow.Home.data;
      const ids = Object.keys(data);
      if (!ids.length) return;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const id of ids) {
        const {w, h} = this._nodeSize(id);
        x0 = Math.min(x0, data[id].pos_x); y0 = Math.min(y0, data[id].pos_y);
        x1 = Math.max(x1, data[id].pos_x + w); y1 = Math.max(y1, data[id].pos_y + h);
      }
      const box = this.$refs.canvas.getBoundingClientRect();
      const pad = 60;
      const z = Math.max(this.editor.zoom_min,
                         Math.min(1.1, (box.width - pad) / (x1 - x0), (box.height - pad) / (y1 - y0)));
      this._setView(z, (box.width / 2 - (x0 + x1) / 2) * z, (box.height / 2 - (y0 + y1) / 2) * z);
    },

    _setView(z, tx, ty) {
      const ed = this.editor;
      ed.zoom = z;
      ed.zoom_last_value = z;
      ed.canvas_x = tx;
      ed.canvas_y = ty;
      ed.precanvas.style.transform = `translate(${tx}px, ${ty}px) scale(${z})`;
      ed.dispatch('zoom', z);
    },

    // Zoom by `factor`, keeping the canvas point under (clientX, clientY) fixed.
    // Drawflow scales around the canvas centre C: screen = C + (p - C)*z + t.
    _zoomAt(clientX, clientY, factor) {
      const ed = this.editor;
      if (!ed) return;
      const z = ed.zoom;
      const z2 = Math.min(ed.zoom_max, Math.max(ed.zoom_min, z * factor));
      if (z2 === z) return;
      const r = this.$refs.canvas.getBoundingClientRect();
      const cx = r.width / 2, cy = r.height / 2;
      const mx = clientX - r.left, my = clientY - r.top;
      const px = cx + (mx - cx - ed.canvas_x) / z;      // canvas point under the cursor
      const py = cy + (my - cy - ed.canvas_y) / z;
      this._setView(z2, mx - cx - (px - cx) * z2, my - cy - (py - cy) * z2);
    },

    _zoomCenter(factor) {
      const r = this.$refs.canvas.getBoundingClientRect();
      this._zoomAt(r.left + r.width / 2, r.top + r.height / 2, factor);
    },

    zoomIn() { this.editor && this._zoomCenter(1.2); },
    zoomOut() { this.editor && this._zoomCenter(1 / 1.2); },
  };
}
