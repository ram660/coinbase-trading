import type {
  BacktestStats,
  Candle,
  IndicatorPoint,
  OptionStrategyAction,
  SignalBias,
  StrategyRecommendation,
  TechnicalSummary,
  TickerMeta,
  VolatilityRegime,
  WealthsimpleAccount,
} from '../types/market';

function calcEMA(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = new Array(values.length).fill(null);
  if (values.length < period) return result;
  const k = 2 / (period + 1);
  let sum = 0;
  for (let i = 0; i < period; i++) {
    sum += values[i];
  }
  let ema = sum / period;
  result[period - 1] = ema;
  for (let i = period; i < values.length; i++) {
    ema = values[i] * k + ema * (1 - k);
    result[i] = ema;
  }
  return result;
}

function calcSMA(values: number[], period: number): (number | null)[] {
  const result: (number | null)[] = new Array(values.length).fill(null);
  if (values.length < period) return result;
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= period) sum -= values[i - period];
    if (i >= period - 1) {
      result[i] = sum / period;
    }
  }
  return result;
}

function calcWilderRSI(closes: number[], period = 14): (number | null)[] {
  const result: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length <= period) return result;

  let gainSum = 0;
  let lossSum = 0;
  for (let i = 1; i <= period; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff >= 0) gainSum += diff;
    else lossSum += Math.abs(diff);
  }

  let avgGain = gainSum / period;
  let avgLoss = lossSum / period;
  result[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);

  for (let i = period + 1; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? Math.abs(diff) : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    result[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }

  return result;
}

function calcMACD(closes: number[], fast = 12, slow = 26, signal = 9) {
  const emaFast = calcEMA(closes, fast);
  const emaSlow = calcEMA(closes, slow);
  const macdLine: (number | null)[] = closes.map((_, i) => {
    const f = emaFast[i];
    const s = emaSlow[i];
    return f !== null && s !== null ? f - s : null;
  });

  const validStart = macdLine.findIndex((v) => v !== null);
  const signalLine: (number | null)[] = new Array(closes.length).fill(null);
  const histLine: (number | null)[] = new Array(closes.length).fill(null);

  if (validStart !== -1) {
    const validMacd = macdLine.slice(validStart).map((v) => v ?? 0);
    const sigSub = calcEMA(validMacd, signal);
    for (let i = 0; i < sigSub.length; i++) {
      const idx = validStart + i;
      signalLine[idx] = sigSub[i];
      if (sigSub[i] !== null && macdLine[idx] !== null) {
        histLine[idx] = (macdLine[idx] as number) - (sigSub[i] as number);
      }
    }
  }

  return { macdLine, signalLine, histLine };
}

function calcBollinger(closes: number[], period = 20, mult = 2) {
  const sma = calcSMA(closes, period);
  const upper: (number | null)[] = new Array(closes.length).fill(null);
  const lower: (number | null)[] = new Array(closes.length).fill(null);
  const percentB: (number | null)[] = new Array(closes.length).fill(null);
  const width: (number | null)[] = new Array(closes.length).fill(null);

  for (let i = period - 1; i < closes.length; i++) {
    const mean = sma[i];
    if (mean === null) continue;
    let sqSum = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const diff = closes[j] - mean;
      sqSum += diff * diff;
    }
    const std = Math.sqrt(sqSum / period);
    const u = mean + mult * std;
    const l = mean - mult * std;
    upper[i] = u;
    lower[i] = l;
    const bandRange = u - l;
    percentB[i] = bandRange > 0 ? (closes[i] - l) / bandRange : 0.5;
    width[i] = mean > 0 ? (bandRange / mean) * 100 : 0;
  }

  return { upper, middle: sma, lower, percentB, width };
}

function calcATR(candles: Candle[], period = 14): (number | null)[] {
  const result: (number | null)[] = new Array(candles.length).fill(null);
  if (candles.length <= period) return result;

  const trs: number[] = candles.map((c, i) => {
    if (i === 0) return c.high - c.low;
    const prevClose = candles[i - 1].close;
    return Math.max(c.high - c.low, Math.abs(c.high - prevClose), Math.abs(c.low - prevClose));
  });

  let sum = 0;
  for (let i = 1; i <= period; i++) sum += trs[i];
  let atr = sum / period;
  result[period] = atr;

  for (let i = period + 1; i < candles.length; i++) {
    atr = (atr * (period - 1) + trs[i]) / period;
    result[i] = atr;
  }
  return result;
}

function calcRollingVWAP(candles: Candle[], window = 24): (number | null)[] {
  const result: (number | null)[] = new Array(candles.length).fill(null);
  for (let i = 0; i < candles.length; i++) {
    const start = Math.max(0, i - window + 1);
    let pvSum = 0;
    let vSum = 0;
    for (let j = start; j <= i; j++) {
      const typical = (candles[j].high + candles[j].low + candles[j].close) / 3;
      const vol = Math.max(candles[j].volume, 1);
      pvSum += typical * vol;
      vSum += vol;
    }
    result[i] = vSum > 0 ? pvSum / vSum : candles[i].close;
  }
  return result;
}

