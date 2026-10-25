"""Development ledger. Score TRI never credits account cash."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
import math
from .ledger import AccountState, Receivable, confirm_dividend, pay_receivables

@dataclass
class SecurityReceivable:
    event_id:str
    security_id:str
    shares:float
    created:date
    reason:str
    source:str

@dataclass
class DevBook:
    account:AccountState
    security_receivables:list[SecurityReceivable]=field(default_factory=list)
    event_rows:list[dict]=field(default_factory=list)
    cash_flows:list[dict]=field(default_factory=list)
    receivable_rows:list[dict]=field(default_factory=list)
    frozen:dict[str,str]=field(default_factory=dict)
    fractional_settlements:dict=field(default_factory=dict)
    fractional_rows:list[dict]=field(default_factory=list)
    # Incremental mirrors of the spinoff rows inside event_rows; maintained so the
    # daily/weekly loops never rescan the whole event history.
    spinoff_rows:list[dict]=field(default_factory=list)
    spinoff_children:set[str]=field(default_factory=set)

    def split(self,event,mark):
        a=self.account
        if event.event_id in a.processed_event_ids:return
        old=a.shares_of(event.security_id);ratio=event.shares_new_per_old
        q=old*ratio;whole=math.floor(q+1e-9);fraction=q-whole
        a.set_shares(event.security_id,whole)
        if fraction>1e-8:
            self.security_receivables.append(SecurityReceivable(event.event_id,event.security_id,fraction,event.effective_session,'fractional_share_right_cash_in_lieu_unverified',event.source))
        a.processed_event_ids.add(event.event_id)
        self.event_rows.append(dict(session_date=event.effective_session.isoformat(),event_id=event.event_id,
            security_id=event.security_id,event_type=event.event_type,shares_before=old,shares_after=whole,
            ratio=ratio,receivable_shares=fraction,cash_amount=0,mechanical_value_error=(whole+fraction-q)*mark,
            source=event.source,handling='whole shares converted; fractional entitlement restricted, no invented cash'))

    def dividend(self,event,entitled):
        a=self.account
        if event.event_id in a.processed_event_ids:return
        if event.pay_date is None:raise ValueError('held dividend lacks verified payment date: '+event.event_id)
        confirm_dividend(a,event.security_id,event.event_id,event.cash_amount_per_share,event.effective_session,event.pay_date,entitled)
        amount=entitled*event.cash_amount_per_share
        self.event_rows.append(dict(session_date=event.effective_session.isoformat(),event_id=event.event_id,
            security_id=event.security_id,event_type='cash_dividend',shares_before=entitled,shares_after=a.shares_of(event.security_id),
            ratio='',receivable_shares=0,cash_amount=amount,mechanical_value_error=0,source=event.source,handling='receivable at ex-date; no score TRI cash credit'))
        self.receivable_rows.append(dict(session_date=event.effective_session.isoformat(),event_id=event.event_id,security_id=event.security_id,
            operation='cash_receivable_created',shares=entitled,amount=amount,source=event.source))

    def spinoff(self,event,children,metadata,marks,entitled_shares=None):
        a=self.account
        if event.event_id in a.processed_event_ids:return
        old=a.shares_of(event.security_id) if entitled_shares is None else entitled_shares
        if not children:raise ValueError('held spinoff lacks actual child entitlement: '+event.event_id)
        for child in children:
            sid=child['security_id'];q=old*float(child['shares_per_old']);whole=math.floor(q+1e-9);fraction=q-whole
            if sid not in marks:raise ValueError('held spinoff child has no reliable event-day mark: '+sid)
            a.set_shares(sid,a.shares_of(sid)+whole)
            settlement=self.fractional_settlements.get(sid+'|'+event.effective_session.isoformat())
            cash_amount=0.
            if fraction>1e-8:
                if settlement:
                    cash_amount=fraction*settlement['price']
                    cash_id=event.event_id+'|'+sid+'|fractional_cash'
                    a.receivable_cash+=cash_amount
                    a.receivables.append(Receivable(cash_id,sid,cash_amount,event.effective_session,date.fromisoformat(settlement['pay_date']),fraction))
                    self.fractional_rows.append(dict(session_date=event.effective_session.isoformat(),event_id=cash_id,parent_event_id=event.event_id,
                        security_id=sid,fractional_shares=fraction,valuation_price=settlement['price'],amount=cash_amount,
                        pay_date=settlement['pay_date'],fractional_cash_valuation=settlement['valuation'],source=settlement['source']))
                    self.receivable_rows.append(dict(session_date=event.effective_session.isoformat(),event_id=cash_id,security_id=sid,
                        operation='fractional_cash_receivable_created',shares=fraction,amount=cash_amount,source=settlement['source'],pay_date=settlement['pay_date']))
                else:self.security_receivables.append(SecurityReceivable(event.event_id,sid,fraction,event.effective_session,'fractional_share_right_cash_in_lieu_unverified',child['source']))
            self.receivable_rows.append(dict(session_date=event.effective_session.isoformat(),event_id=event.event_id,security_id=sid,
                operation='spinoff_shares_delivered_and_fractional_right',shares=q,amount=q*marks[sid],source=child['source']))
            row=dict(session_date=event.effective_session.isoformat(),event_id=event.event_id,security_id=event.security_id,
                event_type='spinoff',shares_before=old,shares_after=a.shares_of(event.security_id),child_security_id=sid,child_shares_delivered=whole,
                ratio=child['shares_per_old'],receivable_shares=fraction,cash_amount=cash_amount,mechanical_value_error=0,source=child['source'],
                handling='parent shares unchanged; child whole shares delivered; fractional cash receivable' if settlement and fraction>1e-8 else 'parent shares unchanged; child whole shares delivered; fractional right restricted')
            self.event_rows.append(row);self.spinoff_rows.append(row);self.spinoff_children.add(sid)
        for row in self.fractional_rows:
            if row['parent_event_id']==event.event_id:
                row['equity_at_confirmation']=self.components(marks)['total_equity']
                row['equity_ratio']=row['amount']/row['equity_at_confirmation']
                row['exceeds_0_1_percent']=row['equity_ratio']>0.001
        a.processed_event_ids.add(event.event_id)

    def pay(self,d):
        before=self.account.cash
        for r in pay_receivables(self.account,d):
            fractional=r.event_id.endswith('|fractional_cash')
            self.receivable_rows.append(dict(session_date=d.isoformat(),event_id=r.event_id,security_id=r.security_id,
                operation='cash_receivable_paid',shares=r.entitled_shares,amount=r.amount,source='verified event pay_date'))
            if fractional:self.receivable_rows[-1]['source']='investor-authorized fractional settlement policy'
            self.cash_flows.append(dict(session_date=d.isoformat(),event_id=r.event_id,security_id=r.security_id,cash_delta=r.amount,kind='fractional_cash_payment' if fractional else 'dividend_payment'))
        return self.account.cash-before

    def payments_on(self,d,start):
        """Per-security cash movement of the flows appended since index start.

        run_dev calls pay(d) once per session and only then reads today's flows,
        so the tail appended by that call is exactly the set of flows dated d.
        """
        by=defaultdict(float)
        for f in self.cash_flows[start:]:by[f['security_id']]+=f['cash_delta']
        return by

    def components(self,marks):
        tradable=frozen=receivable=0.
        for sid,q in self.account.positions.items():
            value=q*marks[sid]
            if sid in self.frozen:frozen+=value
            else:tradable+=value
        for r in self.security_receivables:receivable+=r.shares*marks[r.security_id]
        cash=self.account.cash;rc=self.account.receivable_cash
        return dict(tradable_stock_value=tradable,frozen_stock_value=frozen,cash=cash,receivable_cash=rc,
            receivable_security_value=receivable,total_equity=tradable+frozen+cash+rc+receivable)

    def frozen_ids(self):
        # Actual shares and pending shares of the same security occupy one slot.
        pending={r.security_id for r in self.security_receivables}
        return set(self.frozen)|pending

    def require_resolved_partial_delivery(self):
        overlap={r.security_id for r in self.security_receivables if self.account.shares_of(r.security_id)}
        if overlap:
            raise ValueError('partially delivered security requires verified fractional settlement before weekly allocation: '+','.join(sorted(overlap)))

    def children_after_signal(self,signal_date):
        """A new distribution cannot be disposed using a pre-distribution signal."""
        return {r['child_security_id'] for r in self.spinoff_rows
                if r['session_date']>signal_date}
