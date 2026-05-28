/* CardScanner — Alpine.js dashboard logic (Round 2 + Round 3) */

// ---------------------------------------------------------------------------
// C2: Confidence dot color helper
// Returns Tailwind bg color class based on comp_confidence string.
// ---------------------------------------------------------------------------
function confidenceDotColor(conf) {
  if (conf === 'high')   return 'bg-emerald-400';
  if (conf === 'medium') return 'bg-amber-400';
  if (conf === 'low')    return 'bg-rose-400';
  return 'bg-slate-600';
}

// ---------------------------------------------------------------------------
// C2: Parse a JSON-string field defensively (array or object)
// ---------------------------------------------------------------------------
function safeParseJSON(str, fallback) {
  if (!str) return fallback;
  try { return JSON.parse(str); } catch (e) { return fallback; }
}

// ---------------------------------------------------------------------------
// Chart hash memoization (B6)
// ---------------------------------------------------------------------------
function _jsonHash(obj) {
  const s = JSON.stringify(obj);
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
  }
  return h;
}

// ---------------------------------------------------------------------------
// Virtual scroller (B1)
// Renders only the rows visible in the scroll window plus a buffer.
// ---------------------------------------------------------------------------
const ROW_HEIGHT = 48;   // px — must match CSS
const VIRT_BUFFER = 5;   // extra rows above/below viewport

function virtualizeInventory(allItems, scrollTop, viewportHeight) {
  const visibleCount = Math.ceil(viewportHeight / ROW_HEIGHT);
  const firstVisible = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - VIRT_BUFFER);
  const lastVisible  = Math.min(allItems.length - 1, firstVisible + visibleCount + VIRT_BUFFER * 2);
  return {
    paddingTop:    firstVisible * ROW_HEIGHT,
    paddingBottom: Math.max(0, (allItems.length - 1 - lastVisible) * ROW_HEIGHT),
    rows:          allItems.slice(firstVisible, lastVisible + 1),
    startIndex:    firstVisible,
  };
}

