import json
import re
from typing import Optional
from typing import TYPE_CHECKING


# TypedDict was added to typing in python 3.8
try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict

from ddtrace.constants import _SINGLE_SPAN_SAMPLING_MAX_PER_SEC
from ddtrace.constants import _SINGLE_SPAN_SAMPLING_MAX_PER_SEC_NO_LIMIT
from ddtrace.constants import _SINGLE_SPAN_SAMPLING_MECHANISM
from ddtrace.constants import _SINGLE_SPAN_SAMPLING_RATE
from ddtrace.internal.compat import pattern_type
from ddtrace.internal.constants import MAX_UINT_64BITS as _MAX_UINT_64BITS
from ddtrace.internal.constants import SAMPLING_DECISION_TRACE_TAG_KEY
from ddtrace.internal.glob_matching import GlobMatcher
from ddtrace.internal.logger import get_logger
from ddtrace.internal.rate_limiter import RateLimiter
from ddtrace.internal.utils.cache import cachedmethod
from ddtrace.settings import _config as config


log = get_logger(__name__)

try:
    from json.decoder import JSONDecodeError
except ImportError:
    # handling python 2.X import error
    JSONDecodeError = ValueError  # type: ignore

if TYPE_CHECKING:  # pragma: no cover
    from typing import Any  # noqa
    from typing import Dict  # noqa
    from typing import List  # noqa
    from typing import Text  # noqa
    from typing import Tuple  # noqa

    from ddtrace.context import Context
    from ddtrace.span import Span

# Big prime number to make hashing better distributed
KNUTH_FACTOR = 1111111111111111111
MAX_SPAN_ID = 2 ** 64


class SamplingMechanism(object):
    DEFAULT = 0
    AGENT_RATE = 1
    REMOTE_RATE = 2
    TRACE_SAMPLING_RULE = 3
    MANUAL = 4
    APPSEC = 5
    REMOTE_RATE_USER = 6
    REMOTE_RATE_DATADOG = 7
    SPAN_SAMPLING_RULE = 8


# Use regex to validate trace tag value
TRACE_TAG_RE = re.compile(r"^-([0-9])$")


SpanSamplingRules = TypedDict(
    "SpanSamplingRules",
    {
        "name": str,
        "service": str,
        "sample_rate": float,
        "max_per_second": int,
    },
    total=False,
)

SPAN_SAMPLING_JSON_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "anyOf": [
            {"properties": {"service": {"type": "string"}}, "required": ["service"]},
            {"properties": {"resource": {"type": "string"}}, "required": ["resource"]},
            {"properties": {"name": {"type": "string"}}, "required": ["name"]},
            {"properties": {"tags": {"type": "object"}}, "required": ["tags"]},
        ],
        "properties": {"max_per_second": {"type": "integer"}, "sample_rate": {"type": "number"}},
    },
}


def _set_trace_tag(
    context,  # type: Context
    sampling_mechanism,  # type: int
):
    # type: (...) -> Optional[Text]

    value = "-%d" % sampling_mechanism

    context._meta[SAMPLING_DECISION_TRACE_TAG_KEY] = value

    return value


def _unset_trace_tag(
    context,  # type: Context
):
    # type: (...) -> Optional[Text]
    if SAMPLING_DECISION_TRACE_TAG_KEY not in context._meta:
        return None

    value = context._meta[SAMPLING_DECISION_TRACE_TAG_KEY]
    del context._meta[SAMPLING_DECISION_TRACE_TAG_KEY]
    return value


def validate_sampling_decision(
    meta,  # type: Dict[str, str]
):
    # type: (...) -> Dict[str, str]
    value = meta.get(SAMPLING_DECISION_TRACE_TAG_KEY)
    if value:
        # Skip propagating invalid sampling mechanism trace tag
        if TRACE_TAG_RE.match(value) is None:
            del meta[SAMPLING_DECISION_TRACE_TAG_KEY]
            meta["_dd.propagation_error"] = "decoding_error"
            log.warning("failed to decode _dd.p.dm: %r", value, exc_info=True)
    return meta


def update_sampling_decision(
    context,  # type: Context
    sampling_mechanism,  # type: int
    sampled,  # type: bool
):
    # type: (...) -> Optional[Text]
    # When sampler keeps trace, we need to set sampling decision trace tag.
    # If sampler rejects trace, we need to remove sampling decision trace tag to avoid unnecessary propagation.
    if sampled and sampling_mechanism != SamplingMechanism.MANUAL:
        return _set_trace_tag(context, sampling_mechanism)
    else:
        return _unset_trace_tag(context)


