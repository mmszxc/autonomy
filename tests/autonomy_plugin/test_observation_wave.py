import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[2] / "plugins"))
from autonomy.error_classifier import E_ENDPOINT_MISCONFIGURED, E2_PROVIDER_EXHAUSTED, classify_error_text, should_circuit_count


def test_404_is_endpoint_configuration_not_provider_exhaustion():
    kind, _, _ = __import__('autonomy.error_classifier', fromlist=['classify']).classify('', {'http_status': 404})
    assert kind == E_ENDPOINT_MISCONFIGURED
    assert kind != E2_PROVIDER_EXHAUSTED
    assert should_circuit_count(kind) is False


def test_refusal_with_substantive_remainder_is_not_refusal():
    kind, _ = classify_error_text('抱歉，我不能执行。这里是实质分析结果：答案为42。')
    assert kind != 'refusal'