function calcSupertrend(
  candles: Candle[],
  period = 10,
  mult = 3
): { line: (number | null)[]; dir: (1 | -1 | null)[]; xTrendFast: (number | null)[] } {
  const n = candles.length;
  const line: (number | null)[] = new Array(n).fill(null);
  const dir: (1 | -1 | null)[] = new Array(n).fill(null);
  const hl2 = candles.map((c) => (c.high + c.low) / 2);
  const xTrendFast = calcEMA(hl2, 13);
  const atr = calcATR(candles, period);

  let prevFinalUpper = 0;
  let prevFinalLower = 0;
  let prevDir: 1 | -1 = 1;
  let initialized = false;

  for (let i = 0; i < n; i++) {
    const a = atr[i];
    if (a === null) continue;

    const basicUpper = hl2[i] + mult * a;
    const basicLower = hl2[i] - mult * a;

    if (!initialized) {
      prevFinalUpper = basicUpper;
      prevFinalLower = basicLower;
      prevDir = candles[i].close >= hl2[i] ? 1 : -1;
      initialized = true;
      dir[i] = prevDir;
      line[i] = prevDir === 1 ? prevFinalLower : prevFinalUpper;
      continue;
    }

    const prevClose = candles[i - 1].close;
    const finalUpper =
      basicUpper < prevFinalUpper || prevClose > prevFinalUpper ? basicUpper : prevFinalUpper;
    const finalLower =
      basicLower > prevFinalLower || prevClose < prevFinalLower ? basicLower : prevFinalLower;

    let currDir: 1 | -1 = prevDir;
    if (prevDir === 1 && candles[i].close < finalLower) {
      currDir = -1;
    } else if (prevDir === -1 && candles[i].close > finalUpper) {
      currDir = 1;
    }

    dir[i] = currDir;
    line[i] = currDir === 1 ? finalLower : finalUpper;

    prevFinalUpper = finalUpper;
    prevFinalLower = finalLower;
    prevDir = currDir;
  }

  return { line, dir, xTrendFast };
}

function calcDMI(
  candles: Candle[],
  period = 14
): { diPlus: (number | null)[]; diMinus: (number | null)[]; adx: (number | null)[] } {
  const n = candles.length;
  const diPlus: (number | null)[] = new Array(n).fill(null);
  const diMinus: (number | null)[] = new Array(n).fill(null);
  const adx: (number | null)[] = new Array(n).fill(null);

  if (n <= period * 2) return { diPlus, diMinus, adx };

  const tr: number[] = new Array(n).fill(0);
  const plusDm: number[] = new Array(n).fill(0);
  const minusDm: number[] = new Array(n).fill(0);

  for (let i = 1; i < n; i++) {
    const upMove = candles[i].high - candles[i - 1].high;
    const downMove = candles[i - 1].low - candles[i].low;
    plusDm[i] = upMove > downMove && upMove > 0 ? upMove : 0;
    minusDm[i] = downMove > upMove && downMove > 0 ? downMove : 0;
    tr[i] = Math.max(
      candles[i].high - candles[i].low,
      Math.abs(candles[i].high - candles[i - 1].close),
      Math.abs(candles[i].low - candles[i - 1].close)
    );
  }

  let smTr = 0;
  let smPlusDm = 0;
  let smMinusDm = 0;
  for (let i = 1; i <= period; i++) {
    smTr += tr[i];
    smPlusDm += plusDm[i];
    smMinusDm += minusDm[i];
  }

  const dxArr: (number | null)[] = new Array(n).fill(null);

  for (let i = period; i < n; i++) {
    if (i > period) {
      smTr = smTr - smTr / period + tr[i];
      smPlusDm = smPlusDm - smPlusDm / period + plusDm[i];
      smMinusDm = smMinusDm - smMinusDm / period + minusDm[i];
    }

    const pDi = smTr > 0 ? (100 * smPlusDm) / smTr : 0;
    const mDi = smTr > 0 ? (100 * smMinusDm) / smTr : 0;
    diPlus[i] = pDi;
    diMinus[i] = mDi;
    const sumDi = pDi + mDi;
    dxArr[i] = sumDi > 0 ? (100 * Math.abs(pDi - mDi)) / sumDi : 0;
  }

  const adxStart = period * 2 - 1;
  if (adxStart < n) {
    let dxSum = 0;
    for (let i = period; i <= adxStart; i++) {
      dxSum += dxArr[i] ?? 0;
    }
    let currAdx = dxSum / period;
    adx[adxStart] = currAdx;

    for (let i = adxStart + 1; i < n; i++) {
      currAdx = (currAdx * (period - 1) + (dxArr[i] ?? 0)) / period;
      adx[i] = currAdx;
    }
  }

  return { diPlus, diMinus, adx };
}

