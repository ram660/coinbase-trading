export type AssetClass = 'US Equity' | 'Canadian TSX' | 'Crypto';

export type SectorTheme = 'Chips' | 'AI' | 'Quantum' | 'Index' | 'Crypto';

export type WealthsimpleAccount = 'TFSA' | 'RRSP' | 'Non-Registered' | 'Crypto';

export type Timeframe = '15m' | '1h' | '4h' | '1d';

export interface Candle {
  time: number; // Unix timestamp in ms
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface TickerMeta {
  symbol: string;
  name: string;
  assetClass: AssetClass;
  sector?: SectorTheme;
  currency: 'USD' | 'CAD';
  optionsEligible: boolean;
  wealthsimpleNote: string;
  proxySymbol?: string;
}

export interface IndicatorPoint {
  time: number;
  close: number;
  volume: number;
  ema9: number | null;
  ema21: number | null;
  ema50: number | null;
  ema200: number | null;
  bbUpper: number | null;
  bbMiddle: number | null;
  bbLower: number | null;
  bbPercentB: number | null;
  bbWidth: number | null;
  rsi: number | null;
  macd: number | null;
  macdSignal: number | null;
  macdHist: number | null;
  atr: number | null;
  atrPercent: number | null;
  vwap: number | null;
  rvol: number | null;
  supertrend: number | null;
  supertrendDir: 1 | -1 | null;
  xTrendFast: number | null;
  diPlus: number | null;
  diMinus: number | null;
  adx: number | null;
}

export type VolatilityRegime = 'Squeeze' | 'Normal' | 'Expanding' | 'Spike';

export type SignalBias = 'Strong Bullish' | 'Bullish' | 'Neutral' | 'Bearish' | 'Strong Bearish';

export type OptionStrategyAction =
  | 'Buy Call'
  | 'Buy Put'
  | 'Covered Call'
  | 'Cash-Secured Put'
  | 'Bull Call Spread'
  | 'Bear Put Spread'
  | 'Long Straddle'
  | 'Spot Accumulate'
  | 'Scale Out / Hedge'
  | 'Wait / Hold';

export interface StrategyRecommendation {
  action: OptionStrategyAction;
  bias: SignalBias;
  confidence: number; // 0-100 computed strictly from indicator confluence
  targetDelta: string;
  suggestedDTE: string;
  strikeGuidance: string;
  stopLossPrice: number;
  targetPrice: number;
  riskRewardRatio: string;
  rationale: string[];
  wealthsimpleRuleNote: string;
}

export interface TechnicalSummary {
  price: number;
  change24h: number;
  changePercent24h: number;
  high24h: number;
  low24h: number;
  volume24h: number;
  rsi: number;
  rsiState: 'Oversold' | 'Neutral-Low' | 'Neutral' | 'Neutral-High' | 'Overbought';
  rsiDivergence: 'Bullish Divergence' | 'Bearish Divergence' | 'None';
  macd: number;
  macdSignal: number;
  macdHist: number;
  macdEvent: 'Bullish Cross' | 'Bearish Cross' | 'Bullish Momentum' | 'Bearish Momentum' | 'Flat';
  bbUpper: number;
  bbMiddle: number;
  bbLower: number;
  bbPercentB: number;
  bbWidth: number;
  bbWidthChangePct: number;
  atr: number;
  atrPercent: number;
  atrSpikeRatio: number;
  rvol: number;
  vwap: number;
  emaTrend: 'Bullish Stack' | 'Bearish Stack' | 'Mixed / Transition';
  volatilityRegime: VolatilityRegime;
  supertrend: number;
  supertrendDir: 'Bullish' | 'Bearish';
  supertrendEvent: 'Buy Flip' | 'Sell Flip' | 'Bullish Trend' | 'Bearish Trend';
  diPlus: number;
  diMinus: number;
  adx: number;
  trendStrength: 'Strong Trend' | 'Developing' | 'Choppy / Weak';
  supportLevel: number;
  resistanceLevel: number;
  confluenceScore: number; // -100 to +100
  strategy: StrategyRecommendation;
}

export interface MarketSnapshot {
  meta: TickerMeta;
  candles: Candle[];
  series: IndicatorPoint[];
  summary: TechnicalSummary;
  source: 'live' | 'cached' | 'simulated';
  updatedAt: number;
}

export type AlertConditionType =
  | 'VOLATILITY_SPIKE'
  | 'BB_UPPER_BREAK'
  | 'BB_LOWER_BREAK'
  | 'BB_SQUEEZE'
  | 'RSI_OVERBOUGHT'
  | 'RSI_OVERSOLD'
  | 'MACD_BULLISH_CROSS'
  | 'MACD_BEARISH_CROSS'
  | 'PRICE_ABOVE'
  | 'PRICE_BELOW';

export interface AlertRule {
  id: string;
  symbol: string; // specific symbol or 'ALL'
  condition: AlertConditionType;
  threshold: number;
  enabled: boolean;
  label: string;
  createdAt: number;
  lastTriggeredAt?: number;
}

export interface TriggeredAlert {
  id: string;
  ruleId: string;
  symbol: string;
  condition: AlertConditionType;
  title: string;
  message: string;
  price: number;
  currency: 'USD' | 'CAD';
  rsi: number;
  bbWidth: number;
  atrSpikeRatio: number;
  suggestedAction: OptionStrategyAction;
  severity: 'critical' | 'warning' | 'info';
  timestamp: number;
  read: boolean;
}

export type SentimentLabel = 'Bullish' | 'Bearish' | 'Neutral';

export interface NewsArticle {
  id: string;
  title: string;
  url: string;
  source: string;
  publishedAt: number;
  summary: string;
  sentiment: SentimentLabel;
  sentimentScore: number; // -100 to +100
}

export interface SocialPost {
  id: string;
  author: string;
  handle: string;
  platform: 'Mastodon' | 'Community';
  content: string;
  url: string;
  publishedAt: number;
  sentiment: SentimentLabel;
  sentimentScore: number;
}

export interface SentimentFeedResponse {
  symbol: string;
  query: string;
  overallScore: number; // -100 to +100
  overallLabel: 'Strong Bullish' | 'Bullish' | 'Mixed / Neutral' | 'Bearish' | 'Strong Bearish';
  bullishPct: number;
  bearishPct: number;
  neutralPct: number;
  articles: NewsArticle[];
  posts: SocialPost[];
  updatedAt: number;
}

export type AIMode = 'flash-lite' | 'quant';

export interface GroundedResearchBrief {
  symbol: string;
  briefMarkdown: string;
  sources: Array<{ title: string; uri: string }>;
  poweredBy: 'gemini-search' | 'gemini-flash-lite' | 'feed-summary';
  cached?: boolean;
  generatedAt: number;
}

export interface BacktestStats {
  bullishSignalsTested: number;
  bullishWinRatePct: number;
  bullishAvgMovePct: number;
  bearishSignalsTested: number;
  bearishWinRatePct: number;
  bearishAvgMovePct: number;
  lookbackBars: number;
}

export interface TimeframeAlignmentRow {
  timeframe: Timeframe;
  price: number;
  rsi: number;
  macdBias: 'Bullish' | 'Bearish' | 'Flat';
  bbPercentB: number;
  emaTrend: 'Bullish' | 'Bearish' | 'Mixed';
  regime: VolatilityRegime;
}

export interface AIOrderPrediction {
  symbol: string;
  account: WealthsimpleAccount;
  recommendedOrder:
    | 'Buy Call'
    | 'Buy Put'
    | 'Sell Covered Call'
    | 'Sell Cash-Secured Put'
    | 'Spot Long'
    | 'No Trade / Wait';
  convictionTier: 'High Conviction' | 'Moderate' | 'Low / Skip';
  estimatedWinProbability: number;
  mtfAlignment: TimeframeAlignmentRow[];
  backtest: BacktestStats;
  orderTicket: {
    action: string;
    optionType: string;
    strikeZone: string;
    expiryDTE: string;
    orderType: string;
    limitGuidance: string;
    maxPositionSizePct: string;
    stopLossTrigger: string;
    takeProfitTarget: string;
  };
  bullCase: string[];
  bearRisks: string[];
  invalidationCondition: string;
  poweredBy: 'gemini-3.1-flash-lite' | 'gemini-4-hydrogen' | 'quant-engine';
  cached?: boolean;
  generatedAt: number;
}

export type TradeSide =
  | 'Buy Call'
  | 'Buy Put'
  | 'Sell Covered Call'
  | 'Sell Cash-Secured Put'
  | 'Buy Shares / Spot'
  | 'Sell Shares / Spot';

export interface PortfolioTrade {
  id: string;
  symbol: string;
  account: WealthsimpleAccount;
  side: TradeSide;
  strike?: number;
  expiryDate?: string; // YYYY-MM-DD
  contractsOrQty: number;
  entryPrice: number; // per share / option contract premium per share
  currentMarkPrice?: number; // optional manual mark update for open options
  exitPrice?: number;
  currency: 'USD' | 'CAD';
  fxRateAtEntry: number;
  fxFeeCad: number;
  status: 'Open' | 'Closed';
  openedAt: number;
  closedAt?: number;
  notes: string;
  setupSnapshot: {
    rsi: number;
    macdEvent: string;
    bbPercentB: number;
    volatilityRegime: string;
  };
}

export interface TradeDraft {
  symbol: string;
  side: TradeSide;
  strike?: number;
  expiryDate?: string;
  contractsOrQty: number;
  entryPrice: number;
  currency: 'USD' | 'CAD';
  notes: string;
  setupSnapshot: {
    rsi: number;
    macdEvent: string;
    bbPercentB: number;
    volatilityRegime: string;
  };
}



