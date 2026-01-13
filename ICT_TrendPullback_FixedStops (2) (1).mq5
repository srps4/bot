//+------------------------------------------------------------------+
//|                  ICT_TrendPullback_FixedStops.mq5                 |
//|  15m trend + 15m FVG pullback, execute on chart TF (ex: M1)       |
//|  Fixed SL/TP points (default 30/60) + broker stop-level handling  |
//|  This version improves FVG detection by scanning the most recent  |
//|  bars first, making it more likely to find valid gaps near the    |
//|  current price.  It also fixes position selection for all MT5     |
//|  builds and keeps the trading window configurable.               |
//+------------------------------------------------------------------+
#property strict
#property version   "2.02"

//-------------------- Inputs --------------------
input ENUM_TIMEFRAMES InpTrendTF        = PERIOD_M15;   // trend timeframe
input ENUM_TIMEFRAMES InpFVGTF          = PERIOD_M15;   // FVG timeframe (keep same as trend)
input int             InpTrendBars      = 200;          // bars to scan for trend swings
input int             InpFVGLookbackBars= 60;           // how far back to search for freshest FVG

input bool            InpUseTradingWindow = true;
input string          InpTradeStartHHMM  = "20:00";    // server time (24h)
input string          InpTradeEndHHMM    = "16:15";    // server time (24h)

input bool            InpOnlyOneTrade    = true;

input bool            InpUseFixedStops   = true;
input int             InpFixedSL_Pts     = 30;          // fixed SL in points
input int             InpFixedTP_Pts     = 60;          // fixed TP in points

input double          InpRiskPct         = 1.0;         // risk percent of equity per trade
input int             InpSlBufferPoints  = 2;           // buffer for structure-based stops

input int             InpMaxSpreadPts    = 200;         // skip trades if spread exceeds this (points)
input long            InpMagic           = 123456;      // magic number

//-------------------- Globals --------------------
double point_val; // point value for symbol
int    digits;    // digits for symbol

// parsed trade window minutes (0–1439)
int g_tradeStartMin = 0;
int g_tradeEndMin   = 0;

// --- logging helpers ---
// Track the most recent trade's timestamps and identifiers.  Use the proper
// MQL5 datetime type instead of the erroneous 'ddatetime' token.
datetime g_lastEntryTime = 0;
ulong    g_lastTicket    = 0;

// Persistent storage for position ID and associated trade details.  These
// variables are populated when a new position is opened and used when the
// position closes to build the trade quality log.
ulong    g_lastPosId      = 0;
double   g_lastEntryPrice = 0;
double   g_lastSL         = 0;
double   g_lastTP         = 0;
bool     g_lastIsLong     = false;


//-------------------- Utility functions --------------------

// Parse HH:MM string into hour and minute; returns true on success
bool ParseHHMM(const string s,int &hh,int &mm)
  {
   string parts[];
   int n = StringSplit(s,':',parts);
   if(n != 2) return(false);
   long h = StringToInteger(parts[0]);
   long m = StringToInteger(parts[1]);
   if(h<0 || h>23 || m<0 || m>59) return(false);
   hh = (int)h;
   mm = (int)m;
   return(true);
  }

// Determine whether current server time falls within the configured trading window
bool IsWithinWindow()
  {
   if(!InpUseTradingWindow) return(true);
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(),dt);
   int nowMin = dt.hour*60 + dt.min;
   // If start <= end, simple range check
   if(g_tradeStartMin <= g_tradeEndMin)
      return(nowMin >= g_tradeStartMin && nowMin <= g_tradeEndMin);
   // If window crosses midnight or spans to afternoon, allow if after start or before end
   return(nowMin >= g_tradeStartMin || nowMin <= g_tradeEndMin);
  }

// Check if there is an existing open position for this symbol with our magic number
bool HasOpenPosition()
  {
   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      ulong ticket = (ulong)PositionGetTicket(i);
      if(ticket==0) continue;
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol) continue;
      long magic = (long)PositionGetInteger(POSITION_MAGIC);
      if(magic==InpMagic) return(true);
     }
   return(false);
  }

// Get current spread in broker points
double GetSpreadPts()
  {
   double ask = SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   if(ask<=0 || bid<=0) return(0.0);
   return((ask - bid)/point_val);
  }

// Broker minimum stop level and freeze level combined (in points)
double GetMinStopDistancePoints()
  {
   long stops  = SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL);
   long freeze = SymbolInfoInteger(_Symbol,SYMBOL_TRADE_FREEZE_LEVEL);
   long need = stops;
   if(freeze > need) need = freeze;
   if(need < 0) need = 0;
   return((double)need);
  }