export function computeIndicatorSeries(candles: Candle[]): IndicatorPoint[] {
  const closes = candles.map((c) => c.close);
  const volumes = candles.map((c) => c.volume);

  const ema9 = calcEMA(closes, 9);
  const ema21 = calcEMA(closes, 21);
  const ema50 = calcEMA(closes, 50);
  const ema200 = calcEMA(closes, 200);
  const bb = calcBollinger(closes, 20, 2);
  const rsi = calcWilderRSI(closes, 14);
  const macd = calcMACD(closes, 12, 26, 9);
  const atr = calcATR(candles, 14);
  const vwap = calcRollingVWAP(candles, 24);
  const volSma = calcSMA(volumes, 20);
  const st = calcSupertrend(candles, 10, 3);
  const dmi = calcDMI(candles, 14);

  return candles.map((c, i) => ({
    time: c.time,
    close: c.close,
    volume: c.volume,
    ema9: ema9[i],
    ema21: ema21[i],
    ema50: ema50[i],
    ema200: ema200[i],
    bbUpper: bb.upper[i],
    bbMiddle: bb.middle[i],
    bbLower: bb.lower[i],
    bbPercentB: bb.percentB[i],
    bbWidth: bb.width[i],
    rsi: rsi[i],
    macd: macd.macdLine[i],
    macdSignal: macd.signalLine[i],
    macdHist: macd.histLine[i],
    atr: atr[i],
    atrPercent: atr[i] !== null && c.close > 0 ? ((atr[i] as number) / c.close) * 100 : null,
    vwap: vwap[i],
    rvol: volSma[i] && (volSma[i] as number) > 0 ? c.volume / (volSma[i] as number) : 1,
    supertrend: st.line[i],
    supertrendDir: st.dir[i],
    xTrendFast: st.xTrendFast[i],
    diPlus: dmi.diPlus[i],
    diMinus: dmi.diMinus[i],
    adx: dmi.adx[i],
  }));
}

function detectRSIDivergence(candles: Candle[], series: IndicatorPoint[]): 'Bullish Divergence' | 'Bearish Divergence' | 'None' {
  if (candles.length < 30) return 'None';
  const recent = series.slice(-24);
  const valid = recent.filter((p) => p.rsi !== null);
  if (valid.length < 15) return 'None';

  const half = Math.floor(valid.length / 2);
  const firstHalf = valid.slice(0, half);
  const secondHalf = valid.slice(half);

  const minPrice1 = Math.min(...firstHalf.map((p) => p.close));
  const minPrice2 = Math.min(...secondHalf.map((p) => p.close));
  const minRsi1 = Math.min(...firstHalf.map((p) => p.rsi as number));
  const minRsi2 = Math.min(...secondHalf.map((p) => p.rsi as number));

  if (minPrice2 < minPrice1 * 0.995 && minRsi2 > minRsi1 + 3 && minRsi2 < 45) {
    return 'Bullish Divergence';
  }

  const maxPrice1 = Math.max(...firstHalf.map((p) => p.close));
  const maxPrice2 = Math.max(...secondHalf.map((p) => p.close));
  const maxRsi1 = Math.max(...firstHalf.map((p) => p.rsi as number));
  const maxRsi2 = Math.max(...secondHalf.map((p) => p.rsi as number));

  if (maxPrice2 > maxPrice1 * 1.005 && maxRsi2 < maxRsi1 - 3 && maxRsi2 > 55) {
    return 'Bearish Divergence';
  }

  return 'None';
}

