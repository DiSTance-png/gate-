from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import json

import pytest
import requests

from gate_quant.client import AmbiguousOrderError, GateFuturesClient
from gate_quant.protection_identity import confirm_protection, matches
from gate_quant.protection_lifecycle import is_system_protection
from gate_quant.execution_journal import ExecutionJournal
from gate_quant.execution_reconciler import reconcile_intent
from scripts.sync_gate_ledger import _exit_reason
from test_execution_reconciler import FakeGate, prepared, settings
from test_gate_offline import StopReplacementClient
from gate_quant.service import GateTradingService
from gate_quant.risk import RiskLimits


REQUEST = dict(contract='BTC_USDT', size='-4', trigger_price='90', rule=2)
TAG = 't-gate-rsl-identity-00'


def stop_row():
    return dict(id=2118705525796372480, status='open',
                initial=dict(contract='BTC_USDT', size=-4, text='api', is_reduce_only=True, price='0', tif='ioc'),
                trigger=dict(price='90.00', rule=2, strategy_type=0, price_type=0))


class Session:
    def __init__(self, *, timeout=False, echo=False, failed=False):
        self.rows, self.calls = [], []
        self.timeout, self.echo, self.failed = timeout, echo, failed

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method == 'POST':
            payload = json.loads(kwargs['data'])
            row = {**payload, 'id': 2118705525796372480 + len(self.rows), 'status': 'open'}
            initial = row['initial']
            initial['is_reduce_only'] = initial.pop('reduce_only')
            if not self.echo:
                initial['text'] = 'api'
            self.rows.append(row)
            if self.timeout:
                raise requests.Timeout('lost response')
            result = {'id': row['id']}
        elif url.endswith('/price_orders'):
            result = deepcopy(self.rows)
        else:
            result = deepcopy(next(row for row in self.rows if url.endswith('/' + str(row['id']))))
        class Response:
            content = b'{}'
            def raise_for_status(inner):
                pass
            def json(inner):
                return result
        return Response()


def test_real_api_text_is_confirmed_by_exact_id_and_survives_restart():
    session = Session()
    client = GateFuturesClient(settings(), session)
    created = client.create_protection_order(client_id=TAG, **REQUEST)
    assert client.protection_identities.get(TAG)['exchange_id'] == str(created['id'])
    assert confirm_protection(client, created, client_id=TAG, **REQUEST)['initial']['text'] == 'api'
    restarted = GateFuturesClient(settings(), session)
    found = restarted.find_protection_by_client_id(TAG, 'BTC_USDT')
    assert found['_gate_verified_client_id'] == TAG
    assert is_system_protection(found)
    assert restarted.create_protection_order(client_id=TAG, **REQUEST)['reconciled']
    assert sum(method == 'POST' for method, _ in session.calls) == 1


@pytest.mark.parametrize('path,value', [
    (('id',), 7), (('status',), 'finished'), (('initial', 'contract'), 'ETH_USDT'),
    (('initial', 'size'), 4), (('initial', 'size'), -3),
    (('initial', 'is_reduce_only'), False), (('trigger', 'rule'), 1),
    (('trigger', 'price'), '91'), (('trigger', 'price_type'), 1),
    (('initial', 'auto_size'), 'close_short'), (('initial', 'price'), '89'),
    (('trigger', 'rule'), 'invalid'), (('trigger', 'price'), 'NaN'),
])
def test_same_id_with_wrong_protection_semantics_is_rejected(path, value):
    row = stop_row()
    target = row
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert not matches(row, client_id=TAG, exchange_id='2118705525796372480', **REQUEST)


def test_untagged_order_is_never_adopted_by_shape_only():
    assert not matches(stop_row(), client_id=TAG, **REQUEST)
    assert not is_system_protection(stop_row())


def test_confirmation_retries_reads_without_post_when_list_lags(monkeypatch):
    monkeypatch.setattr('gate_quant.protection_identity.time.sleep', lambda _: None)
    class Lagging:
        calls = 0
        def protection_orders(self, contract):
            self.calls += 1
            return [] if self.calls < 3 else [stop_row()]
    client = Lagging()
    assert confirm_protection(client, {'id': stop_row()['id']}, client_id=TAG, **REQUEST)
    assert client.calls == 3


def test_timeout_with_api_text_never_reposts_or_adopts_unknown_order():
    session = Session(timeout=True)
    client = GateFuturesClient(settings(), session)
    with pytest.raises(AmbiguousOrderError):
        client.create_protection_order(client_id=TAG, **REQUEST)
    restarted = GateFuturesClient(settings(), session)
    with pytest.raises(AmbiguousOrderError, match='not retried'):
        restarted.create_protection_order(client_id=TAG, **REQUEST)
    assert sum(method == 'POST' for method, _ in session.calls) == 1
    assert not is_system_protection(restarted.protection_orders()[0])


def test_timeout_with_preserved_text_recovers_without_duplicate():
    session = Session(timeout=True, echo=True)
    client = GateFuturesClient(settings(), session)
    assert client.create_protection_order(client_id=TAG, **REQUEST)['reconciled']
    assert client.protection_identities.get(TAG)['exchange_id']
    assert sum(method == 'POST' for method, _ in session.calls) == 1


@pytest.mark.parametrize('changes', [dict(environment='live', public_market_environment='live'),
                                     dict(api_key='other-account'), dict(settle='btc'),
                                     dict(testnet_base_url='https://other.invalid/api/v4')])