// Ensure stop-loss and take-profit meet broker distance requirements
bool NormalizeStops(double entry,double &sl,double &tp,bool isBuy)
  {
   double minDistPts = GetMinStopDistancePoints();
   double minDist    = minDistPts * point_val;
   if(minDistPts > 0)
     {
      if(isBuy)
        {
         if(entry - sl < minDist) sl = entry - minDist;
         if(tp    - entry < minDist) tp = entry + minDist;
        }
      else
        {
         if(sl - entry < minDist) sl = entry + minDist;
         if(entry - tp < minDist) tp = entry - minDist;
        }
     }
   sl = NormalizeDouble(sl,digits);
   tp = NormalizeDouble(tp,digits);
   if(isBuy)
      return(sl > 0 && sl < entry && tp > entry);
   else
      return(tp > 0 && tp < entry && sl > entry);
  }

// Calculate lot size based on fixed stop distance (in points) and risk percent
double CalculateLotFromSLPts(double sl_points)
  {
   if(sl_points <= 0) return(0.0);
   double equity    = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * InpRiskPct / 100.0;
   double tv = SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE);
   double ts = SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   if(tv<=0 || ts<=0) return(0.0);
   double money_per_point = tv / (ts/point_val);
   if(money_per_point <= 0) return(0.0);
   double lots = riskMoney / (sl_points * money_per_point);
   double minLot = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MAX);
   double step   = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
   lots = MathMax(minLot,MathMin(maxLot,lots));
   // Hard cap: avoid unrealistic large volumes in testing; cap to 1 lot by default
   if(lots > 1.0) lots = 1.0;
   lots = MathFloor(lots/step)*step;
   if(lots < minLot) lots = minLot;
   return(lots);
  }

