'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

type Position = { instrument_key:string; symbol?:string; quantity:number; average_price:number; last_price:number; market_value:number; realized_pnl:number; unrealized_pnl:number };
type Order = { id:string; instrument_key:string; side:'BUY'|'SELL'; quantity:number; filled_quantity:number; average_filled_price?:number; status:string; strategy_id?:string };
type Fill = { id:string; instrument_key:string; symbol:string; side:'BUY'|'SELL'; quantity:number; price:number; fees:number; gross_value:number; timestamp:string };
type EntrySignal = { reason:string;window_start_price:number;observed_price:number;window_change_pct:number;entry_threshold_pct?:number;previous_price:number;sample_change_pct:number;session_change_pct:number;recent_15m_change_pct:number;momentum_score:number;recent_volume?:number;baseline_volume?:number;relative_volume?:number;minimum_relative_volume?:number;volume_signal?:string;nifty_price?:number;nifty_session_change_pct?:number;nifty_recent_15m_change_pct?:number;market_alignment:string;entry_mode?:string;entry_check?:EntryCheck;structural_stop?:number;entry_threshold_source?:string;stock_noise_pct?:number;notional?:number;cost_floor_pct?:number;initial_stop?:number;initial_stop_source?:string;risk_pct?:number;lock_at_pct?:number;ride_at_pct?:number };
type EntryCheck = {triggered:boolean;reason:string;window:number[];first_price?:number;last_price?:number;rise_pct?:number;trigger_price?:number;threshold_pct?:number;threshold_source?:string;noise_pct?:number};
type ExitState = {phase:string;reason?:string|null;entry_price:number;last_price:number;peak_price:number;stop_price:number;stop_source:string;stop_distance_pct:number;unrealized_pct:number;net_of_cost_pct:number;cost_floor_pct:number;lock_at_pct?:number;ride_at_pct?:number;structural_stop?:number;max_risk_stop?:number;breakeven_stop?:number;trail_low?:number;trail_window?:number;volume_state?:string;breaches?:number;confirmation_samples?:number;seconds_held?:number;time_stop_seconds?:number;drawdown_from_peak_pct?:number};
type MonitoringObservation = {timestamp:string;symbol:string;instrument_key:string;price:number;previous_price?:number;sample_change_pct:number;window_start_price:number;window_change_pct:number;session_change_pct?:number;recent_15m_change_pct?:number;momentum_score?:number;range_position_pct?:number;recent_volume?:number;baseline_volume?:number;relative_volume?:number;volume_signal?:string;nifty_price?:number;nifty_sample_change_pct?:number;nifty_window_change_pct?:number;nifty_session_change_pct?:number;nifty_recent_15m_change_pct?:number;decision:string;held?:boolean;entry_check?:EntryCheck;exit?:ExitState|null;entry_signal?:EntrySignal};
type RunEvent = { timestamp:string; type:string; symbol?:string; reason?:string; observed_price?:number; entry_price?:number; entry_signal?:EntrySignal; exit_state?:ExitState|null };
type CostModel = {notional:number;buy_fees:number;sell_fees:number;round_trip_fees:number;slippage:number;total_cost:number;breakeven_pct:number;slippage_bps:number;product:string};
type DecisionCount = {decision:string;count:number;share_pct:number};
type ReplayMetrics = {net_pnl:number;gross_pnl:number;fees:number;round_trips:number;wins:number;losses:number;win_rate_pct:number;average_win?:number;average_loss?:number;best_trade?:number;worst_trade?:number;average_hold_seconds?:number;exit_reasons:Array<{reason:string;count:number}>};
type ReplayTrade = {symbol:string;entry_price:number;entry_time:string;quantity:number;cost_floor_pct:number;structural_stop:number;initial_stop:number;initial_stop_source:string;exit_reason:string;exit_price:number;net_pnl:number;gross_pnl:number;fees:number;seconds_held?:number;samples:number;exit_state?:ExitState};
type ReplayReport = {id:string;mode:string;source_run_id:string;baseline:ReplayMetrics;candidate:ReplayMetrics;delta:{net_pnl:number;round_trips:number;fees:number};trades:ReplayTrade[];decision_counts:Array<{decision:string;count:number}>;period:{observations:number;symbols:number}};
type MomentumRun = {
  id:string; status:string; started_at?:string; finished_at?:string; initial_equity?:number;
  scan_count:number; poll_count:number; session_pnl:number; fills:Fill[]; events:RunEvent[]; errors:string[];
  open_positions:Array<{symbol:string;quantity:number;entry_price:number;last_price:number;stop_price?:number;phase?:string;stop_source?:string;unrealized_pct?:number;cost_floor_pct?:number;structural_stop?:number}>;
  candidates:Array<{symbol:string;momentum_score:number;recent_15m_change_pct:number}>;
  config:{account_id?:string;duration_seconds:number;max_positions:number;allocation_per_position:number;entry_momentum_pct:number;minimum_relative_volume?:number;reversal_pct:number;hard_stop_pct:number;universe_name?:string;universe_size?:number;entry_mode?:string;exit_mode?:string;entry_bars?:number;require_nifty_confirmation?:boolean;entry_cost_multiple?:number;entry_noise_multiple?:number;entry_timeframe_seconds?:number;survive_stop_multiple?:number;lock_multiple?:number;ride_multiple?:number;min_gap_multiple?:number;trail_window?:number;fast_trail_window?:number;volume_decay_ratio?:number;confirmation_samples?:number;time_stop_seconds?:number};
  metrics?:{gross_pnl:number;fees:number;net_pnl:number};
  cost_model?:CostModel;
  decision_counts?:DecisionCount[];
  monitoring?:MonitoringObservation[];
  monitoring_count?:number;
  portfolio:{initial_cash:number;cash:number;equity:number;fees_paid:number;realized_pnl:number;positions:Position[]};
};
type Summary = {
  portfolio:{initial_cash:number;cash:number;equity:number;fees_paid:number;realized_pnl:number;unrealized_pnl:number;positions:Position[]};
  orders:Order[]; fills:Fill[]; stream:{state:string;market_statuses:Record<string,string>;quotes_received:number;last_error?:string};
};
type BatchResult = {started:Array<{label:string;run:MomentumRun}>;failed:Array<{label:string;error:string}>};
type ScheduleSlot = {index:number;start_ist:string;end_ist:string;account_id:string;label:string;duration_seconds:number;max_positions:number;entry_timeframe_seconds:number;status:string;runner_id?:string|null;detail?:string|null};
type Coverage = {covered_pct:number;largest_gap_seconds:number;concurrent_peak:number;session_open_ist?:string;session_close_ist?:string};
type SchedulePlan = {session_date:string;account_prefix:string;slots:ScheduleSlot[];coverage:Coverage};
type TokenStatus = {has_token:boolean;likely_valid:boolean;token_type?:'analytics'|'oauth'|null;updated_at?:string|null;expires_at_ist?:string|null;oauth_configured:boolean};
type ScheduleStatus = {enabled:boolean;running:boolean;session_date:string;is_trading_day:boolean;entry_timeframes:number[];max_positions:number;reentry_cooldown_seconds:number;plan:SchedulePlan|null;report_ready:boolean;last_actions:Array<Record<string,unknown>>};
type ReportRun = {run_id:string;account_id:string;config_key:string;status:string;started_at?:string;finished_at?:string;duration_seconds?:number;max_positions?:number;entry_timeframe_seconds?:number;net_pnl:number;gross_pnl:number;fees:number;round_trips:number;wins:number;win_rate_pct:number;symbols_traded:string[];exit_reasons:Record<string,number>;monitoring_count:number;errors:string[]};
type ConfigRollup = {key:string|number;runs:number;net_pnl:number;round_trips:number;wins:number;win_rate_pct:number};
type DailyReport = {session_date:string;generated_at:string;runs:ReportRun[];totals:{runs:number;completed:number;runs_that_traded:number;net_pnl:number;fees:number;round_trips:number;wins:number;win_rate_pct:number;observations:number;symbols_observed:number};best_run:ReportRun|null;worst_run:ReportRun|null;by_duration:ConfigRollup[];by_positions:ConfigRollup[];by_timeframe:ConfigRollup[];decision_totals:Array<{decision:string;count:number;share_pct:number}>;coverage:Coverage|null;plan_slots:number};
type Outcome = {symbol:string;quantity:number;buyPrice:number;sellPrice:number;grossPnl:number;fees:number;netPnl:number;exitReason:string};