class SpanSamplingRule:
    """A span sampling rule to evaluate and potentially tag each span upon finish."""

    __slots__ = (
        "_service_matcher",
        "_name_matcher",
        "_resource_matcher",
        "_tag_value_matchers",
        "_sample_rate",
        "_max_per_second",
        "_sampling_id_threshold",
        "_limiter",
        "_matcher",
    )

    def __init__(
        self,
        sample_rate,  # type: float
        max_per_second,  # type: int
        service=None,  # type: Optional[str]
        name=None,  # type: Optional[str]
        resource=None,  # type: Optional[str]
        tags=None,  # type: Optional[dict]
    ):
        self._sample_rate = sample_rate
        self._sampling_id_threshold = self._sample_rate * MAX_SPAN_ID

        self._max_per_second = max_per_second
        self._limiter = RateLimiter(max_per_second)

        # we need to create matchers for the service and/or name pattern provided
        self._service_matcher = GlobMatcher(service) if service is not None else None
        self._name_matcher = GlobMatcher(name) if name is not None else None
        self._resource_matcher = GlobMatcher(resource) if resource is not None else None
        self._tag_value_matchers = {k: GlobMatcher(v) for k, v in tags.items()} if tags is not None else {}

    def sample(self, span):
        # type: (Span) -> bool
        if self._sample(span):
            if self._limiter.is_allowed(span.start_ns):
                self.apply_span_sampling_tags(span)
                return True
        return False

    def _sample(self, span):
        # type: (Span) -> bool
        if self._sample_rate == 1:
            return True
        elif self._sample_rate == 0:
            return False

        return ((span.span_id * KNUTH_FACTOR) % MAX_SPAN_ID) <= self._sampling_id_threshold

    def match(self, span):
        # type: (Span) -> bool
        """Determines if the span's service and name match the configured patterns"""
        name = span.name
        service = span.service
        resource = span.resource
        tags = span.get_tags()

        # If a span lacks these fields, we can't match on it
        if service is None and name is None and not resource and not tags:
            return False

        # Default to True in the absence of a rule
        # For whichever rules it does have, it will attempt to match on them
        service_match = True
        name_match = True
        resource_match = True
        tag_match = True

        if self._service_matcher:
            if service is None:
                return False
            else:
                service_match = self._service_matcher.match(service)

        if self._name_matcher:
            if name is None:
                return False
            else:
                name_match = self._name_matcher.match(name)

        if self._resource_matcher:
            if resource is None:
                return False
            else:
                resource_match = self._resource_matcher.match(resource)

        if self._tag_value_matchers:
            if tags is None:
                return False
            else:
                for tag_key in self._tag_value_matchers.keys():
                    value = span.get_tag(tag_key)
                    if value is not None:
                        tag_match = self._tag_value_matchers[tag_key].match(value)
                    else:
                        # if we don't match with all specified tags for a rule, it's not a match
                        return False

        return service_match and name_match and resource_match and tag_match

    def apply_span_sampling_tags(self, span):
        # type: (Span) -> None
        span.set_metric(_SINGLE_SPAN_SAMPLING_MECHANISM, SamplingMechanism.SPAN_SAMPLING_RULE)
        span.set_metric(_SINGLE_SPAN_SAMPLING_RATE, self._sample_rate)
        # Only set this tag if it's not the default -1
        if self._max_per_second != _SINGLE_SPAN_SAMPLING_MAX_PER_SEC_NO_LIMIT:
            span.set_metric(_SINGLE_SPAN_SAMPLING_MAX_PER_SEC, self._max_per_second)


def get_span_sampling_rules():
    # type: () -> List[SpanSamplingRule]
    json_rules = _get_span_sampling_json()
    sampling_rules = []
    for rule in json_rules:
        # If sample_rate not specified default to 100%
        sample_rate = rule.get("sample_rate", 1.0)
        service = rule.get("service")
        name = rule.get("name")
        resource = rule.get("resource")
        tags = rule.get("tags")

        if not service and not name:
            raise ValueError("Sampling rules must supply at least 'service' or 'name', got {}".format(json.dumps(rule)))

        # If max_per_second not specified default to no limit
        max_per_second = rule.get("max_per_second", _SINGLE_SPAN_SAMPLING_MAX_PER_SEC_NO_LIMIT)
        if service:
            _check_unsupported_pattern(service)
        if name:
            _check_unsupported_pattern(name)

        try:
            sampling_rule = SpanSamplingRule(
                sample_rate=sample_rate,
                service=service,
                name=name,
                resource=resource,
                tags=tags,
                max_per_second=max_per_second,
            )
        except Exception as e:
            raise ValueError("Error creating single span sampling rule {}: {}".format(json.dumps(rule), e))
        sampling_rules.append(sampling_rule)
    return sampling_rules


