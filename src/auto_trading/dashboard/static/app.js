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

// 청산/진입 사유 코드 → 초보 친화 한국어. 모르는 코드는 원문 그대로.
const REASON_KO = {
  partial_take: "부분 익절",
  trailing: "트레일링 청산",
  stop_loss: "손절",
  take_profit: "익절",
  eod: "마감 청산",
  eod_liquidation: "마감 청산",
  time_exit: "시간 청산",
  index_momentum_daytrade_entry: "지수 모멘텀 진입",
};
function reasonKo(code) {
  if (!code) return "—";
  if (REASON_KO[code]) return REASON_KO[code];
  // 차단 사유 패턴(시장 게이트·발굴 필터) — 동적 시장명이 붙어 표로 못 만든다.
  if (code.endsWith("_below_sma")) return `${code.replace("_below_sma", "")} 추세 이탈(이동평균 아래)`;
  if (code.startsWith("breadth_")) return "시장폭 미달";
  if (code === "avoid_symbols") return "회피 종목";
  if (code === "daily_notional_limit") return "일일 매수대금 한도 소진";
  return code;
}

// 보유시간(분) → "2분" / "1시간 5분".
function formatHold(mins) {
  if (mins == null || Number.isNaN(mins)) return "—";
  if (mins < 60) return `${mins}분`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h}시간 ${m}분` : `${h}시간`;
}

// 회전율(거래대금÷시작자본)을 "0.41배 (41%)" 로.
function formatTurnover(v) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v.toFixed(2)}배 (${(v * 100).toFixed(0)}%)`;
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
          const tripCount = (payload.round_trips || []).length;
          const pnl = payload.realized_pnl_after_cost ?? payload.realized_pnl ?? null;
          recent.push({
            date: d,
            buys,
            roundtrips: tripCount,
            pnl,
            errors: payload.error_count || 0,
            note: payload.note_ko || "",
          });
          // 왕복(round_trips)이 매수↔매도/청산을 짝지은 단위 — day-trade 는 liquidations 가
          // 비어 있어 청산표 기준 승률이 항상 "—" 였다. round_trips 로 승/패를 센다.
          const trips = payload.round_trips || [];
          totalTrades += buys + trips.length;
          errors += payload.error_count || 0;
          for (const rt of trips) {
            if (rt.pnl == null) continue;
            if (rt.pnl > 0) wins++;
            else if (rt.pnl < 0) losses++;
          }
        }
        const wlTotal = wins + losses;
        this.kpi = {
          cumulative_pnl: equity[equity.length - 1].cumulative_pnl,
          win_rate: wlTotal > 0 ? `${((wins / wlTotal) * 100).toFixed(1)}%` : "—",
          win_loss_label: wlTotal > 0 ? `${wins}승 ${losses}패` : "왕복 기록 없음",
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
    reasonKo,
    formatHold,
    formatTurnover,
    // 왕복(round_trips) 승/패 요약 — day-trade 성과의 핵심.
    tripSummary() {
      const trips = this.detail?.round_trips || [];
      let wins = 0;
      let losses = 0;
      for (const t of trips) {
        if (t.pnl == null) continue;
        if (t.pnl > 0) wins++;
        else if (t.pnl < 0) losses++;
      }
      const total = wins + losses;
      return {
        wins,
        losses,
        count: trips.length,
        rate: total > 0 ? `${((wins / total) * 100).toFixed(0)}%` : "—",
      };
    },
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
