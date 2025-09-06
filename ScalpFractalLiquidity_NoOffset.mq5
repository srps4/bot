//+------------------------------------------------------------------+
//| ScalpFractalLiquidity_NoOffset.mq5 (MT5 EA, M1, no entry offset) |
//+------------------------------------------------------------------+
#property strict
#include <Trade/Trade.mqh>

CTrade trade;

//=== Inputs ===
input bool     UseMarketOnTouch     = true;   // true: market at touch; false: place limits at level
input int      TP_Ticks_Min         = 12;
input int      TP_Ticks_Max         = 20;
input int      SL_Ticks_Min         = 10;
input int      SL_Ticks_Max         = 18;
input double   TargetTP_Cash_Min    = 10.0;
input double   TargetTP_Cash_Max    = 50.0;
input double   TargetSL_Cash        = 40.0;
input int      MaxConcurrent        = 5;
// trailing
input bool     UseTrailing          = true;
input int      BE_Trigger_Ticks     = 12;
input int      BE_Buffer_Ticks      = 1;
input int      Trail_Gap_Ticks      = 10;
input int      Trail_Step_Ticks     = 2;
// risk guards
input double   DailyDD_Limit_Pct    = 5.0;
input double   OverallDD_Limit_Pct  = 10.0;
// gates
input bool     UseSpreadGate        = true;
input double   MaxSpreadPoints      = 30.0;   // points
input bool     UseSessionGate       = true;
input int      SessionStartHour     = 7;
input int      SessionEndHour       = 20;
input bool     UseRangeGate         = true;
input int      MinBarRangeTicks     = 6;
// trend bias (optional)
input bool     UseTrendBias         = false;
input int      EMALength            = 50;
input int      EMASlopeLookback     = 10;
// basket TP
input bool     EnableBasketTP       = true;
input double   BasketTP_Cash        = 150.0;

//=== State ===
int      hFractals=INVALID_HANDLE, hEMA=INVALID_HANDLE;
datetime lastBar=0;
double   tickSize=0.0, tickValue1Lot=0.0;
double   volMin=0.01, volMax=10.0, volStep=0.01;
double   initialEquity=0.0, dayStartEquity=0.0;
int      dayOfYear=-1;
bool     dayBlocked=false, hardStop=false;

// trailing store
struct TrailState { long ticket; double best; bool beArmed; };
TrailState trailMap[];

//--- helpers
int  FindTrail(long t){ for(int i=0;i<ArraySize(trailMap);++i) if(trailMap[i].ticket==t) return i; return -1; }
void EnsureTrail(long t,double entry){ int i=FindTrail(t); if(i<0){ TrailState s; s.ticket=t; s.best=entry; s.beArmed=false; int n=ArraySize(trailMap); ArrayResize(trailMap,n+1); trailMap[n]=s; } }
void RemoveTrail(long t){ int i=FindTrail(t); if(i>=0) ArrayRemove(trailMap,i,1); }
double Clamp(double v,double step){ return MathFloor(v/step)*step; }