export function buildTechnicalSummary(
  meta: TickerMeta,
  candles: Candle[],
  series: IndicatorPoint[],
  account: WealthsimpleAccount
): TechnicalSummary {
  const last = series[series.length - 1];
  const prev = series[series.length - 2] ?? last;
  const price = last.close;

  const lookback24 = candles.slice(-24);
  const ref24 = candles[Math.max(0, candles.length - 25)] ?? candles[0];
  const change24h = price - ref24.close;
  const changePercent24h = ref24.close > 0 ? (change24h / ref24.close) * 100 : 0;
  const high24h = Math.max(...lookback24.map((c) => c.high));
  const low24h = Math.min(...lookback24.map((c) => c.low));
  const volume24h = lookback24.reduce((acc, c) => acc + c.volume, 0);

  const rsi = last.rsi ?? 50;
  const rsiState: TechnicalSummary['rsiState'] =
    rsi < 30
      ? 'Oversold'
      : rsi < 42
        ? 'Neutral-Low'
        : rsi <= 58
          ? 'Neutral'
          : rsi <= 70
            ? 'Neutral-High'
            : 'Overbought';

  const rsiDivergence = detectRSIDivergence(candles, series);

  const macd = last.macd ?? 0;
  const macdSignal = last.macdSignal ?? 0;
  const macdHist = last.macdHist ?? 0;
  const prevMacd = prev.macd ?? macd;
  const prevSig = prev.macdSignal ?? macdSignal;
  const prevHist = prev.macdHist ?? macdHist;

  let macdEvent: TechnicalSummary['macdEvent'] = 'Flat';
  if (prevMacd <= prevSig && macd > macdSignal) macdEvent = 'Bullish Cross';
  else if (prevMacd >= prevSig && macd < macdSignal) macdEvent = 'Bearish Cross';
  else if (macdHist > 0 && macdHist >= prevHist) macdEvent = 'Bullish Momentum';
  else if (macdHist < 0 && macdHist <= prevHist) macdEvent = 'Bearish Momentum';
  else if (macd > macdSignal) macdEvent = 'Bullish Momentum';
  else if (macd < macdSignal) macdEvent = 'Bearish Momentum';

  const bbUpper = last.bbUpper ?? price * 1.03;
  const bbMiddle = last.bbMiddle ?? price;
  const bbLower = last.bbLower ?? price * 0.97;
  const bbPercentB = last.bbPercentB ?? 0.5;
  const bbWidth = last.bbWidth ?? 4;

  const recentWidths = series
    .slice(-40)
    .map((p) => p.bbWidth)
    .filter((w): w is number => w !== null);
  const avgBbWidth = recentWidths.length > 0 ? recentWidths.reduce((a, b) => a + b, 0) / recentWidths.length : bbWidth;
  const minBbWidth = recentWidths.length > 0 ? Math.min(...recentWidths) : bbWidth;
  const maxBbWidth = recentWidths.length > 0 ? Math.max(...recentWidths) : bbWidth;
  const prevBbWidth = prev.bbWidth ?? bbWidth;
  const bbWidthChangePct = prevBbWidth > 0 ? ((bbWidth - prevBbWidth) / prevBbWidth) * 100 : 0;

  const atr = last.atr ?? price * 0.02;
  const atrPercent = last.atrPercent ?? 2;
  const recentAtrs = series
    .slice(-21, -1)
    .map((p) => p.atr)
    .filter((a): a is number => a !== null);
  const avgAtr = recentAtrs.length > 0 ? recentAtrs.reduce((a, b) => a + b, 0) / recentAtrs.length : atr;
  const atrSpikeRatio = avgAtr > 0 ? atr / avgAtr : 1;

  const rvol = last.rvol ?? 1;
  const vwap = last.vwap ?? price;

  let emaTrend: TechnicalSummary['emaTrend'] = 'Mixed / Transition';
  if (last.ema9 && last.ema21 && last.ema50) {
    if (last.ema9 > last.ema21 && last.ema21 > last.ema50) emaTrend = 'Bullish Stack';
    else if (last.ema9 < last.ema21 && last.ema21 < last.ema50) emaTrend = 'Bearish Stack';
  }

  // Volatility regime classification
  const widthPercentile =
    maxBbWidth > minBbWidth ? (bbWidth - minBbWidth) / (maxBbWidth - minBbWidth) : 0.5;

  let volatilityRegime: VolatilityRegime = 'Normal';
  if (atrSpikeRatio >= 1.55 || bbWidthChangePct >= 22 || (bbWidth > avgBbWidth * 1.45 && rvol >= 1.5)) {
    volatilityRegime = 'Spike';
  } else if (widthPercentile <= 0.22) {
    volatilityRegime = 'Squeeze';
  } else if (bbWidth > avgBbWidth * 1.15 || atrSpikeRatio >= 1.25) {
    volatilityRegime = 'Expanding';
  }

  // Supertrend (10, hl2, 3) & DMI / ADX (14, 14)
  const supertrend = last.supertrend ?? price;
  const stDirNum = last.supertrendDir ?? 1;
  const prevStDirNum = prev.supertrendDir ?? stDirNum;
  const supertrendDir: TechnicalSummary['supertrendDir'] = stDirNum === 1 ? 'Bullish' : 'Bearish';
  const supertrendEvent: TechnicalSummary['supertrendEvent'] =
    prevStDirNum === -1 && stDirNum === 1
      ? 'Buy Flip'
      : prevStDirNum === 1 && stDirNum === -1
        ? 'Sell Flip'
        : stDirNum === 1
          ? 'Bullish Trend'
          : 'Bearish Trend';

  const diPlus = last.diPlus ?? 20;
  const diMinus = last.diMinus ?? 20;
  const adx = last.adx ?? 20;
  const trendStrength: TechnicalSummary['trendStrength'] =
    adx >= 25 ? 'Strong Trend' : adx >= 18 ? 'Developing' : 'Choppy / Weak';

  // Support / Resistance from 30-bar swing highs/lows
  const swingWindow = candles.slice(-30);
  const supportLevel = Math.min(...swingWindow.map((c) => c.low));
  const resistanceLevel = Math.max(...swingWindow.map((c) => c.high));

  // Confluence score (-100 to +100)
  let score = 0;
  // RSI contribution (-30 to +30)
  if (rsi < 30) score += 24;
  else if (rsi < 40) score += 12;
  else if (rsi > 72) score -= 24;
  else if (rsi > 62) score -= 10;
  else if (rsi >= 50) score += 5;
  else score -= 4;

  if (rsiDivergence === 'Bullish Divergence') score += 14;
  if (rsiDivergence === 'Bearish Divergence') score -= 14;

  // MACD contribution (-28 to +28)
  if (macdEvent === 'Bullish Cross') score += 26;
  else if (macdEvent === 'Bearish Cross') score -= 26;
  else if (macdEvent === 'Bullish Momentum') score += 13;
  else if (macdEvent === 'Bearish Momentum') score -= 13;

  // Bollinger Bands contribution
  if (bbPercentB <= 0.08) score += 18;
  else if (bbPercentB >= 0.95 && rsi > 68) score -= 18;
  else if (bbPercentB > 0.65 && emaTrend === 'Bullish Stack') score += 8;
  else if (bbPercentB < 0.35 && emaTrend === 'Bearish Stack') score -= 10;

  // EMA & VWAP trend contribution
  if (emaTrend === 'Bullish Stack') score += 10;
  if (emaTrend === 'Bearish Stack') score -= 10;
  if (price > vwap) score += 5;
  else score -= 5;

  // Supertrend (10, hl2, 3) + DMI/ADX (14, 14) confirmation & chop filter
  if (supertrendEvent === 'Buy Flip') score += 18;
  else if (supertrendEvent === 'Sell Flip') score -= 18;
  else if (supertrendDir === 'Bullish') score += 8;
  else score -= 8;

  if (adx >= 22) {
    if (diPlus > diMinus + 3) score += 10;
    else if (diMinus > diPlus + 3) score -= 10;
  } else if (adx < 18) {
    // Dampen directional breakout score in choppy/sideways markets to protect TFSA options against theta
    score = score * 0.72;
  }

  const confluenceScore = Math.max(-100, Math.min(100, Math.round(score)));

  const strategy = generateWealthsimpleStrategy({
    meta,
    account,
    price,
    rsi,
    rsiState,
    rsiDivergence,
    macdEvent,
    bbPercentB,
    bbWidth,
    bbUpper,
    bbLower,
    bbMiddle,
    atr,
    atrSpikeRatio,
    rvol,
    vwap,
    emaTrend,
    volatilityRegime,
    supertrend,
    supertrendEvent,
    supertrendDir,
    diPlus,
    diMinus,
    adx,
    trendStrength,
    confluenceScore,
    supportLevel,
    resistanceLevel,
  });

  return {
    price,
    change24h,
    changePercent24h,
    high24h,
    low24h,
    volume24h,
    rsi,
    rsiState,
    rsiDivergence,
    macd,
    macdSignal,
    macdHist,
    macdEvent,
    bbUpper,
    bbMiddle,
    bbLower,
    bbPercentB,
    bbWidth,
    bbWidthChangePct,
    atr,
    atrPercent,
    atrSpikeRatio,
    rvol,
    vwap,
    emaTrend,
    volatilityRegime,
    supertrend,
    supertrendDir,
    supertrendEvent,
    diPlus,
    diMinus,
    adx,
    trendStrength,
    supportLevel,
    resistanceLevel,
    confluenceScore,
    strategy,
  };
}

