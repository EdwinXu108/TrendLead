"""The independent audit's quote source keeps only the raw open and close text of each row."""
import pytest

from leadtrend_v05 import stage4_acceptance


def write_sources(root):
    norm = root / 'data/v05/normalized'
    norm.mkdir(parents=True)
    (norm / 'prices.csv').write_text(
        'security_id,session_date,open,close,volume\n'
        'A,2020-01-02,1.10,1.20,5\n'
        'A,2020-01-03,2.5,2.60,6\n'
        'A,2030-01-01,9,9,9\n', encoding='utf-8')
    (norm / 'securities.csv').write_text('security_id,ticker\nA,AAA\n', encoding='utf-8')
    (norm / 'spinoff_child_prices.csv').write_text('child_ticker,session_date\n', encoding='utf-8')


def test_independent_quotes_keep_only_raw_open_and_close_text(tmp_path):
    write_sources(tmp_path)
    quotes = stage4_acceptance.independent_quotes(tmp_path, '2024-06-28')
    q = quotes['A', '2020-01-02']
    assert q['open'] == '1.10' and q['close'] == '1.20'  # the text is kept exactly as written
    assert float(q['close']) == 1.2
    assert ('A', '2030-01-01') not in quotes  # rows after the end date are dropped
    assert type(q).__slots__ == ('open', 'close')
    with pytest.raises(KeyError):
        q['volume']  # no other field is retained