// ---------------------------------------------------------------------------
// Main Alpine component
// ---------------------------------------------------------------------------
function cardscanner() {
  return {
    // ---- global state ----
    tab: 'Dashboard',
    dragOver: false,
    batchYear: '',
    batchSetBrand: '',
    job: null,
    jobPoll: null,
    uploadQueue: [],      // [{file, status, job, label}] — pending video uploads
    queuePoll: null,
    queueProcessing: false,
    stats: {},
    insights: [],
    actionQ: {},
    recent: [],

    // ---- inventory (server-driven, B2) ----
    inventory: [],          // full page from server
    invTotal: 0,            // total matching count from server
    invLoading: false,
    invOffset: 0,
    invLimit: 200,

    // ---- virtual scroller (B1) ----
    virtRows: [],           // slice currently rendered
    virtPaddingTop: 0,
    virtPaddingBottom: 0,
    virtStartIndex: 0,
    invScrollTop: 0,
    invViewportHeight: 400,

    // ---- filter state (B2 + C4) ----
    search: '',
    filterHits: false,
    filterReview: false,
    sortBy: 'recent',
    filterEra: [],
    filterSport: [],
    filterEbayStatus: '',
    filterMinValue: '',
    filterMaxValue: '',
    filterAutograph: false,
    filterRelic: false,
    filterGraded: false,
    filterLowConfidence: false,   // C4: low comp confidence filter chip
    facets: { eras: {}, ebay_status: {}, value_buckets: {}, tags: {} },

    // ---- bulk actions (B3) ----
    selectedIds: [],
    bulkActionOpen: false,
    bulkStatusTarget: '',
    bulkChannelTarget: '',
    bulkNoteText: '',
    bulkLotLabel: '',
    bulkLocationTarget: '',
    bulkLocationPosition: '',

    // ---- storage locations (m10) ----
    storageLocations: [],         // [{id,name,kind,notes,card_count,created_at}]
    storageFilterId: '',          // current inventory storage filter (string for select compat)
    newLocation: { open: false, name: '', kind: 'binder', notes: '' },
    // m11: "where did this batch come from?" prompt
    syncPrompt: { open: false, locationId: '', position: '' },

    // ---- keyboard nav (B4) ----
    focusedRowIndex: -1,
    showKeyboardHelp: false,
    inventoryFirstVisit: true,
    showKeyboardHint: false,
    _ghPending: false,   // for g→h sequence

    // ---- activity stripe (B5) ----
    stripe: { scanned24h: 0, hits24h: 0, value24h: 0, newCardIds: [] },
    stripeFilterActive: false,

    // ---- chart memoization (B6) ----
    _lastChartHash: null,
    eraChart: null,
    playerChart: null,

    // ---- suggestions (C1) ----
    suggestions: [],        // raw items from /api/suggestions
    suggestionsLoading: false,
    dismissedSuggestions: new Set(), // local dismiss for grade-kind cards
    showGradeInfoModal: false,
    gradeInfoCard: null,

    // ---- C2: grading queue ----
    gradingQueue: [],            // items added from suggestions
    showGradingBatchModal: false,
    gradingService: 'PSA',
    gradingServiceLevel: 'Value',
    gradingBatchSending: false,
    gradingQueues: [],           // in-flight submissions from server
    showMarkBackModal: false,
    markBackQueue: null,
    markBackGrades: [],

    // ---- C1: Money tab ----
    salesSummary: {},
    recentSales: [],
    salesSyncing: false,
    showManualSaleModal: false,
    manualSale: { listing_id: null, sold_price: null, fees: null, channel: 'eBay', sold_at: '' },
    salesByMonthChart: null,
    salesChannelChart: null,
    _lastSalesHash: null,

    // ---- C3: Bulk lot proposals ----
    bulkLotProposals: [],
    showBuildLotModal: false,
    activeLotProposal: null,
    buildLotTitle: '',
    buildLotPrice: 0,
    buildLotCardIds: [],
    buildLotIdsText: '',

    // ---- R5: failures + retry ----
    recentFailures: [],

    // ---- C4: Re-identify spinner ----
    reidentifySpinner: false,

    // ---- other ----
    listings: [],
    achievements: [],
    env: {},
    editing: null,
    listingPreview: null,
    openSettings: false,
    anthropicKey: '',
    keySaved: false,
    toast: null,
    valueMilestones: [100, 500, 1000, 5000, 10000, 25000, 50000],

    // =========================================================================
    // Init
    // =========================================================================
    async init() {
      this.anthropicKey = localStorage.getItem('anthropic_key') || '';
      this.inventoryFirstVisit = localStorage.getItem('inv_visited') !== '1';
      await this.refreshAll();
      setInterval(() => this.poll(), 5000);
      this.loadAchievements();
      // Keyboard handler (B4)
      document.addEventListener('keydown', (e) => this.keyboardHandler(e));
      // Resume tracking if there's an active job (survives page reload)
      await this.resumeActiveJob();
    },

    async resumeActiveJob() {
      try {
        const jobs = await fetch('/api/scans/jobs').then(r => r.json());
        const active = jobs.find(j => ['queued', 'processing'].includes(j.status));
        if (active) {
          this.job = active;
          if (this.jobPoll) clearInterval(this.jobPoll);
          this.jobPoll = setInterval(() => this.pollJob(), 1500);
        }
      } catch (e) {}
    },

    async refreshAll() {
      await Promise.all([
        this.loadStats(), this.loadInsights(), this.loadActionQ(),
        this.loadRecent(), this.loadInventory(), this.loadListings(),
        this.loadEnv(), this.loadStripe(), this.loadRecentFailures(),
        this.loadStorageLocations(),
      ]);
      this.drawCharts();
    },

    // m10: storage location helpers
    async loadStorageLocations() {
      try {
        const r = await fetch('/api/storage-locations').then(r => r.json());
        this.storageLocations = Array.isArray(r) ? r : [];
      } catch (e) { this.storageLocations = []; }
    },

    async createStorageLocation() {
      const name = (this.newLocation.name || '').trim();
      if (!name) return;
      try {
        const r = await fetch('/api/storage-locations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name,
            kind: this.newLocation.kind || 'other',
            notes: this.newLocation.notes || null,
          }),
        });
        if (!r.ok) throw new Error('status ' + r.status);
        const loc = await r.json();
        await this.loadStorageLocations();
        // Auto-select the new (or matched) location in whichever context opened the dialog.
        if (this.editing) this.editing.storage_location_id = loc.id;
        if (this.bulkActionOpen) this.bulkLocationTarget = String(loc.id);
        this.newLocation = { open: false, name: '', kind: 'binder', notes: '' };
        this._showToastMsg(`Saved location "${loc.name}".`);
      } catch (e) {
        this._showToastMsg('Could not save location: ' + e.message);
      }
    },

    async bulkMoveToLocation() {
      if (!this.selectedIds.length) return;
      const patch = {};
      if (this.bulkLocationTarget === '' || this.bulkLocationTarget === null) return;
      patch.storage_location_id = this.bulkLocationTarget === '__clear'
        ? null
        : Number(this.bulkLocationTarget);
      if (this.bulkLocationPosition) patch.storage_position = this.bulkLocationPosition;
      await fetch('/api/bulk/patch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: this.selectedIds, patch }),
      });
      this.bulkLocationTarget = '';
      this.bulkLocationPosition = '';
      this.selectedIds = [];
      await Promise.all([this.loadInventory(), this.loadStorageLocations()]);
    },

    // R5: failures + retry
    async loadRecentFailures() {
      try {
        const r = await fetch('/api/scans/recent-failures?limit=30').then(r => r.json());
        this.recentFailures = r.items || [];
      } catch (e) { this.recentFailures = []; }
    },

    async clearFailures() {
      await fetch('/api/scans/recent-failures', { method: 'DELETE' });
      this.recentFailures = [];
    },

    async reidentifyJob(jobId) {
      if (!jobId) return;
      const headers = {};
      const key = localStorage.getItem('anthropic_key');
      if (key) headers['X-Anthropic-Key'] = key;
      try {
        const r = await fetch('/api/scans/jobs/' + jobId + '/reidentify', { method: 'POST', headers });
        if (!r.ok) throw new Error('status ' + r.status);
        const data = await r.json();
        this._showToastMsg('Re-identifying ' + data.cards + ' cards from job ' + jobId + '...');
      } catch (e) {
        this._showToastMsg('Re-identify failed: ' + e.message);
      }
    },


    // =========================================================================
    // Data loaders
    // =========================================================================
    async loadStats() {
      const r = await fetch('/api/stats').then(r => r.json());
      this.stats = r;
    },
    async loadInsights() {
      const r = await fetch('/api/insights').then(r => r.json());
      this.insights = r.items || [];
    },
    async loadActionQ() {
      const r = await fetch('/api/action-queue').then(r => r.json());
      this.actionQ = r;
    },
    async loadRecent() {
      const r = await fetch('/api/inventory/recent?n=10').then(r => r.json());
      this.recent = r;
    },

    // B2: Server-driven inventory fetch
    async loadInventory() {
      this.invLoading = true;
      this.selectedIds = [];
      this.focusedRowIndex = -1;
      const params = this._buildInventoryParams();
      params.set('limit', String(this.invLimit));
      params.set('offset', String(this.invOffset));
      try {
        const r = await fetch('/api/inventory?' + params).then(r => r.json());
        let items = r.items || [];
        // C4: client-side post-filter for low comp confidence
        // TODO: expose comp_confidence as a server-side filter param in a future round
        if (this.filterLowConfidence) {
          items = items.filter(c => c.comp_confidence === 'low' && (c.comp_median || c.est_value_raw || 0) >= 5);
        }
        this.inventory = items;
        this.invTotal = r.total || 0;
      } finally {
        this.invLoading = false;
      }
      this._refreshVirt();
      // B6: refresh facets via requestIdleCallback after a write
      this._scheduleIdleFacets();
    },

    _buildInventoryParams() {
      const params = new URLSearchParams();
      if (this.search)          params.set('q', this.search);
      if (this.filterHits)      params.set('hit_only', 'true');
      if (this.filterReview)    params.set('needs_review', 'true');
      if (this.sortBy)          params.set('sort', this.sortBy);
      if (this.filterEbayStatus) params.set('ebay_status', this.filterEbayStatus);
      if (this.filterMinValue)  params.set('min_value', this.filterMinValue);
      if (this.filterMaxValue)  params.set('max_value', this.filterMaxValue);
      if (this.filterAutograph) params.set('autograph', 'true');
      if (this.filterRelic)     params.set('relic', 'true');
      if (this.filterGraded)    params.set('graded', 'true');
      for (const era of this.filterEra) params.append('era', era);
      for (const sp of this.filterSport) params.append('sport', sp);
      // m10: storage location filter ('' = any, '0' = unassigned, N = specific)
      if (this.storageFilterId !== '' && this.storageFilterId !== null && this.storageFilterId !== undefined) {
        params.set('storage_location_id', String(this.storageFilterId));
      }
      return params;
    },

    // B2: Load facets (filter chip counts)
    async loadFacets() {
      try {
        const r = await fetch('/api/inventory/facets').then(r => r.json());
        this.facets = r;
      } catch (e) { /* non-critical */ }
    },

    // B6: Schedule facet refresh at idle time
    _scheduleIdleFacets() {
      if (typeof requestIdleCallback !== 'undefined') {
        requestIdleCallback(() => this.loadFacets(), { timeout: 2000 });
      } else {
        setTimeout(() => this.loadFacets(), 500);
      }
    },

    // C1: Load suggestions from /api/suggestions
    async loadSuggestions() {
      this.suggestionsLoading = true;
      try {
        const r = await fetch('/api/suggestions').then(r => r.json());
        this.suggestions = (r.items || []).filter(s => !this.dismissedSuggestions.has(s.id + ':' + s.kind));
      } catch (e) {
        this.suggestions = [];
      } finally {
        this.suggestionsLoading = false;
      }
    },

    // C1: Group suggestions by kind for display
    suggestionsGrouped() {
      const kindOrder = ['grade', 'list', 'verify_comps', 'reshoot', 'bulk'];
      const groups = {};
      for (const item of this.suggestions) {
        if (!groups[item.kind]) groups[item.kind] = [];
        groups[item.kind].push(item);
      }
      // Return ordered array of { kind, label, items }
      return kindOrder
        .filter(k => groups[k] && groups[k].length)
        .map(k => ({
          kind: k,
          label: { grade: 'Grade', bulk: 'Bulk Lot', list: 'List Now', reshoot: 'Reshoot', verify_comps: 'Verify Comps' }[k] || k,
          items: groups[k],
        }));
    },

    // C1: Total non-dismissed suggestions count
    suggestionsCount() {
      return this.suggestions.length;
    },

    // C1: Dismiss a grade suggestion locally
    dismissSuggestion(item) {
      this.dismissedSuggestions.add(item.id + ':' + item.kind);
      this.suggestions = this.suggestions.filter(s => !(s.id === item.id && s.kind === item.kind));
    },

    // C1/C2: Action dispatchers per kind
    // C2: Grade kind now opens the info modal (which has "Add to queue" button)
    suggestionAction(item) {
      if (item.kind === 'grade') {
        this.gradeInfoCard = item;
        this.showGradeInfoModal = true;
      } else if (item.kind === 'bulk') {
        // Pre-populate bulk toolbar with this card's ID and switch to Inventory
        this.selectedIds = [item.id];
        this.bulkLotLabel = '';
        this.tab = 'Inventory';
        this.onInventoryTabEnter();
      } else if (item.kind === 'list') {
        // Open eBay listing preview for this card
        this.previewListing({ id: item.id });
      } else if (item.kind === 'reshoot') {
        // Open edit modal with card from inventory (fetch it first)
        this._openCardById(item.id, (c) => {
          c.needs_photo_verification = true;
          this.editing = c;
        });
      } else if (item.kind === 'verify_comps') {
        // Open edit modal scrolled to notes
        this._openCardById(item.id, (c) => {
          this.editing = c;
          this.$nextTick(() => {
            const el = document.querySelector('[x-ref="notesField"], textarea[x-model="editing.notes"]');
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
          });
        });
      }
    },

    // Helper: fetch a card by ID then run a callback
    async _openCardById(id, cb) {
      try {
        const r = await fetch('/api/inventory/' + id).then(r => r.json());
        cb(JSON.parse(JSON.stringify(r)));
      } catch (e) {
        // Fallback: find in loaded inventory
        const c = this.inventory.find(x => x.id === id);
        if (c) cb(JSON.parse(JSON.stringify(c)));
      }
    },

    async loadListings() {
      const r = await fetch('/api/listings').then(r => r.json());
      this.listings = r.items;
    },
    async loadAchievements() {
      const r = await fetch('/api/achievements').then(r => r.json());
      this.achievements = r.items;
    },
    async loadEnv() {
      const r = await fetch('/api/auth/status').then(r => r.json());
      this.env = r;
    },

    // B5 / C5: Load last-24h activity stripe data
    // C5: Prefer GET /api/stats/last_24h; fall back to client-side derivation if 404
    async loadStripe() {
      const cutoff = Date.now() - 24 * 60 * 60 * 1000;
      try {
        const resp = await fetch('/api/stats/last_24h');
        if (resp.ok) {
          const d = await resp.json();
          this.stripe = {
            scanned24h: d.scanned || 0,
            hits24h:    d.hits    || 0,
            value24h:   d.value_added || 0,
            newCardIds: d.ids    || [],
          };
          return;
        }
        // 404 means old backend without this endpoint — fall through to client-side
        if (resp.status !== 404) throw new Error('stripe fetch status ' + resp.status);
      } catch (e) {
        // Network error or non-404 server error: fall through to client-side derivation
      }
      // Client-side fallback (R2 original logic)
      try {
        const recentData = await fetch('/api/inventory?sort=recent&limit=500').then(r => r.json());
        const items24h = (recentData.items || []).filter(c => {
          const t = c.created_at ? new Date(c.created_at).getTime() : 0;
          return t >= cutoff;
        });
        this.stripe = {
          scanned24h: items24h.length,
          hits24h:    items24h.filter(c => c.is_hit_watchlist).length,
          value24h:   items24h.reduce((s, c) => s + (c.comp_median || c.est_value_raw || 0), 0),
          newCardIds: items24h.map(c => c.id),
        };
      } catch (e) {
        this.stripe = { scanned24h: 0, hits24h: 0, value24h: 0, newCardIds: [] };
      }
    },

    // C4: Filter inventory to cards in the low_comp_confidence action-queue bucket
    filterLowCompConfidence() {
      const ids = (this.actionQ.low_comp_confidence || []).map(c => c.id);
      if (!ids.length) return;
      this.tab = 'Inventory';
      this.onInventoryTabEnter();
      // Pull the loaded inventory then keep only the matching IDs
      this.sortBy = 'value_desc';
      this.filterMinValue = '5';
      this.loadInventory().then(() => {
        const set = new Set(ids);
        this.inventory = this.inventory.filter(c => set.has(c.id));
        this._refreshVirt();
      });
    },

    // B5: Filter inventory to "added today"
    filterToday() {
      this.stripeFilterActive = !this.stripeFilterActive;
      if (this.stripeFilterActive) {
        this.sortBy = 'recent';
        // Jump to inventory tab and show only today's cards
        this.tab = 'Inventory';
        // We just reload; the date filter is client-applied after fetch
        this.loadInventory().then(() => {
          this.inventory = this.inventory.filter(c => {
            const t = c.created_at ? new Date(c.created_at).getTime() : 0;
            return t >= Date.now() - 24 * 60 * 60 * 1000;
          });
          this._refreshVirt();
        });
      } else {
        this.loadInventory();
      }
    },

    // =========================================================================
    // B1: Virtual scroller
    // =========================================================================
    _refreshVirt() {
      const vp = this.invViewportHeight || 400;
      const result = virtualizeInventory(this.inventory, this.invScrollTop, vp);
      this.virtRows       = result.rows;
      this.virtPaddingTop = result.paddingTop;
      this.virtPaddingBottom = result.paddingBottom;
      this.virtStartIndex = result.startIndex;
    },

    onInvScroll(el) {
      this.invScrollTop = el.scrollTop;
      this.invViewportHeight = el.clientHeight;
      this._refreshVirt();
    },

    onInvResize(el) {
      this.invViewportHeight = el.clientHeight || 400;
      this._refreshVirt();
    },

    // =========================================================================
    // B2: Filter helpers
    // =========================================================================
    applyFilters() {
      this.invOffset = 0;
      this.loadInventory();
    },

    toggleEraFilter(era) {
      const idx = this.filterEra.indexOf(era);
      if (idx === -1) this.filterEra.push(era);
      else this.filterEra.splice(idx, 1);
      this.applyFilters();
    },

    toggleSportFilter(sport) {
      const idx = this.filterSport.indexOf(sport);
      if (idx === -1) this.filterSport.push(sport);
      else this.filterSport.splice(idx, 1);
      this.applyFilters();
    },

    clearAllFilters() {
      this.search = '';
      this.filterHits = false;
      this.filterReview = false;
      this.sortBy = 'recent';
      this.filterEra = [];
      this.filterEbayStatus = '';
      this.filterMinValue = '';
      this.filterMaxValue = '';
      this.filterAutograph = false;
      this.filterRelic = false;
      this.filterGraded = false;
      this.filterLowConfidence = false;
      this.stripeFilterActive = false;
      this.applyFilters();
    },

    activeFilterCount() {
      let n = 0;
      if (this.search) n++;
      if (this.filterHits) n++;
      if (this.filterReview) n++;
      if (this.filterEra.length) n++;
      if (this.filterEbayStatus) n++;
      if (this.filterMinValue || this.filterMaxValue) n++;
      if (this.filterAutograph) n++;
      if (this.filterRelic) n++;
      if (this.filterGraded) n++;
      if (this.filterLowConfidence) n++;
      return n;
    },

    // =========================================================================
    // B3: Bulk actions
    // =========================================================================
    async selectAllMatching() {
      // POST /api/bulk/ids with current filter spec (body mirrors query params)
      const params = this._buildInventoryParams();
      const body = {};
      for (const [k, v] of params.entries()) {
        if (body[k]) {
          body[k] = Array.isArray(body[k]) ? [...body[k], v] : [body[k], v];
        } else {
          body[k] = v;
        }
      }
      try {
        const r = await fetch('/api/bulk/ids', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }).then(r => r.json());
        this.selectedIds = r.ids || [];
      } catch (e) {
        // Fallback: select currently loaded page
        this.selectedIds = this.inventory.map(c => c.id);
      }
    },

    async bulkPatchStatus(status) {
      if (!this.selectedIds.length) return;
      await fetch('/api/bulk/patch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: this.selectedIds, patch: { status } }),
      });
      this.selectedIds = [];
      await this.loadInventory();
      this._scheduleIdleFacets();
    },

    async bulkPatchChannel(channel) {
      if (!this.selectedIds.length) return;
      await fetch('/api/bulk/patch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: this.selectedIds, patch: { channel } }),
      });
      this.selectedIds = [];
      await this.loadInventory();
    },

    async bulkAddNote() {
      const note = this.bulkNoteText.trim();
      if (!note || !this.selectedIds.length) return;
      await fetch('/api/bulk/patch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: this.selectedIds, patch: { notes_append: note } }),
      });
      this.bulkNoteText = '';
      this.selectedIds = [];
      await this.loadInventory();
    },

    async bulkRecomputeComps() {
      if (!this.selectedIds.length) return;
      await fetch('/api/bulk/recompute-comps', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: this.selectedIds }),
      });
      this.selectedIds = [];
    },

    async bulkMoveToBulkLot() {
      const label = this.bulkLotLabel.trim() || prompt('Enter lot label:');
      if (!label) return;
      if (!confirm(`Move ${this.selectedIds.length} cards to lot "${label}"? This sets their status to Bulk.`)) return;
      await fetch('/api/bulk/move-to-bulk-lot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: this.selectedIds, lot_label: label }),
      });
      this.bulkLotLabel = '';
      this.selectedIds = [];
      await this.loadInventory();
      this._scheduleIdleFacets();
    },

    async bulkDelete() {
      if (!this.selectedIds.length) return;
      if (!confirm(`Soft-delete ${this.selectedIds.length} cards? (recoverable — sets status to Deleted)`)) return;
      await fetch('/api/bulk/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: this.selectedIds }),
      });
      this.selectedIds = [];
      await this.loadInventory();
      this._scheduleIdleFacets();
    },

    // Legacy bulk list (kept for Listings tab button)
    async bulkList() {
      if (!confirm(`Draft eBay listings for ${this.selectedIds.length} cards?`)) return;
      await fetch('/api/listings/publish-bulk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ card_ids: this.selectedIds, publish: false }),
      });
      this.selectedIds = [];
      await this.loadListings();
      this.tab = 'Listings';
    },

    toggleSelectRow(id) {
      const idx = this.selectedIds.indexOf(id);
      if (idx === -1) this.selectedIds.push(id);
      else this.selectedIds.splice(idx, 1);
    },

    toggleAll(e) {
      this.selectedIds = e.target.checked ? this.inventory.map(c => c.id) : [];
    },

    // =========================================================================
    // B4: Keyboard navigation
    // =========================================================================
    keyboardHandler(e) {
      // Never fire shortcuts when user is typing in an input/textarea/select
      const tag = (e.target && e.target.tagName) ? e.target.tagName.toLowerCase() : '';
      const isEditable = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable;

      // Escape always works
      if (e.key === 'Escape') {
        if (this.showKeyboardHelp) { this.showKeyboardHelp = false; return; }
        if (this.editing) { this.editing = null; return; }
        if (this.listingPreview) { this.listingPreview = null; return; }
        this.focusedRowIndex = -1;
        this._ghPending = false;
        return;
      }

      // '/' focuses search — don't block even in inputs if we want, but spec says focus search
      if (e.key === '/' && !isEditable) {
        e.preventDefault();
        const el = document.getElementById('inv-search');
        if (el) el.focus();
        return;
      }

      if (isEditable) return;

      // Only on Inventory tab (except '?' which works everywhere)
      if (e.key === '?') {
        e.preventDefault();
        this.showKeyboardHelp = !this.showKeyboardHelp;
        return;
      }

      if (this.tab !== 'Inventory') return;

      switch (e.key) {
        case 'j': {
          e.preventDefault();
          this.focusedRowIndex = Math.min(this.focusedRowIndex + 1, this.virtRows.length - 1);
          this._scrollToFocused();
          break;
        }
        case 'k': {
          e.preventDefault();
          this.focusedRowIndex = Math.max(this.focusedRowIndex - 1, 0);
          this._scrollToFocused();
          break;
        }
        case 'x': {
          e.preventDefault();
          const row = this.virtRows[this.focusedRowIndex];
          if (row) this.toggleSelectRow(row.id);
          break;
        }
        case 'e': {
          e.preventDefault();
          const row = this.virtRows[this.focusedRowIndex];
          if (row) this.openCard(row);
          break;
        }
        case 'l': {
          e.preventDefault();
          const row = this.virtRows[this.focusedRowIndex];
          if (row) this.previewListing(row);
          break;
        }
        case 'g': {
          e.preventDefault();
          this._ghPending = true;
          setTimeout(() => { this._ghPending = false; }, 800);
          break;
        }
        case 'h': {
          if (this._ghPending) {
            e.preventDefault();
            this._ghPending = false;
            this.focusedRowIndex = 0;
            const el = document.getElementById('inv-scroll');
            if (el) el.scrollTop = 0;
            this.invScrollTop = 0;
            this._refreshVirt();
          }
          break;
        }
        default:
          break;
      }
    },

    _scrollToFocused() {
      const el = document.getElementById('inv-scroll');
      if (!el) return;
      // The focused row is relative to the rendered virtRows, but in the absolute list
      // use virtStartIndex + focusedRowIndex for absolute position
      const absIdx = this.focusedRowIndex; // focusedRowIndex is within virtRows
      const top = absIdx * ROW_HEIGHT;
      const bottom = top + ROW_HEIGHT;
      if (top < el.scrollTop) el.scrollTop = top;
      else if (bottom > el.scrollTop + el.clientHeight) el.scrollTop = bottom - el.clientHeight;
    },

    onInventoryTabEnter() {
      if (this.inventoryFirstVisit) {
        this.showKeyboardHint = true;
        localStorage.setItem('inv_visited', '1');
        this.inventoryFirstVisit = false;
        setTimeout(() => { this.showKeyboardHint = false; }, 4000);
      }
      // Load facets when entering the inventory tab
      this.loadFacets();
    },

    // =========================================================================
    // B5: Stripe / progress bar
    // =========================================================================
    stripeText() {
      const s = this.stripe;
      if (!s.scanned24h) return 'No cards scanned in the last 24h';
      return `${s.scanned24h} scanned · ${s.hits24h} hits · $${this.formatNum(s.value24h)} added`;
    },

    jobProgressPct() {
      if (!this.job || !this.job.total) return 0;
      return Math.round((this.job.processed / this.job.total) * 100);
    },

    // =========================================================================
    // Scan / Drive / upload
    // =========================================================================
    saveKey() {
      localStorage.setItem('anthropic_key', this.anthropicKey || '');
      this.keySaved = true;
      setTimeout(() => this.keySaved = false, 1200);
    },

    onFiles(files) { this.uploadFiles(files); },
    onDrop(e) { this.dragOver = false; this.uploadFiles(e.dataTransfer.files); },

    async uploadFiles(files) {
      if (!files || !files.length) return;

      const allFiles = Array.from(files);
      const videos = allFiles.filter(f => /\.(mov|mp4|m4v)$/i.test(f.name) || f.type.startsWith('video/'));
      const photos = allFiles.filter(f => !videos.includes(f));

      // Queue all videos
      for (const v of videos) {
        const sizeMB = (v.size / 1024 / 1024).toFixed(1);
        this.uploadQueue.push({
          file: v,
          name: v.name,
          sizeMB,
          status: 'pending',  // pending | uploading | processing | done | error
          job: null,
          label: `${v.name} (${sizeMB} MB)`,
        });
      }

      // Upload photos immediately (non-queued, same as before)
      if (photos.length) {
        const headers = {};
        const key = localStorage.getItem('anthropic_key');
        if (key) headers['X-Anthropic-Key'] = key;

        this.job = { status: 'uploading', source: 'photo', label: `Uploading ${photos.length} photo(s)...`, total: 0, processed: 0 };
        const fd = new FormData();
        for (const f of photos) fd.append('files', f);
        fd.append('label', `Batch of ${photos.length}`);
        try {
          const res = await fetch('/api/scans/upload', { method: 'POST', body: fd, headers }).then(r => r.json());
          this.job = { id: res.job_id, total: res.queued, processed: 0, status: 'queued', source: 'photo', label: `Batch of ${photos.length}` };
          if (this.jobPoll) clearInterval(this.jobPoll);
          this.jobPoll = setInterval(() => this.pollJob(), 1500);
        } catch (e) {
          this.job = null; alert('Upload failed: ' + e.message); return;
        }
      }

      // Start processing the video queue
      if (videos.length) this.processQueue();
    },

    async processQueue() {
      if (this.queueProcessing) return;
      this.queueProcessing = true;

      const headers = {};
      const key = localStorage.getItem('anthropic_key');
      if (key) headers['X-Anthropic-Key'] = key;

      while (true) {
        const next = this.uploadQueue.find(q => q.status === 'pending');
        if (!next) break;

        next.status = 'uploading';

        const fd = new FormData();
        fd.append('file', next.file);
        fd.append('label', 'Video scan');
        if (this.batchYear) fd.append('batch_year', this.batchYear);
        if (this.batchSetBrand) fd.append('batch_set_brand', this.batchSetBrand);

        try {
          const res = await fetch('/api/scans/upload-video', { method: 'POST', body: fd, headers }).then(r => r.json());
          if (res.error || res.detail) {
            next.status = 'error';
            next.label = `${next.name} — ${res.detail || res.error}`;
            continue;
          }
          next.job = { id: res.job_id, total: 0, processed: 0, status: 'queued', source: 'video' };
          next.status = 'processing';
          // Free the file reference
          next.file = null;
        } catch (e) {
          next.status = 'error';
          next.label = `${next.name} — upload failed`;
          continue;
        }
      }

      // Start polling all processing queue items
      if (!this.queuePoll) {
        this.queuePoll = setInterval(() => this.pollQueue(), 2000);
      }
      this.queueProcessing = false;
    },

    async pollQueue() {
      const active = this.uploadQueue.filter(q => q.status === 'processing' && q.job);
      if (!active.length) {
        // All done — check if everything is finished
        const allDone = this.uploadQueue.every(q => q.status === 'done' || q.status === 'error');
        if (allDone && this.uploadQueue.length) {
          clearInterval(this.queuePoll);
          this.queuePoll = null;
          await this.refreshAll();
          this.drawCharts();
          // Clear queue after a delay so user can see the final state
          setTimeout(() => { this.uploadQueue = []; }, 5000);
        }
        return;
      }

      for (const item of active) {
        try {
          const j = await fetch('/api/scans/jobs/' + item.job.id).then(r => r.json());
          item.job = j;
          if (j.status === 'done') {
            item.status = 'done';
            await this.refreshAll();
            this.drawCharts();
          } else if (j.status === 'error' || j.status === 'abandoned') {
            item.status = 'error';
          }
        } catch (e) { /* retry next poll */ }
      }
    },

    async pollJob() {
      if (!this.job) return;
      const j = await fetch('/api/scans/jobs/' + this.job.id).then(r => r.json());
      this.job = j;
      if (j.status === 'done') {
        clearInterval(this.jobPoll);
        await this.refreshAll();
        this.drawCharts();
        await this.loadAchievements();
        const p = await fetch('/api/achievements/pending').then(r => r.json());
        for (const a of p.items || []) this.celebrate(a);
        if ((p.items || []).length) await fetch('/api/achievements/seen', { method: 'POST' });
        setTimeout(() => { this.job = null; }, 3000);
      }
    },

    async poll() {
      try {
        const p = await fetch('/api/achievements/pending').then(r => r.json());
        for (const a of p.items || []) this.celebrate(a);
        if ((p.items || []).length) await fetch('/api/achievements/seen', { method: 'POST' });
      } catch (e) {}
    },

    celebrate(a) {
      this.toast = a;
      if (a.confetti) {
        confetti({ particleCount: 140, spread: 80, origin: { y: 0.7 } });
        setTimeout(() => confetti({ particleCount: 80, angle: 60, spread: 55, origin: { x: 0 } }), 250);
        setTimeout(() => confetti({ particleCount: 80, angle: 120, spread: 55, origin: { x: 1 } }), 400);
      }
      setTimeout(() => this.toast = null, 4000);
    },

    // =========================================================================
    // B6: Chart memoization
    // =========================================================================
    drawCharts() {
      const eraEl = document.getElementById('eraChart');
      const playerEl = document.getElementById('playerChart');
      if (!eraEl || !playerEl) return;

      // B6: Only re-draw when data changes
      const newHash = _jsonHash({
        era: this.stats.era_distribution,
        top: this.stats.top_players,
      });
      if (newHash === this._lastChartHash) return;
      this._lastChartHash = newHash;

      if (this.eraChart) this.eraChart.destroy();
      if (this.playerChart) this.playerChart.destroy();

      const eras = this.stats.era_distribution || {};
      this.eraChart = new Chart(eraEl, {
        type: 'doughnut',
        data: {
          labels: Object.keys(eras),
          datasets: [{
            data: Object.values(eras),
            backgroundColor: ['#a855f7', '#f59e0b', '#10b981', '#06b6d4', '#ef4444', '#64748b'],
          }],
        },
        options: { plugins: { legend: { position: 'bottom', labels: { color: '#cbd5e1' } } } },
      });
      const tp = (this.stats.top_players || []);
      this.playerChart = new Chart(playerEl, {
        type: 'bar',
        data: {
          labels: tp.map(p => p.player),
          datasets: [{ label: 'Cards', data: tp.map(p => p.count), backgroundColor: '#d946ef' }],
        },
        options: {
          indexAxis: 'y',
          plugins: { legend: { display: false } },
          scales: { x: { ticks: { color: '#cbd5e1' } }, y: { ticks: { color: '#cbd5e1' } } },
        },
      });
    },

    // =========================================================================
    // Utility
    // =========================================================================
    formatNum(n) {
      if (typeof n !== 'number') return n;
      if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
      return n.toFixed(2);
    },

    nextMilestone() {
      const v = this.stats.total_value || 0;
      const m = this.valueMilestones.find(x => x > v);
      if (!m) return 'Milestone master';
      return `Next: $${m.toLocaleString()}`;
    },
    valueProgress() {
      const v = this.stats.total_value || 0;
      const m = this.valueMilestones.find(x => x > v) || v;
      const prev = [0, ...this.valueMilestones].reverse().find(x => x <= v) || 0;
      return Math.min(100, Math.max(0, ((v - prev) / (m - prev)) * 100));
    },

    // C3: detect reidentify endpoint support (called once on init)
    reidentifySupported: false,
    async _detectReidentify() {
      try {
        // Use a known-good card ID placeholder; if we get 405 the endpoint exists but rejects non-POST
        const r = await fetch('/api/inventory/1/reidentify', { method: 'OPTIONS' });
        // Accept 200, 204, 405 (method not allowed but exists), 404 means no route
        this.reidentifySupported = r.status !== 404;
      } catch (e) {
        this.reidentifySupported = false;
      }
    },

    // C4: Re-identify a card with Claude (wired — was C3 stub, now fully wired)
    async reidentifyCard() {
      if (!this.editing) return;
      const id = this.editing.id;
      this.reidentifySpinner = true;
      try {
        const headers = { 'Content-Type': 'application/json' };
        const key = localStorage.getItem('anthropic_key');
        if (key) headers['X-Anthropic-Key'] = key;
        const r = await fetch('/api/inventory/' + id + '/reidentify', { method: 'POST', headers });
        if (r.status === 404) {
          this._showToastMsg('Re-identify endpoint not available on this backend.');
          return;
        }
        if (!r.ok) throw new Error('status ' + r.status);
        const updated = await r.json();
        this.editing = { ...this.editing, ...updated };
        this._showToastMsg('Card re-identified successfully.');
      } catch (e) {
        this._showToastMsg('Re-identify failed: ' + e.message);
      } finally {
        this.reidentifySpinner = false;
      }
    },

    // Utility: show a transient info toast (non-achievement)
    _showToastMsg(msg) {
      this.toast = { icon: 'ℹ️', title: msg, description: '', confetti: false };
      setTimeout(() => { this.toast = null; }, 3500);
    },

    // =========================================================================
    // C1: Money tab — sales summary + charts + recent sales + sync
    // =========================================================================

    // Called when user enters the Money tab
    async onMoneyTabEnter() {
      await Promise.all([
        this.loadSalesSummary(),
        this.loadRecentSales(),
        this.loadGradingQueues(),
        this.loadBulkLotProposals(),
      ]);
    },

    // C1: Fetch sales summary and draw charts
    async loadSalesSummary() {
      try {
        const r = await fetch('/api/sales/summary');
        if (r.status === 404) return; // backend not yet deployed
        if (!r.ok) { this._showToastMsg('Sales summary unavailable (' + r.status + ').'); return; }
        const data = await r.json();
        this.salesSummary = data;
        this._drawSalesCharts(data);
      } catch (e) {
        this._showToastMsg('Failed to load sales summary.');
      }
    },

    // C1: Compute this-month net from by_month map
    salesMonthNet() {
      const by = this.salesSummary.by_month || {};
      const key = new Date().toISOString().slice(0, 7); // "YYYY-MM"
      return (by[key] || {}).net || 0;
    },

    // C1: Draw monthly P&L + channel donut charts (memoized)
    _drawSalesCharts(data) {
      const newHash = _jsonHash({ by_month: data.by_month, by_channel: data.by_channel });
      if (newHash === this._lastSalesHash) return;
      this._lastSalesHash = newHash;

      // Monthly P&L bar+line chart
      const monthEl = document.getElementById('salesByMonthChart');
      if (monthEl) {
        if (this.salesByMonthChart) this.salesByMonthChart.destroy();
        const byMonth = data.by_month || {};
        const labels = Object.keys(byMonth).sort();
        const grossData = labels.map(k => byMonth[k].gross || 0);
        const feesData  = labels.map(k => byMonth[k].fees  || 0);
        const netData   = labels.map(k => byMonth[k].net   || 0);
        this.salesByMonthChart = new Chart(monthEl, {
          type: 'bar',
          data: {
            labels,
            datasets: [
              { label: 'Gross', data: grossData, backgroundColor: '#10b981', order: 2 },
              { label: 'Fees',  data: feesData,  backgroundColor: '#ef4444', order: 2 },
              { label: 'Net',   data: netData,   type: 'line', borderColor: '#22d3ee',
                backgroundColor: 'transparent', pointBackgroundColor: '#22d3ee', order: 1 },
            ],
          },
          options: {
            plugins: { legend: { labels: { color: '#cbd5e1' } } },
            scales: {
              x: { ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } },
              y: { ticks: { color: '#94a3b8' }, grid: { color: '#1e293b' } },
            },
          },
        });
      }

      // Channel donut chart
      const channelEl = document.getElementById('salesChannelChart');
      if (channelEl) {
        if (this.salesChannelChart) this.salesChannelChart.destroy();
        const byChannel = data.by_channel || {};
        const chLabels = Object.keys(byChannel);
        const chData   = chLabels.map(k => (byChannel[k].gross || 0));
        this.salesChannelChart = new Chart(channelEl, {
          type: 'doughnut',
          data: {
            labels: chLabels,
            datasets: [{
              data: chData,
              backgroundColor: ['#a855f7', '#f59e0b', '#10b981', '#06b6d4', '#ef4444'],
            }],
          },
          options: { plugins: { legend: { position: 'bottom', labels: { color: '#cbd5e1' } } } },
        });
      }
    },

    // C1: Fetch last 20 recent sales
    async loadRecentSales() {
      try {
        const r = await fetch('/api/sales/recent?n=20');
        if (r.status === 404) return;
        if (!r.ok) { this._showToastMsg('Recent sales unavailable.'); return; }
        this.recentSales = await r.json();
      } catch (e) {
        this._showToastMsg('Failed to load recent sales.');
      }
    },

    // C1: Sync eBay sales (fire-and-forget)
    async syncEbaySales() {
      this.salesSyncing = true;
      try {
        const r = await fetch('/api/sales/sync', { method: 'POST' });
        if (!r.ok && r.status !== 404) throw new Error('status ' + r.status);
        if (r.status === 404) {
          this._showToastMsg('Sales sync endpoint not yet available on this backend.');
          return;
        }
        this._showToastMsg('eBay sales sync queued. Refresh in a moment.');
        setTimeout(() => this.loadSalesSummary(), 3000);
      } catch (e) {
        this._showToastMsg('Sync failed: ' + e.message);
      } finally {
        this.salesSyncing = false;
      }
    },

    // C1: Submit manual sale
    async submitManualSale() {
      const m = this.manualSale;
      if (!m.listing_id || !m.sold_price) {
        this._showToastMsg('Listing ID and sold price are required.');
        return;
      }
      try {
        const body = { sold_price: m.sold_price, channel: m.channel };
        if (m.fees) body.fees = m.fees;
        if (m.sold_at) body.sold_at = m.sold_at;
        const r = await fetch('/api/sales/' + m.listing_id + '/mark-sold-manual', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (r.status === 404) { this._showToastMsg('Endpoint not available.'); return; }
        if (!r.ok) throw new Error('status ' + r.status);
        this.showManualSaleModal = false;
        this.manualSale = { listing_id: null, sold_price: null, fees: null, channel: 'eBay', sold_at: '' };
        this._showToastMsg('Sale recorded.');
        await Promise.all([this.loadSalesSummary(), this.loadRecentSales()]);
      } catch (e) {
        this._showToastMsg('Failed to record sale: ' + e.message);
      }
    },

    // =========================================================================
    // C2: Grading queue flow
    // =========================================================================

    // C2: Add a card to the local grading queue
    addToGradingQueue(item) {
      if (!this.gradingQueue.find(g => g.id === item.id)) {
        this.gradingQueue.push(item);
      }
      this.dismissedSuggestions.add(item.id + ':' + item.kind);
      this.suggestions = this.suggestions.filter(s => !(s.id === item.id && s.kind === item.kind));
    },

    // C2: Estimated total grading cost (client-side, based on selected service/level)
    gradingEstCost() {
      const costs = { 'PSA-Value': 25, 'PSA-Regular': 75, 'PSA-Express': 150,
                      'SGC-Value': 20, 'SGC-Regular': 30, 'SGC-Express': 60 };
      const key = this.gradingService + '-' + this.gradingServiceLevel;
      const perCard = costs[key] || 25;
      return this.formatNum(this.gradingQueue.length * perCard);
    },

    // C2: Projected post-grade value (rough: PSA Value tier → 1.5× median)
    gradingProjectedValue() {
      const multipliers = { Value: 1.5, Regular: 3, Express: 5 };
      const mult = multipliers[this.gradingServiceLevel] || 1.5;
      const total = this.gradingQueue.reduce((s, item) => s + (item.value || 0), 0);
      return this.formatNum(total * mult);
    },

    // C2: Send the grading batch — download CSV then queue cards
    async sendGradingBatch() {
      if (!this.gradingQueue.length) return;
      this.gradingBatchSending = true;
      const cardIds = this.gradingQueue.map(g => g.id);
      try {
        // Step 1: Download CSV
        const csvResp = await fetch('/api/grading/build-submission', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ card_ids: cardIds, service: this.gradingService, service_level: this.gradingServiceLevel }),
        });
        if (csvResp.status === 404) { this._showToastMsg('Grading endpoint not yet available.'); return; }
        if (!csvResp.ok) throw new Error('CSV build status ' + csvResp.status);
        // Trigger file download
        const blob = await csvResp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'grading_submission.csv';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        // Step 2: Mark cards as queued
        const queueResp = await fetch('/api/grading/queue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ card_ids: cardIds, service: this.gradingService, service_level: this.gradingServiceLevel }),
        });
        if (!queueResp.ok && queueResp.status !== 404) throw new Error('queue status ' + queueResp.status);

        this._showToastMsg('Batch sent! ' + cardIds.length + ' cards queued for ' + this.gradingService + '.');
        this.gradingQueue = [];
        this.showGradingBatchModal = false;
        await this.loadGradingQueues();
      } catch (e) {
        this._showToastMsg('Grading batch failed: ' + e.message);
      } finally {
        this.gradingBatchSending = false;
      }
    },

    // C2: Load in-flight grading queues from server
    async loadGradingQueues() {
      try {
        const r = await fetch('/api/grading/queues');
        if (r.status === 404) return;
        if (!r.ok) return;
        this.gradingQueues = await r.json();
      } catch (e) { /* non-critical */ }
    },

    // C2: Open mark-back modal for a submission
    openMarkBackModal(q) {
      this.markBackQueue = q;
      // Build grade entry list from the queue's cards
      this.markBackGrades = (q.cards || []).map(c => ({ card_id: c.id, card_title: c.title, grade: '' }));
      // If no card list provided, create blank entries by count
      if (!this.markBackGrades.length) {
        for (let i = 0; i < (q.card_count || 1); i++) {
          this.markBackGrades.push({ card_id: null, grade: '' });
        }
      }
      this.showMarkBackModal = true;
    },

    // C2: Submit mark-back grades
    async submitMarkBack() {
      if (!this.markBackQueue) return;
      const grades = this.markBackGrades.filter(g => g.grade && g.card_id);
      if (!grades.length) { this._showToastMsg('Enter at least one grade.'); return; }
      try {
        const r = await fetch('/api/grading/' + this.markBackQueue.submission_id + '/mark-back', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(grades),
        });
        if (r.status === 404) { this._showToastMsg('Endpoint not available.'); return; }
        if (!r.ok) throw new Error('status ' + r.status);
        this._showToastMsg('Grades submitted. Cards moved to Researching status.');
        this.showMarkBackModal = false;
        await this.loadGradingQueues();
      } catch (e) {
        this._showToastMsg('Mark-back failed: ' + e.message);
      }
    },

    // =========================================================================
    // C3: Bulk lot proposals
    // =========================================================================

    async loadBulkLotProposals() {
      try {
        const r = await fetch('/api/bulk-lots/proposals');
        if (r.status === 404) return;
        if (!r.ok) return;
        this.bulkLotProposals = await r.json();
      } catch (e) { /* non-critical */ }
    },

    openBuildLotModal(proposal) {
      this.activeLotProposal = proposal;
      this.buildLotTitle = proposal.suggested_title || '';
      this.buildLotPrice = proposal.suggested_price || 0;
      this.buildLotCardIds = proposal.card_ids || [];
      this.buildLotIdsText = (proposal.card_ids || []).join('\n');
      this.showBuildLotModal = true;
    },

    async submitBuildLot(andList) {
      const cardIds = this.buildLotIdsText.split(/[\n,\s]+/).map(s => parseInt(s.trim(), 10)).filter(Boolean);
      if (!cardIds.length) { this._showToastMsg('No card IDs provided.'); return; }
      if (!this.buildLotTitle) { this._showToastMsg('Title is required.'); return; }
      try {
        const body = {
          card_ids: cardIds,
          label: this.activeLotProposal?.cluster_label || this.buildLotTitle,
          listing_title: this.buildLotTitle,
          price: this.buildLotPrice,
        };
        const r = await fetch('/api/bulk-lots/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (r.status === 404) { this._showToastMsg('Bulk lots endpoint not yet available.'); return; }
        if (!r.ok) throw new Error('create status ' + r.status);
        const result = await r.json();
        const lotId = result.bulk_lot_id;
        this._showToastMsg('Bulk lot created (' + cardIds.length + ' cards).');

        if (andList && lotId) {
          const lr = await fetch('/api/bulk-lots/' + lotId + '/list-on-ebay', { method: 'POST' });
          if (!lr.ok && lr.status !== 404) throw new Error('list status ' + lr.status);
          if (lr.ok) this._showToastMsg('Lot listed on eBay!');
        }
        this.showBuildLotModal = false;
        await this.loadBulkLotProposals();
      } catch (e) {
        this._showToastMsg('Failed to create lot: ' + e.message);
      }
    },

    async buildAndListLot(proposal) {
      this.openBuildLotModal(proposal);
      // Modal will handle the actual creation; user can review title/price first
    },

    openCard(c) { this.editing = JSON.parse(JSON.stringify(c)); },

    async saveCard() {
      const id = this.editing.id;
      const payload = (({ year, set_brand, player, card_no, parallel, condition, est_value_raw, status, channel, notes, is_graded, is_autograph, is_relic, sport, storage_location_id, storage_position }) =>
        ({ year, set_brand, player, card_no, parallel, condition, est_value_raw, status, channel, notes, is_graded, is_autograph, is_relic, sport, storage_location_id, storage_position }))(this.editing);
      // Coerce empty string from <select> to null so the backend clears the field.
      if (payload.storage_location_id === '' || payload.storage_location_id === undefined) {
        payload.storage_location_id = null;
      }
      await fetch('/api/inventory/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      this.editing = null;
      await Promise.all([this.loadInventory(), this.loadStorageLocations()]);
      this._scheduleIdleFacets();
    },

    async deleteCard() {
      if (!confirm('Delete this card from the inventory?')) return;
      await fetch('/api/inventory/' + this.editing.id, { method: 'DELETE' });
      this.editing = null;
      await this.loadInventory();
      this._scheduleIdleFacets();
    },

    async previewListing(c) {
      const r = await fetch('/api/listings/preview/' + c.id).then(r => r.json());
      this.listingPreview = r;
    },

    async publishListing(publish) {
      const r = await fetch('/api/listings/publish', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          card_id: this.listingPreview.card_id, overrides: {
            title: this.listingPreview.title,
            format: this.listingPreview.format,
            price: this.listingPreview.price,
            duration: this.listingPreview.duration,
          }, publish,
        }),
      }).then(r => r.json());
      if (!r.success) {
        alert('Listing failed: ' + (r.error || 'unknown'));
      } else {
        this.listingPreview = null;
        await this.loadListings();
        await this.loadInventory();
        const p = await fetch('/api/achievements/pending').then(r => r.json());
        for (const a of p.items || []) this.celebrate(a);
      }
    },

    async markSold(L) {
      const price = parseFloat(prompt('Sold price ($)', String(L.price)));
      if (!price) return;
      await fetch(`/api/listings/${L.id}/mark-sold`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sold_price: price }),
      });
      await this.refreshAll();
    },
  };
}
