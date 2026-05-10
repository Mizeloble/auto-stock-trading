// 대시보드 클라이언트 로직 — Alpine 컴포넌트 + Chart.js 초기화.
// 외부 의존: Tailwind/Chart.js/Alpine.js (전부 base.html 의 CDN).

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) {
    if (res.status === 404) return null;
    throw new Error(`API ${path} ${res.status}`);
  }
  return res.json();
}

const KRW = new Intl.NumberFormat("ko-KR");

function formatPnl(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const rounded = Math.round(v);
  const sign = rounded > 0 ? "+" : "";
  return sign + KRW.format(rounded) + "원";
}

function formatNum(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return KRW.format(Math.round(v));
}

function formatPct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return sign + v.toFixed(2) + "%";
}

function liquidationPnl(li) {
  if (li.sell_price == null || li.buy_price == null) return null;
  return (li.sell_price - li.buy_price) * li.quantity;
}

let equityChartInstance = null;
let backtestEquityChart = null;
let backtestSymbolChart = null;

function overviewPage() {
  return {
    loading: true,
    empty: false,
    period: "",
    kpi: { cumulative_pnl: 0, win_rate: "—", win_loss_label: "", total_trades: 0, errors: 0 },
    recent: [],
    formatPnl,
    async load() {
      try {
        const equity = await api("/api/equity");
        const dailyList = await api("/api/daily");
        const dates = (dailyList?.dates || []).slice(0, 7);

        if (!equity || equity.length === 0) {
          this.loading = false;
          this.empty = true;
          return;
        }

        let wins = 0;
        let losses = 0;
        let totalTrades = 0;
        let errors = 0;
        const recent = [];
        for (const d of dates) {
          const payload = await api(`/api/daily/${d}`);
          if (!payload) continue;
          const buys = (payload.buys || []).length;
          const liqs = (payload.liquidations || []).length;
          const pnl = payload.realized_pnl_after_cost ?? payload.realized_pnl ?? null;
          recent.push({
            date: d,
            buys,
            liquidations: liqs,
            pnl,
            errors: payload.error_count || 0,
            note: payload.note_ko || "",
          });
          totalTrades += buys + liqs;
          errors += payload.error_count || 0;
          for (const li of payload.liquidations || []) {
            const p = liquidationPnl(li);
            if (p == null) continue;
            if (p > 0) wins++;
            else if (p < 0) losses++;
          }
        }
        const wlTotal = wins + losses;
        this.kpi = {
          cumulative_pnl: equity[equity.length - 1].cumulative_pnl,
          win_rate: wlTotal > 0 ? `${((wins / wlTotal) * 100).toFixed(1)}%` : "—",
          win_loss_label: wlTotal > 0 ? `${wins}승 ${losses}패` : "청산 기록 없음",
          total_trades: totalTrades,
          errors,
        };
        this.recent = recent;
        this.period = `${equity[0].date} → ${equity[equity.length - 1].date} · ${equity.length}일`;
        this.loading = false;
        this.empty = false;
        this.$nextTick(() => renderEquityChart(equity));
      } catch (e) {
        console.error(e);
        this.loading = false;
        this.empty = true;
      }
    },
  };
}

