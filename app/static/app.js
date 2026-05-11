/* CardScanner — Alpine.js dashboard logic */

function cardscanner() {
  return {
    tab: 'Dashboard',
    dragOver: false,
    job: null,
    jobPoll: null,
    stats: {},
    insights: [],
    actionQ: {},
    recent: [],
    inventory: [],
    selectedIds: [],
    listings: [],
    achievements: [],
    env: {},
    search: '',
    filterHits: false,
    filterReview: false,
    editing: null,
    listingPreview: null,
    openSettings: false,
    anthropicKey: '',
    keySaved: false,
    toast: null,
    eraChart: null,
    playerChart: null,
    valueMilestones: [100, 500, 1000, 5000, 10000, 25000, 50000],
    drive: {connected: false, inbox_count: 0, pair_count: 0},
    autoPoll: false,
    autoTimer: null,

    async init() {
      this.anthropicKey = localStorage.getItem('anthropic_key') || '';
      await this.refreshAll();
      // poll lightly for celebrations + auth status
      setInterval(() => this.poll(), 5000);
      this.loadAchievements();
    },

    async refreshAll() {
      await Promise.all([
        this.loadStats(), this.loadInsights(), this.loadActionQ(),
        this.loadRecent(), this.loadInventory(), this.loadListings(),
        this.loadEnv(),
      ]);
      this.drawCharts();
    },

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
    async loadInventory() {
      const params = new URLSearchParams();
      if (this.search) params.set('q', this.search);
      if (this.filterHits) params.set('hit_only', 'true');
      if (this.filterReview) params.set('needs_review', 'true');
      params.set('limit', '200');
      const r = await fetch('/api/inventory?' + params).then(r => r.json());
      this.inventory = r.items;
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
      try {
        this.drive = await fetch('/api/drive/status').then(r => r.json());
      } catch (e) { this.drive = {connected: false}; }
      const ap = localStorage.getItem('auto_poll') === '1';
      if (ap !== this.autoPoll) { this.autoPoll = ap; this.toggleAutoPoll(true); }
    },

    async syncDrive() {
      const headers = {};
      const key = localStorage.getItem('anthropic_key');
      if (key) headers['X-Anthropic-Key'] = key;
      const res = await fetch('/api/drive/sync', {method: 'POST', headers}).then(r => r.json());
      if (!res.job_id) { alert('Drive sync failed: ' + JSON.stringify(res)); return; }
      this.job = {id: res.job_id, total: 0, processed: 0, status: 'queued'};
      if (this.jobPoll) clearInterval(this.jobPoll);
      this.jobPoll = setInterval(() => this.pollJob(), 1500);
      // refresh inbox count
      this.drive = await fetch('/api/drive/status').then(r => r.json());
    },

    toggleAutoPoll(silent) {
      if (!silent) localStorage.setItem('auto_poll', this.autoPoll ? '1' : '0');
      if (this.autoTimer) { clearInterval(this.autoTimer); this.autoTimer = null; }
      if (this.autoPoll) {
        this.autoTimer = setInterval(() => {
          if (!this.job && this.drive.connected && (this.drive.inbox_count || 0) > 0) {
            this.syncDrive();
          }
        }, 5 * 60 * 1000);
      }
    },

    saveKey() {
      localStorage.setItem('anthropic_key', this.anthropicKey || '');
      this.keySaved = true;
      setTimeout(() => this.keySaved = false, 1200);
    },

    onFiles(files) {
      this.uploadFiles(files);
    },
    onDrop(e) {
      this.dragOver = false;
      this.uploadFiles(e.dataTransfer.files);
    },

    async uploadFiles(files) {
      if (!files || !files.length) return;
      const fd = new FormData();
      for (const f of files) fd.append('files', f);
      fd.append('label', `Batch of ${files.length}`);
      const headers = {};
      const key = localStorage.getItem('anthropic_key');
      if (key) headers['X-Anthropic-Key'] = key;
      const res = await fetch('/api/scans/upload', {method: 'POST', body: fd, headers}).then(r => r.json());
      this.job = {id: res.job_id, total: res.queued, processed: 0, status: 'queued'};
      if (this.jobPoll) clearInterval(this.jobPoll);
      this.jobPoll = setInterval(() => this.pollJob(), 1500);
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
        // check for new celebrations
        const p = await fetch('/api/achievements/pending').then(r => r.json());
        for (const a of p.items || []) {
          this.celebrate(a);
        }
        if ((p.items || []).length) await fetch('/api/achievements/seen', {method: 'POST'});
        // hide progress after 3s
        setTimeout(() => this.job = null, 3000);
      }
    },

    async poll() {
      // lightweight stats refresh + celebration check while idle
      try {
        const p = await fetch('/api/achievements/pending').then(r => r.json());
        for (const a of p.items || []) this.celebrate(a);
        if ((p.items || []).length) await fetch('/api/achievements/seen', {method: 'POST'});
      } catch (e) {}
    },

    celebrate(a) {
      this.toast = a;
      if (a.confetti) {
        confetti({particleCount: 140, spread: 80, origin: {y: 0.7}});
        setTimeout(() => confetti({particleCount: 80, angle: 60, spread: 55, origin: {x: 0}}), 250);
        setTimeout(() => confetti({particleCount: 80, angle: 120, spread: 55, origin: {x: 1}}), 400);
      }
      setTimeout(() => this.toast = null, 4000);
    },

    drawCharts() {
      const eraEl = document.getElementById('eraChart');
      const playerEl = document.getElementById('playerChart');
      if (!eraEl || !playerEl) return;
      if (this.eraChart) this.eraChart.destroy();
      if (this.playerChart) this.playerChart.destroy();
      const eras = this.stats.era_distribution || {};
      this.eraChart = new Chart(eraEl, {
        type: 'doughnut',
        data: {
          labels: Object.keys(eras),
          datasets: [{
            data: Object.values(eras),
            backgroundColor: ['#a855f7','#f59e0b','#10b981','#06b6d4','#ef4444','#64748b'],
          }],
        },
        options: {plugins: {legend: {position: 'bottom', labels: {color: '#cbd5e1'}}}},
      });
      const tp = (this.stats.top_players || []);
      this.playerChart = new Chart(playerEl, {
        type: 'bar',
        data: {
          labels: tp.map(p => p.player),
          datasets: [{label: 'Cards', data: tp.map(p => p.count), backgroundColor: '#d946ef'}],
        },
        options: {indexAxis: 'y', plugins: {legend: {display: false}}, scales: {x: {ticks: {color: '#cbd5e1'}}, y: {ticks: {color: '#cbd5e1'}}}},
      });
    },

    formatNum(n) {
      if (typeof n !== 'number') return n;
      if (n >= 1000) return n.toLocaleString(undefined, {maximumFractionDigits: 0});
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

    openCard(c) { this.editing = JSON.parse(JSON.stringify(c)); },

    async saveCard() {
      const id = this.editing.id;
      const payload = (({year,set_brand,player,card_no,parallel,condition,est_value_raw,status,channel,notes,is_graded,is_autograph,is_relic}) =>
        ({year,set_brand,player,card_no,parallel,condition,est_value_raw,status,channel,notes,is_graded,is_autograph,is_relic}))(this.editing);
      await fetch('/api/inventory/' + id, {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      this.editing = null;
      await this.refreshAll();
    },
    async deleteCard() {
      if (!confirm('Delete this card from the inventory?')) return;
      await fetch('/api/inventory/' + this.editing.id, {method: 'DELETE'});
      this.editing = null;
      await this.refreshAll();
    },

    async previewListing(c) {
      const r = await fetch('/api/listings/preview/' + c.id).then(r => r.json());
      this.listingPreview = r;
    },

    async publishListing(publish) {
      const r = await fetch('/api/listings/publish', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({card_id: this.listingPreview.card_id, overrides: {
          title: this.listingPreview.title,
          format: this.listingPreview.format,
          price: this.listingPreview.price,
          duration: this.listingPreview.duration,
        }, publish}),
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

    async bulkList() {
      if (!confirm(`Draft eBay listings for ${this.selectedIds.length} cards?`)) return;
      await fetch('/api/listings/publish-bulk', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({card_ids: this.selectedIds, publish: false}),
      });
      this.selectedIds = [];
      await this.loadListings();
      this.tab = 'Listings';
    },

    async markSold(L) {
      const price = parseFloat(prompt('Sold price ($)', String(L.price)));
      if (!price) return;
      await fetch(`/api/listings/${L.id}/mark-sold`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({sold_price: price}),
      });
      await this.refreshAll();
    },

    toggleAll(e) {
      this.selectedIds = e.target.checked ? this.inventory.map(c => c.id) : [];
    },
  };
}
