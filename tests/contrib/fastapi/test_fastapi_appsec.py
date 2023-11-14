import json

from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
import pytest

import ddtrace
from ddtrace.appsec._constants import APPSEC
from ddtrace.appsec._constants import SPAN_DATA_NAMES
from ddtrace.contrib.fastapi import patch as fastapi_patch
from ddtrace.contrib.fastapi import unpatch as fastapi_unpatch
from ddtrace.ext import http
from ddtrace.internal import constants
from ddtrace.internal import core
from tests.appsec.appsec.test_processor import _IP
from tests.appsec.appsec.test_processor import RULES_GOOD_PATH
from tests.appsec.appsec.test_processor import RULES_SRB
from tests.appsec.appsec.test_processor import RULES_SRB_METHOD
from tests.appsec.appsec.test_processor import RULES_SRB_RESPONSE
from tests.utils import DummyTracer
from tests.utils import TracerSpanContainer
from tests.utils import override_env
from tests.utils import override_global_config

from . import app as fastapi_app


def _aux_appsec_prepare_tracer(tracer, appsec_enabled=True):
    tracer._asm_enabled = appsec_enabled
    # Hack: need to pass an argument to configure so that the processors are recreated
    tracer.configure(api_version="v0.4")


@pytest.fixture
def tracer():
    original_tracer = ddtrace.tracer
    tracer = DummyTracer()
    ddtrace.tracer = tracer
    fastapi_patch()
    yield tracer
    ddtrace.tracer = original_tracer
    fastapi_unpatch()


@pytest.fixture
def app(tracer):
    return fastapi_app.get_app()


@pytest.fixture
def client(tracer, app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def test_spans(tracer):
    container = TracerSpanContainer(tracer)
    yield container
    container.reset()


def get_response_body(response):
    return response.text


# IP Blocking


def fastapi_ipblock_nomatch_200_json(app, client, tracer, test_spans, ip):
    @app.get("/")
    def route():
        return "OK"

    _aux_appsec_prepare_tracer(tracer)
    for ip in [_IP.MONITORED, _IP.BYPASS, _IP.DEFAULT]:
        with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_GOOD_PATH)):
            resp = client.get("/", headers={"X-Real-Ip": ip})
            root_span = test_spans.pop_traces()[0][0]
            assert resp.status_code == 200
            assert not core.get_item("http.request.blocked", span=root_span)


def test_ipblock_nomatch_200_bypass(app, client, tracer, test_spans):
    fastapi_ipblock_nomatch_200_json(app, client, tracer, test_spans, _IP.BYPASS)


def test_ipblock_nomatch_200_monitor(app, client, tracer, test_spans):
    fastapi_ipblock_nomatch_200_json(app, client, tracer, test_spans, _IP.MONITORED)


def test_ipblock_nomatch_200_default(app, client, tracer, test_spans):
    fastapi_ipblock_nomatch_200_json(app, client, tracer, test_spans, _IP.DEFAULT)


def test_ipblock_match_403_json(app, client, tracer, test_spans):
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_GOOD_PATH)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/foobar", headers={"X-Real-Ip": _IP.BLOCKED})
        assert resp.status_code == 403
        assert get_response_body(resp) == constants.BLOCKED_RESPONSE_JSON
        root_span = test_spans.pop_traces()[0][0]
        assert root_span.get_tag(http.STATUS_CODE) == "403"
        assert root_span.get_tag(http.URL) == "http://testserver/foobar"
        assert root_span.get_tag(http.METHOD) == "GET"
        assert root_span.get_tag(http.USER_AGENT) == "testclient"
        assert root_span.get_tag(SPAN_DATA_NAMES.RESPONSE_HEADERS_NO_COOKIES + ".content-type") == "text/json"
        assert root_span.get_tag(APPSEC.JSON)
        loaded = json.loads(root_span.get_tag(APPSEC.JSON))
        assert loaded["triggers"][0]["rule"]["id"] == "blk-001-001"
        assert root_span.get_tag("appsec.event") == "true"
        assert root_span.get_tag("appsec.blocked") == "true"


# Request Blocking on Request


def test_request_suspicious_request_block_match_query_value(app, client, tracer, test_spans):
    @app.get("/index.html")
    def test_route(toto: str = ""):
        return PlainTextResponse(f"Ok: {toto}")

    # value xtrace must be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/index.html?toto=xtrace")
        assert resp.status_code == 403
        assert get_response_body(resp) == constants.BLOCKED_RESPONSE_JSON
        root_span = test_spans.pop_traces()[0][0]
        loaded = json.loads(root_span.get_tag(APPSEC.JSON))
        assert [t["rule"]["id"] for t in loaded["triggers"]] == ["tst-037-001"]
        assert root_span.get_tag(http.STATUS_CODE) == "403"
        assert root_span.get_tag(http.URL) == "http://testserver/index.html?toto=xtrace"
        assert root_span.get_tag(http.METHOD) == "GET"
        assert root_span.get_tag(http.USER_AGENT).startswith("testclient")
        assert root_span.get_tag(SPAN_DATA_NAMES.RESPONSE_HEADERS_NO_COOKIES + ".content-type") == "text/json"
    # other values must not be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/index.html?toto=ytrace")
        assert resp.status_code == 200
        assert get_response_body(resp) == "Ok: ytrace"
    # appsec disabled must not block
    with override_global_config(dict(_asm_enabled=False)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer, appsec_enabled=False)
        resp = client.get("/index.html?toto=xtrace")
        assert resp.status_code == 200
        assert get_response_body(resp) == "Ok: xtrace"


