import pytest


@pytest.fixture(autouse=True)
def isolated_protection_identity_store(tmp_path, monkeypatch):
    import gate_quant.protection_identity as identity
    monkeypatch.setattr(identity, 'STORE_PATH', tmp_path / 'protection-orders.db')
