# CMN-C2-289 - Unit tests: the caller-input validation contract.
#
# Pure helpers, no framework involved. The interesting cases are the ones that
# look like they parse: NaN and the infinities are real floats and real JSON
# literals, and every comparison against NaN is False - so a value that reaches
# a range check unparsed passes it. Each field is probed with the whole matrix
# rather than a representative case, because the failure is per call site.

import pytest

from src.services.validation import (
    CallerInputError,
    bounded_int,
    budget_amount,
    finite_in_range,
    inert_identifier,
    inert_target_status,
    iso_date,
    render_text,
    setting_name,
)

_NON_FINITE = ["NaN", "nan", "Infinity", "-Infinity", "inf", float("nan"), float("inf"), float("-inf")]


class TestFiniteInRange:
    @pytest.mark.parametrize("value", _NON_FINITE)
    def test_non_finite_is_rejected(self, value):
        with pytest.raises(CallerInputError) as exc:
            finite_in_range(value, "amount", 0.0, 100.0)
        assert exc.value.field == "amount"

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_is_rejected(self, value):
        """isinstance(True, int) is True in Python, so bools reach numeric code."""
        with pytest.raises(CallerInputError):
            finite_in_range(value, "amount", 0.0, 100.0)

    @pytest.mark.parametrize("value", ["", "  ", "abc", None, [1], {"a": 1}, object()])
    def test_non_numeric_is_rejected(self, value):
        with pytest.raises(CallerInputError):
            finite_in_range(value, "amount", 0.0, 100.0)

    @pytest.mark.parametrize("value", [-0.001, 100.001, 1e30, -1e30])
    def test_out_of_range_is_rejected(self, value):
        with pytest.raises(CallerInputError):
            finite_in_range(value, "amount", 0.0, 100.0)

    @pytest.mark.parametrize("value,expected", [(0, 0.0), ("100", 100.0), ("1,000", None), (12.5, 12.5)])
    def test_valid_values_parse(self, value, expected):
        if expected is None:  # grouped digits within a wider range
            assert finite_in_range(value, "amount", 0.0, 10_000.0) == 1000.0
        else:
            assert finite_in_range(value, "amount", 0.0, 100.0) == expected

    def test_the_error_never_carries_the_value(self):
        with pytest.raises(CallerInputError) as exc:
            finite_in_range("zqx_secret_zqx", "amount", 0.0, 1.0)
        assert "zqx_secret_zqx" not in str(exc.value)


class TestBudgetAmount:
    @pytest.mark.parametrize("value", _NON_FINITE + [0, -1, 1e12, True, "abc"])
    def test_rejected(self, value):
        with pytest.raises(CallerInputError):
            budget_amount(value, "campaign.daily_budget")

    @pytest.mark.parametrize(
        "value,expected",
        [(350000, "350000"), ("350,000", "350000"), ("1234.56", "1234.56"), (0.5, "0.50")],
    )
    def test_accepted(self, value, expected):
        assert budget_amount(value, "campaign.daily_budget") == expected


class TestInertIdentifier:
    @pytest.mark.parametrize("value", ["1001", "C-42", "camp_01", "a" * 20])
    def test_accepted(self, value):
        assert inert_identifier(value, "campaign_id") == value

    @pytest.mark.parametrize(
        "value",
        ["../etc/passwd", "1001 1002", "a" * 21, "-lead", "id!", 1001, ["1001"], "<|im_start|>"],
    )
    def test_rejected(self, value):
        with pytest.raises(CallerInputError):
            inert_identifier(value, "campaign_id")

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_absent_is_not_malformed(self, value):
        assert inert_identifier(value, "campaign_id") == ""


class TestRenderText:
    @pytest.mark.parametrize(
        "value",
        ["Summer Sale 2026", "Q3 90d 1234 STAR 2026 ratio 0.123456", "秋のキャンペーン", "A&B (Tokyo) - #1"],
    )
    def test_ordinary_campaign_wording_is_accepted(self, value):
        assert render_text(value, "campaign.name", 100) == value

    @pytest.mark.parametrize(
        "value",
        [
            "a@b.example",  # a mail address needs '@'
            "<b>Sale</b>",  # markup needs '<' '>'
            "<|im_start|>",  # a control token needs '<' '|' '>'
            "[INST] go",  # and this one needs brackets
            'say "hi"',  # quotes would break the rendered payload
            "line\nbreak",  # control characters
            "a" * 101,  # over the cap
            42,
        ],
    )
    def test_rejected(self, value):
        with pytest.raises(CallerInputError):
            render_text(value, "campaign.name", 100)


class TestSettingName:
    @pytest.mark.parametrize("value", ["Objective", "daily_cap", "Bid Strategy v2"])
    def test_accepted(self, value):
        assert setting_name(value, "campaign.settings.name") == value

    @pytest.mark.parametrize("value", ["", "  ", "-lead", "a@b", "a" * 42, None, 7])
    def test_rejected(self, value):
        with pytest.raises(CallerInputError):
            setting_name(value, "campaign.settings.name")


class TestIsoDate:
    @pytest.mark.parametrize("value", ["2026-08-01", "2026-02-29"])
    def test_accepted_calendar_dates(self, value):
        # 2026 is not a leap year, so the second one must NOT parse.
        if value == "2026-02-29":
            with pytest.raises(CallerInputError):
                iso_date(value, "campaign.start_date")
        else:
            assert iso_date(value, "campaign.start_date") == value

    @pytest.mark.parametrize("value", ["2026-13-45", "26-08-01", "2026/08/01", "2026-08-1", 20260801])
    def test_rejected(self, value):
        with pytest.raises(CallerInputError):
            iso_date(value, "campaign.start_date")


class TestTargetStatus:
    @pytest.mark.parametrize("value,expected", [("active", "active"), ("PAUSED", "paused")])
    def test_accepted(self, value, expected):
        assert inert_target_status(value, "campaign.status") == expected

    @pytest.mark.parametrize("value", ["archived", "deleted", 1, ["active"]])
    def test_rejected(self, value):
        with pytest.raises(CallerInputError):
            inert_target_status(value, "campaign.status")


class TestBoundedInt:
    @pytest.mark.parametrize("value", _NON_FINITE + [0, 601, 12.5, True, "abc"])
    def test_rejected(self, value):
        with pytest.raises(CallerInputError):
            bounded_int(value, "timeout_s", 1, 600)

    @pytest.mark.parametrize("value,expected", [(30, 30), ("30", 30), (1, 1), (600, 600)])
    def test_accepted(self, value, expected):
        assert bounded_int(value, "timeout_s", 1, 600) == expected