def test_request_suspicious_request_block_match_uri(app, client, tracer, test_spans):
    @app.get("/.git")
    def test_route():
        return PlainTextResponse("git file")

    # value .git must be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/.git")
        assert resp.status_code == 403
        assert get_response_body(resp) == constants.BLOCKED_RESPONSE_JSON
        root_span = test_spans.pop_traces()[0][0]
        loaded = json.loads(root_span.get_tag(APPSEC.JSON))
        assert [t["rule"]["id"] for t in loaded["triggers"]] == ["tst-037-002"]
        assert root_span.get_tag(http.STATUS_CODE) == "403"
        assert root_span.get_tag(http.URL) == "http://testserver/.git"
        assert root_span.get_tag(http.METHOD) == "GET"
        assert root_span.get_tag(http.USER_AGENT).startswith("testclient")
        assert root_span.get_tag(SPAN_DATA_NAMES.RESPONSE_HEADERS_NO_COOKIES + ".content-type") == "text/json"
    # other values must not be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/legit")
        assert resp.status_code == 404
    # appsec disabled must not block
    with override_global_config(dict(_asm_enabled=False)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer, appsec_enabled=False)
        resp = client.get("/.git")
        assert resp.status_code == 200
        assert get_response_body(resp) == "git file"
    # we must block with uri.raw not containing scheme or netloc
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/we_should_block")
        assert resp.status_code == 403
        assert get_response_body(resp) == constants.BLOCKED_RESPONSE_JSON
        root_span = test_spans.pop_traces()[0][0]
        loaded = json.loads(root_span.get_tag(APPSEC.JSON))
        assert [t["rule"]["id"] for t in loaded["triggers"]] == ["tst-037-010"]


def test_request_suspicious_request_block_match_header(app, client, tracer, test_spans):
    @app.get("/")
    def test_route():
        return PlainTextResponse("OK")

    # value 01972498723465 must be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)

        resp = client.get("/", headers={"User-Agent": "01972498723465"})
        assert resp.status_code == 403
        assert get_response_body(resp) == constants.BLOCKED_RESPONSE_JSON
        root_span = test_spans.pop_traces()[0][0]
        loaded = json.loads(root_span.get_tag(APPSEC.JSON))
        assert [t["rule"]["id"] for t in loaded["triggers"]] == ["tst-037-004"]
    # other values must not be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)

        resp = client.get("/", headers={"User-Agent": "31972498723467"})
        assert resp.status_code == 200
    # appsec disabled must not block
    with override_global_config(dict(_asm_enabled=False)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer, appsec_enabled=False)

        resp = client.get("/", headers={"User-Agent": "01972498723465"})
        assert resp.status_code == 200


def test_request_suspicious_request_block_match_method(app, client, tracer, test_spans):
    @app.get("/")
    @app.post("/")
    def test_route():
        return PlainTextResponse("OK")

    # GET must be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB_METHOD)):
        _aux_appsec_prepare_tracer(tracer)

        resp = client.get("/")
        assert resp.status_code == 403
        assert get_response_body(resp) == constants.BLOCKED_RESPONSE_JSON
        root_span = test_spans.pop_traces()[0][0]
        loaded = json.loads(root_span.get_tag(APPSEC.JSON))
        assert [t["rule"]["id"] for t in loaded["triggers"]] == ["tst-037-006"]
    # POST must not be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB_METHOD)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.post("/", data="post data")
        assert resp.status_code == 200
    # GET must pass if appsec disabled
    with override_global_config(dict(_asm_enabled=False)), override_env(dict(DD_APPSEC_RULES=RULES_SRB_METHOD)):
        _aux_appsec_prepare_tracer(tracer, appsec_enabled=False)

        resp = client.get("/")
        assert resp.status_code == 200


def test_request_suspicious_request_block_match_cookies(app, client, tracer, test_spans):
    @app.get("/")
    def test_route():
        return PlainTextResponse("OK")

    # value jdfoSDGFkivRG_234 must be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/", cookies={"keyname": "jdfoSDGFkivRG_234"})
        assert resp.status_code == 403
        assert get_response_body(resp) == constants.BLOCKED_RESPONSE_JSON
        root_span = test_spans.pop_traces()[0][0]
        loaded = json.loads(root_span.get_tag(APPSEC.JSON))
        assert [t["rule"]["id"] for t in loaded["triggers"]] == ["tst-037-008"]
    # other value must not be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/", cookies={"keyname": "jdfoSDGFHappykivRG_234"})
        assert resp.status_code == 200
    # appsec disabled must not block
    with override_global_config(dict(_asm_enabled=False)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer, appsec_enabled=False)
        resp = client.get("/", cookies={"keyname": "jdfoSDGFkivRG_234"})
        assert resp.status_code == 200


def test_request_suspicious_request_block_match_path_params(app, client, tracer, test_spans):
    @app.get("/params/{item}")
    def dynamic_url(item):
        return PlainTextResponse(item)

    # value AiKfOeRcvG45 must be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/params/AiKfOeRcvG45")
        assert resp.status_code == 403
        assert get_response_body(resp) == constants.BLOCKED_RESPONSE_JSON
        root_span = test_spans.pop_traces()[0][0]
        loaded = json.loads(root_span.get_tag(APPSEC.JSON))
        assert [t["rule"]["id"] for t in loaded["triggers"]] == ["tst-037-007"]
    # other values must not be blocked
    with override_global_config(dict(_asm_enabled=True)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer)
        resp = client.get("/params/Anything")
        assert resp.status_code == 200
        assert get_response_body(resp) == "Anything"
    # appsec disabled must not block
    with override_global_config(dict(_asm_enabled=False)), override_env(dict(DD_APPSEC_RULES=RULES_SRB)):
        _aux_appsec_prepare_tracer(tracer, appsec_enabled=False)
        resp = client.get("/params/AiKfOeRcvG45")
        assert resp.status_code == 200
        assert get_response_body(resp) == "AiKfOeRcvG45"