bool SessionOk(){
  if(!UseSessionGate) return true;
  MqlDateTime dt; TimeToStruct(TimeCurrent(), dt);
  int hh=dt.hour;
  if(SessionStartHour<=SessionEndHour) return (hh>=SessionStartHour && hh<=SessionEndHour);
  return (hh>=SessionStartHour || hh<=SessionEndHour); // crosses midnight
}
bool SpreadOk(){
  if(!UseSpreadGate) return true;
  double a=SymbolInfoDouble(_Symbol,SYMBOL_ASK), b=SymbolInfoDouble(_Symbol,SYMBOL_BID);
  return ((a-b)/_Point <= MaxSpreadPoints);
}
bool RangeOk(){
  if(!UseRangeGate) return true;
  double ph=iHigh(_Symbol,PERIOD_M1,1), pl=iLow(_Symbol,PERIOD_M1,1);
  if(ph==0||pl==0||tickSize<=0.0) return false;
  int rng=(int)MathRound((ph-pl)/tickSize);
  return (rng>=MinBarRangeTicks);
}
int OpenCount(){
  int c=0;
  for(int i=0;i<PositionsTotal();++i){
    ulong ticket = PositionGetTicket(i);     // auto-selects the position
    if(ticket==0) continue;
    if((string)PositionGetString(POSITION_SYMBOL)==_Symbol) c++;
  }
  return c;
}
double FloatPnl(){
  double s=0.0;
  for(int i=0;i<PositionsTotal();++i){
    ulong ticket = PositionGetTicket(i);
    if(ticket==0) continue;
    if((string)PositionGetString(POSITION_SYMBOL)!=_Symbol) continue;
    s += PositionGetDouble(POSITION_PROFIT);
  }
  return s;
}
void CloseAll(){
  for(int i=PositionsTotal()-1; i>=0; --i){
    ulong ticket = PositionGetTicket(i);         // auto-selects the position
    if(ticket==0) continue;
    if((string)PositionGetString(POSITION_SYMBOL)!=_Symbol) continue;
    trade.PositionClose(ticket);                 // MT5 has a ticket overload
  }
}
void DeleteAllPendings(){
  for(int i=(int)OrdersTotal()-1;i>=0;--i){
    ulong ticket = OrderGetTicket(i);
    if(!OrderSelect(ticket)) continue;
    if((string)OrderGetString(ORDER_SYMBOL)!=_Symbol) continue;
    ENUM_ORDER_TYPE typ=(ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
    if(typ==ORDER_TYPE_BUY_LIMIT || typ==ORDER_TYPE_SELL_LIMIT)
      trade.OrderDelete(ticket);
  }
}

void RiskGuards(){
  double eq=AccountInfoDouble(ACCOUNT_EQUITY);
  MqlDateTime dt; TimeToStruct(TimeCurrent(), dt);
  if(dt.day_of_year!=dayOfYear){ dayOfYear=dt.day_of_year; dayStartEquity=eq; dayBlocked=false; }
  if(eq<=initialEquity - initialEquity*(OverallDD_Limit_Pct/100.0) && !hardStop){
    CloseAll(); DeleteAllPendings(); hardStop=true; Print("OVERALL DD stop");
  }
  if(eq<=dayStartEquity - dayStartEquity*(DailyDD_Limit_Pct/100.0) && !dayBlocked){
    CloseAll(); DeleteAllPendings(); dayBlocked=true; Print("DAILY DD pause");
  }
}

double PickLots(int tpTicks,int slTicks){
  if(tickValue1Lot<=0.0) return volMin;
  double tpCash = TargetTP_Cash_Min + (double)MathRand()/32767.0 * (TargetTP_Cash_Max-TargetTP_Cash_Min);
  double l1 = tpCash / (tpTicks*tickValue1Lot);
  double l2 = TargetSL_Cash / (slTicks*tickValue1Lot);
  double lots = Clamp(MathMin(l1,l2), volStep);
  lots = MathMax(lots, volMin); lots = MathMin(lots, volMax);
  return lots;
}

bool PlaceLimit(string dir,double entry,int tpTicks,int slTicks,double lots){
  int digits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
  entry = NormalizeDouble(entry, digits);
  double sl,tp;
  if(dir=="LONG"){
    sl=NormalizeDouble(entry - slTicks*tickSize, digits);
    tp=NormalizeDouble(entry + tpTicks*tickSize, digits);
    return trade.BuyLimit(lots, entry, _Symbol, sl, tp, ORDER_TIME_GTC, 0, "L@fract");
  }else{
    sl=NormalizeDouble(entry + slTicks*tickSize, digits);
    tp=NormalizeDouble(entry - tpTicks*tickSize, digits);
    return trade.SellLimit(lots, entry, _Symbol, sl, tp, ORDER_TIME_GTC, 0, "S@fract");
  }
}
bool PlaceMarket(string dir,int tpTicks,int slTicks,double lots,double refPrice){
  int digits = (int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
  double sl,tp; bool ok=false;
  if(dir=="LONG"){
    sl=NormalizeDouble(refPrice - slTicks*tickSize, digits);
    tp=NormalizeDouble(refPrice + tpTicks*tickSize, digits);
    ok = trade.Buy(lots, _Symbol, 0.0, sl, tp, "L MKT");
  }else{
    sl=NormalizeDouble(refPrice + slTicks*tickSize, digits);
    tp=NormalizeDouble(refPrice - tpTicks*tickSize, digits);
    ok = trade.Sell(lots, _Symbol, 0.0, sl, tp, "S MKT");
  }
  return ok;
}

//+------------------------------------------------------------------+
//| EA lifecycle                                                     |
//+------------------------------------------------------------------+
int OnInit(){
  hFractals = iFractals(_Symbol,PERIOD_M1);
  if(hFractals==INVALID_HANDLE){ Print("iFractals failed"); return(INIT_FAILED); }
  if(UseTrendBias){
    hEMA = iMA(_Symbol,PERIOD_M1,EMALength,0,MODE_EMA,PRICE_CLOSE);
    if(hEMA==INVALID_HANDLE){ Print("iMA failed"); return(INIT_FAILED); }
  }
  tickSize      = SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
  tickValue1Lot = SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE);
  volMin        = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN);
  volMax        = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MAX);
  volStep       = SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);

  initialEquity = AccountInfoDouble(ACCOUNT_EQUITY);
  dayStartEquity= initialEquity;
  MqlDateTime dt; TimeToStruct(TimeCurrent(), dt);
  dayOfYear     = dt.day_of_year;
  lastBar       = 0;
  ArrayResize(trailMap, 0); 
  return(INIT_SUCCEEDED);
}

