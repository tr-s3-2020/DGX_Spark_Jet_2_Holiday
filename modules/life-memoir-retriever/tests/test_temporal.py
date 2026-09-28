import pytest
from life_memoir.temporal import parse_time, resolve
from life_memoir.models import TimeClaim


def e(id, claims):
    return dict(entry_id=id, version=1, time_assertions=claims)


@pytest.mark.parametrize('text', ['我二十虚岁进厂', '我大概二十周岁进厂', '我农历1970年进厂', '父亲1970年进厂', '我年轻时进厂'])
def test_unsupported_precision_preserves_original(text):
    birth = e('birth', parse_time('1950年出生', 'source', {}))
    event = e('event', parse_time(text, 'source2', {'birth': 'birth'}))
    r = resolve([birth, event])['event']
    assert r['alternatives'][0]['earliest_year'] is None
    assert event['time_assertions'][0]['raw_text'] == text


def test_conflict_and_cycle_are_not_forced_to_a_year():
    claims = [TimeClaim(relation='in_year', raw_text=f'{y}年', source_id='s', year_value=y, calendar='gregorian').model_dump() for y in [1970, 1980]]
    assert resolve([e('a', claims)])['a']['state'] == 'conflicted'
    def link(other):
        return [TimeClaim(relation='years_after', raw_text='三年后', source_id='s', offset_years=3, anchor_ref=other, calendar='gregorian').model_dump()]
    r = resolve([e('a', link('b')), e('b', link('a'))])
    assert r['a']['state'] == r['b']['state'] == 'conflicted'
    assert all(x['alternatives'][0]['earliest_year'] is None for x in r.values())


def test_unknown_age_basis_remains_tentative():
    birth = e('b', parse_time('1950年出生', 's1', {}))
    event = e('e', parse_time('二十岁进厂', 's2', {'birth': 'b'}))
    r = resolve([birth, event])['e']
    assert r['state'] == 'tentative'
    assert r['alternatives'][0]['assumption_notes']


def test_last_year_uses_original_timezone_year():
    claim = parse_time('去年退休', 's', {}, '2025-01-01T00:30:00+08:00')[0]
    assert claim['year_value'] == 2024