// Determine the best supported filling mode for this symbol
ENUM_ORDER_TYPE_FILLING GetBestFilling()
  {
   // Determine the allowed filling modes for this symbol. SYMBOL_FILLING_MODE
   // returns a bit mask of allowed fill policies. According to the
   // documentation: SYMBOL_FILLING_FOK = 1, SYMBOL_FILLING_IOC = 2 and
   // absence of these flags implies return is allowed. Do bitwise checks
   // against these flags to decide which ORDER_FILLING_* constant to use.
   long fm = SymbolInfoInteger(_Symbol,SYMBOL_FILLING_MODE);
   // These symbolic constants are defined by the terminal, but if they are
   // unavailable for some reason, define fallback values (1 and 2) below.
#ifndef SYMBOL_FILLING_FOK
#define SYMBOL_FILLING_FOK 1
#endif
#ifndef SYMBOL_FILLING_IOC
#define SYMBOL_FILLING_IOC 2
#endif
   // Prefer FOK if allowed
   if((fm & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK)
      return(ORDER_FILLING_FOK);
   // Else prefer IOC if allowed
   if((fm & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC)
      return(ORDER_FILLING_IOC);
   // Otherwise default to RETURN; this is always allowed except under market execution
   return(ORDER_FILLING_RETURN);
  }

//-------------------- Market structure and FVG logic --------------------

// Identify swing highs in an array of highs; i is index into array; len is lookback
bool IsSwingHigh(const double &h[],int i,int len)
  {
   for(int k=1;k<=len;k++)
     {
      if(h[i] <= h[i+k]) return(false);
      if(h[i] <= h[i-k]) return(false);
     }
   return(true);
  }

// Identify swing lows in an array of lows
bool IsSwingLow(const double &l[],int i,int len)
  {
   for(int k=1;k<=len;k++)
     {
      if(l[i] >= l[i+k]) return(false);
      if(l[i] >= l[i-k]) return(false);
     }
   return(true);
  }

// Determine trend direction on the 15m chart using most recent two swing highs and lows
// Returns +1 for uptrend, -1 for downtrend, 0 for sideways
int GetTrendDir_M15()
  {
   int n = Bars(_Symbol,InpTrendTF);
   if(n < 50) return(0);
   int take = MathMin(n, InpTrendBars);
   static double hi[], lo[];
   ArrayResize(hi,take);
   ArrayResize(lo,take);
   ArraySetAsSeries(hi,true);
   ArraySetAsSeries(lo,true);
   if(CopyHigh(_Symbol,InpTrendTF,0,take,hi) <= 0) return(0);
   if(CopyLow (_Symbol,InpTrendTF,0,take,lo) <= 0) return(0);
   int len=2;
   double sh1=0, sh2=0, sl1=0, sl2=0;
   bool h1=false,h2=false,l1=false,l2=false;
   // scan from near current bar (index 5) forward to find two latest swings
   for(int i=5;i<take-5;i++)
     {
      if(IsSwingHigh(hi,i,len))
        {
         if(!h1) { sh1=hi[i]; h1=true; }
         else if(!h2) { sh2=sh1; sh1=hi[i]; h2=true; }
        }
      if(IsSwingLow(lo,i,len))
        {
         if(!l1) { sl1=lo[i]; l1=true; }
         else if(!l2) { sl2=sl1; sl1=lo[i]; l2=true; }
        }
      if(h2 && l2) break;
     }
   if(!(h2 && l2)) return(0);
   // Determine directional bias. Prefer strong structure first (both highs and lows rising/falling).
   bool strongUp   = (sh1 > sh2 && sl1 > sl2);
   bool strongDown = (sh1 < sh2 && sl1 < sl2);
   if(strongUp)    return(+1);
   if(strongDown)  return(-1);
   // If only the highs or only the lows are rising, still treat as uptrend.
   if(sh1 > sh2 || sl1 > sl2) return(+1);
   // If only the highs or only the lows are falling, treat as downtrend.
   if(sh1 < sh2 || sl1 < sl2) return(-1);
   // Otherwise sideways
   return(0);
  }

// Find the most recent fair-value gap on 15m in the direction of trend
// For bullish trend: look for low[i] > high[i+2]
// For bearish trend: look for high[i] < low[i+2]
// Because arrays are series=true, index 0 is most recent bar, so we scan from 0 to take-3
bool FindFreshFVG_15m(int trendDir,double &lowB,double &highB)
  {
   lowB=0; highB=0;
   if(trendDir==0) return(false);
   int n = Bars(_Symbol,InpFVGTF);
   if(n < 10) return(false);
   int take = MathMin(n, InpFVGLookbackBars + 3);
   static double h[], l[];
   ArrayResize(h,take);
   ArrayResize(l,take);
   ArraySetAsSeries(h,true);
   ArraySetAsSeries(l,true);
   if(CopyHigh(_Symbol,InpFVGTF,0,take,h) <= 0) return(false);
   if(CopyLow (_Symbol,InpFVGTF,0,take,l) <= 0) return(false);
   // iterate from most recent bar; ensure we have at least 3 candles (i, i+1, i+2)
   // Two checks are performed for each triple to capture both the classical middle-candle FVG and the variation using the most recent candle.
   for(int i=0; i <= take-3; i++)
     {
      int idxLeft  = i+2; // oldest candle in 3-candle formation
      int idxMid   = i+1; // middle candle (displacement candle)
      int idxRight = i;   // most recent candle

      if(trendDir > 0)
        {
         // bullish FVG definition 1: low of middle candle > high of left candle
         if(l[idxMid] > h[idxLeft])
           {
            lowB  = h[idxLeft];
            highB = l[idxMid];
            if(lowB < highB) return(true);
           }
         // bullish FVG definition 2: low of current candle > high of left candle
         if(l[idxRight] > h[idxLeft])
           {
            lowB  = h[idxLeft];
            highB = l[idxRight];
            if(lowB < highB) return(true);
           }
        }
      else
        {
         // bearish FVG definition 1: high of middle candle < low of left candle
         if(h[idxMid] < l[idxLeft])
           {
            lowB  = h[idxMid];
            highB = l[idxLeft];
            if(lowB < highB) return(true);
           }
         // bearish FVG definition 2: high of current candle < low of left candle
         if(h[idxRight] < l[idxLeft])
           {
            lowB  = h[idxRight];
            highB = l[idxLeft];
            if(lowB < highB) return(true);
           }
        }
     }
   return(false);
  }

//-------------------- Order placement --------------------

bool PlaceMarketOrder(bool isBuy,double sl,double tp,double lot)
  {
   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);
   double ask = SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol,SYMBOL_BID);
   if(ask<=0 || bid<=0) return(false);
   double price = isBuy ? ask : bid;
   double sl2=sl, tp2=tp;
   if(!NormalizeStops(price,sl2,tp2,isBuy))
     {
      Print("Skip: stops invalid after normalization.");
      return(false);
     }
   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = _Symbol;
   req.magic        = InpMagic;
   req.volume       = lot;
   req.type         = isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price        = price;
   req.sl           = sl2;
   req.tp           = tp2;
   req.deviation    = 50;
   req.type_filling = GetBestFilling();
   req.type_time    = ORDER_TIME_GTC;
   req.comment      = isBuy ? "ICT_Long" : "ICT_Short";
   bool ok = OrderSend(req,res);
   if(!ok)
     {
      Print((isBuy?"Buy":"Sell")," failed: ",GetLastError()," retcode=",res.retcode);
      return(false);
     }

   // --------- NEW: remember last opened POSITION ticket for logging ---------
   g_lastTicket    = 0;
   g_lastEntryTime = 0;
   g_lastPosId      = 0;
g_lastEntryPrice = 0;
g_lastSL         = 0;
g_lastTP         = 0;
g_lastIsLong     = isBuy;

   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong pos_ticket = PositionGetTicket(i);
      if(!PositionSelectByTicket(pos_ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         PositionGetInteger(POSITION_MAGIC)  == InpMagic)
        {
         g_lastTicket     = pos_ticket;
g_lastEntryTime  = (datetime)PositionGetInteger(POSITION_TIME);
g_lastPosId      = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
g_lastEntryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
g_lastSL         = PositionGetDouble(POSITION_SL);
g_lastTP         = PositionGetDouble(POSITION_TP);
g_lastIsLong     = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
break;

        }
     }
   //----------------------------------------------------------------

   return(true);
  }