function generateWealthsimpleStrategy(input: {
  meta: TickerMeta;
  account: WealthsimpleAccount;
  price: number;
  rsi: number;
  rsiState: TechnicalSummary['rsiState'];
  rsiDivergence: TechnicalSummary['rsiDivergence'];
  macdEvent: TechnicalSummary['macdEvent'];
  bbPercentB: number;
  bbWidth: number;
  bbUpper: number;
  bbLower: number;
  bbMiddle: number;
  atr: number;
  atrSpikeRatio: number;
  rvol: number;
  vwap: number;
  emaTrend: TechnicalSummary['emaTrend'];
  volatilityRegime: VolatilityRegime;
  supertrend: number;
  supertrendEvent: TechnicalSummary['supertrendEvent'];
  supertrendDir: TechnicalSummary['supertrendDir'];
  diPlus: number;
  diMinus: number;
  adx: number;
  trendStrength: TechnicalSummary['trendStrength'];
  confluenceScore: number;
  supportLevel: number;
  resistanceLevel: number;
}): StrategyRecommendation {
  const {
    meta,
    account,
    price,
    rsi,
    rsiDivergence,
    macdEvent,
    bbPercentB,
    bbUpper,
    bbLower,
    bbMiddle,
    atr,
    atrSpikeRatio,
    rvol,
    vwap,
    emaTrend,
    volatilityRegime,
    supertrend,
    supertrendEvent,
    supertrendDir,
    diPlus,
    diMinus,
    adx,
    trendStrength,
    confluenceScore,
  } = input;

  const fmtPx = (v: number) => (v >= 1000 ? v.toFixed(0) : v < 2 ? v.toFixed(4) : v.toFixed(2));

  const bias: SignalBias =
    confluenceScore >= 45
      ? 'Strong Bullish'
      : confluenceScore >= 18
        ? 'Bullish'
        : confluenceScore <= -45
          ? 'Strong Bearish'
          : confluenceScore <= -18
            ? 'Bearish'
            : 'Neutral';

  const confidence = Math.min(94, Math.max(38, Math.round(45 + Math.abs(confluenceScore) * 0.5)));
  const rationale: string[] = [];

  if (rsi < 32) rationale.push(`RSI (${rsi.toFixed(1)}) is deeply oversold, signaling exhaustion of selling pressure.`);
  else if (rsi > 70) rationale.push(`RSI (${rsi.toFixed(1)}) is overbought, raising mean-reversion pullback risk.`);
  else rationale.push(`RSI at ${rsi.toFixed(1)} leaves room for directional continuation.`);

  if (rsiDivergence !== 'None') rationale.push(`${rsiDivergence} detected between price swings and RSI momentum.`);

  if (macdEvent === 'Bullish Cross') rationale.push('MACD just crossed above its Signal line, confirming upside momentum shift.');
  else if (macdEvent === 'Bearish Cross') rationale.push('MACD crossed below Signal line, indicating bearish momentum acceleration.');
  else rationale.push(`MACD shows ${macdEvent.toLowerCase()} relative to signal baseline.`);

  rationale.push(
    `Supertrend (10, hl2, 3) is ${supertrendEvent}; DMI(14) reads +DI ${diPlus.toFixed(1)} vs -DI ${diMinus.toFixed(1)} with ADX ${adx.toFixed(1)} (${trendStrength}).`
  );

  if (adx < 18) {
    rationale.push('ADX below 18 warns of choppy sideways price action — beware buying single-leg Calls/Puts into theta decay.');
  }

  if (volatilityRegime === 'Squeeze') {
    rationale.push('Bollinger BandWidth is in a tight squeeze, signaling an imminent high-velocity breakout.');
  } else if (volatilityRegime === 'Spike') {
    rationale.push(`Volatility spike active (ATR ${atrSpikeRatio.toFixed(2)}× baseline, RVOL ${rvol.toFixed(2)}×); option premiums are elevated.`);
  } else if (bbPercentB <= 0.15) {
    rationale.push('Price is hugging the lower Bollinger Band (%B < 0.15), favoring mean-reversion bounce.');
  } else if (bbPercentB >= 0.85) {
    rationale.push('Price is testing the upper Bollinger Band (%B > 0.85); watch for breakout or upper-band rejection.');
  }

  rationale.push(`Price is ${price >= vwap ? 'above' : 'below'} rolling VWAP with ${emaTrend.toLowerCase()} EMA alignment.`);

  const isCryptoAsset = meta.assetClass === 'Crypto' || account === 'Crypto';
  const isRegistered = account === 'TFSA' || account === 'RRSP';

  let action: OptionStrategyAction = 'Wait / Hold';
  let targetDelta = 'N/A';
  let suggestedDTE = 'N/A';
  let strikeGuidance = 'No active entry';
  let stopLossPrice = price - atr * 1.5;
  let targetPrice = price + atr * 2.5;
  let wealthsimpleRuleNote = '';

  if (isCryptoAsset || !meta.optionsEligible) {
    const trendSetup =
      supertrendDir === 'Bullish' &&
      adx >= 20 &&
      diPlus > diMinus &&
      rsi >= 42 &&
      rsi <= 68 &&
      confluenceScore >= 20;
    const bounceSetup =
      rsi <= 33 && (rsiDivergence === 'Bullish Divergence' || bbPercentB <= 0.14);
    const overextendedTakeProfit =
      (rsi >= 73 && bbPercentB >= 0.9) || supertrendEvent === 'Sell Flip';

    if (trendSetup) {
      action = 'Spot Accumulate';
      targetDelta = 'Trend (15–20% Cap)';
      suggestedDTE = '2–7d Swing';
      strikeGuidance = `Limit buy ~$${fmtPx(price * 0.994)} (Supertrend Bull + ADX ${adx.toFixed(0)}); trail stop @ $${fmtPx(supertrend)}`;
      stopLossPrice = Math.max(supertrend * 0.995, price - atr * 1.6);
      targetPrice = Math.max(bbUpper, price + atr * 3.0);
    } else if (bounceSetup) {
      action = 'Spot Accumulate';
      targetDelta = 'Bounce (10–15% Cap)';
      suggestedDTE = '1–4d Reversion';
      strikeGuidance = `Limit dip entry ~$${fmtPx(Math.min(price * 0.992, bbLower * 1.005))} on RSI ${rsi.toFixed(0)} bounce → target VWAP/20SMA`;
      stopLossPrice = bbLower - atr * 1.1;
      targetPrice = Math.max(bbMiddle, vwap, price + atr * 2.4);
    } else if (overextendedTakeProfit || (confluenceScore <= -25 && adx >= 18)) {
      action = 'Scale Out / Hedge';
      targetDelta = 'Take 50% Profit';
      suggestedDTE = 'Immediate Trim';
      strikeGuidance =
        rsi >= 73
          ? `Scale out 50% near $${fmtPx(price)} (RSI overbought); trail rest at $${fmtPx(supertrend)}`
          : `Exit/trim 50–100% on Bearish flip below $${fmtPx(supertrend)} or hedge via ${meta.proxySymbol ?? 'BITI.TO / IBIT Puts'}`;
      stopLossPrice = price + atr * 1.5;
      targetPrice = Math.min(bbLower, price - atr * 2.2);
    } else if (confluenceScore >= 28 && rsi <= 70) {
      action = 'Spot Accumulate';
      targetDelta = '10–15% Spot';
      suggestedDTE = '3–7d Swing';
      strikeGuidance = `Limit entry ~$${fmtPx(price * 0.993)}; scale 50% at +2.5× ATR ($${fmtPx(price + atr * 2.5)})`;
      stopLossPrice = Math.min(bbLower, price - atr * 1.6);
      targetPrice = Math.max(bbUpper, price + atr * 2.8);
    } else {
      action = 'Wait / Hold';
      targetDelta = 'Cash Reserve';
      suggestedDTE = '—';
      strikeGuidance =
        adx < 18
          ? `Chop filter active (ADX ${adx.toFixed(1)} < 18): stay in CAD to avoid ~1.5–2% WS Crypto spread bleed`
          : rsi > 68
            ? `Extended RSI (${rsi.toFixed(0)}): wait for pullback to VWAP ($${fmtPx(vwap)}) before buying`
            : `Await Supertrend Buy flip + ADX ≥ 20, BB Squeeze break, or RSI ≤ 32 dip`;
    }
    wealthsimpleRuleNote =
      'Wealthsimple Crypto supports spot only (no native options; ~1.5–2% spread). Use Limit Orders, risk ≤2% of your $2k account per trade ($300–$400 max position), and keep 40% cash for dips. Use IBIT/BTCC.TO for TFSA tax-free trades.';
  } else if (volatilityRegime === 'Squeeze' && Math.abs(confluenceScore) < 25) {
    if (account === 'Non-Registered') {
      action = 'Long Straddle';
      targetDelta = '±0.50 ATM';
      suggestedDTE = '30–45 DTE';
      strikeGuidance = `ATM Call + Put near $${Math.round(price)} before volatility expands`;
      stopLossPrice = price - atr * 2.0;
      targetPrice = price + atr * 3.0;
      wealthsimpleRuleNote =
        'Non-Registered margin/cash account supports multi-leg volatility plays. Buy 30–45 DTE to protect against theta decay while waiting for the BB squeeze release.';
    } else {
      action = 'Wait / Hold';
      targetDelta = 'Wait for break';
      suggestedDTE = '30–45 DTE';
      strikeGuidance = `Set alert for BB break above $${bbUpper.toFixed(2)} (Call) or below $${bbLower.toFixed(2)} (Put)`;
      wealthsimpleRuleNote = `In a ${account}, avoid buying expensive neutral straddles until direction confirms out of the Bollinger Squeeze.`;
    }
  } else if (confluenceScore >= 30) {
    if (account === 'Non-Registered' && volatilityRegime === 'Spike') {
      action = 'Bull Call Spread';
      targetDelta = '+0.55 / -0.25';
      suggestedDTE = '30–45 DTE';
      strikeGuidance = `Buy $${(price * 0.99).toFixed(1)} Call / Sell $${bbUpper.toFixed(1)} Call to offset high IV`;
    } else {
      action = 'Buy Call';
      targetDelta = '0.50–0.60 Delta';
      suggestedDTE = '35–45 DTE';
      strikeGuidance = `Slightly ITM/ATM Call strike near $${(price * 0.985).toFixed(2)}`;
    }
    stopLossPrice = Math.min(bbMiddle * 0.99, price - atr * 1.5);
    targetPrice = Math.max(bbUpper, price + atr * 2.8);
    wealthsimpleRuleNote = isRegistered
      ? `${account} permits Long Calls cleanly. Keep contract sizing ≤ 3–5% of ${account} capital because option losses permanently reduce contribution room.`
      : 'Non-Registered account allows both Long Calls and vertical Bull Call Spreads to cap IV crush.';
  } else if (confluenceScore <= -30) {
    if (account === 'Non-Registered' && volatilityRegime === 'Spike') {
      action = 'Bear Put Spread';
      targetDelta = '-0.55 / +0.25';
      suggestedDTE = '30–45 DTE';
      strikeGuidance = `Buy $${(price * 1.01).toFixed(1)} Put / Sell $${bbLower.toFixed(1)} Put`;
    } else {
      action = 'Buy Put';
      targetDelta = '-0.50 to -0.60 Delta';
      suggestedDTE = '30–45 DTE';
      strikeGuidance = `Slightly ITM/ATM Put strike near $${(price * 1.015).toFixed(2)}`;
    }
    stopLossPrice = Math.max(bbMiddle * 1.01, price + atr * 1.5);
    targetPrice = Math.min(bbLower, price - atr * 2.8);
    wealthsimpleRuleNote = isRegistered
      ? `${account} allows Long Puts for directional downside or Protective Puts against existing shares. Naked short calls/puts are prohibited by CRA rules.`
      : 'Non-Registered account supports Long Puts and Bear Put Spreads; avoid naked short calls with unlimited risk.';
  } else if (rsi >= 55 && rsi <= 68 && volatilityRegime !== 'Squeeze') {
    action = 'Covered Call';
    targetDelta = '0.25–0.30 Delta';
    suggestedDTE = '21–35 DTE';
    strikeGuidance = `Sell OTM Call above resistance ($${Math.max(bbUpper, price * 1.04).toFixed(2)}) per 100 shares`;
    stopLossPrice = price - atr * 1.6;
    targetPrice = Math.max(bbUpper, price * 1.04);
    wealthsimpleRuleNote = isRegistered
      ? `Covered Calls are CRA-approved in ${account}s and generate tax-free/tax-deferred income on 100-share Canadian/US holdings.`
      : 'Covered Calls collect elevated option premium while capping upside at the selected OTM strike.';
  } else if (!isRegistered && rsi < 38 && bbPercentB < 0.25) {
    action = 'Cash-Secured Put';
    targetDelta = '-0.25 to -0.30 Delta';
    suggestedDTE = '30 DTE';
    strikeGuidance = `Sell OTM Put near lower BB ($${bbLower.toFixed(2)}) only if willing to own 100 shares`;
    stopLossPrice = bbLower - atr;
    targetPrice = bbMiddle;
    wealthsimpleRuleNote =
      'Cash-Secured Puts require Non-Registered collateral and are NOT permitted inside TFSA or RRSP accounts under CRA rules.';
  } else {
    action = 'Wait / Hold';
    targetDelta = 'Stand aside';
    suggestedDTE = '—';
    strikeGuidance = 'Conflicting RSI/MACD signals; preserve capital until confluence > 30';
    stopLossPrice = price - atr * 1.5;
    targetPrice = price + atr * 2.0;
    wealthsimpleRuleNote = `No clean high-probability edge in ${account}. Set a Volatility Spike or MACD Cross alert to catch the next move.`;
  }

  const risk = Math.max(0.01, Math.abs(price - stopLossPrice));
  const reward = Math.max(0.01, Math.abs(targetPrice - price));
  const rr = (reward / risk).toFixed(2);

  return {
    action,
    bias,
    confidence,
    targetDelta,
    suggestedDTE,
    strikeGuidance,
    stopLossPrice,
    targetPrice,
    riskRewardRatio: `1 : ${rr}`,
    rationale,
    wealthsimpleRuleNote,
  };
}

