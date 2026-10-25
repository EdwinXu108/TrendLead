"""Shared score availability with a hard boundary on calculated return values."""
from bisect import bisect_left
from collections import defaultdict
from datetime import date
from pathlib import Path
from .scoring import build_tri, momentum_score
from .stage2_enrich import read_csv
from . import paths

class ScoreEngine:
    def __init__(self, inputs, sessions, root:Path, return_end:date):
        self.inputs=inputs;self.sessions=sessions;self.pos={d:i for i,d in enumerate(sessions)}
        self.return_end=return_end;self.prices=defaultdict(dict);self.events=defaultdict(list)
        for p in inputs.prices:self.prices[p.security_id][p.session_date]=p
        self.first_price_date={sid:min(px) for sid,px in self.prices.items()}
        for e in inputs.events:self.events[e.security_id].append(e)
        self.tri={};self.blocks=defaultdict(list)
        path=Path(root)/paths.SCORING_BLOCKS
        if path.exists():
            for r in read_csv(path):self.blocks[r['security_id']].append((date.fromisoformat(r['effective_session']),r['reason']))
        # Absolutely no TRI/return is constructed beyond the registered boundary.
        dates=[d for d in sessions if d<=return_end]
        for sid,px in self.prices.items():
            if sid==inputs.config.benchmark_security_id:continue
            ps=[p for d,p in px.items() if d<=return_end]
            ev=[e for e in self.events[sid] if e.effective_session<=return_end]
            self.tri[sid]=build_tri(ps,ev,dates)

    def availability(self,sid,d,L):
        """Inspect dates/data only: never divide prices or calculate returns."""
        px=self.prices.get(sid,{})
        if not px:return 'no_price_data'
        if any(since<=d for since,_ in self.blocks[sid]):
            return next(why for since,why in sorted(self.blocks[sid]) if since<=d)
        i=self.pos[d];window=self.sessions[max(0,i-L):i+1]
        if len(window)<L+1:return 'insufficient_history'
        first=self.first_price_date[sid]
        missing=[x for x in window if x not in px]
        if missing:
            if all(x<first for x in missing):return 'insufficient_history'
            return 'missing_price_at_signal' if d not in px else 'price_gap_in_window'
        for e in self.events[sid]:
            if e.effective_session>d:continue
            if e.effective_session not in px:return 'unresolved_event_price_gap'
            if e.event_type=='spinoff' and e.spinoff_valuation=='vendor_factor' and window[0]<=e.effective_session<=d:
                return 'spinoff_vendor_valuation_in_window'
            if e.event_type=='spinoff' and e.spinoff_value_per_old_share is None:
                return 'unresolved_event_price_gap'
        return ''

    def score(self,sid,d,L):
        if d>self.return_end:raise ValueError('holdout signal/return calculation forbidden')
        reason=self.availability(sid,d,L)
        if reason:return None,None,None,0,reason
        result=self.tri.get(sid)
        if result is None:return None,None,None,0,'no_price_data'
        return momentum_score(result.tri,d,L,self.sessions,session_index=self.pos,
            first_valid_price_date=self.first_price_date[sid],unresolved_from=result.invalid_from)
