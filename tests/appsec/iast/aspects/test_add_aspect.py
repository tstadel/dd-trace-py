#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
import sys

import pytest

from ddtrace.appsec.iast import oce


def setup():
    oce._enabled = True


@pytest.mark.parametrize(
    "obj1, obj2",
    [
        (3.5, 3.3),
        # (complex(2, 1), complex(3, 4)),
        ("Hello ", "world"),
        ("🙀", "🙀"),
        (b"Hi", b""),
        (["a"], ["b"]),
        (bytearray("a", "utf-8"), bytearray("b", "utf-8")),
        (("a", "b"), ("c", "d")),
    ],
)
@pytest.mark.skipif(sys.version_info < (3, 6, 0), reason="Python 3.6+ only")
def test_add_aspect_successful(obj1, obj2):
    import ddtrace.appsec.iast._taint_tracking.aspects as ddtrace_aspects

    assert ddtrace_aspects.add_aspect(obj1, obj2) == obj1 + obj2


@pytest.mark.parametrize(
    "obj1, obj2",
    [(b"Hi", ""), ("Hi", b""), ({"a", "b"}, {"c", "d"}), (dict(), dict())],
)
@pytest.mark.skipif(sys.version_info < (3, 6, 0), reason="Python 3.6+ only")
def test_add_aspect_type_error(obj1, obj2):
    import ddtrace.appsec.iast._taint_tracking.aspects as ddtrace_aspects

    with pytest.raises(TypeError) as e_info1:
        obj1 + obj2

    with pytest.raises(TypeError) as e_info2:
        ddtrace_aspects.add_aspect(obj1, obj2)

    assert str(e_info2.value) == str(e_info1.value)


@pytest.mark.parametrize(
    "obj1, obj2, should_be_tainted",
    [
        (3.5, 3.3, False),
        (complex(2, 1), complex(3, 4), False),
        ("Hello ", "world", True),
        (b"bye ", b"".join((b"bye", b" ")), True),
        ("🙀", "".join(("🙀", "")), True),
        ("a", "a", True),
        (b"a", b"a", True),
        (b"Hi", b"", True),
        (b"Hi ", b" world", True),
        (["a"], ["b"], False),
        (bytearray(b"a"), bytearray(b"b"), True),
        (("a", "b"), ("c", "d"), False),
    ],
)
@pytest.mark.skipif(sys.version_info < (3, 6, 0), reason="Python 3.6+ only")
def test_add_aspect_tainting_left_hand(obj1, obj2, should_be_tainted):
    import ddtrace.appsec.iast._taint_tracking.aspects as ddtrace_aspects
    from ddtrace.appsec.iast._taint_dict import clear_taint_mapping
    from ddtrace.appsec.iast._taint_tracking import get_tainted_ranges
    from ddtrace.appsec.iast._taint_tracking import is_pyobject_tainted
    from ddtrace.appsec.iast._taint_tracking import setup
    from ddtrace.appsec.iast._taint_tracking import taint_pyobject
    from ddtrace.appsec.iast._taint_tracking import Source
    from ddtrace.appsec.iast._taint_tracking import OriginType

    setup(bytes.join, bytearray.join)
    clear_taint_mapping()

    if should_be_tainted:
        obj1 = taint_pyobject(obj1, Source("test_add_aspect_tainting_left_hand", obj1, OriginType.PARAMETER))
        if len(obj1):
            assert get_tainted_ranges(obj1)

    result = ddtrace_aspects.add_aspect(obj1, obj2)
    print("obj 1")
    print(obj1)
    print("obj 2")
    print(obj2)
    print("result")
    print(result)
    assert result == obj1 + obj2
    if isinstance(obj2, (bytes, str, bytearray)) and len(obj2):
        assert result is not obj1 + obj2
    assert is_pyobject_tainted(result) == should_be_tainted
    if should_be_tainted:
        assert get_tainted_ranges(result) == get_tainted_ranges(obj1)


@pytest.mark.parametrize(
    "obj1, obj2, should_be_tainted",
    [
        (3.5, 3.3, False),
        (complex(2, 1), complex(3, 4), False),
        ("Hello ", "world", True),
        (b"a", b"a", True),
        (b"bye ", b"bye ", True),
        ("🙀", "🙀", True),
        (b"Hi", b"", False),
         (["a"], ["b"], False),
        (bytearray("a", "utf-8"), bytearray("b", "utf-8"), True),
        (("a", "b"), ("c", "d"), False),
    ],
)
@pytest.mark.skipif(sys.version_info < (3, 6, 0), reason="Python 3.6+ only")
def test_add_aspect_tainting_right_hand(obj1, obj2, should_be_tainted):
    import ddtrace.appsec.iast._taint_tracking.aspects as ddtrace_aspects
    from ddtrace.appsec.iast._taint_dict import clear_taint_mapping
    from ddtrace.appsec.iast._taint_tracking import get_tainted_ranges
    from ddtrace.appsec.iast._taint_tracking import is_pyobject_tainted
    from ddtrace.appsec.iast._taint_tracking import setup
    from ddtrace.appsec.iast._taint_tracking import taint_pyobject
    from ddtrace.appsec.iast._taint_tracking import Source
    from ddtrace.appsec.iast._taint_tracking import OriginType
    from ddtrace.appsec.iast._taint_tracking import TaintRange

    setup(bytes.join, bytearray.join)
    clear_taint_mapping()
    if should_be_tainted:
        obj2 = taint_pyobject(obj2, Source("test_add_aspect_tainting_right_hand", repr(obj2), OriginType.PARAMETER))
        if len(obj2):
            assert get_tainted_ranges(obj2)

    result = ddtrace_aspects.add_aspect(obj1, obj2)

    assert result == obj1 + obj2

    assert is_pyobject_tainted(result) == should_be_tainted
    if isinstance(obj2, (str, bytes, bytearray)) and len(obj2):
        tainted_ranges = get_tainted_ranges(result)
        assert type(tainted_ranges) is list
        assert all(type(c) is TaintRange for c in tainted_ranges)
        assert (tainted_ranges != []) == should_be_tainted
        if should_be_tainted:
            assert len(tainted_ranges) == len(get_tainted_ranges(obj1)) + len(get_tainted_ranges(obj2))