//+--================================================================+
//| ENHANCED LOGGING SYSTEM: GOOD vs BAD TRADE IDENTIFICATION       |
//| Add this to your EA to classify trade quality                   |
//+================================================================+

//+------------------------------------------------------------------+
//| TRADE QUALITY CLASSIFICATION SYSTEM                              |
//| Identifies: GOOD (keep), MEDIOCRE (refine), BAD (avoid)        |
//+------------------------------------------------------------------+

struct TradeQualityRecord {
    datetime entryTime;
    datetime exitTime;
    double entryPrice;
    double exitPrice;
    double slPrice;
    double tpPrice;
    double pnl;
    double slDist;    // SL distance in points
    double tpDist;    // TP distance in points
    int barsHeld;
    string quality;   // "GOOD", "MEDIOCRE", "BAD", "DISASTER"
    string exitReason;
    double mae;       // Maximum Adverse Excursion (points)
    double mfe;       // Maximum Favorable Excursion (points)
    bool isLong;
    string timeSession;
    int hourOfEntry;
    double maePct;    // MAE as % of SL distance
    double mfePct;    // MFE as % of TP distance
};

TradeQualityRecord tradeQualities[];
int qualityCount = 0;

// Classify trade quality based on multiple factors
string ClassifyTradeQuality(double mae, double mfe, double slDist, double tpDist,
                           int barsHeld, string exitReason, bool isLong)
{
    double maePct = slDist > 0 ? (mae / slDist) * 100 : 0;
    double mfePct = tpDist > 0 ? (mfe / tpDist) * 100 : 0;
    
    // ===== GOOD TRADE =====
    // Hit TP, low MAE, reasonable holding time
    if(exitReason == "TP")
    {
        // Perfect: TP hit, MAE < 30%, bars 5-240
        if(maePct < 30 && barsHeld >= 5 && barsHeld <= 240)
            return "GOOD";
        
        // Good but with caveat: TP but very fast scalp
        if(barsHeld < 5 && maePct < 50)
            return "GOOD";  // Fast wins are still good if we risk little
    }
    
    // ===== MEDIOCRE TRADE =====
    // Winning but with warning signs
    if(exitReason == "TP")
    {
        // TP hit but high MAE (got bailed out by reversal)
        if(maePct >= 50 && maePct < 80)
            return "MEDIOCRE";
        
        // TP hit but took extremely long (luck)
        if(barsHeld > 480)  // >8 hours on M1
            return "MEDIOCRE";
    }
    
    // ===== BAD TRADE =====
    // Losing trades with clear bad entry pattern
    if(exitReason == "SL")
    {
        // Hit SL very quickly (wrong direction immediately)
        if(barsHeld <= 10 && maePct > 70)
            return "BAD";
        
        // Hit SL with massive MAE (entered against prevailing direction)
        if(maePct > 80)
            return "BAD";
        
        // SL hit during bad time (will refine with session data)
        return "BAD";
    }
    
    // ===== DISASTER TRADE =====
    // Multiple bad factors = sign of revenge trading/forced entry
    if(exitReason == "SL" && barsHeld < 5 && maePct > 90)
        return "DISASTER";
    
    return "UNKNOWN";
}

// Get trading session name
string GetTradingSession()
{
    MqlDateTime dt;
    TimeToStruct(TimeCurrent(), dt);
    int hour = dt.hour;
    
    if(hour >= 8 && hour < 12)
        return "London";
    else if(hour >= 13 && hour < 22)
        return "NY";
    else
        return "Asia";
}