void OnTick(){
  if(hardStop) return;
  RiskGuards();
  if(dayBlocked || !SessionOk() || !SpreadOk() || !RangeOk()) return;

  ManagePositions(); // every tick

  // new bar setups
  datetime bt=iTime(_Symbol,PERIOD_M1,0);
  if(bt==0 || bt==lastBar) return;
  lastBar=bt;

  // dynamic arrays (avoid "unsupported array type")
  double upper[]; ArrayResize(upper,3);
  double lower[]; ArrayResize(lower,3);
  if(CopyBuffer(hFractals,0,0,3,upper)<=0) return; // upper (sell)
  if(CopyBuffer(hFractals,1,0,3,lower)<=0) return; // lower (buy)

  bool upperOK = (upper[2]!=0.0), lowerOK = (lower[2]!=0.0);
  double upPx=upper[2], loPx=lower[2];

  // optional trend bias
  bool allowShort=true, allowLong=true;
  if(UseTrendBias){
    double emaBuf[]; ArrayResize(emaBuf, EMASlopeLookback+1);
    if(CopyBuffer(hEMA,0,0,EMASlopeLookback+1,emaBuf)>0){
      double slope = emaBuf[0] - emaBuf[EMASlopeLookback];
      allowShort=(slope<0); allowLong=(slope>0);
    }
  }

  int cap = MathMax(0, MaxConcurrent - OpenCount());
  if(cap<=0) return;

  MqlTick t; SymbolInfoTick(_Symbol,t);

  // SELL at upper fractal (no offset)
  if(upperOK && allowShort){
    int tpTicks = (TP_Ticks_Min==TP_Ticks_Max? TP_Ticks_Min : (TP_Ticks_Min + (int)MathRand()%(TP_Ticks_Max-TP_Ticks_Min+1)));
    int slTicks = (SL_Ticks_Min==SL_Ticks_Max? SL_Ticks_Min : (SL_Ticks_Min + (int)MathRand()%(SL_Ticks_Max-SL_Ticks_Min+1)));
    double lots = PickLots(tpTicks, slTicks);
    if(UseMarketOnTouch){
      if(t.bid >= upPx) PlaceMarket("SHORT", tpTicks, slTicks, lots, t.bid);
    }else{
      PlaceLimit("SHORT", upPx, tpTicks, slTicks, lots);
    }
  }

  // BUY at lower fractal (no offset)
  if(lowerOK && allowLong){
    int tpTicks = (TP_Ticks_Min==TP_Ticks_Max? TP_Ticks_Min : (TP_Ticks_Min + (int)MathRand()%(TP_Ticks_Max-TP_Ticks_Min+1)));
    int slTicks = (SL_Ticks_Min==SL_Ticks_Max? SL_Ticks_Min : (SL_Ticks_Min + (int)MathRand()%(SL_Ticks_Max-SL_Ticks_Min+1)));
    double lots = PickLots(tpTicks, slTicks);
    if(UseMarketOnTouch){
      if(t.ask <= loPx) PlaceMarket("LONG", tpTicks, slTicks, lots, t.ask);
    }else{
      PlaceLimit("LONG", loPx, tpTicks, slTicks, lots);
    }
  }
}