def _get_span_sampling_json():
    # type: () -> List[Dict[str, Any]]
    env_json_rules = _get_env_json()
    file_json_rules = _get_file_json()

    if env_json_rules and file_json_rules:
        log.warning(
            (
                "DD_SPAN_SAMPLING_RULES and DD_SPAN_SAMPLING_RULES_FILE detected. "
                "Defaulting to DD_SPAN_SAMPLING_RULES value."
            )
        )
        return env_json_rules
    return env_json_rules or file_json_rules or []


def _get_file_json():
    # type: () -> Optional[List[Dict[str, Any]]]
    file_json_raw = config._sampling_rules_file
    if file_json_raw:
        with open(file_json_raw) as f:
            return _load_span_sampling_json(f.read())
    return None


def _get_env_json():
    # type: () -> Optional[List[Dict[str, Any]]]
    env_json_raw = config._sampling_rules
    if env_json_raw:
        return _load_span_sampling_json(env_json_raw)
    return None


def _load_span_sampling_json(raw_json_rules):
    # type: (str) -> List[Dict[str, Any]]
    try:
        json_rules = json.loads(raw_json_rules)
        if not isinstance(json_rules, list):
            raise TypeError("DD_SPAN_SAMPLING_RULES is not list, got %r" % json_rules)
    except JSONDecodeError:
        raise ValueError("Unable to parse DD_SPAN_SAMPLING_RULES=%r" % raw_json_rules)

    return json_rules


def _check_unsupported_pattern(string):
    # type: (str) -> None
    # We don't support pattern bracket expansion or escape character
    unsupported_chars = {"[", "]", "\\"}
    for char in string:
        if char in unsupported_chars:
            raise ValueError("Unsupported Glob pattern found, character:%r is not supported" % char)


def is_single_span_sampled(span):
    # type: (Span) -> bool
    return span.get_metric(_SINGLE_SPAN_SAMPLING_MECHANISM) == SamplingMechanism.SPAN_SAMPLING_RULE