// Calculate MAE and MFE for a closed position (needs bar history)
// Calculate MAE and MFE for a closed position (needs bar history)
void CalculateMAEMFE(datetime entryTime, double entryPrice, 
                     datetime exitTime, double exitPrice,
                     bool isLong, double &mae, double &mfe)
{
   int entryBar = iBarShift(_Symbol, PERIOD_CURRENT, entryTime, false);
   int exitBar  = iBarShift(_Symbol, PERIOD_CURRENT, exitTime,  false);

   mae = 0;
   mfe = 0;

   if(entryBar < 0 || exitBar < 0) return;

   double worstPrice = entryPrice;
   double bestPrice  = entryPrice;

   for(int i = exitBar; i <= entryBar; i++)
     {
      double high = iHigh(_Symbol, PERIOD_CURRENT, i);
      double low  = iLow(_Symbol, PERIOD_CURRENT, i);
      if(high > bestPrice)  bestPrice  = high;
      if(low  < worstPrice) worstPrice = low;
     }

   // Convert to points
   if(isLong)
     {
      mae = (entryPrice - worstPrice) / _Point;   // adverse move in points
      mfe = (bestPrice  - entryPrice) / _Point;   // favorable move in points
     }
   else
     {
      mae = (worstPrice - entryPrice) / _Point;
      mfe = (entryPrice - bestPrice)  / _Point;
     }

   if(mae < 0) mae = 0;
   if(mfe < 0) mfe = 0;
}


// Log trade quality with comprehensive metrics
void LogTradeQuality(datetime entryTime, datetime exitTime,
                     double entryPrice, double exitPrice,
                     double slPrice, double tpPrice,
                     double pnl, int barsHeld,
                     string exitReason, bool isLong)
{
    if(ArraySize(tradeQualities) <= qualityCount)
        ArrayResize(tradeQualities, qualityCount + 100);

  double slDist = 0, tpDist = 0;
if(isLong)
  {
   slDist = (entryPrice - slPrice) / _Point;
   tpDist = (tpPrice   - entryPrice) / _Point;
  }
else
  {
   slDist = (slPrice   - entryPrice) / _Point;
   tpDist = (entryPrice - tpPrice)  / _Point;
  }

    // Calculate MAE/MFE
    double mae = 0, mfe = 0;
    CalculateMAEMFE(entryTime, entryPrice, exitTime, exitPrice, isLong, mae, mfe);
    
    double maePct = slDist > 0 ? (mae / slDist) * 100 : 0;
    double mfePct = tpDist > 0 ? (mfe / tpDist) * 100 : 0;
    
    // Classify quality
    string quality = ClassifyTradeQuality(mae, mfe, slDist, tpDist, barsHeld, exitReason, isLong);
    
    // Store in array
    tradeQualities[qualityCount].entryTime = entryTime;
    tradeQualities[qualityCount].exitTime = exitTime;
    tradeQualities[qualityCount].entryPrice = entryPrice;
    tradeQualities[qualityCount].exitPrice = exitPrice;
    tradeQualities[qualityCount].slPrice = slPrice;
    tradeQualities[qualityCount].tpPrice = tpPrice;
    tradeQualities[qualityCount].pnl = pnl;
    tradeQualities[qualityCount].barsHeld = barsHeld;
    tradeQualities[qualityCount].quality = quality;
    tradeQualities[qualityCount].exitReason = exitReason;
    tradeQualities[qualityCount].mae = mae;
    tradeQualities[qualityCount].mfe = mfe;
    tradeQualities[qualityCount].isLong = isLong;
    tradeQualities[qualityCount].timeSession = GetTradingSession();
    tradeQualities[qualityCount].maePct = maePct;
    tradeQualities[qualityCount].mfePct = mfePct;
    
    MqlDateTime dt;
    TimeToStruct(entryTime, dt);
    tradeQualities[qualityCount].hourOfEntry = dt.hour;
    
    qualityCount++;
    
    // Write to CSV immediately for analysis
    WriteTradeQualityLog(quality, entryTime, entryPrice, exitPrice, pnl, 
                         barsHeld, exitReason, maePct, mfePct, 
                         GetTradingSession(), isLong);
}

