"""SEC answers a rate-limited request with 403 — and uses 403 for an absent index
too. Status alone cannot separate them, so `_is_throttle` reads the body.

Getting this wrong in either direction is a real failure:
  - treat a throttle as "not found" -> the filing is dropped with no counter and
    no log, the issuer never reaches `threshold`, and no outage guard can see it.
    The same silent-drop shape as the NaN price, one layer upstream.
  - treat a weekend 403 as a throttle -> every non-trading day stalls through four
    backoffs and then raises, instead of returning [].
"""
from scraper.edgar import _RETRY_STATUS, _is_throttle


class Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


THROTTLE_BODY = "Request Rate Threshold Exceeded. Please slow down."
UNDECLARED_BODY = "Your Request Originates from an Undeclared Automated Tool"
ABSENT_BODY = "<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"


def test_403_rate_limit_is_a_throttle():
    assert _is_throttle(Resp(403, THROTTLE_BODY)) is True


def test_403_undeclared_agent_is_a_throttle():
    assert _is_throttle(Resp(403, UNDECLARED_BODY)) is True


def test_403_for_an_absent_index_is_not_a_throttle():
    """A weekend/holiday date. Must stay swallowable as an empty index."""
    assert _is_throttle(Resp(403, ABSENT_BODY)) is False


def test_429_is_a_throttle_whatever_the_body():
    assert _is_throttle(Resp(429, "")) is True


def test_404_is_never_a_throttle():
    assert _is_throttle(Resp(404, THROTTLE_BODY)) is False


def test_200_is_never_a_throttle():
    assert _is_throttle(Resp(200, THROTTLE_BODY)) is False


def test_none_response_is_not_a_throttle():
    """`e.response` can be None on a transport-level error."""
    assert _is_throttle(None) is False


def test_unreadable_body_is_not_a_throttle():
    class Boom:
        status_code = 403

        @property
        def text(self):
            raise RuntimeError("body unreadable")
    assert _is_throttle(Boom()) is False


def test_403_is_not_blanket_retried():
    """It must be matched on body, not added to the status set — that would retry
    every weekend four times over."""
    assert 403 not in _RETRY_STATUS
