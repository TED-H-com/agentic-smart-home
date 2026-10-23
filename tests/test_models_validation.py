import pytest
from agentic_controller.models import TimeWindow
from pydantic import ValidationError


@pytest.mark.parametrize("after,before", [("10:00:00", "10:00:00"),
    ("10:00:00", "11:00:00"), ("22:00:00", "06:00:00")])
def test_time_window_preserves_home_assistant_supported_bounds(after, before):
    assert TimeWindow.model_validate({"after": after, "before": before}).model_dump() == {
        "after": after, "before": before}


@pytest.mark.parametrize("bound", ["after", "before"])
def test_time_window_rejects_invalid_clock_value(bound):
    with pytest.raises(ValidationError):
        TimeWindow.model_validate({"after": "10:00:00", "before": "11:00:00", bound: "24:00:00"})