// Write to CSV file for external analysis
void WriteTradeQualityLog(string quality, datetime entryTime, double entryPrice,
                         double exitPrice, double pnl, int barsHeld,
                         string exitReason, double maePct, double mfePct, 
                         string session, bool isLong)
{
    Print("WriteTradeQualityLog CALLED ", quality, " pnl=", pnl);   // DEBUG
    
    int handle = FileOpen("Trade_Quality_Log.csv", FILE_READ | FILE_WRITE | FILE_CSV | FILE_COMMON);
    if(handle == INVALID_HANDLE)
    {
        // If file doesn't exist, create with header
        handle = FileOpen("Trade_Quality_Log.csv", FILE_WRITE | FILE_CSV | FILE_COMMON);
        if(handle != INVALID_HANDLE)
        {
            FileWriteString(handle, "Quality,EntryTime,EntryPrice,ExitPrice,PnL,BarsHeld,ExitReason,MAE%,MFE%,Session,Direction\n");
        }
    }
    
    if(handle == INVALID_HANDLE)
{
   Print("FileOpen failed: ", GetLastError());
   return;
}

    
    FileSeek(handle, 0, SEEK_END);
    
    string direction = isLong ? "LONG" : "SHORT";
    string line = quality + "," + 
                  TimeToString(entryTime) + "," +
                  DoubleToString(entryPrice, 2) + "," +
                  DoubleToString(exitPrice, 2) + "," +
                  DoubleToString(pnl, 2) + "," +
                  IntegerToString(barsHeld) + "," +
                  exitReason + "," +
                  DoubleToString(maePct, 1) + "%," +
                  DoubleToString(mfePct, 1) + "%," +
                  session + "," +
                  direction;
    
    FileWriteString(handle, line + "\n");
    FileClose(handle);
}

// Generate comprehensive quality report
void GenerateQualityReport()
{
    if(qualityCount == 0) return;
    
    int good = 0, mediocre = 0, bad = 0, disaster = 0;
    double goodPnL = 0, mediocPnL = 0, badPnL = 0, disasterPnL = 0;
    int goodWins = 0, mediocWins = 0;
    
    // Categorize all trades
    for(int i = 0; i < qualityCount; i++)
    {
        if(tradeQualities[i].quality == "GOOD")
        {
            good++;
            goodPnL += tradeQualities[i].pnl;
            if(tradeQualities[i].pnl > 0) goodWins++;
        }
        else if(tradeQualities[i].quality == "MEDIOCRE")
        {
            mediocre++;
            mediocPnL += tradeQualities[i].pnl;
            if(tradeQualities[i].pnl > 0) mediocWins++;
        }
        else if(tradeQualities[i].quality == "BAD")
        {
            bad++;
            badPnL += tradeQualities[i].pnl;
        }
        else if(tradeQualities[i].quality == "DISASTER")
        {
            disaster++;
            disasterPnL += tradeQualities[i].pnl;
        }
    }
    
    Print("\n========== TRADE QUALITY ANALYSIS ==========");
    Print("Total Trades: ", qualityCount);
    
    Print("\n[GOOD TRADES] - Keep & Scale");
    Print("  Count: ", good, " (", DoubleToString((double)good/qualityCount*100, 1), "%)");
    if(good > 0)
    {
        Print("  Total PnL: $", DoubleToString(goodPnL, 2));
        Print("  Avg Per Trade: $", DoubleToString(goodPnL / good, 2));
        Print("  Win Rate: ", DoubleToString((double)goodWins/good*100, 1), "%");
    }
    
    Print("\n[MEDIOCRE TRADES] - Refine Entry Rules");
    Print("  Count: ", mediocre, " (", DoubleToString((double)mediocre/qualityCount*100, 1), "%)");
    if(mediocre > 0)
    {
        Print("  Total PnL: $", DoubleToString(mediocPnL, 2));
        Print("  Avg Per Trade: $", DoubleToString(mediocPnL / mediocre, 2));
        Print("  Win Rate: ", DoubleToString((double)mediocWins/mediocre*100, 1), "%");
    }
    
    Print("\n[BAD TRADES] - Add Filters to Avoid");
    Print("  Count: ", bad, " (", DoubleToString((double)bad/qualityCount*100, 1), "%)");
    if(bad > 0)
    {
        Print("  Total PnL: $", DoubleToString(badPnL, 2));
        Print("  Avg Per Trade: $", DoubleToString(badPnL / bad, 2));
    }
    
    Print("\n[DISASTER TRADES] - Critical - Add Guardrails");
    Print("  Count: ", disaster, " (", DoubleToString((double)disaster/qualityCount*100, 1), "%)");
    if(disaster > 0)
    {
        Print("  Total PnL: $", DoubleToString(disasterPnL, 2));
        Print("  Avg Per Trade: $", DoubleToString(disasterPnL / disaster, 2));
    }
    
    Print("\n========== RECOMMENDATION ==========");
    double badPct = (double)(bad + disaster) / qualityCount * 100;
    if(badPct > 50)
    {
        Print("⚠️  WARNING: ", DoubleToString(badPct, 1), "% of trades are BAD/DISASTER");
        Print("   ACTION: Implement entry confirmation filters immediately");
    }
    
    if(disaster > 0 && disaster > qualityCount * 0.1)
    {
        Print("🚨 CRITICAL: ", DoubleToString((double)disaster/qualityCount*100, 1), "% disaster trades detected");
        Print("   ACTION: Add guardrails + session-based filtering");
    }
    
    // Identify worst session
    int londonBad = 0, nyBad = 0, asiaBad = 0;
    for(int i = 0; i < qualityCount; i++)
    {
        if(tradeQualities[i].quality == "BAD" || tradeQualities[i].quality == "DISASTER")
        {
            if(tradeQualities[i].timeSession == "London") londonBad++;
            else if(tradeQualities[i].timeSession == "NY") nyBad++;
            else if(tradeQualities[i].timeSession == "Asia") asiaBad++;
        }
    }
    
    Print("\n========== SESSION ANALYSIS ==========");
    Print("London Bad/Disaster: ", londonBad);
    Print("NY Bad/Disaster: ", nyBad);
    Print("Asia Bad/Disaster: ", asiaBad);
    
    if(londonBad > nyBad && londonBad > asiaBad)
        Print("⚠️  WORST SESSION: London - Consider skipping first 30 mins");
    else if(nyBad > londonBad && nyBad > asiaBad)
        Print("⚠️  WORST SESSION: NY - Consider skipping first 30 mins");
    
    Print("\n✅ CSV Log saved to: Trade_Quality_Log.csv");
    Print("   Open in Excel and pivot by Quality/Session for detailed analysis");
}