void ManagePositions(){
  if(EnableBasketTP && FloatPnl()>=BasketTP_Cash){ CloseAll(); DeleteAllPendings(); return; }
  if(!UseTrailing) return;

  MqlTick tk; SymbolInfoTick(_Symbol, tk);

  for(int i=0; i<PositionsTotal(); ++i){
    ulong ticket = PositionGetTicket(i);               // auto-selects
    if(ticket==0) continue;
    if((string)PositionGetString(POSITION_SYMBOL)!=_Symbol) continue;

    long   type  = (long)PositionGetInteger(POSITION_TYPE);
    double entry =       PositionGetDouble(POSITION_PRICE_OPEN);
    double sl    =       PositionGetDouble(POSITION_SL);
    double tp    =       PositionGetDouble(POSITION_TP);

    EnsureTrail((long)ticket, entry);
    int idx = FindTrail((long)ticket);
    if(idx<0) continue;

    if(type==POSITION_TYPE_BUY){
      trailMap[idx].best = MathMax(trailMap[idx].best, tk.ask);
      if(!trailMap[idx].beArmed && tk.bid >= entry + BE_Trigger_Ticks*tickSize){
        trailMap[idx].beArmed = true; double newSL = entry + BE_Buffer_Ticks*tickSize;
        if(newSL>sl) trade.PositionModify(_Symbol,newSL,tp);
      }
      if(trailMap[idx].beArmed){
        double target = trailMap[idx].best - Trail_Gap_Ticks*tickSize;
        int steps = (int)MathFloor((target - sl)/(Trail_Step_Ticks*tickSize));
        if(steps>0) trade.PositionModify(_Symbol, sl + steps*Trail_Step_Ticks*tickSize, tp);
      }
    }else{ // SELL
      trailMap[idx].best = MathMin(trailMap[idx].best, tk.bid);
      if(!trailMap[idx].beArmed && tk.ask <= entry - BE_Trigger_Ticks*tickSize){
        trailMap[idx].beArmed = true; double newSL = entry - BE_Buffer_Ticks*tickSize;
        if(newSL<sl || sl==0.0) trade.PositionModify(_Symbol,newSL,tp);
      }
      if(trailMap[idx].beArmed){
        double target = trailMap[idx].best + Trail_Gap_Ticks*tickSize;
        int steps = (int)MathFloor((sl - target)/(Trail_Step_Ticks*tickSize));
        if(steps>0) trade.PositionModify(_Symbol, sl - steps*Trail_Step_Ticks*tickSize, tp);
      }
    }
  }
}

void OnTradeTransaction(const MqlTradeTransaction& trans,const MqlTradeRequest& req,const MqlTradeResult& res){
  if(trans.type==TRADE_TRANSACTION_DEAL_DELETE) RemoveTrail((long)trans.deal);
}
