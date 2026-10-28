import pytest
from autonomy import tick


@pytest.mark.parametrize('status', [
    'ok', 'run_timeout', 'preempted', 'yielded_for_memory',
    'yielded_user_recent', 'yielded_outside_hours', 'busy', 'blocked', 'stopped',
])
def test_design_outcomes_exit_zero(status):
    assert tick.is_normal_status(status) is True


@pytest.mark.parametrize('status', ['failed', 'stuck_run', 'gateway_unavailable', None])
def test_exceptional_outcomes_exit_nonzero(status):
    assert tick.is_normal_status(status) is False
