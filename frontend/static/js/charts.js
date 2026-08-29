/* TradingView lightweight-charts 래퍼 — 앱 테마에 맞춘 공통 설정 */

const CHART_THEME = {
  layout: {
    background: { type: "solid", color: "transparent" },
    textColor: "#96a2b8",
    fontFamily:
      '"Pretendard", -apple-system, "Segoe UI", "Malgun Gothic", sans-serif',
    fontSize: 11,
  },
  grid: {
    vertLines: { color: "rgba(38,47,66,0.55)" },
    horzLines: { color: "rgba(38,47,66,0.55)" },
  },
  rightPriceScale: { borderColor: "#262f42" },
  timeScale: { borderColor: "#262f42", timeVisible: true, secondsVisible: false },
  crosshair: {
    mode: 1,
    vertLine: { color: "#38bdf8", width: 1, style: 2, labelBackgroundColor: "#2f81f7" },
    horzLine: { color: "#38bdf8", width: 1, style: 2, labelBackgroundColor: "#2f81f7" },
  },
  handleScale: { axisPressedMouseMove: { time: true, price: false } },
};

const _charts = new Map();

/** 컨테이너에 차트를 만들거나 기존 것을 비워 재사용한다. */
function makeChart(id, opts = {}) {
  const node = document.getElementById(id);
  if (!node) return null;

  const existing = _charts.get(id);
  if (existing) {
    existing.series.forEach((s) => {
      try {
        existing.chart.removeSeries(s);
      } catch (_) {
        /* 이미 제거됨 */
      }
    });
    existing.series = [];
    return existing;
  }

  const chart = LightweightCharts.createChart(node, {
    ...CHART_THEME,
    width: node.clientWidth,
    height: node.clientHeight,
    ...opts,
  });
  const entry = { chart, series: [], node };
  _charts.set(id, entry);

  // 레이아웃 변화에 맞춰 폭을 따라가게 한다
  new ResizeObserver(() => {
    chart.applyOptions({ width: node.clientWidth, height: node.clientHeight });
  }).observe(node);

  return entry;
}

function addLine(entry, data, opts = {}) {
  const s = entry.chart.addLineSeries({
    color: "#38bdf8",
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
    ...opts,
  });
  s.setData(data);
  entry.series.push(s);
  return s;
}

function addArea(entry, data, opts = {}) {
  const s = entry.chart.addAreaSeries({
    lineColor: "#38bdf8",
    topColor: "rgba(56,189,248,0.28)",
    bottomColor: "rgba(56,189,248,0.02)",
    lineWidth: 2,
    priceLineVisible: false,
    ...opts,
  });
  s.setData(data);
  entry.series.push(s);
  return s;
}

function addCandles(entry, data, opts = {}) {
  const s = entry.chart.addCandlestickSeries({
    upColor: "#22c55e",
    downColor: "#ef4444",
    borderUpColor: "#22c55e",
    borderDownColor: "#ef4444",
    wickUpColor: "#22c55e",
    wickDownColor: "#ef4444",
    priceLineVisible: false,
    ...opts,
  });
  s.setData(data);
  entry.series.push(s);
  return s;
}

/** 임계선 — 경보/위험/허용치를 수평선으로 표시한다. */
function addThreshold(series, value, title, color) {
  if (value === null || value === undefined) return;
  series.createPriceLine({
    price: value,
    color,
    lineWidth: 1,
    lineStyle: 2,
    axisLabelVisible: true,
    title,
  });
}

function fitChart(entry) {
  entry?.chart.timeScale().fitContent();
}