export function runHistoricalSignalBacktest(
  candles: Candle[],
  series: IndicatorPoint[]
): BacktestStats {
  const horizon = 8;
  let bullSignals = 0;
  let bullWins = 0;
  let bullMoveSum = 0;
  let bearSignals = 0;
  let bearWins = 0;
  let bearMoveSum = 0;

  let lastBullIdx = -10;
  let lastBearIdx = -10;

  for (let i = 26; i < series.length - horizon; i++) {
    const p = series[i];
    const prev = series[i - 1];
    if (
      p.rsi === null ||
      p.macd === null ||
      p.macdSignal === null ||
      prev.macd === null ||
      prev.macdSignal === null ||
      p.bbPercentB === null
    ) {
      continue;
    }

    const bullCross = prev.macd <= prev.macdSignal && p.macd > p.macdSignal;
    const bullTrigger =
      (bullCross && p.rsi < 62) || (p.rsi < 34 && p.bbPercentB < 0.22);

    const bearCross = prev.macd >= prev.macdSignal && p.macd < p.macdSignal;
    const bearTrigger =
      (bearCross && p.rsi > 38) || (p.rsi > 68 && p.bbPercentB > 0.82);

    const futureSlice = candles.slice(i + 1, i + 1 + horizon);
    const entry = candles[i].close;
    const endClose = futureSlice[futureSlice.length - 1].close;

    if (bullTrigger && i - lastBullIdx >= 5) {
      lastBullIdx = i;
      bullSignals++;
      const maxHigh = Math.max(...futureSlice.map((c) => c.high));
      const pctMove = ((endClose - entry) / entry) * 100;
      const peakGain = ((maxHigh - entry) / entry) * 100;
      bullMoveSum += pctMove;
      if (pctMove > 0 || peakGain >= 1.2) bullWins++;
    }

    if (bearTrigger && i - lastBearIdx >= 5) {
      lastBearIdx = i;
      bearSignals++;
      const minLow = Math.min(...futureSlice.map((c) => c.low));
      const pctDrop = ((entry - endClose) / entry) * 100;
      const peakDrop = ((entry - minLow) / entry) * 100;
      bearMoveSum += pctDrop;
      if (pctDrop > 0 || peakDrop >= 1.2) bearWins++;
    }
  }

  return {
    bullishSignalsTested: bullSignals,
    bullishWinRatePct: bullSignals > 0 ? Math.round((bullWins / bullSignals) * 100) : 0,
    bullishAvgMovePct: bullSignals > 0 ? Number((bullMoveSum / bullSignals).toFixed(2)) : 0,
    bearishSignalsTested: bearSignals,
    bearishWinRatePct: bearSignals > 0 ? Math.round((bearWins / bearSignals) * 100) : 0,
    bearishAvgMovePct: bearSignals > 0 ? Number((bearMoveSum / bearSignals).toFixed(2)) : 0,
    lookbackBars: candles.length,
  };
}