function renderEquityChart(points) {
  const canvas = document.getElementById("equityChart");
  if (!canvas) return;
  if (equityChartInstance) equityChartInstance.destroy();
  equityChartInstance = new Chart(canvas, {
    data: {
      labels: points.map((p) => p.date),
      datasets: [
        {
          type: "line",
          label: "누적 손익",
          data: points.map((p) => p.cumulative_pnl),
          borderColor: "#818cf8",
          backgroundColor: "rgba(129,140,248,0.12)",
          borderWidth: 2,
          tension: 0.2,
          pointRadius: 2,
          yAxisID: "y",
          fill: true,
        },
        {
          type: "bar",
          label: "일별 손익",
          data: points.map((p) => p.daily_pnl),
          backgroundColor: points.map((p) =>
            (p.daily_pnl ?? 0) >= 0 ? "rgba(52,211,153,0.7)" : "rgba(244,114,182,0.7)"
          ),
          yAxisID: "y1",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: {
          position: "left",
          grid: { color: "rgba(148,163,184,0.1)" },
          ticks: { color: "#94a3b8", callback: (v) => KRW.format(v) },
          title: { display: true, text: "누적 손익(원)", color: "#64748b" },
        },
        y1: {
          position: "right",
          grid: { display: false },
          ticks: { color: "#94a3b8", callback: (v) => KRW.format(v) },
          title: { display: true, text: "일별 손익(원)", color: "#64748b" },
        },
        x: { grid: { display: false }, ticks: { color: "#94a3b8" } },
      },
      plugins: {
        legend: { labels: { color: "#cbd5e1" } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${formatPnl(ctx.parsed.y)}`,
          },
        },
      },
    },
  });
}

function dailyPage() {
  return {
    loading: true,
    dates: [],
    selected: null,
    detail: null,
    formatPnl,
    formatPct,
    formatNum,
    liquidationPnl,
    async load() {
      try {
        const data = await api("/api/daily");
        this.dates = data?.dates || [];
        this.loading = false;
        const params = new URLSearchParams(location.search);
        const initial = params.get("date") || this.dates[0] || null;
        if (initial) await this.select(initial);
      } catch (e) {
        console.error(e);
        this.loading = false;
      }
    },
    async select(date) {
      this.selected = date;
      this.detail = await api(`/api/daily/${date}`);
    },
  };
}

function backtestsPage() {
  return {
    loading: true,
    runs: [],
    selected: null,
    detail: null,
    metricKeys: [
      ["수익률", "total_return_pct", "%"],
      ["CAGR", "cagr_pct", "%"],
      ["샤프", "sharpe", ""],
      ["MDD", "max_drawdown_pct", "%"],
      ["승률", "win_rate_pct", "%"],
      ["연환산 변동성", "annualized_vol_pct", "%"],
      ["매매", "num_trades", "건"],
      ["라운드트립", "num_round_trips", "건"],
    ],
    formatNum,
    async load() {
      try {
        const data = await api("/api/backtests");
        this.runs = data?.runs || [];
        this.loading = false;
        if (this.runs.length > 0) await this.select(this.runs[0].name);
      } catch (e) {
        console.error(e);
        this.loading = false;
      }
    },
    async select(name) {
      this.selected = name;
      this.detail = await api(`/api/backtests/${encodeURIComponent(name)}`);
      this.$nextTick(() => {
        if (!this.detail) return;
        renderBacktestEquity(this.detail.equity || []);
        renderBacktestSymbolPnl(this.detail.symbol_pnl || []);
      });
    },
    formatMetric(key, suffix) {
      const v = this.detail?.report?.[key];
      if (v === null || v === undefined) return "—";
      if (typeof v === "number") {
        const formatted = Number.isInteger(v) ? v.toString() : v.toFixed(2);
        return formatted + suffix;
      }
      return String(v) + suffix;
    },
    metricColor(key) {
      const v = this.detail?.report?.[key];
      if (typeof v !== "number") return "text-slate-300";
      if (key === "max_drawdown_pct") return v < 0 ? "text-rose-400" : "text-slate-300";
      if (key === "total_return_pct" || key === "cagr_pct") {
        return v > 0 ? "text-emerald-400" : v < 0 ? "text-rose-400" : "text-slate-300";
      }
      return "text-slate-100";
    },
  };
}

function renderBacktestEquity(points) {
  const canvas = document.getElementById("btEquityChart");
  if (!canvas) return;
  if (backtestEquityChart) backtestEquityChart.destroy();
  if (points.length === 0) return;
  backtestEquityChart = new Chart(canvas, {
    type: "line",
    data: {
      labels: points.map((p) => p.date),
      datasets: [
        {
          label: "Equity",
          data: points.map((p) => p.equity),
          borderColor: "#818cf8",
          backgroundColor: "rgba(129,140,248,0.12)",
          borderWidth: 2,
          tension: 0.1,
          pointRadius: 0,
          fill: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: {
          grid: { color: "rgba(148,163,184,0.1)" },
          ticks: { color: "#94a3b8", callback: (v) => KRW.format(v) },
        },
        x: {
          grid: { display: false },
          ticks: { color: "#94a3b8", maxTicksLimit: 8 },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: (ctx) => formatNum(ctx.parsed.y) + "원" },
        },
      },
    },
  });
}

function renderBacktestSymbolPnl(rows) {
  const canvas = document.getElementById("btSymbolChart");
  if (!canvas) return;
  if (backtestSymbolChart) backtestSymbolChart.destroy();
  if (rows.length === 0) return;
  backtestSymbolChart = new Chart(canvas, {
    type: "bar",
    data: {
      labels: rows.map((r) => r.symbol),
      datasets: [
        {
          label: "PnL",
          data: rows.map((r) => r.pnl),
          backgroundColor: rows.map((r) =>
            r.pnl >= 0 ? "rgba(52,211,153,0.75)" : "rgba(244,114,182,0.75)"
          ),
        },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => formatPnl(ctx.parsed.x) } },
      },
      scales: {
        y: { ticks: { color: "#94a3b8" }, grid: { display: false } },
        x: {
          grid: { color: "rgba(148,163,184,0.1)" },
          ticks: { color: "#94a3b8", callback: (v) => KRW.format(v) },
        },
      },
    },
  });
}
