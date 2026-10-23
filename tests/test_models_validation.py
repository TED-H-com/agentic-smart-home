import pytest
from agentic_controller.models import TimeWindow
from pydantic import ValidationError


def test_time_window_start_end_must_differ():
    with pytest.raises(ValidationError, match="時間區段的開始與結束不能相同"):
        TimeWindow.model_validate({"after": "10:00:00", "before": "10:00:00"})

    # Valid time window
    tw = TimeWindow.model_validate({"after": "10:00:00", "before": "11:00:00"})
    assert tw.after == "10:00:00"
    assert tw.before == "11:00:00"
