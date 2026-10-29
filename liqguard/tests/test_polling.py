from liqguard.telegram import polling


def test_session_uses_short_request_timeout():
    assert polling.make_session().timeout == polling.REQUEST_TIMEOUT


def test_polling_kwargs_match_dispatcher_signature():
    import inspect

    from aiogram import Dispatcher

    params = inspect.signature(Dispatcher.start_polling).parameters
    for name in polling.polling_kwargs():
        assert name in params


def test_stalled_connection_recovers_well_under_a_minute():
    assert polling.worst_case_stall_seconds() < 30
    assert polling.BACKOFF.max_delay <= 5
