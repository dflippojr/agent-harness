import pytest

from harness.warmup import READY, ModelWarmer


@pytest.fixture(autouse=True)
def model_awake(monkeypatch):
    """Scripted-model tests have no llama-server to ask whether the model is asleep."""
    async def state(self, model):
        return READY
    monkeypatch.setattr(ModelWarmer, "state", state)
