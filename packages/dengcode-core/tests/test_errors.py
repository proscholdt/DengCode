from __future__ import annotations

import pytest

from dengcode_core import errors


@pytest.mark.parametrize(
    "cls",
    [
        errors.AuthError,
        errors.NotFoundError,
        errors.TransientError,
        errors.RateLimitError,
        errors.UnsupportedOperation,
    ],
)
def test_adapter_errors_share_root(cls: type[Exception]) -> None:
    assert issubclass(cls, errors.AdapterError)
    assert issubclass(cls, errors.DengCodeError)


def test_rate_limit_is_transient_and_keeps_retry_after() -> None:
    err = errors.RateLimitError("429", retry_after_s=2.5)
    assert isinstance(err, errors.TransientError)
    assert err.retry_after_s == 2.5


@pytest.mark.parametrize(
    "cls",
    [
        errors.PermissionDeniedError,
        errors.CostLimitExceeded,
        errors.ConfigError,
        errors.BudgetExceeded,
        errors.ScopeError,
    ],
)
def test_policy_errors_are_not_adapter_errors(cls: type[Exception]) -> None:
    assert issubclass(cls, errors.DengCodeError)
    assert not issubclass(cls, errors.AdapterError)