// In production the dashboard and API live on different hosts (Vercel + Railway),
// so the API origin is injected at build time. Locally it falls back to the dev proxy.
const configuredApiBase = (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_API_BASE) || '';
const apiBase = configuredApiBase.replace(/\/$/, '') || (typeof window !== 'undefined' && window.location.port !== '8000' ? '/api' : '');
const money = new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:2});
const number = new Intl.NumberFormat('en-IN',{maximumFractionDigits:2});
const signedMoney = (value:number) => `${value >= 0 ? '+' : '−'}${money.format(Math.abs(value))}`;
const signedPct = (value:number) => `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
const universeName = (run:MomentumRun) => run.config.universe_name || 'NIFTY 50';

async function request<T=unknown>(path:string, options?:RequestInit):Promise<T> {
  const response = await fetch(apiBase + path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as {detail?:string};
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

function runOutcomes(run:MomentumRun):Outcome[] {
  const grouped = new Map<string,Fill[]>();
  run.fills.forEach(fill=>grouped.set(fill.symbol,[...(grouped.get(fill.symbol)||[]),fill]));
  return [...grouped.entries()].map(([symbol,fills])=>{
    const buys=fills.filter(fill=>fill.side==='BUY'), sells=fills.filter(fill=>fill.side==='SELL');
    const buyQty=buys.reduce((sum,fill)=>sum+fill.quantity,0), sellQty=sells.reduce((sum,fill)=>sum+fill.quantity,0);
    const buyValue=buys.reduce((sum,fill)=>sum+fill.gross_value,0), sellValue=sells.reduce((sum,fill)=>sum+fill.gross_value,0);
    const fees=fills.reduce((sum,fill)=>sum+fill.fees,0);
    const exit=run.events.find(event=>event.type==='EXIT_FILLED'&&event.symbol===symbol);
    return {symbol,quantity:Math.max(buyQty,sellQty),buyPrice:buyQty?buyValue/buyQty:0,sellPrice:sellQty?sellValue/sellQty:0,grossPnl:sellValue-buyValue,fees,netPnl:sellValue-buyValue-fees,exitReason:(exit?.reason||'OPEN').replaceAll('_',' ')};
  });
}

export default function Home() {
  const [summary,setSummary]=useState<Summary|null>(null), [runs,setRuns]=useState<MomentumRun[]>([]);
  const [tab,setTab]=useState<'home'|'runs'|'reports'>('home'), [selectedRunId,setSelectedRunId]=useState<string|null>(null);
  const [detail,setDetail]=useState<MomentumRun|null>(null), [detailError,setDetailError]=useState('');
  const [busy,setBusy]=useState(''), [notice,setNotice]=useState('Connecting to the paper engine…');
  const [duration,setDuration]=useState(600), [allocation,setAllocation]=useState(100000), [maxPositions,setMaxPositions]=useState(2);
  const [exitMode,setExitMode]=useState<'RATCHET'|'REVERSAL'>('RATCHET'), [trailWindow,setTrailWindow]=useState(24), [requireNifty,setRequireNifty]=useState(false);
  const [entryTimeframe,setEntryTimeframe]=useState(0);
  const [cost,setCost]=useState<CostModel|null>(null);
  const refresh=useCallback(async()=>{
    try { const [nextSummary,nextRuns]=await Promise.all([request<Summary>('/dashboard/summary?account_id=momentum'),request<MomentumRun[]>('/momentum-runners')]); setSummary(nextSummary);setRuns(nextRuns);setNotice('Paper engine connected'); }
    catch(error){setNotice(error instanceof Error?error.message:'Dashboard connection failed');}
  },[]);
  useEffect(()=>{const first=window.setTimeout(refresh,0),timer=window.setInterval(refresh,4000);return()=>{window.clearTimeout(first);window.clearInterval(timer);};},[refresh]);
  useEffect(()=>{if(allocation<=0)return;let live=true;const timer=window.setTimeout(()=>{request<CostModel>(`/cost-model?notional=${allocation}`).then(result=>{if(live)setCost(result)}).catch(()=>{if(live)setCost(null)})},250);return()=>{live=false;window.clearTimeout(timer);};},[allocation]);
  const activeRuns=useMemo(()=>runs.filter(run=>['RUNNING','STOPPING'].includes(run.status)),[runs]);
  const activeRun=activeRuns[0], latestRun=runs[0];
  const selectedRun=detail&&detail.id===selectedRunId?detail:null;
  // A stale detail object is filtered by the id guard above, so the effect
  // never needs to clear state synchronously - it only fetches.
  useEffect(()=>{
    if(!selectedRunId)return;
    let live=true;
    const load=()=>request<MomentumRun>(`/momentum-runners/${selectedRunId}`)
      .then(result=>{if(live){setDetail(result);setDetailError('');}})
      .catch(problem=>{if(live)setDetailError(problem instanceof Error?problem.message:'Run could not be loaded');});
    load();
    // A live run keeps growing, so refresh its detail while it is still going.
    const timer=window.setInterval(()=>{if(runs.find(r=>r.id===selectedRunId&&['RUNNING','STOPPING'].includes(r.status)))load()},5000);
    return()=>{live=false;window.clearInterval(timer);};
  },[selectedRunId,runs]);
  const marketStatus=summary?.stream.market_statuses.NSE_EQ||'NOT CONNECTED', initialCash=summary?.portfolio.initial_cash||100000, totalPnl=(summary?.portfolio.equity||initialCash)-initialCash;
  const startRun=async()=>{setBusy('start');try{const run=await request<MomentumRun>('/momentum-runners',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_id:'momentum',duration_seconds:duration,max_positions:maxPositions,allocation_per_position:allocation,candidate_limit:10,minimum_score:0.15,minimum_relative_volume:1.2,entry_momentum_pct:0.10,reversal_pct:0.10,hard_stop_pct:0.35,entry_mode:'THREE_BAR',exit_mode:exitMode,entry_bars:3,require_nifty_confirmation:requireNifty,entry_timeframe_seconds:entryTimeframe,trail_window:trailWindow,fast_trail_window:Math.min(6,trailWindow)})});setNotice('New momentum run started');setRuns(current=>[run,...current]);await refresh();}catch(error){setNotice(error instanceof Error?error.message:'Run could not be started');}finally{setBusy('');}};
  const stopRun=async()=>{if(!activeRun)return;setBusy('stop');try{await request(`/momentum-runners/${activeRun.id}/stop`,{method:'POST'});setNotice('Stopping run and closing paper positions…');await refresh();}catch(error){setNotice(error instanceof Error?error.message:'Run could not be stopped');}finally{setBusy('');}};
  const openRun=(id:string)=>{setSelectedRunId(id);setTab('runs');window.scrollTo({top:0,behavior:'smooth'});};
  return <main>
    <header className="topbar"><button className="brand" onClick={()=>{setTab('home');setSelectedRunId(null)}} aria-label="Jupiter home"><span className="brand-mark">J</span><span><strong>Jupiter</strong><small>Momentum paper lab</small></span></button><nav className="tabs" aria-label="Primary navigation"><button className={tab==='home'?'active':''} onClick={()=>{setTab('home');setSelectedRunId(null)}}>Home</button><button className={tab==='runs'?'active':''} onClick={()=>{setTab('runs');setSelectedRunId(null)}}>Runs <span>{runs.length}</span></button><button className={tab==='reports'?'active':''} onClick={()=>{setTab('reports');setSelectedRunId(null)}}>Reports</button></nav><div className="market-pill" data-open={marketStatus==='NORMAL_OPEN'}><span/>NSE · {marketStatus.replaceAll('_',' ')}</div></header>
    {tab==='home'?<HomeView summary={summary} runs={runs} latestRun={latestRun} activeRun={activeRun} activeRuns={activeRuns} initialCash={initialCash} totalPnl={totalPnl} duration={duration} setDuration={setDuration} allocation={allocation} setAllocation={setAllocation} maxPositions={maxPositions} setMaxPositions={setMaxPositions} exitMode={exitMode} setExitMode={setExitMode} trailWindow={trailWindow} setTrailWindow={setTrailWindow} requireNifty={requireNifty} setRequireNifty={setRequireNifty} entryTimeframe={entryTimeframe} setEntryTimeframe={setEntryTimeframe} cost={cost} busy={busy} startRun={startRun} stopRun={stopRun} openRun={openRun} onLaunched={refresh}/>:selectedRunId?(selectedRun?<RunDetail run={selectedRun} back={()=>setSelectedRunId(null)}/>:<section className="run-detail"><button className="back-button" onClick={()=>setSelectedRunId(null)}>← All runs</button><Empty text={detailError||'Loading the recorded trace…'}/></section>):tab==='reports'?<ReportsView openRun={openRun}/>:<RunsView runs={runs} openRun={openRun}/>} 
    <footer><span>{notice}</span><span>Private local research · Paper execution only</span></footer>
  </main>;
}

function HomeView({summary,runs,latestRun,activeRun,activeRuns,initialCash,totalPnl,duration,setDuration,allocation,setAllocation,maxPositions,setMaxPositions,exitMode,setExitMode,trailWindow,setTrailWindow,requireNifty,setRequireNifty,entryTimeframe,setEntryTimeframe,cost,busy,startRun,stopRun,openRun,onLaunched}:{summary:Summary|null;runs:MomentumRun[];latestRun?:MomentumRun;activeRun?:MomentumRun;activeRuns:MomentumRun[];initialCash:number;totalPnl:number;duration:number;setDuration:(value:number)=>void;allocation:number;setAllocation:(value:number)=>void;maxPositions:number;setMaxPositions:(value:number)=>void;exitMode:'RATCHET'|'REVERSAL';setExitMode:(value:'RATCHET'|'REVERSAL')=>void;trailWindow:number;setTrailWindow:(value:number)=>void;requireNifty:boolean;setRequireNifty:(value:boolean)=>void;entryTimeframe:number;setEntryTimeframe:(value:number)=>void;cost:CostModel|null;busy:string;startRun:()=>void;stopRun:()=>void;openRun:(id:string)=>void;onLaunched:()=>void;}) {
  const recentFills=latestRun?.fills.slice(-6).reverse()||[];
  return <><section className="hero home-hero"><div><p className="eyebrow">NIFTY 100 momentum research</p><h1>Track the move.<br/><em>Study the turn.</em></h1><p className="hero-copy">A focused paper account that surveys continuously, enters on three rising prints only when the move is larger than what the round trip costs, then manages the position with a stop that only ever moves up.</p></div><section className="new-run-card" aria-label="Start a new run"><div className="card-heading"><div><p className="eyebrow">New run</p><h2>Set the guardrails</h2></div><span className="paper-chip">PAPER</span></div><label>Duration<select value={duration} onChange={event=>setDuration(Number(event.target.value))} disabled={!!activeRun}><option value={300}>5 minutes</option><option value={600}>10 minutes</option><option value={900}>15 minutes</option><option value={1800}>30 minutes</option><option value={3600}>60 minutes</option></select></label><div className="form-pair"><label>Per position<input type="number" min="5000" max="200000" step="5000" value={allocation} onChange={event=>setAllocation(Number(event.target.value))} disabled={!!activeRun}/></label><label>Max positions<select value={maxPositions} onChange={event=>setMaxPositions(Number(event.target.value))} disabled={!!activeRun}><option value={1}>1</option><option value={2}>2</option><option value={3}>3</option><option value={4}>4</option></select></label></div><div className="form-pair"><label>Exit rule<select value={exitMode} onChange={event=>setExitMode(event.target.value as 'RATCHET'|'REVERSAL')} disabled={!!activeRun}><option value="RATCHET">Ratcheting stop</option><option value="REVERSAL">Fixed reversal</option></select></label><label>Trail window<input type="number" min="4" max="120" step="2" value={trailWindow} onChange={event=>setTrailWindow(Number(event.target.value))} disabled={!!activeRun||exitMode!=='RATCHET'}/></label></div><div className="form-pair"><label>NIFTY confirmation<select value={requireNifty?'on':'off'} onChange={event=>setRequireNifty(event.target.value==='on')} disabled={!!activeRun}><option value="off">Context only — does not gate</option><option value="on">Required for entry</option></select></label><label>Entry timeframe<select value={entryTimeframe} onChange={event=>setEntryTimeframe(Number(event.target.value))} disabled={!!activeRun}><option value={0}>5-second ticks</option><option value={60}>1-minute bars</option><option value={180}>3-minute bars</option><option value={300}>5-minute bars</option></select></label></div>{entryTimeframe>0&&<div className="cost-hint"><span>Resampled entry</span><b>{entryTimeframe/60}-minute bars</b><small>The three-bar check waits for {entryTimeframe/60*3} minutes of closed bars before it can fire, and seeds the stop from each bar&rsquo;s real intrabar low rather than a five-second sample.</small></div>}{cost&&<div className={`cost-hint ${cost.breakeven_pct>0.2?'tight':''}`}><span>Breakeven at this size</span><b>{cost.breakeven_pct.toFixed(3)}%</b><small>{money.format(cost.total_cost)} of fees and slippage per round trip. {cost.breakeven_pct>0.2?'Flat brokerage dominates here — a larger position lowers this sharply.':'Costs are amortised well at this size.'}</small></div>}<div className="rule-line"><span>Entry</span><b>3-BAR{entryTimeframe>0?` · ${entryTimeframe/60}M`:' · 5S'}</b><span>Relative volume</span><b>≥1.20×</b><span>NIFTY short-term</span><b>{requireNifty?'MUST BE POSITIVE':'CONTEXT ONLY'}</b><span>Exit</span><b>{exitMode==='RATCHET'?'RATCHET':'−0.10% REVERSAL'}</b></div>{activeRun?<button className="stop-run" onClick={stopRun} disabled={!!busy}>{busy==='stop'?'Closing positions…':'Stop current run'}</button>:<button className="start-run" onClick={startRun} disabled={!!busy||allocation<=0}>{busy==='start'?'Starting…':'Start new run'}<span>→</span></button>}</section></section>
  <section className="equity-banner"><div><p className="eyebrow">Total equity</p><strong>{money.format(summary?.portfolio.equity||initialCash)}</strong><span className={totalPnl>=0?'positive':'negative'}>{signedMoney(totalPnl)} since reset</span></div><div className="equity-meta"><div><small>Starting capital</small><b>{money.format(initialCash)}</b></div><div><small>Available cash</small><b>{money.format(summary?.portfolio.cash||initialCash)}</b></div><div><small>Execution costs</small><b>{money.format(summary?.portfolio.fees_paid||0)}</b></div><div><small>Completed runs</small><b>{runs.filter(run=>run.status==='COMPLETED').length}</b></div></div></section>
  {activeRuns.length?<div className="active-stack">{activeRuns.map(run=><ActiveRun key={run.id} run={run} openRun={openRun}/>)}</div>:<LatestRun run={latestRun} openRun={openRun}/>} 
  <BatchLauncher duration={duration} allocation={allocation} maxPositions={maxPositions} disabled={false} onLaunched={onLaunched}/>
  <section className="home-grid"><article className="panel process-panel"><div className="panel-head"><div><p className="eyebrow">Loop logic</p><h2>What happens during a run</h2></div></div><ol className="process-list"><li><span>01</span><div><strong>Survey NIFTY 100</strong><p>Rank session and 15-minute momentum once a minute, keeping only names trading above 1.2× their usual volume.</p></div></li><li><span>02</span><div><strong>Wait for three rising prints</strong><p>Poll every five seconds and enter when the third price clears the first. The window low becomes the starting stop.</p></div></li><li><span>03</span><div><strong>Ratchet the stop upward</strong><p>Hold wide through the noise, lock above cost once ahead, then trail the rolling low. The stop never moves down.</p></div></li></ol></article><article className="panel"><div className="panel-head"><div><p className="eyebrow">Latest execution</p><h2>Recent paper fills</h2></div>{latestRun&&<button className="text-button" onClick={()=>openRun(latestRun.id)}>Full report →</button>}</div>{recentFills.length?<div className="fill-list">{recentFills.map(fill=><div className="fill-row" key={fill.id}><span className={`side-dot ${fill.side.toLowerCase()}`}/><div><strong>{fill.symbol}</strong><small>{fill.side} · {fill.quantity} shares</small></div><b>{money.format(fill.price)}</b></div>)}</div>:<Empty text="No fills yet. Start a run when the market is open."/>}</article></section></>;
}

function ActiveRun({run,openRun}:{run:MomentumRun;openRun:(id:string)=>void}) {return <section className="active-strip"><div><span className="live-dot"/><div><p className="eyebrow">{run.config.account_id||'Run in progress'}</p><h2>{armLabel(run)}</h2></div></div><div className="run-stats"><span><small>Scans</small><b>{run.scan_count}</b></span><span><small>Price polls</small><b>{run.poll_count}</b></span><span><small>Open positions</small><b>{run.open_positions.length}/{run.config.max_positions}</b></span><span><small>Live P&L</small><b className={run.session_pnl>=0?'positive':'negative'}>{signedMoney(run.session_pnl)}</b></span></div><button className="text-button" onClick={()=>openRun(run.id)}>Open live details →</button></section>}
function LatestRun({run,openRun}:{run?:MomentumRun;openRun:(id:string)=>void}) {if(!run)return <section className="latest-empty"><p className="eyebrow">Ready</p><h2>Your first run will appear here.</h2><p>Start with five minutes and review the entries, reversals, fees, and final equity.</p></section>;return <section className="latest-run"><div><p className="eyebrow">Latest run</p><h2>{formatDate(run.started_at)}</h2><span className={`status ${run.status.toLowerCase()}`}>{run.status}</span></div><div className="latest-metrics"><span><small>Net result</small><b className={run.session_pnl>=0?'positive':'negative'}>{signedMoney(run.session_pnl)}</b></span><span><small>Stocks traded</small><b>{new Set(run.fills.map(fill=>fill.symbol)).size}</b></span><span><small>Final equity</small><b>{money.format(run.portfolio.equity)}</b></span></div><button className="secondary" onClick={()=>openRun(run.id)}>Review run</button></section>}

function RunsView({runs,openRun}:{runs:MomentumRun[];openRun:(id:string)=>void}) {return <section className="runs-page"><div className="page-title"><div><p className="eyebrow">Run archive</p><h1>Every test.<br/><em>Every outcome.</em></h1><p>Completed and active momentum runs, kept separately from legacy strategies.</p></div><div className="archive-count"><strong>{runs.length}</strong><span>total runs</span></div></div>{runs.length?<div className="runs-list">{runs.map((run,index)=><button className="run-row" key={run.id} onClick={()=>openRun(run.id)}><span className="run-number">{String(runs.length-index).padStart(2,'0')}</span><div className="run-main"><span className={`status ${run.status.toLowerCase()}`}>{run.status}</span><h2>{formatDate(run.started_at)}</h2><small>{Math.round(run.config.duration_seconds/60)} min · {run.scan_count} scans · {new Set(run.fills.map(fill=>fill.symbol)).size} stocks</small></div><div className="run-symbols">{[...new Set(run.fills.map(fill=>fill.symbol))].slice(0,4).map(symbol=><span key={symbol}>{symbol}</span>)}</div><div className="run-result"><small>Net P&L</small><strong className={run.session_pnl>=0?'positive':'negative'}>{signedMoney(run.session_pnl)}</strong><span>{money.format(run.portfolio.equity)} final equity</span></div><span className="row-arrow">→</span></button>)}</div>:<Empty text="No runs have been recorded yet."/>}</section>}

function RunDetail({run,back}:{run:MomentumRun;back:()=>void}) {const outcomes=useMemo(()=>runOutcomes(run),[run]);const duration=run.started_at&&run.finished_at?(new Date(run.finished_at).getTime()-new Date(run.started_at).getTime())/60000:run.config.duration_seconds/60;const gross=run.metrics?.gross_pnl??outcomes.reduce((sum,row)=>sum+row.grossPnl,0),fees=run.metrics?.fees??outcomes.reduce((sum,row)=>sum+row.fees,0);return <section className="run-detail"><button className="back-button" onClick={back}>← All runs</button><div className="detail-title"><div><span className={`status ${run.status.toLowerCase()}`}>{run.status}</span><p className="eyebrow">Momentum run · {run.id.slice(0,8)}</p><h1>{formatDate(run.started_at)}</h1><p>{number.format(duration)} minutes · {run.scan_count} {universeName(run)} survey cycles · {run.poll_count} price polls</p></div><div className={`result-orb ${run.session_pnl>=0?'gain':'loss'}`}><span>Net result</span><strong>{signedMoney(run.session_pnl)}</strong><small>{signedPct((run.session_pnl/(run.initial_equity||run.portfolio.initial_cash))*100)}</small></div></div><section className="detail-kpis"><Metric label="Starting equity" value={money.format(run.initial_equity||run.portfolio.initial_cash)}/><Metric label="Final equity" value={money.format(run.portfolio.equity)}/><Metric label="Gross trading P&L" value={signedMoney(gross)} tone={gross>=0?'positive':'negative'}/><Metric label="Execution costs" value={money.format(fees)}/></section><CostFloorPanel run={run}/><EntryEvidence run={run}/><PositionLedger run={run}/><DecisionFunnel run={run}/><MonitoringTrace run={run}/><ReplayPanel run={run}/><section className="panel outcome-panel"><div className="panel-head"><div><p className="eyebrow">Stock outcomes</p><h2>Round trips</h2></div><span className="count">{outcomes.length} stocks</span></div>{outcomes.length?<div className="table-wrap"><table><thead><tr><th>Stock</th><th>Qty</th><th>Buy</th><th>Sell</th><th>Exit</th><th>Gross P&L</th><th>Fees</th><th>Net P&L</th></tr></thead><tbody>{outcomes.map(row=><tr key={row.symbol}><td className="stock-name">{row.symbol}</td><td>{row.quantity}</td><td>{money.format(row.buyPrice)}</td><td>{row.sellPrice?money.format(row.sellPrice):'Open'}</td><td><span className="reason">{row.exitReason}</span></td><td className={row.grossPnl>=0?'positive':'negative'}>{signedMoney(row.grossPnl)}</td><td>{money.format(row.fees)}</td><td className={row.netPnl>=0?'positive':'negative'}><strong>{signedMoney(row.netPnl)}</strong></td></tr>)}</tbody></table></div>:<Empty text="This run did not find enough movement to place a trade."/>}</section><section className="detail-grid"><article className="panel"><div className="panel-head"><div><p className="eyebrow">Execution timeline</p><h2>Entries and exits</h2></div></div><div className="timeline">{run.events.filter(event=>['ENTRY_FILLED','EXIT_FILLED'].includes(event.type)).map((event,index)=><div className="timeline-row" key={`${event.timestamp}-${index}`}><span className={`timeline-dot ${event.type==='ENTRY_FILLED'?'buy':'sell'}`}/><div><strong>{event.symbol}</strong><small>{event.type==='ENTRY_FILLED'?'Position opened':`Closed · ${(event.reason||'').replaceAll('_',' ')}`}</small></div><b>{event.observed_price?money.format(event.observed_price):'—'}</b><time>{formatTime(event.timestamp)}</time></div>)}</div></article><article className="panel"><div className="panel-head"><div><p className="eyebrow">Run settings</p><h2>Guardrails used</h2></div></div><dl className="settings-list"><div><dt>Tradable universe</dt><dd>{universeName(run)}</dd></div><div><dt>Capital per position</dt><dd>{money.format(run.config.allocation_per_position)}</dd></div><div><dt>Maximum positions</dt><dd>{run.config.max_positions}</dd></div><div><dt>Entry momentum</dt><dd>+{run.config.entry_momentum_pct}%</dd></div><div><dt>NIFTY momentum</dt><dd>{run.config.require_nifty_confirmation?'Required positive':'Recorded, not gated'}</dd></div><div><dt>Relative volume</dt><dd>≥{(run.config.minimum_relative_volume||1.2).toFixed(2)}×</dd></div><div><dt>Entry rule</dt><dd>{(run.config.entry_mode||'ROLLING_WINDOW').replaceAll('_',' ').toLowerCase()}{run.config.entry_bars?` · ${run.config.entry_bars} bars`:''}</dd></div><div><dt>Entry timeframe</dt><dd>{run.config.entry_timeframe_seconds?`${run.config.entry_timeframe_seconds/60}-minute bars`:'5-second ticks'}</dd></div><div><dt>Entry bar</dt><dd>max of {run.config.entry_momentum_pct}%, {run.config.entry_cost_multiple??1}× cost, {run.config.entry_noise_multiple??2}× noise</dd></div><div><dt>Exit rule</dt><dd>{(run.config.exit_mode||'REVERSAL').toLowerCase()}</dd></div>{run.config.exit_mode==='RATCHET'?<><div><dt>Survive stop</dt><dd>{run.config.survive_stop_multiple}× floor</dd></div><div><dt>Lock at</dt><dd>{run.config.lock_multiple}× floor</dd></div><div><dt>Ride from</dt><dd>{run.config.ride_multiple}× floor</dd></div><div><dt>Trail window</dt><dd>{run.config.trail_window} samples</dd></div><div><dt>Fast trail on decay</dt><dd>{run.config.fast_trail_window} samples</dd></div><div><dt>Confirmation</dt><dd>{run.config.confirmation_samples} bars</dd></div><div><dt>Time stop</dt><dd>{run.config.time_stop_seconds}s</dd></div></>:<><div><dt>Trailing reversal</dt><dd>−{run.config.reversal_pct}%</dd></div><div><dt>Hard stop</dt><dd>−{run.config.hard_stop_pct}%</dd></div></>}</dl>{run.errors.length>0&&<div className="error-box">{run.errors.join(' · ')}</div>}</article></section></section>}

function EntryEvidence({run}:{run:MomentumRun}) {
  const entries=run.events.filter(event=>event.type==='ENTRY_FILLED');
  if(!entries.length)return null;
  const captured=entries.filter(event=>event.entry_signal);
  return <section className="panel evidence-panel"><div className="panel-head"><div><p className="eyebrow">Why we bought</p><h2>Entry evidence</h2></div><span className="count">Stock + NIFTY + volume confirmation</span></div>{captured.length?<div className="signal-grid">{captured.map(event=>{const signal=event.entry_signal!;const against=signal.market_alignment==='AGAINST_BROAD_MARKET';return <article className="signal-card" key={`${event.symbol}-${event.timestamp}`}><div className="signal-title"><div><strong>{event.symbol}</strong><small>{formatTime(event.timestamp)}</small></div><span className={against?'against':'aligned'}>{against?'AGAINST NIFTY':'WITH NIFTY'}</span></div><p>Bought because the monitored price rose <b>{signedPct(signal.window_change_pct)}</b>, NIFTY short-term momentum was positive, and relative volume was <b>{optionalRatio(signal.relative_volume)}</b>.</p><dl><div><dt>Latest 5-sec change</dt><dd className={signal.sample_change_pct>=0?'positive':'negative'}>{signedPct(signal.sample_change_pct)}</dd></div><div><dt>Stock · 15 min</dt><dd className={signal.recent_15m_change_pct>=0?'positive':'negative'}>{signedPct(signal.recent_15m_change_pct)}</dd></div><div><dt>NIFTY · 15 min</dt><dd className={(signal.nifty_recent_15m_change_pct||0)>=0?'positive':'negative'}>{optionalPct(signal.nifty_recent_15m_change_pct)}</dd></div><div><dt>Relative volume</dt><dd className={(signal.relative_volume||0)>=1.2?'positive':'negative'}>{optionalRatio(signal.relative_volume)}</dd></div><div><dt>Price window</dt><dd>{money.format(signal.window_start_price)} → {money.format(signal.observed_price)}</dd></div><div><dt>Volume threshold</dt><dd>≥{(signal.minimum_relative_volume||1.2).toFixed(2)}×</dd></div>{signal.structural_stop!==undefined&&<div><dt>Three-bar trigger</dt><dd>{money.format(signal.structural_stop)}</dd></div>}{signal.initial_stop!==undefined&&<div><dt>Initial stop</dt><dd>{money.format(signal.initial_stop)}</dd></div>}{signal.cost_floor_pct!==undefined&&<div><dt>Cost floor</dt><dd>{signal.cost_floor_pct.toFixed(3)}%</dd></div>}{signal.entry_threshold_pct!==undefined&&<div><dt>Bar it cleared</dt><dd>≥{signal.entry_threshold_pct.toFixed(3)}%<em>{(signal.entry_threshold_source||'').replaceAll('_',' ').toLowerCase()}</em></dd></div>}{signal.stock_noise_pct!==undefined&&signal.stock_noise_pct!==null&&<div><dt>Stock noise · 3 bar</dt><dd>{signal.stock_noise_pct.toFixed(3)}%</dd></div>}{signal.risk_pct!==undefined&&<div><dt>Risk at entry</dt><dd className="negative">{signedPct(-Math.abs(signal.risk_pct))}</dd></div>}</dl></article>})}</div>:<div className="legacy-note"><strong>Detailed entry evidence was not recorded for this legacy run.</strong><p>The earlier engine required a short-window price increase and a rising latest sample, but it did not save every observation or NIFTY context. We will not invent those missing values; all future runs record them.</p></div>}</section>;
}

const BATCH_ARMS=[
  {label:'5s-ticks', note:'Three 5-second prints',  overrides:{entry_timeframe_seconds:0}},
  {label:'1m-bars',  note:'Three 1-minute bars',    overrides:{entry_timeframe_seconds:60}},
  {label:'3m-bars',  note:'Three 3-minute bars',    overrides:{entry_timeframe_seconds:180}},
  {label:'5m-bars',  note:'Three 5-minute bars',    overrides:{entry_timeframe_seconds:300}},
];

function BatchLauncher({duration,allocation,maxPositions,disabled,onLaunched}:{duration:number;allocation:number;maxPositions:number;disabled:boolean;onLaunched:()=>void}) {
  const [picked,setPicked]=useState<string[]>(['5s-ticks','1m-bars','3m-bars','5m-bars']);
  const [busy,setBusy]=useState(false), [result,setResult]=useState<BatchResult|null>(null), [error,setError]=useState('');
  const toggle=(label:string)=>setPicked(current=>current.includes(label)?current.filter(item=>item!==label):[...current,label]);
  const launch=async()=>{
    setBusy(true);setError('');setResult(null);
    try{
      const body={base:{duration_seconds:duration,allocation_per_position:allocation,max_positions:maxPositions,entry_mode:'THREE_BAR',exit_mode:'RATCHET'},
                  account_prefix:'fwd',
                  variants:BATCH_ARMS.filter(arm=>picked.includes(arm.label)).map(arm=>({label:arm.label,overrides:arm.overrides}))};
      setResult(await request<BatchResult>('/momentum-runners/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}));
      onLaunched();
    }catch(problem){setError(problem instanceof Error?problem.message:'Batch could not be started');}
    finally{setBusy(false);}
  };
  return <section className="panel batch-panel"><div className="panel-head"><div><p className="eyebrow">Run several configurations at once</p><h2>Comparison batch</h2></div><span className="count">{picked.length} selected</span></div>
    <p className="trace-help">Each arm gets its own paper account, so they never share cash or positions. Same universe, same clock, different rule - which is the only way one run tells you anything about another.</p>
    <div className="arm-grid">{BATCH_ARMS.map(arm=><button key={arm.label} className={`arm ${picked.includes(arm.label)?'on':''}`} onClick={()=>toggle(arm.label)} disabled={disabled||busy}><strong>{arm.label}</strong><small>{arm.note}</small></button>)}</div>
    <button className="secondary batch-go" onClick={launch} disabled={disabled||busy||!picked.length}>{busy?'Starting…':`Start ${picked.length} run${picked.length===1?'':'s'}`}</button>
    {error&&<div className="error-box">{error}</div>}
    {result&&<div className="batch-result">{result.started.map(item=><span className="reason good" key={item.label}>{item.label} started</span>)}{result.failed.map(item=><span className="reason" key={item.label}>{item.label}: {item.error}</span>)}</div>}
  </section>;
}

function CostFloorPanel({run}:{run:MomentumRun}) {
  const cost=run.cost_model;
  if(!cost)return null;
  const floor=cost.breakeven_pct, ratchet=run.config.exit_mode!=='REVERSAL';
  const risk=(run.config.survive_stop_multiple??2)*floor, lock=(run.config.lock_multiple??1.5)*floor, ride=(run.config.ride_multiple??3)*floor;
  const thin=!ratchet&&run.config.reversal_pct<floor;
  return <section className="panel cost-panel"><div className="panel-head"><div><p className="eyebrow">Cost floor</p><h2>What a round trip must clear</h2></div><span className="count">{money.format(cost.notional)} per position · {cost.product}</span></div>
    <div className="cost-grid"><div className="cost-hero"><small>Breakeven move</small><strong>{floor.toFixed(3)}%</strong><span>{money.format(cost.total_cost)} of cost on {money.format(cost.notional)}</span></div>
    <dl className="cost-breakdown"><div><dt>Buy side fees</dt><dd>{money.format(cost.buy_fees)}</dd></div><div><dt>Sell side fees</dt><dd>{money.format(cost.sell_fees)}</dd></div><div><dt>Slippage · {cost.slippage_bps}bps × 2</dt><dd>{money.format(cost.slippage)}</dd></div><div><dt>Total to recover</dt><dd>{money.format(cost.total_cost)}</dd></div></dl></div>
    {ratchet?<div className="ladder"><div className="ladder-step survive"><small>1 · Survive</small><b>−{risk.toFixed(3)}%</b><span>Fixed stop under entry. Wide enough to sit through quote noise.</span></div><div className="ladder-step lock"><small>2 · Lock at +{lock.toFixed(3)}%</small><b>+{floor.toFixed(3)}%</b><span>Stop jumps above cost. The round trip can no longer lose.</span></div><div className="ladder-step ride"><small>3 · Ride from +{ride.toFixed(3)}%</small><b>trailing</b><span>Stop follows the {run.config.trail_window??24}-sample low, never closer than {((run.config.min_gap_multiple??1.5)*floor).toFixed(3)}%.</span></div></div>
    :<div className={thin?'cost-warning':'legacy-note'}><strong>{thin?'This run exits below its own cost floor.':'Fixed reversal exit'}</strong><p>{thin?`A −${run.config.reversal_pct}% reversal exit and a −${run.config.hard_stop_pct}% hard stop are both inside the ${floor.toFixed(3)}% breakeven, so a textbook winning trade still finishes negative. Raise the allocation or switch this run to the ratcheting exit.`:`Exits on a −${run.config.reversal_pct}% pullback from peak or a −${run.config.hard_stop_pct}% hard stop, independent of the ${floor.toFixed(3)}% cost floor.`}</p></div>}
  </section>;
}

function DecisionFunnel({run}:{run:MomentumRun}) {
  const counts=run.decision_counts||[];
  if(!counts.length)return null;
  const peak=Math.max(...counts.map(row=>row.count)), total=counts.reduce((sum,row)=>sum+row.count,0);
  return <section className="panel funnel-panel"><div className="panel-head"><div><p className="eyebrow">Where observations stopped</p><h2>Decision funnel</h2></div><span className="count">{total} price checks</span></div>
    <p className="trace-help">Every five-second check lands on exactly one outcome. The tallest bar is the gate that actually decided this run.</p>
    <div className="funnel">{counts.map(row=><div className="funnel-row" key={row.decision}><span className={`decision ${decisionTone(row.decision)}`}>{row.decision.replaceAll('_',' ')}</span><div className="funnel-bar"><i className={decisionTone(row.decision)} style={{width:`${Math.max(row.count/peak*100,1.5)}%`}}/></div><b>{row.count}</b><small>{row.share_pct.toFixed(1)}%</small></div>)}</div>
  </section>;
}

function StopChart({path,entry}:{path:MonitoringObservation[];entry:number}) {
  if(path.length<2)return null;
  const prices=path.map(row=>row.price), stops=path.map(row=>row.exit!.stop_price);
  const low=Math.min(entry,...prices,...stops), high=Math.max(entry,...prices,...stops), span=high-low||1;
  const width=100, height=34;
  const at=(index:number)=>index/(path.length-1)*width, level=(value:number)=>height-(value-low)/span*height;
  const points=(values:number[])=>values.map((value,index)=>`${at(index).toFixed(2)},${level(value).toFixed(2)}`).join(' ');
  return <svg className="stop-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label="Price against the trailing stop"><polyline className="chart-entry" vectorEffect="non-scaling-stroke" points={`0,${level(entry).toFixed(2)} ${width},${level(entry).toFixed(2)}`}/><polyline className="chart-stop" vectorEffect="non-scaling-stroke" points={points(stops)}/><polyline className="chart-price" vectorEffect="non-scaling-stroke" points={points(prices)}/></svg>;
}

function PositionLedger({run}:{run:MomentumRun}) {
  const monitoring=useMemo(()=>run.monitoring||[],[run.monitoring]);
  const positions=useMemo(()=>run.events.filter(event=>event.type==='ENTRY_FILLED').map(entry=>({
    entry,
    exit:run.events.find(event=>event.type==='EXIT_FILLED'&&event.symbol===entry.symbol),
    path:monitoring.filter(row=>row.symbol===entry.symbol&&row.exit),
  })),[run.events,monitoring]);
  if(!positions.length)return null;
  return <section className="panel ledger-panel"><div className="panel-head"><div><p className="eyebrow">How each position was managed</p><h2>Exit ladder</h2></div><span className="count">{positions.length} position{positions.length===1?'':'s'}</span></div>
    <p className="trace-help">The stop only ever moves up. The chart shows the last price against the stop it was ratcheting behind, with the entry price flat across.</p>
    <div className="ledger">{positions.map(({entry,exit,path})=>{
      const signal=entry.entry_signal, final=exit?.exit_state||path[path.length-1]?.exit;
      const reached=path.reduce((best,row)=>Math.max(best,phaseRank(row.exit?.phase)),0);
      return <article className="ledger-card" key={`${entry.symbol}-${entry.timestamp}`}>
        <div className="ledger-head"><div><strong>{entry.symbol}</strong><small>{formatTime(entry.timestamp)}{exit?` → ${formatTime(exit.timestamp)}`:' · still open'}</small></div><span className={`reason ${exit?.reason&&['TRAILING_STOP','BREAKEVEN_STOP'].includes(exit.reason)?'good':''}`}>{(exit?.reason||'OPEN').replaceAll('_',' ')}</span></div>
        <StopChart path={path} entry={signal?.observed_price||entry.observed_price||0}/>
        <div className="phase-track">{['SURVIVE','LOCK','RIDE'].map((phase,index)=><span key={phase} className={index<=reached?'reached':''}>{phase}</span>)}</div>
        <dl className="ledger-facts">
          <div><dt>Entry</dt><dd>{money.format(signal?.observed_price||0)}</dd></div>
          <div><dt>Cost floor</dt><dd>{(signal?.cost_floor_pct??final?.cost_floor_pct??0).toFixed(3)}%</dd></div>
          <div><dt>Three-bar trigger</dt><dd>{signal?.structural_stop?money.format(signal.structural_stop):'—'}</dd></div>
          <div><dt>Initial stop</dt><dd>{signal?.initial_stop?money.format(signal.initial_stop):'—'}<em>{(signal?.initial_stop_source||'').replaceAll('_',' ').toLowerCase()}</em></dd></div>
          <div><dt>Risk at entry</dt><dd className="negative">{signal?.risk_pct!==undefined?signedPct(-Math.abs(signal.risk_pct)):'—'}</dd></div>
          <div><dt>Peak reached</dt><dd>{final?money.format(final.peak_price):'—'}</dd></div>
          <div><dt>Final stop</dt><dd>{final?money.format(final.stop_price):'—'}</dd></div>
          <div><dt>Exit</dt><dd>{exit?.observed_price?money.format(exit.observed_price):'—'}</dd></div>
          <div><dt>Move net of cost</dt><dd className={(final?.net_of_cost_pct??0)>=0?'positive':'negative'}>{final?signedPct(final.net_of_cost_pct):'—'}</dd></div>
          <div><dt>Held</dt><dd>{final?.seconds_held!==undefined?`${Math.round(final.seconds_held)}s`:'—'}</dd></div>
        </dl>
      </article>;
    })}</div>
  </section>;
}


function TraceChart({rows,symbol}:{rows:MonitoringObservation[];symbol:string}) {
  const [hover,setHover]=useState<number|null>(null);
  if(rows.length<2)return <Empty text="Not enough observations to plot yet."/>;
  const prices=rows.map(row=>row.price);
  const stops=rows.map(row=>row.exit?.stop_price??null);
  const withStops=stops.filter((value):value is number=>value!==null);
  const low=Math.min(...prices,...withStops), high=Math.max(...prices,...withStops);
  const span=(high-low)||Math.max(high*0.0005,0.05);
  const pad=span*0.12, top=high+pad, bottom=low-pad, range=top-bottom;
  const W=1000, H=320;
  const x=(index:number)=>rows.length<2?0:index/(rows.length-1)*W;
  const y=(value:number)=>H-(value-bottom)/range*H;
  const line=(values:Array<number|null>)=>{
    const parts:string[]=[];let open=false;
    values.forEach((value,index)=>{
      if(value===null){open=false;return;}
      parts.push(`${open?'L':'M'}${x(index).toFixed(1)},${y(value).toFixed(1)}`);open=true;
    });
    return parts.join(' ');
  };
  const entries=rows.map((row,index)=>({row,index})).filter(item=>item.row.decision==='ENTRY_FILLED');
  const exits=rows.map((row,index)=>({row,index})).filter(item=>item.row.decision.startsWith('EXIT_'));
  const active=hover===null?null:rows[hover];
  const ticks=[0,0.25,0.5,0.75,1].map(fraction=>bottom+range*fraction);
  return <div className="trace-chart-wrap">
    <svg className="trace-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`${symbol} price over the run`}
         onMouseLeave={()=>setHover(null)}
         onMouseMove={event=>{const box=event.currentTarget.getBoundingClientRect();
           const ratio=(event.clientX-box.left)/box.width;
           setHover(Math.max(0,Math.min(rows.length-1,Math.round(ratio*(rows.length-1)))));}}>
      {ticks.map((value,index)=><line key={index} className="grid" x1={0} x2={W} y1={y(value)} y2={y(value)} vectorEffect="non-scaling-stroke"/>)}
      <path className="chart-stop" d={line(stops)} vectorEffect="non-scaling-stroke"/>
      <path className="chart-price" d={line(prices)} vectorEffect="non-scaling-stroke"/>
      {entries.map(item=><line key={`in-${item.index}`} className="mark-entry" x1={x(item.index)} x2={x(item.index)} y1={0} y2={H} vectorEffect="non-scaling-stroke"/>)}
      {exits.map(item=><line key={`out-${item.index}`} className="mark-exit" x1={x(item.index)} x2={x(item.index)} y1={0} y2={H} vectorEffect="non-scaling-stroke"/>)}
      {hover!==null&&<line className="mark-hover" x1={x(hover)} x2={x(hover)} y1={0} y2={H} vectorEffect="non-scaling-stroke"/>}
    </svg>
    <div className="chart-axis">{[...ticks].reverse().map((value,index)=><span key={index}>{value.toFixed(2)}</span>)}</div>
    <div className="chart-legend">
      <span><i className="swatch price"/>Price</span>
      <span><i className="swatch stop"/>Trailing stop</span>
      <span><i className="swatch entry"/>Entry</span>
      <span><i className="swatch exit"/>Exit</span>
      <span className="chart-range">{formatTime(rows[0].timestamp)} → {formatTime(rows[rows.length-1].timestamp)} · {rows.length} checks</span>
    </div>
    {active&&<div className="chart-readout">
      <b>{formatTime(active.timestamp)}</b>
      <span>{money.format(active.price)}</span>
      {active.exit&&<span>stop {money.format(active.exit.stop_price)} · {active.exit.phase}</span>}
      {active.entry_check?.rise_pct!==undefined&&active.entry_check.rise_pct!==null&&<span>3-bar {signedPct(active.entry_check.rise_pct)}{active.entry_check.threshold_pct!==undefined&&active.entry_check.threshold_pct!==null?` vs ≥${active.entry_check.threshold_pct.toFixed(3)}%`:''}</span>}
      <span className={`decision ${decisionTone(active.decision)}`}>{active.decision.replaceAll('_',' ')}</span>
    </div>}
  </div>;
}

const TRACE_VIEWS={gates:'Entry gates',exit:'Exit management',market:'Market context'} as const;
type TraceView=keyof typeof TRACE_VIEWS;

function MonitoringTrace({run}:{run:MomentumRun}) {
  const monitoring=useMemo(()=>run.monitoring||[],[run.monitoring]);
  const symbols=useMemo(()=>[...new Set(monitoring.map(item=>item.symbol))],[monitoring]);
  const [selectedSymbol,setSelectedSymbol]=useState('');
  const [view,setView]=useState<TraceView>('gates');
  const [filter,setFilter]=useState('ALL');
  const [showAll,setShowAll]=useState(false);
  const [format,setFormat]=useState<'table'|'chart'>('table');
  const symbol=symbols.includes(selectedSymbol)?selectedSymbol:(symbols[0]||'');
  const matching=useMemo(()=>monitoring.filter(item=>item.symbol===symbol&&(filter==='ALL'||item.decision===filter)),[monitoring,symbol,filter]);
  const decisions=useMemo(()=>[...new Set(monitoring.filter(item=>item.symbol===symbol).map(item=>item.decision))],[monitoring,symbol]);
  if(!monitoring.length)return null;
  const rows=format==='chart'||showAll?matching:matching.slice(-200);
  return <section className="panel monitoring-panel"><div className="panel-head"><div><p className="eyebrow">Every price check</p><h2>Monitoring trace</h2></div>
    <div className="trace-controls">
      <div className="format-switch" role="tablist" aria-label="Trace format"><button role="tab" aria-selected={format==='table'} className={format==='table'?'on':''} onClick={()=>setFormat('table')}>Table</button><button role="tab" aria-selected={format==='chart'} className={format==='chart'?'on':''} onClick={()=>setFormat('chart')}>Chart</button></div>
      <label className="trace-select">Stock<select value={symbol} onChange={event=>{setSelectedSymbol(event.target.value);setFilter('ALL')}}>{symbols.map(item=><option key={item}>{item}</option>)}</select></label>
      <label className="trace-select">Columns<select value={view} onChange={event=>setView(event.target.value as TraceView)}>{Object.entries(TRACE_VIEWS).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
      <label className="trace-select">Decision<select value={filter} onChange={event=>setFilter(event.target.value)}><option value="ALL">All</option>{decisions.map(item=><option key={item} value={item}>{item.replaceAll('_',' ')}</option>)}</select></label>
    </div></div>
    <p className="trace-help">{matching.length} of {monitoring.filter(item=>item.symbol===symbol).length} checks for {symbol}{rows.length<matching.length?` · showing the most recent ${rows.length}`:''}. {matching.length>rows.length&&<button className="text-button" onClick={()=>setShowAll(true)}>Show every row →</button>}</p>
    {format==='chart'?<TraceChart rows={rows} symbol={symbol}/>:<div className="table-wrap trace-table"><table><thead><tr><th>Time</th><th>Price</th>{view==='gates'&&<><th>3-bar window</th><th>First → last</th><th>Needed</th><th>Bar set by</th><th>Stock noise</th><th>Trigger price</th><th>Relative volume</th><th>NIFTY 15 min</th></>}{view==='exit'&&<><th>Phase</th><th>Stop</th><th>Gap to stop</th><th>Unrealized</th><th>Net of cost</th><th>Trail low</th><th>Volume</th><th>Breaches</th></>}{view==='market'&&<><th>5-sec change</th><th>Rolling change</th><th>Session</th><th>Stock 15 min</th><th>Score</th><th>Range position</th><th>NIFTY price</th></>}<th>Decision</th></tr></thead>
    <tbody>{rows.map((row,index)=><tr key={`${row.timestamp}-${index}`} className={row.decision==='ENTRY_FILLED'?'entry-row':row.decision.startsWith('EXIT_')?'exit-row':''}>
      <td>{formatTime(row.timestamp)}</td><td className="stock-name">{money.format(row.price)}</td>
      {view==='gates'&&<><td className="mono">{row.entry_check?.window.map(value=>value.toFixed(2)).join(' → ')||'—'}</td><td className={cleared(row.entry_check)?'positive':'negative'}>{row.entry_check?.rise_pct!==undefined&&row.entry_check.rise_pct!==null?signedPct(row.entry_check.rise_pct):'—'}</td><td>{row.entry_check?.threshold_pct!==undefined&&row.entry_check.threshold_pct!==null?`≥${row.entry_check.threshold_pct.toFixed(3)}%`:'—'}</td><td><span className="decision block">{(row.entry_check?.threshold_source||'—').replaceAll('_',' ')}</span></td><td className="mono">{row.entry_check?.noise_pct!==undefined&&row.entry_check.noise_pct!==null?`${row.entry_check.noise_pct.toFixed(3)}%`:'—'}</td><td>{row.entry_check?.trigger_price?money.format(row.entry_check.trigger_price):'—'}</td><td className={(row.relative_volume||0)>=(run.config.minimum_relative_volume||1.2)?'positive':'negative'}>{optionalRatio(row.relative_volume)}</td><td className={!run.config.require_nifty_confirmation?'':(row.nifty_recent_15m_change_pct||0)>0?'positive':'negative'}>{optionalPct(row.nifty_recent_15m_change_pct)}</td></>}
      {view==='exit'&&<><td>{row.exit?<span className={`phase ${row.exit.phase.toLowerCase()}`}>{row.exit.phase}</span>:'—'}</td><td>{row.exit?money.format(row.exit.stop_price):'—'}</td><td>{row.exit?`${row.exit.stop_distance_pct.toFixed(3)}%`:'—'}</td><td className={(row.exit?.unrealized_pct??0)>=0?'positive':'negative'}>{row.exit?signedPct(row.exit.unrealized_pct):'—'}</td><td className={(row.exit?.net_of_cost_pct??0)>=0?'positive':'negative'}>{row.exit?signedPct(row.exit.net_of_cost_pct):'—'}</td><td>{row.exit?.trail_low?money.format(row.exit.trail_low):'—'}</td><td>{row.exit?.volume_state?.toLowerCase()||'—'}</td><td>{row.exit?`${row.exit.breaches??0}/${row.exit.confirmation_samples??0}`:'—'}</td></>}
      {view==='market'&&<><td className={row.sample_change_pct>=0?'positive':'negative'}>{signedPct(row.sample_change_pct)}</td><td className={row.window_change_pct>=0?'positive':'negative'}>{signedPct(row.window_change_pct)}</td><td>{optionalPct(row.session_change_pct)}</td><td>{optionalPct(row.recent_15m_change_pct)}</td><td>{row.momentum_score?.toFixed(3)??'—'}</td><td>{row.range_position_pct!==undefined&&row.range_position_pct!==null?`${row.range_position_pct.toFixed(0)}%`:'—'}</td><td>{row.nifty_price?number.format(row.nifty_price):'—'}</td></>}
      <td><span className={`decision ${decisionTone(row.decision)}`}>{row.decision.replaceAll('_',' ')}</span></td></tr>)}</tbody></table></div>}
  </section>;
}

function ReplayPanel({run}:{run:MomentumRun}) {
  const [mode,setMode]=useState<'EXITS_ONLY'|'FULL'>('EXITS_ONLY');
  const [trailWindow,setTrailWindow]=useState(run.config.trail_window??24);
  const [surviveStop,setSurviveStop]=useState(run.config.survive_stop_multiple??2);
  const [lockMultiple,setLockMultiple]=useState(run.config.lock_multiple??1.5);
  const [rideMultiple,setRideMultiple]=useState(run.config.ride_multiple??3);
  const [confirmation,setConfirmation]=useState(run.config.confirmation_samples??2);
  const [timeStop,setTimeStop]=useState(run.config.time_stop_seconds??240);
  const [report,setReport]=useState<ReplayReport|null>(null);
  const [busy,setBusy]=useState(false), [error,setError]=useState('');
  if(!run.monitoring?.length)return null;
  const replay=async()=>{
    setBusy(true);setError('');
    try{
      const result=await request<ReplayReport>('/backtests/exit-replay',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:run.id,mode,trail_window:trailWindow,fast_trail_window:Math.min(run.config.fast_trail_window??6,trailWindow),survive_stop_multiple:surviveStop,lock_multiple:lockMultiple,ride_multiple:rideMultiple,confirmation_samples:confirmation,time_stop_seconds:timeStop,require_positive_nifty:!!run.config.require_nifty_confirmation})});
      setReport(result);
    }catch(problem){setError(problem instanceof Error?problem.message:'Replay failed');}
    finally{setBusy(false);}
  };
  return <section className="panel replay-panel"><div className="panel-head"><div><p className="eyebrow">Test a different rule on this data</p><h2>Exit replay</h2></div><span className="count">{run.monitoring.length} recorded observations</span></div>
    <p className="trace-help"><b>Same entries</b> replays only the exit rule against the recorded five-second trace, isolating the exit change. <b>Entries and exits</b> re-runs the three-bar entry and the gates too. Both price fills through the same fee and slippage model as the live account.</p>
    <div className="replay-form">
      <label>Replay<select value={mode} onChange={event=>setMode(event.target.value as 'EXITS_ONLY'|'FULL')}><option value="EXITS_ONLY">Same entries</option><option value="FULL">Entries and exits</option></select></label>
      <label>Trail window<input type="number" min="2" max="360" value={trailWindow} onChange={event=>setTrailWindow(Number(event.target.value))}/></label>
      <label>Survive ×<input type="number" min="0.5" max="10" step="0.5" value={surviveStop} onChange={event=>setSurviveStop(Number(event.target.value))}/></label>
      <label>Lock ×<input type="number" min="0.5" max="10" step="0.5" value={lockMultiple} onChange={event=>setLockMultiple(Number(event.target.value))}/></label>
      <label>Ride ×<input type="number" min="0.5" max="20" step="0.5" value={rideMultiple} onChange={event=>setRideMultiple(Number(event.target.value))}/></label>
      <label>Confirm bars<input type="number" min="1" max="20" value={confirmation} onChange={event=>setConfirmation(Number(event.target.value))}/></label>
      <label>Time stop (s)<input type="number" min="10" max="3600" step="10" value={timeStop} onChange={event=>setTimeStop(Number(event.target.value))}/></label>
      <button className="secondary" onClick={replay} disabled={busy}>{busy?'Replaying…':'Run replay'}</button>
    </div>
    {error&&<div className="error-box">{error}</div>}
    {report&&<><div className="replay-result"><ReplaySide title="Recorded run" side={report.baseline}/><ReplaySide title={mode==='FULL'?'Candidate rule':'Candidate exit'} side={report.candidate}/><div className={`replay-delta ${report.delta.net_pnl>=0?'gain':'loss'}`}><small>Difference</small><strong>{signedMoney(report.delta.net_pnl)}</strong><span>{report.delta.round_trips>=0?'+':''}{report.delta.round_trips} round trips</span></div></div>
    {report.trades.length>0&&<div className="table-wrap"><table><thead><tr><th>Stock</th><th>Entry</th><th>Initial stop</th><th>Exit</th><th>Reason</th><th>Held</th><th>Gross</th><th>Fees</th><th>Net</th></tr></thead><tbody>{report.trades.map((trade,index)=><tr key={`${trade.symbol}-${index}`}><td className="stock-name">{trade.symbol}</td><td>{money.format(trade.entry_price)}</td><td>{money.format(trade.initial_stop)}</td><td>{money.format(trade.exit_price)}</td><td><span className="reason">{trade.exit_reason.replaceAll('_',' ')}</span></td><td>{trade.seconds_held?`${Math.round(trade.seconds_held)}s`:'—'}</td><td className={trade.gross_pnl>=0?'positive':'negative'}>{signedMoney(trade.gross_pnl)}</td><td>{money.format(trade.fees)}</td><td className={trade.net_pnl>=0?'positive':'negative'}><strong>{signedMoney(trade.net_pnl)}</strong></td></tr>)}</tbody></table></div>}
    {!report.trades.length&&<Empty text="This rule took no trades on the recorded data."/>}</>}
  </section>;
}

function ReplaySide({title,side}:{title:string;side:ReplayMetrics}) {
  return <div className="replay-side"><small>{title}</small><strong className={side.net_pnl>=0?'positive':'negative'}>{signedMoney(side.net_pnl)}</strong>
    <dl><div><dt>Round trips</dt><dd>{side.round_trips}</dd></div><div><dt>Win rate</dt><dd>{side.win_rate_pct.toFixed(0)}%</dd></div><div><dt>Fees</dt><dd>{money.format(side.fees)}</dd></div>{side.average_hold_seconds!==undefined&&<div><dt>Average hold</dt><dd>{Math.round(side.average_hold_seconds)}s</dd></div>}</dl>
    <div className="reason-chips">{side.exit_reasons.map(row=><span className="reason" key={row.reason}>{row.reason.replaceAll('_',' ')} · {row.count}</span>)}</div></div>;
}

function decisionTone(decision:string):string {
  if(decision==='ENTRY_FILLED')return 'entry';
  if(decision.startsWith('EXIT_'))return 'exit';
  if(decision==='HOLDING_POSITION')return 'hold';
  return 'block';
}
function armLabel(run:MomentumRun):string {
  const bars=run.config.entry_timeframe_seconds?`${run.config.entry_timeframe_seconds/60}m bars`:'5s ticks';
  return `${bars} · ${money.format(run.config.allocation_per_position)}`;
}
function phaseRank(phase?:string):number {return phase==='RIDE'?2:phase==='LOCK'?1:0}
function cleared(check?:EntryCheck):boolean {return !!check&&check.rise_pct!==undefined&&check.rise_pct!==null&&check.threshold_pct!==undefined&&check.threshold_pct!==null&&check.rise_pct>=check.threshold_pct}


const TF_LABEL:Record<number,string>={0:'5s ticks',60:'1m bars',180:'3m bars',300:'5m bars'};
function tfLabel(v?:number){return TF_LABEL[v??0]||`${v}s`}

function ReportsView({openRun}:{openRun:(id:string)=>void}) {
  const [status,setStatus]=useState<ScheduleStatus|null>(null);
  const [dates,setDates]=useState<DailyReport[]>([]);
  const [selected,setSelected]=useState<string>('');
  const [report,setReport]=useState<DailyReport|null>(null);
  const [busy,setBusy]=useState(''), [notice,setNotice]=useState('');
  const refresh=useCallback(async()=>{
    try{
      const [st,list]=await Promise.all([request<ScheduleStatus>('/schedule/status'),request<DailyReport[]>('/reports/daily')]);
      setStatus(st);setDates(list);
      if(!selected&&(list.length||st.session_date))setSelected(list[0]?.session_date||st.session_date);
    }catch(error){setNotice(error instanceof Error?error.message:'Could not load reports');}
  },[selected]);
  useEffect(()=>{const first=window.setTimeout(refresh,0),t=window.setInterval(refresh,15000);return()=>{window.clearTimeout(first);window.clearInterval(t);};},[refresh]);
  useEffect(()=>{if(!selected)return;let live=true;
    request<DailyReport>(`/reports/daily/${selected}`).then(r=>{if(live)setReport(r)}).catch(()=>{if(live)setReport(null)});
    return()=>{live=false};
  },[selected,dates]);
  const buildReport=async()=>{if(!selected)return;setBusy('build');try{const r=await request<DailyReport>(`/reports/daily/${selected}/build`,{method:'POST'});setReport(r);setNotice(`Report built for ${selected}`);await refresh();}catch(error){setNotice(error instanceof Error?error.message:'Build failed');}finally{setBusy('');}};

  return <section className="reports-page">
    <div className="page-title"><div><p className="eyebrow">Automated research</p><h1>Every day.<br/><em>Every configuration.</em></h1><p>Scheduled runs cover the session; each evening rolls up into one report kept here.</p></div>
      <div className="archive-count"><strong>{dates.length}</strong><span>daily reports</span></div></div>

    <ScheduleStrip status={status}/>

    <div className="report-toolbar"><label className="trace-select">Session<select value={selected} onChange={e=>setSelected(e.target.value)}>{[...new Set([status?.session_date,...dates.map(d=>d.session_date)].filter(Boolean) as string[])].map(d=><option key={d}>{d}</option>)}</select></label>
      <button className="secondary" onClick={buildReport} disabled={!!busy||!selected}>{busy==='build'?'Building…':report?'Rebuild report':'Build report'}</button>
      {notice&&<span className="report-notice">{notice}</span>}</div>

    {report?<DailyReportView report={report} openRun={openRun}/>:<Empty text={`No report for ${selected||'this day'} yet. Runs must finish first, or build it now.`}/>}
  </section>;
}

function ScheduleStrip({status}:{status:ScheduleStatus|null}) {
  const [token,setToken]=useState<TokenStatus|null>(null);
  useEffect(()=>{let live=true;const load=()=>request<TokenStatus>('/auth/upstox/status').then(t=>{if(live)setToken(t)}).catch(()=>{});load();const timer=window.setInterval(load,30000);return()=>{live=false;window.clearInterval(timer);};},[]);
  const refreshToken=async()=>{try{const {authorization_url}=await request<{authorization_url:string}>('/auth/upstox/login-url');window.open(authorization_url,'_blank','noopener');}catch(error){alert(error instanceof Error?error.message:'Set UPSTOX_API_KEY/SECRET/REDIRECT_URI on the backend first');}};
  if(!status)return null;
  const plan=status.plan;
  const done=plan?plan.slots.filter(s=>s.status!=='PENDING').length:0;
  return <section className="schedule-strip"><div className="sched-head"><div><span className={`live-dot ${status.enabled?'':'off'}`}/><div><p className="eyebrow">Scheduler</p><h2>{status.enabled?(status.running?'Running':'Enabled'):'Disabled'}</h2></div></div>
    <div className="sched-stats"><span><small>Session</small><b>{status.session_date}</b></span><span><small>Trading day</small><b>{status.is_trading_day?'Yes':'No'}</b></span><span><small>Runs today</small><b>{done}/{plan?plan.slots.length:status.entry_timeframes.length}</b></span><span><small>Positions</small><b>{status.max_positions}</b></span><span><small>Cooldown</small><b>{Math.round(status.reentry_cooldown_seconds/60)}m</b></span><span><small>Report</small><b>{status.report_ready?'Ready':'Pending'}</b></span></div></div>
    <div className="token-row"><span className={`token-dot ${token?.likely_valid?'ok':'stale'}`}/><b>Upstox token</b><span>{token?token.token_type==='analytics'?'analytics · long-lived':token.likely_valid?`OAuth · expires ${token.expires_at_ist?token.expires_at_ist.slice(11,16):''} IST`:token.has_token?'stale — refresh before the open':'not set':'…'}</span>{token?.token_type!=='analytics'&&token?.oauth_configured&&<button className="text-button" onClick={refreshToken}>Refresh token →</button>}</div>
    {!status.enabled&&<p className="sched-warn">Scheduler is off. Set <code>SCHEDULER_ENABLED=true</code> and configure <code>UPSTOX_ANALYTICS_TOKEN</code> for unattended market data and 09:15 runs.</p>}
    {plan&&<div className="sched-timeline">{plan.slots.map(slot=><div key={slot.index} className={`sched-slot ${slot.status.toLowerCase()}`} title={`${slot.label} · ${slot.status}`}><span>{slot.start_ist.slice(11,16)}</span><b>{slot.label}</b><em>{slot.status.toLowerCase()}</em></div>)}</div>}
  </section>;
}

function DailyReportView({report,openRun}:{report:DailyReport;openRun:(id:string)=>void}) {
  const t=report.totals;
  return <>
    <section className="detail-kpis report-kpis"><Metric label="Net P&L" value={signedMoney(t.net_pnl)} tone={t.net_pnl>=0?'positive':'negative'}/><Metric label="Runs" value={`${t.runs_that_traded}/${t.runs} traded`}/><Metric label="Round trips" value={`${t.round_trips}`}/><Metric label="Win rate" value={`${t.win_rate_pct}%`}/><Metric label="Fees" value={money.format(t.fees)}/><Metric label="Observations" value={number.format(t.observations)}/></section>

    <section className="report-grid">
      <RollupCard title="By entry timeframe" rows={report.by_timeframe} render={k=>tfLabel(Number(k))}/>
      <RollupCard title="By duration" rows={report.by_duration} render={k=>`${Number(k)/60}m`}/>
      <RollupCard title="By max positions" rows={report.by_positions} render={k=>`${k} pos`}/>
    </section>

    {(report.best_run||report.worst_run)&&<section className="report-extremes">
      {report.best_run&&<article className={`extreme good`}><p className="eyebrow">Best arm</p><h3>{report.best_run.config_key}</h3><strong>{signedMoney(report.best_run.net_pnl)}</strong><small>{report.best_run.round_trips} trades · {report.best_run.win_rate_pct}% win</small></article>}
      {report.worst_run&&<article className={`extreme bad`}><p className="eyebrow">Worst arm</p><h3>{report.worst_run.config_key}</h3><strong>{signedMoney(report.worst_run.net_pnl)}</strong><small>{report.worst_run.round_trips} trades · {report.worst_run.win_rate_pct}% win</small></article>}
    </section>}

    <section className="panel"><div className="panel-head"><div><p className="eyebrow">Per run</p><h2>Every arm today</h2></div><span className="count">{report.runs.length} runs</span></div>
      <div className="table-wrap"><table><thead><tr><th>Config</th><th>Started</th><th>Timeframe</th><th>Trades</th><th>Win%</th><th>Fees</th><th>Net P&L</th><th></th></tr></thead>
      <tbody>{report.runs.map(r=><tr key={r.run_id}><td className="stock-name">{r.config_key}</td><td>{r.started_at?formatTime(r.started_at):'—'}</td><td>{tfLabel(r.entry_timeframe_seconds)}</td><td>{r.round_trips}</td><td>{r.win_rate_pct}%</td><td>{money.format(r.fees)}</td><td className={r.net_pnl>=0?'positive':'negative'}><strong>{signedMoney(r.net_pnl)}</strong></td><td><button className="text-button" onClick={()=>openRun(r.run_id)}>Open →</button></td></tr>)}</tbody></table></div>
      {!report.runs.length&&<Empty text="No runs recorded for this session."/>}</section>

    {report.decision_totals.length>0&&<section className="panel"><div className="panel-head"><div><p className="eyebrow">Across every run</p><h2>What the day did</h2></div><span className="count">{number.format(report.decision_totals.reduce((s,d)=>s+d.count,0))} checks</span></div>
      <div className="funnel">{report.decision_totals.slice(0,10).map(d=><div className="funnel-row" key={d.decision}><span className={`decision ${decisionTone(d.decision)}`}>{d.decision.replaceAll('_',' ')}</span><div className="funnel-bar"><i className={decisionTone(d.decision)} style={{width:`${Math.max(d.share_pct,1.5)}%`}}/></div><b>{d.count}</b><small>{d.share_pct.toFixed(1)}%</small></div>)}</div></section>}
  </>;
}

function RollupCard({title,rows,render}:{title:string;rows:ConfigRollup[];render:(key:string|number)=>string}) {
  const peak=Math.max(1,...rows.map(r=>Math.abs(r.net_pnl)));
  return <article className="panel rollup"><div className="panel-head"><div><p className="eyebrow">Comparison</p><h2>{title}</h2></div></div>
    <div className="rollup-rows">{rows.map(r=><div className="rollup-row" key={String(r.key)}><span className="rollup-key">{render(r.key)}</span><div className="rollup-bar"><i className={r.net_pnl>=0?'pos':'neg'} style={{width:`${Math.abs(r.net_pnl)/peak*100}%`}}/></div><b className={r.net_pnl>=0?'positive':'negative'}>{signedMoney(r.net_pnl)}</b><small>{r.runs} run{r.runs===1?'':'s'} · {r.round_trips} trades</small></div>)}</div>
    {!rows.length&&<Empty text="No runs yet."/>}</article>;
}

function Metric({label,value,tone}:{label:string;value:string;tone?:string}) {return <div className="metric"><span>{label}</span><strong className={tone||''}>{value}</strong></div>}
function Empty({text}:{text:string}) {return <div className="empty">{text}</div>}
function formatDate(value?:string){return value?new Intl.DateTimeFormat('en-IN',{dateStyle:'medium',timeStyle:'short'}).format(new Date(value)):'Waiting to start'}
function formatTime(value:string){return new Intl.DateTimeFormat('en-IN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(value))}
function optionalPct(value?:number){return value===undefined||value===null?'Unavailable':signedPct(value)}
function optionalRatio(value?:number){return value===undefined||value===null?'Unavailable':`${value.toFixed(2)}×`}