// Example: Call this when position closes in your OnTick()
/*
// After detecting position is closed:
LogTradeQuality(
    positionOpenTime,      // Entry time
    TimeCurrent(),         // Exit time
    positionOpenPrice,     // Entry price
    currentBid,            // Exit price (use bid for sells, ask for buys)
    positionStopLoss,      // SL level
    positionTakeProfit,    // TP level
    positionProfit,        // P&L
    barsHeld,              // Number of M1 bars held
    positionExitReason,    // "TP" or "SL"
    isBuyPosition          // Direction
);

// At end of backtest, generate report:
GenerateQualityReport();
*/

//-------------------- Initialization --------------------

int OnInit()
  {
   point_val = _Point;
   digits    = _Digits;
   int sh,sm,eh,em;
   if(!ParseHHMM(InpTradeStartHHMM,sh,sm) || !ParseHHMM(InpTradeEndHHMM,eh,em))
     {
      Print("Invalid trade window format (expect HH:MM)");
      return(INIT_FAILED);
     }
   g_tradeStartMin = sh*60 + sm;
   g_tradeEndMin   = eh*60 + em;
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   // Print summary in the tester log when backtest ends
   GenerateQualityReport();
  }

//-------------------- Tick handling --------------------
void OnTick()
{
   static datetime last_bar = 0;
   datetime t = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(t == last_bar) return;
   last_bar = t;
   // Only trade at start of new bar

   // --------- Detect closure of the last trade and log it (FIXED) ---------
   // Requires these globals to exist and be populated at entry time:
   // g_lastTicket, g_lastPosId, g_lastEntryPrice, g_lastSL, g_lastTP, g_lastIsLong, point_val
   if(g_lastTicket != 0)
   {
      // If position with that ticket is no longer open, it closed
      if(!PositionSelectByTicket(g_lastTicket))
      {
         // prevent double-logging
         ulong posTicket = g_lastTicket;
         g_lastTicket = 0;

         ulong posId = g_lastPosId;
         g_lastPosId = 0;

         if(posId == 0)
         {
            Print("Trade log: missing stored POSITION_IDENTIFIER (g_lastPosId==0).");
         }
         else
         {
            // Load history for THIS position only
            if(!HistorySelectByPosition((long)posId))
            {
               Print("Trade log: HistorySelectByPosition failed: ", GetLastError());
            }
            else
            {
               int total = (int)HistoryDealsTotal();
               if(total <= 0)
               {
                  Print("Trade log: no deals found for posId=", posId);
               }
               else
               {
                  ulong entryDeal = 0, exitDeal = 0;

                  // Find entry + exit deals for this position and symbol
                  for(int i = 0; i < total; i++)
                  {
                     ulong d = HistoryDealGetTicket(i);
                     if(d == 0) continue;

                     if(HistoryDealGetString(d, DEAL_SYMBOL) != _Symbol) continue;
                     if((ulong)HistoryDealGetInteger(d, DEAL_POSITION_ID) != posId) continue;

                     long entryFlag = (long)HistoryDealGetInteger(d, DEAL_ENTRY);
                     if(entryFlag == DEAL_ENTRY_IN)
                        entryDeal = d;
                     else if(entryFlag == DEAL_ENTRY_OUT)
                        exitDeal = d;
                  }

                  if(entryDeal == 0 || exitDeal == 0)
                  {
                     Print("Trade log: could not locate entry/exit deal. posId=", posId,
                           " entryDeal=", entryDeal, " exitDeal=", exitDeal);
                  }
                  else
                  {
                     datetime entryTime  = (datetime)HistoryDealGetInteger(entryDeal, DEAL_TIME);
                     double   entryPrice = HistoryDealGetDouble(entryDeal, DEAL_PRICE);

                     datetime exitTime   = (datetime)HistoryDealGetInteger(exitDeal, DEAL_TIME);
                     double   exitPrice  = HistoryDealGetDouble(exitDeal, DEAL_PRICE);
                     double   pnl        = HistoryDealGetDouble(exitDeal, DEAL_PROFIT);

                     // bars held on CURRENT chart timeframe (best effort)
                     int entryBar = iBarShift(_Symbol, PERIOD_CURRENT, entryTime, false);
                     int exitBar  = iBarShift(_Symbol, PERIOD_CURRENT, exitTime,  false);
                     int barsHeld = (entryBar >= 0 && exitBar >= 0) ? (entryBar - exitBar) : 0;
                     if(barsHeld < 0) barsHeld = 0;

                     // Determine exit reason (best effort using stored SL/TP)
                     string exitReason = "MANUAL";
                     double tol = 2.0 * point_val; // tolerance
                     if(g_lastTP > 0 && MathAbs(exitPrice - g_lastTP) <= tol) exitReason = "TP";
                     else if(g_lastSL > 0 && MathAbs(exitPrice - g_lastSL) <= tol) exitReason = "SL";

                     LogTradeQuality(
                        entryTime, exitTime,
                        entryPrice, exitPrice,
                        g_lastSL, g_lastTP,
                        pnl, barsHeld,
                        exitReason,
                        g_lastIsLong
                     );
                  }
               }
            }
         }
      }
   }
   // --------------------------------------------------------------------

   if(InpOnlyOneTrade && HasOpenPosition()) return;
   if(!IsWithinWindow()) return;

   // spread filter
   double spr = GetSpreadPts();
   if(InpMaxSpreadPts > 0 && spr > InpMaxSpreadPts) return;

   // determine trend direction
   int trendDir = GetTrendDir_M15();
   if(trendDir == 0) return;

   // find freshest FVG
   double fvgLow=0, fvgHigh=0;
   if(!FindFreshFVG_15m(trendDir, fvgLow, fvgHigh)) return;

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(ask<=0 || bid<=0) return;

   // require price inside FVG zone
   bool inZone = false;
   if(trendDir > 0)
      inZone = (bid <= fvgHigh && bid >= fvgLow);
   else
      inZone = (ask >= fvgLow && ask <= fvgHigh);
   if(!inZone) return;

   // compute stops
   double sl=0, tp=0;
   double sl_pts=0;

   if(InpUseFixedStops)
   {
      double minStopPts = GetMinStopDistancePoints();
      double useSLPts   = MathMax((double)InpFixedSL_Pts, minStopPts);
      double rrRatio    = (InpFixedTP_Pts > 0 ? (double)InpFixedTP_Pts / (double)InpFixedSL_Pts : 2.0);
      double useTPPts   = useSLPts * rrRatio;

      sl_pts = useSLPts;

      if(trendDir > 0)
      {
         sl = ask - useSLPts * point_val;
         tp = ask + useTPPts * point_val;
      }
      else
      {
         sl = bid + useSLPts * point_val;
         tp = bid - useTPPts * point_val;
      }
   }
   else
   {
      if(trendDir > 0)
      {
         sl = fvgLow - InpSlBufferPoints * point_val;
         sl_pts = (ask - sl)/point_val;
         tp = ask + sl_pts * 2.0 * point_val;
      }
      else
      {
         sl = fvgHigh + InpSlBufferPoints * point_val;
         sl_pts = (sl - bid)/point_val;
         tp = bid - sl_pts * 2.0 * point_val;
      }
   }

   // ensure positive stop distance
   if(InpUseFixedStops)
      sl_pts = (double)InpFixedSL_Pts;
   if(sl_pts <= 0) return;

   // compute lot size
   double lot = CalculateLotFromSLPts(sl_pts);
   if(lot <= 0) return;

   // place order
   if(trendDir > 0)
      PlaceMarketOrder(true, sl, tp, lot);
   else
      PlaceMarketOrder(false, sl, tp, lot);
}