def test_identity_is_isolated_by_environment_account_endpoint_and_settle(changes):
    session = Session()
    client = GateFuturesClient(settings(), session)
    client.create_protection_order(client_id=TAG, **REQUEST)
    other = GateFuturesClient(replace(settings(), **changes), session)
    assert other.protection_identities.get(TAG) is None
    assert other.find_protection_by_client_id(TAG, 'BTC_USDT') is None
    assert not is_system_protection(other.protection_orders()[0])


def test_concurrent_same_client_id_sends_one_post():
    session = Session()
    clients = [GateFuturesClient(settings(), session) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: c.create_protection_order(client_id=TAG, **REQUEST), clients))
    assert len(results) == 2
    assert sum(method == 'POST' for method, _ in session.calls) == 1


def test_cannot_reuse_client_id_for_different_price():
    client = GateFuturesClient(settings(), Session())
    client.create_protection_order(client_id=TAG, **REQUEST)
    with pytest.raises(ValueError, match='different parameters'):
        client.create_protection_order(client_id=TAG, **{**REQUEST, 'trigger_price': '91'})


def test_lookup_uses_saved_id_when_open_list_lags():
    client = GateFuturesClient(settings(), Session())
    client.create_protection_order(client_id=TAG, **REQUEST)
    client.protection_orders = lambda contract=None: []
    assert client.find_protection_by_client_id(TAG, 'BTC_USDT')['_gate_verified_client_id'] == TAG


class ApiTextGate(FakeGate):
    def create_protection_order(self, **kwargs):
        row = super().create_protection_order(**kwargs)
        row['initial']['text'] = 'api'
        return {'id': row['id']}

    def find_protection_by_client_id(self, client_id, contract):
        return None

    def price_order(self, order_id):
        return next(row for row in self.protections if row['id'] == order_id)


def test_api_text_partial_fill_then_restart_does_not_flatten(tmp_path):
    journal = ExecutionJournal(tmp_path / 'execution.db')
    intent = prepared(journal)
    gate = ApiTextGate()
    assert reconcile_intent(gate, journal, intent, settings(), now_ms=2000)['status'] == 'partially_filled'
    gate.order_row.update(status='finished', left='0')
    gate.position['size'] = '10'
    restarted = ExecutionJournal(tmp_path / 'execution.db')
    result = reconcile_intent(gate, restarted, restarted.get(intent['client_id']), settings(), now_ms=3000)
    assert result['status'] == 'filled_protected'
    assert gate.created_rules == [2, 1, 2, 1]
    assert not gate.reductions


def test_api_text_decimal_position_is_protected(tmp_path):
    journal = ExecutionJournal(tmp_path / 'execution.db')
    gate = ApiTextGate(size='7.7', left='0', position_size='7.7')
    result = reconcile_intent(gate, journal, prepared(journal, requested='7.7'), settings(), now_ms=2000)
    assert result['status'] == 'filled_protected'
    assert not gate.reductions


def test_stop_replacement_confirms_api_text_without_unnecessary_restore():
    class Client(StopReplacementClient):
        def create_protection_order(self, **kwargs):
            row = super().create_protection_order(**kwargs)
            row['initial']['text'] = 'api'
            return {'id': row['id']}
    client = Client()
    result = GateTradingService(client, RiskLimits(1, 1, 1)).replace_stop_loss_safely(
        contract='SOL_USDT', position_size='17.9', mark_price='101.49',
        trigger_price='99.2', client_id='t-gate-slu-identity')
    assert result['replaced']
    assert len(client.created) == 1


def test_rollback_exit_reason_is_explicit():
    assert _exit_reason({'text': 't-gate-rclose-75130d2d'}) == '成交保护校验失败，安全回滚平仓'


def test_saved_id_with_changed_price_does_not_prove_ownership():
    session = Session()
    client = GateFuturesClient(settings(), session)
    client.create_protection_order(client_id=TAG, **REQUEST)
    session.rows[0]['trigger']['price'] = '91'
    assert not is_system_protection(client.protection_orders()[0])
    assert client.find_protection_by_client_id(TAG, 'BTC_USDT') is None


def test_api_text_take_profit_failure_cancels_known_stop_and_only_reduces_new_fill(tmp_path):
    session = Session()
    client = GateFuturesClient(settings(), session)
    original = client.create_protection_order
    def create(**kwargs):
        if kwargs['rule'] == 1:
            raise RuntimeError('TP rejected')
        return original(**kwargs)
    client.create_protection_order = create
    entry = FakeGate(size='4', left='0', position_size='14')
    client.order = entry.order
    client.find_by_client_id = entry.find_by_client_id
    client.positions = entry.positions
    client.create_order = entry.create_order
    def cancel(order_id):
        session.rows = [row for row in session.rows if str(row['id']) != order_id]
        return {'id': order_id, 'status': 'finished'}
    client.cancel_protection_order = cancel
    journal = ExecutionJournal(tmp_path / 'execution.db')
    result = reconcile_intent(client, journal, prepared(journal, baseline='10', requested='4'), settings(), now_ms=2000)
    assert result['status'] == 'take_profit_failed_flatten_attempted'
    assert not session.rows
    assert Decimal(str(entry.reductions[0]['size'])) == Decimal('-4')
