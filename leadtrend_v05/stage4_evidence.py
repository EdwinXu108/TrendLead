"""Unpriced rights remain explicit in a stopped run; never invent cash or marks."""
from .stage2_enrich import CHILDREN

def unvalued_entitlements(day,entitled,events,children,securities):
    result=[]
    for event in events:
        q=entitled.get(event.security_id,0)
        if event.event_type!='spinoff' or not q or children.get(event.event_id):continue
        ticker=securities[event.security_id].ticker
        for child,ratio,url in CHILDREN.get((ticker,day.isoformat()),[]):
            result.append(dict(event_id=event.event_id,parent_security_id=event.security_id,
                entitled_parent_shares=q,child_ticker=child,child_shares_per_parent=ratio,
                contractual_child_shares=q*ratio,market_value=None,cash_in_lieu=None,
                delivery_or_cash_date=None,source_url=url,
                status='confirmed contractual entitlement, valuation unavailable; no cash credit and no zero-value deletion'))
    return result