class SamplingRule:
    """
    Definition of a sampling rule used by :class:`DatadogSampler` for applying a sample rate on a span
    """

    NO_RULE = object()

    def __init__(
        self,
        sample_rate,  # type: float
        service=NO_RULE,  # type: Any
        name=NO_RULE,  # type: Any
        resource=NO_RULE,  # type: Any
        tags=NO_RULE,  # type: Any
        target_span="root",  # type: str
    ):
        # type: (...) -> None
        """
        Configure a new :class:`SamplingRule`

        .. code:: python

            DatadogSampler([
                # Sample 100% of any trace
                SamplingRule(sample_rate=1.0),

                # Sample no healthcheck traces
                SamplingRule(sample_rate=0, name='flask.request'),

                # Sample all services ending in `-db` based on a regular expression
                SamplingRule(sample_rate=0.5, service=re.compile('-db$')),

                # Sample based on service name using custom function
                SamplingRule(sample_rate=0.75, service=lambda service: 'my-app' in service),
            ])

        :param sample_rate: The sample rate to apply to any matching spans
        :type sample_rate: :obj:`float` greater than or equal to 0.0 and less than or equal to 1.0
        :param service: Rule to match the `span.service` on, default no rule defined
        :type service: :obj:`object` to directly compare, :obj:`function` to evaluate, or :class:`re.Pattern` to match
        :param name: Rule to match the `span.name` on, default no rule defined
        :type name: :obj:`object` to directly compare, :obj:`function` to evaluate, or :class:`re.Pattern` to match
        """
        # Enforce sample rate constraints
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError(
                (
                    "SamplingRule(sample_rate={}) must be greater than or equal to 0.0 and less than or equal to 1.0"
                ).format(sample_rate)
            )
        self._service_matcher = GlobMatcher(service) if service is not None and type(service) is str else None
        self._name_matcher = GlobMatcher(name) if name is not None and type(name) is str else None
        self._resource_matcher = GlobMatcher(resource) if resource is not None and type(resource) is str else None
        self._tag_value_matchers = (
            {k: GlobMatcher(v) for k, v in tags.items()} if tags is not None and type(tags) is dict else {}
        )

        self.sample_rate = sample_rate
        self.service = service
        self.name = name
        self.tags = tags
        self.resource = resource
        # default root
        self.target_span = target_span

    @property
    def sample_rate(self):
        # type: () -> float
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, sample_rate):
        # type: (float) -> None
        self._sample_rate = sample_rate
        self._sampling_id_threshold = sample_rate * _MAX_UINT_64BITS

    def _pattern_matches(self, prop, pattern):
        # If the rule is not set, then assume it matches
        # DEV: Having no rule and being `None` are different things
        #   e.g. ignoring `span.service` vs `span.service == None`
        if pattern is self.NO_RULE:
            return True
        # If the pattern is callable (e.g. a function) then call it passing the prop
        #   The expected return value is a boolean so cast the response in case it isn't
        if callable(pattern):
            try:
                return bool(pattern(prop))
            except Exception:
                log.warning("%r pattern %r failed with %r", self, pattern, prop, exc_info=True)
                # Their function failed to validate, assume it is a False
                return False

        # The pattern is a regular expression and the prop is a string
        if isinstance(pattern, pattern_type):
            try:
                return bool(pattern.match(str(prop)))
            except (ValueError, TypeError):
                # This is to guard us against the casting to a string (shouldn't happen, but still)
                log.warning("%r pattern %r failed with %r", self, pattern, prop, exc_info=True)
                return False

        # Exact match on the values
        if prop == pattern:
            return True

    @cachedmethod()
    def _matches(self, key):
        # type: (Tuple[Optional[str], str, Optional[str]]) -> bool
        service, name, resource = key
        for prop, pattern in [(service, self.service), (name, self.name), (resource, self.resource)]:
            if not self._pattern_matches(prop, pattern):
                return False
        else:
            return True

    def matches(self, span):
        # type: (Span) -> bool
        """
        Return if this span matches this rule

        :param span: The span to match against
        :type span: :class:`ddtrace.span.Span`
        :returns: Whether this span matches or not
        :rtype: :obj:`bool`
        """
        # if we're just interested in root spans and this isn't one, then return False
        if self.target_span == "root":
            if span.parent_id is not None:
                return False

        glob_match = self.glob_matches(span)
        # we return early here because _matches() doesn't support tags
        if self.tags is not self.NO_RULE:
            return glob_match
        # self._matches exists to maintain legacy pattern values such as regex and functions
        return self._matches((span.service, span.name, span.resource))

    def glob_matches(self, span):
        # type: (Span) -> bool
        name = span.name
        service = span.service
        resource = span.resource
        tags = span.get_tags()

        # Default to True in the absence of a rule
        # For whichever rules it does have, it will attempt to match on them
        service_match = True
        name_match = True
        resource_match = True
        tag_match = True

        if self._service_matcher:
            if service is None:
                return False
            else:
                service_match = self._service_matcher.match(service)

        if self._name_matcher:
            if name is None:
                return False
            else:
                name_match = self._name_matcher.match(name)

        if self._resource_matcher:
            if resource is None:
                return False
            else:
                resource_match = self._resource_matcher.match(resource)

        if self._tag_value_matchers:
            tag_match = self.tag_match(tags)

        return service_match and name_match and resource_match and tag_match

    def tag_match(self, tags):
        if tags is None:
            return False

        tag_match = False
        for tag_key in self._tag_value_matchers.keys():
            value = tags.get(tag_key)
            if value is not None:
                tag_match = self._tag_value_matchers[tag_key].match(value)
            else:
                # if we don't match with all specified tags for a rule, it's not a match
                return False
        return tag_match

    def sample(self, span):
        # type: (Span) -> bool
        """
        Return if this rule chooses to sample the span

        :param span: The span to sample against
        :type span: :class:`ddtrace.span.Span`
        :returns: Whether this span was sampled
        :rtype: :obj:`bool`
        """
        if self.sample_rate == 1:
            return True
        elif self.sample_rate == 0:
            return False

        return ((span._trace_id_64bits * KNUTH_FACTOR) % _MAX_UINT_64BITS) <= self._sampling_id_threshold

    def _no_rule_or_self(self, val):
        return "NO_RULE" if val is self.NO_RULE else val

    def __repr__(self):
        return "{}(sample_rate={!r}, service={!r}, name={!r})".format(
            self.__class__.__name__,
            self.sample_rate,
            self._no_rule_or_self(self.service),
            self._no_rule_or_self(self.name),
        )

    __str__ = __repr__

    def __eq__(self, other):
        # type: (Any) -> bool
        if not isinstance(other, SamplingRule):
            raise TypeError("Cannot compare SamplingRule to {}".format(type(other)))

        return self.sample_rate == other.sample_rate and self.service == other.service and self.name == other.name
