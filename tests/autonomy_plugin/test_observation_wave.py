import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[2] / "plugins"))
from autonomy.error_classifier import E_ENDPOINT_MISCONFIGURED, E2_PROVIDER_EXHAUSTED, _is_refusal, should_circuit_count


def test_404_is_endpoint_configuration_not_provider_exhaustion():
    kind, _, _ = __import__('autonomy.error_classifier', fromlist=['classify']).classify('', {'http_status': 404})
    assert kind == E_ENDPOINT_MISCONFIGURED
    assert kind != E2_PROVIDER_EXHAUSTED
    assert should_circuit_count(kind) is False


def test_refusal_with_substantive_remainder_is_not_refusal():
    english = ("I'm sorry I can't answer that specific question, but here is some "
               "relevant analysis: the system shows three distinct patterns of behavior "
               "under high load conditions, specifically the observed traces diverge.")
    chinese = ('抱歉,我不能执行。这里是实质分析结果：系统表现出三个可复现的瓶颈，'
               '首先是请求排队时间增加，其次是数据库写入延迟变长，最后是服务端重试次数上升。'
               '这些现象需要结合时间序列、日志与独立请求样本进一步验证。')
    assert _is_refusal(english) is False
    assert _is_refusal(chinese) is False
    assert _is_refusal('抱歉,我不能执行。') is True
    assert _is_refusal("I'm sorry, I cannot help with this.") is True
