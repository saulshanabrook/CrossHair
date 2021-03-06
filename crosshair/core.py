
# *** Not prioritized for v0 ***
# TODO: increase test coverage: TypeVar('T', int, str) vs bounded type vars
# TODO: enforcement wrapper with preconditions that error: problematic for implies()
# TODO: do not claim "unable to meet preconditions" when we have path timeouts
# TODO: consider raises conditions (guaranteed to raise, guaranteed to not raise?)
# TODO: precondition strengthening ban (Subclass constraint rule)
# TODO: double-check counterexamples
# TODO: contracts for builtins
# TODO: standard library contracts
# TODO: identity-aware repr'ing for result messages
# TODO: mutating symbolic Callables?
# TODO: contracts on the contracts of function and object inputs/outputs?
# TODO: conditions on Callable arguments/return values

from dataclasses import dataclass, replace
from typing import *
import ast
import builtins
import collections
import copy
import enum
import inspect
import io
import itertools
import functools
import linecache
import os.path
import sys
import time
import traceback
import types
import typing

import forbiddenfruit  # type: ignore
import typing_inspect  # type: ignore
import z3  # type: ignore

from crosshair import dynamic_typing
from crosshair.condition_parser import get_fn_conditions, get_class_conditions, ConditionExpr, Conditions, fn_globals
from crosshair.enforce import EnforcedConditions, PostconditionFailed
from crosshair.statespace import TrackingStateSpace, StateSpace, HeapRef, SnapshotRef, SearchTreeNode, model_value_to_python, VerificationStatus, IgnoreAttempt, SinglePathNode, CallAnalysis, MessageType, AnalysisMessage
from crosshair.util import CrosshairInternal, UnexploredPath, IdentityWrapper, AttributeHolder, CrosshairUnsupported
from crosshair.util import debug, set_debug, extract_module_from_file, walk_qualname
from crosshair.type_repo import get_subclass_map


def samefile(f1: Optional[str], f2: Optional[str]) -> bool:
    try:
        return f1 is not None and f2 is not None and os.path.samefile(f1, f2)
    except FileNotFoundError:
        return False


def exception_line_in_file(frames: traceback.StackSummary, filename: str) -> Optional[int]:
    for frame in reversed(frames):
        if samefile(frame.filename, filename):
            return frame.lineno
    return None


def frame_summary_for_fn(frames: traceback.StackSummary, fn: Callable) -> Tuple[str, int]:
    fn_name = fn.__name__
    fn_file = cast(str, inspect.getsourcefile(fn))
    for frame in reversed(frames):
        if (frame.name == fn_name and
            samefile(frame.filename, fn_file)):
            return (frame.filename, frame.lineno)
    try:
        (_, fn_start_line) = inspect.getsourcelines(fn)
        return fn_file, fn_start_line
    except OSError:
        debug(f'Unable to get source information for function {fn_name} in file "{fn_file}"')
        return (fn_file, 0)


_MISSING = object()

def is_pure(obj: object) -> bool:
    if isinstance(obj, type):
        return True if '__dict__' in dir(obj) else hasattr(obj, '__slots__')
    elif callable(obj):
        return inspect.isfunction(obj)  # isfunction selects "user-defined" functions only
    else:
        return True

# TODO Unify common logic here with EnforcedConditions?
class Patched:
    def __init__(self, enabled: Callable[[], bool]):
        self._patches = _PATCH_REGISTRATIONS
        self._enabled = enabled
        self._originals: Dict[IdentityWrapper, Dict[str, object]] = collections.defaultdict(dict)

    def set(self, target: object, key: str, value: object):
        if is_pure(target):
            target.__dict__[key] = value
        else:
            forbiddenfruit.curse(target, key, value)

    def patch(self, target: object, key: str, patched_fn: Callable):
        enabled = self._enabled
        orig_fn = getattr(target, key, None)
        if orig_fn is None:
            self.set(target, key, patched_fn)
        else:
            def call_if_enabled(*a, **kw):
                if enabled():
                    return patched_fn(*a, **kw)
                else:
                    return orig_fn(*a, **kw)
            functools.update_wrapper(call_if_enabled, orig_fn)
            self.set(target, key, call_if_enabled)

    def __enter__(self) -> None:
        for target_wrapper, members in self._patches.items():
            container_originals = self._originals[target_wrapper]
            container = target_wrapper.get()
            for key, val in members.items():
                container_originals[key] = getattr(container, key, _MISSING)
                self.patch(container, key, val)

    def __exit__(self, exc_type, exc_value, tb) -> None:
        for target_wrapper, members in self._patches.items():
            container = target_wrapper.get()
            originals = self._originals[target_wrapper]
            for key, orig_val in originals.items():
                if orig_val is _MISSING:
                    delattr(container, key)
                else:
                    self.set(container, key, orig_val)


class ExceptionFilter:
    analysis: 'CallAnalysis'
    ignore: bool = False
    ignore_with_confirmation: bool = False
    user_exc: Optional[Tuple[Exception, traceback.StackSummary]] = None
    expected_exceptions: Tuple[Type[BaseException], ...]

    def __init__(self, expected_exceptions: FrozenSet[Type[BaseException]] = frozenset()):
        self.expected_exceptions = (NotImplementedError,) + tuple(expected_exceptions)

    def has_user_exception(self) -> bool:
        return self.user_exc is not None

    def __enter__(self) -> 'ExceptionFilter':
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if isinstance(exc_value, (PostconditionFailed, IgnoreAttempt)):
            if isinstance(exc_value, PostconditionFailed):
                # Postcondition : although this indicates a problem, it's with a
                # subroutine; not this function.
                # Usualy we want to ignore this because it will be surfaced more locally
                # in the subroutine.
                debug(
                    F'Ignoring based on internal failed post condition: {exc_value}')
            self.ignore = True
            self.analysis = CallAnalysis()
            return True
        if isinstance(exc_value, self.expected_exceptions):
            self.ignore = True
            self.analysis = CallAnalysis(VerificationStatus.CONFIRMED)
            return True
        if isinstance(exc_value, TypeError):
            exc_str = str(exc_value)
            if ('SmtStr' in exc_str or
                'SmtInt' in exc_str or
                'SmtFloat' in exc_str or
                'expected string or bytes-like object' in exc_str):
                # Ideally we'd attempt literal strings after encountering this.
                # See https://github.com/pschanely/CrossHair/issues/8
                raise CrosshairUnsupported('Detected proxy intolerance: '+exc_str)
        if isinstance(exc_value, (UnexploredPath, CrosshairInternal, z3.Z3Exception)):
            return False  # internal issue: re-raise
        if isinstance(exc_value, BaseException):  # TODO: should this be "Exception" instead?
            # Most other issues are assumed to be user-level exceptions:
            self.user_exc = (
                exc_value, traceback.extract_tb(sys.exc_info()[2]))
            self.analysis = CallAnalysis(VerificationStatus.REFUTED)
            return True  # suppress user-level exception
        return False  # re-raise resource and system issues


class CrossHairValue:
    def __ch_realize__(self):
        raise NotImplementedError

def normalize_pytype(typ: Type) -> Type:
    if typing_inspect.is_typevar(typ):
        # we treat type vars in the most general way possible (the bound, or as 'object')
        bound = typing_inspect.get_bound(typ)
        if bound is not None:
            return normalize_pytype(bound)
        constraints = typing_inspect.get_constraints(typ)
        if constraints:
            raise CrosshairUnsupported
            # TODO: not easy; interpreting as a Union allows the type to be
            # instantiated differently in different places. So, this doesn't work:
            # return Union.__getitem__(tuple(map(normalize_pytype, constraints)))
        return object
    if typ is Any:
        # The distinction between any and object is for type checking, crosshair treats them the same
        return object
    if typ is Type:
        return type
    return typ

def origin_of(typ: Type) -> Type:
    if hasattr(typ, '__origin__'):
        return typ.__origin__
    return typ

def type_arg_of(typ: Type, index: int) -> Type:
    args = type_args_of(typ)
    return args[index] if index < len(args) else object

def type_args_of(typ: Type) -> Tuple[Type, ...]:
    if getattr(typ, '__args__', None):
        return typing_inspect.get_args(typ, evaluate=True)
    else:
        return ()

def name_of_type(typ: Type) -> str:
    return typ.__name__ if hasattr(typ, '__name__') else str(typ).split('.')[-1]

def python_type(o: object) -> Type:
    if hasattr(o, '__ch_pytype__'):
        return o.__ch_pytype__()  # type: ignore
    else:
        return type(o)

def realize(value: object):
    if isinstance(value, CrossHairValue):
        return value.__ch_realize__()
    else:
        return value

def with_realized_args(fn: Callable):
    def realizer(*a, **kw):
        a = map(realize, a)
        kw = {k:realize(v) for (k, v) in kw.items()}
        return fn(*a, **kw)
    functools.update_wrapper(realizer, fn)
    return realizer

_IMMUTABLE_TYPES = (int, float, complex, bool, tuple, frozenset, type(None))
def forget_contents(value: object, space: StateSpace):
    # TODO: pretty sure this doesn't work; need tests here.
    if hasattr(value, '__ch_forget_contents__'):
        value.__ch_forget_contents__(space)  # type: ignore
    elif hasattr(value, '__dict__'):
        for subvalue in value.__dict__.values():
            forget_contents(subvalue, space)
    elif isinstance(value, _IMMUTABLE_TYPES):
        return # immutable
    else:
        # TODO: handle mutable values without __dict__
        raise CrosshairUnsupported


class SmtProxyMarker(CrossHairValue):
    def __ch_pytype__(self):
        bases = type(self).__bases__
        assert len(bases) == 2 and bases[0] is SmtProxyMarker
        return bases[1]
    def __ch_forget_contents__(self, space: StateSpace):
        cls = self.__ch_pytype__()
        clean = proxy_for_type(cls, space, space.uniq())
        for name, val in self.__dict__.items():
            self.__dict__[name] = clean.__dict__[name]


_SMT_PROXY_TYPES: Dict[type, type] = {}

def get_smt_proxy_type(cls: type) -> type:
    if issubclass(cls, SmtProxyMarker):
        return cls
    global _SMT_PROXY_TYPES
    cls_name = name_of_type(cls)
    if cls not in _SMT_PROXY_TYPES:
        def symbolic_init(self):
            self.__class__ = cls
        class_body = { '__init__': symbolic_init }
        try:
            proxy_cls = type(cls_name + '_proxy', (SmtProxyMarker, cls), class_body)
        except TypeError as e:
            if 'is not an acceptable base type' in str(e):
                raise CrosshairUnsupported(f'Cannot subclass {cls_name}')
            else:
                raise
        _SMT_PROXY_TYPES[cls] = proxy_cls
    return _SMT_PROXY_TYPES[cls]

def make_fake_object(statespace: StateSpace, cls: type, varname: str) -> object:
    constructor = get_smt_proxy_type(cls)
    debug(constructor)
    try:
        proxy = constructor()
    except TypeError as e:
        # likely the type has a __new__ that expects arguments
        raise CrosshairUnsupported(f'Unable to proxy {name_of_type(cls)}: {e}')
    for name, typ in get_type_hints(cls).items():
        origin = getattr(typ, '__origin__', None)
        if origin is Callable:
            continue
        value = proxy_for_type(typ, statespace, varname +
                               '.' + name + statespace.uniq())
        object.__setattr__(proxy, name, value)
    return proxy


def choose_type(space: StateSpace, from_type: Type) -> Type:
    subtypes = get_subclass_map()[from_type]
    # Note that this is written strangely to leverage the default
    # preference for false when forking:
    if not subtypes or not space.smt_fork():
        return from_type
    for subtype in subtypes[:-1]:
        if not space.smt_fork():
            return choose_type(space, subtype)
    return choose_type(space, subtypes[-1])


_RESOLVED_FNS: Set[IdentityWrapper[Callable]] = set()
def get_resolved_signature(fn: Callable) -> inspect.Signature:
    wrapped = IdentityWrapper(fn)
    if wrapped not in _RESOLVED_FNS:
        _RESOLVED_FNS.add(wrapped)
        try:
            fn.__annotations__ = get_type_hints(fn)
        except Exception as e:
            debug('Could not resolve annotations on', fn, ':', e)
    return inspect.signature(fn)

def get_constructor_params(cls: Type) -> Iterable[inspect.Parameter]:
    # TODO inspect __new__ as well
    init_fn = cls.__init__
    if init_fn is object.__init__:
        return ()
    init_sig = get_resolved_signature(init_fn)
    return list(init_sig.parameters.values())[1:]

def proxy_class_as_concrete(typ: Type, statespace: StateSpace,
                            varname: str) -> object:
    '''
    Try aggressively to create an instance of a class with symbolic members.
    '''
    data_members = get_type_hints(typ)
    if issubclass(typ, tuple):
        # Special handling for namedtuple which does magic that we don't
        # otherwise support.
        args = {k: proxy_for_type(t, statespace, varname + '.' + k)
                for (k, t) in data_members.items()}
        return typ(**args) # type: ignore
    constructor_params = get_constructor_params(typ)
    EMPTY = inspect.Parameter.empty
    args = {}
    for param in constructor_params:
        name = param.name
        smtname = varname + '.' + name
        annotation = param.annotation
        if annotation is not EMPTY:
            args[name] = proxy_for_type(annotation, statespace, smtname)
        else:
            if param.default is EMPTY:
                debug('unable to create concrete instance of', typ,
                      'due to lack of type annotation on', name)
                return _MISSING
            else:
                # TODO: consider whether we should fall back to a proxy
                # instead of letting this slide. Or try both paths?
                pass
    try:
        obj = typ(**args)
    except BaseException as e:
        debug('unable to create concrete proxy with init:', e)
        return _MISSING

    # Additionally, for any typed members, ensure that they are also
    # symbolic. (classes sometimes have valid states that are not directly
    # constructable)
    for (key, typ) in data_members.items():
        if isinstance(getattr(obj, key, None), CrossHairValue):
            continue
        symbolic_value = proxy_for_type(typ, statespace, varname + '.' + key)
        try:
            setattr(obj, key, symbolic_value)
        except Exception as e:
            debug('Unable to assign symbolic value to concrete class:', e)
            # TODO: consider whether we should fall back to a proxy
            # instead of letting this slide. Or try both paths?
    return obj


def proxy_for_class(typ: Type, space: StateSpace, varname: str, meet_class_invariants: bool) -> object:
    # if the class has data members, we attempt to create a concrete instance with
    # symbolic members; otherwise, we'll create an object proxy that emulates it.
    obj = proxy_class_as_concrete(typ, space, varname)
    if obj is _MISSING:
        debug('Creating', typ, 'as an independent proxy class')
        obj = make_fake_object(space, typ, varname)
    else:
        debug('Creating', typ, 'with symbolic attribute assignments')
    class_conditions = get_class_conditions(typ)
    # symbolic custom classes may assume their invariants:
    if meet_class_invariants and class_conditions is not None:
        for inv_condition in class_conditions.inv:
            if inv_condition.expr is None:
                continue
            isok = False
            with ExceptionFilter() as efilter:
                isok = inv_condition.evaluate({'self': obj})
            if efilter.user_exc:
                raise IgnoreAttempt(
                    f'Class proxy could not meet invariant "{inv_condition.expr_source}" on '
                    f'{varname} (proxy of {typ}) because it raised: {repr(efilter.user_exc[0])}')
            else:
                if efilter.ignore or not isok:
                    raise IgnoreAttempt('Class proxy did not meet invariant ',
                                        inv_condition.expr_source)
    return obj

_PATCH_REGISTRATIONS: Dict[IdentityWrapper, Dict[str, Callable]] = collections.defaultdict(dict)
def register_patch(entity: object, patch_value: Callable, attr_name: Optional[str] = None):
    if attr_name in _PATCH_REGISTRATIONS[IdentityWrapper(entity)]:
        raise CrosshairInternal(f'Doubly registered patch: {object} . {attr_name}')
    if attr_name is None:
        attr_name = getattr(patch_value, '__name__', None)
        assert attr_name is not None
    _PATCH_REGISTRATIONS[IdentityWrapper(entity)][attr_name] = patch_value

def builtin_patches():
    return _PATCH_REGISTRATIONS[IdentityWrapper(builtins)]

_SIMPLE_PROXIES: MutableMapping[object, Callable] = {}

def register_type(typ: Type,
                  creator: Union[Type, Callable]) -> None:
    assert typ is origin_of(typ), \
            f'Only origin types may be registered, not "{typ}": try "{origin_of(typ)}" instead.'
    if typ in _SIMPLE_PROXIES:
        raise CrosshairInternal(f'Duplicate type "{typ}" registered')
    _SIMPLE_PROXIES[typ] = creator

def proxy_for_type(typ: Type, space: StateSpace, varname: str,
                   meet_class_invariants=True,
                   allow_subtypes=False) -> object:
    typ = normalize_pytype(typ)
    origin = origin_of(typ)
    type_args = type_args_of(typ)
    # special cases
    if isinstance(typ, type) and issubclass(typ, enum.Enum):
        enum_values = list(typ)  # type:ignore
        for enum_value in enum_values[:-1]:
            if space.smt_fork():
                return enum_value
        return enum_values[-1]
    proxy_factory = _SIMPLE_PROXIES.get(origin)
    if proxy_factory:
        def recursive_proxy_factory(t: Type):
            return proxy_for_type(t, space, varname + space.uniq(),
                                  allow_subtypes=allow_subtypes)
        recursive_proxy_factory.space = space  # type: ignore
        recursive_proxy_factory.pytype = typ  # type: ignore
        recursive_proxy_factory.varname = varname  # type: ignore
        return proxy_factory(recursive_proxy_factory, *type_args)
    if allow_subtypes and typ is not object:
        typ = choose_type(space, typ)
    return proxy_for_class(typ, space, varname, meet_class_invariants)


def gen_args(sig: inspect.Signature, statespace: StateSpace) -> inspect.BoundArguments:
    args = sig.bind_partial()
    for param in sig.parameters.values():
        smt_name = param.name + statespace.uniq()
        proxy_maker = lambda typ, **kw: proxy_for_type(typ, statespace, smt_name, allow_subtypes=True, **kw)
        has_annotation = (param.annotation != inspect.Parameter.empty)
        value: object
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            if has_annotation:
                varargs_type = List[param.annotation]  # type: ignore
                value = proxy_maker(varargs_type)
            else:
                value = proxy_maker(List[Any])
        elif param.kind == inspect.Parameter.VAR_KEYWORD:
            if has_annotation:
                varargs_type = Dict[str, param.annotation]  # type: ignore
                value = cast(dict, proxy_maker(varargs_type))
                # Using ** on a dict requires concrete string keys. Force
                # instiantiation of keys here:
                value = {k.__str__(): v for (k, v) in value.items()}
            else:
                value = proxy_maker(Dict[str, Any])
        else:
            is_self = param.name == 'self'
            # Object parameters should meet thier invariants iff they are not the
            # class under test ("self").
            meet_class_invariants = not is_self
            allow_subtypes = not is_self
            if has_annotation:
                value = proxy_for_type(param.annotation, statespace, smt_name,
                                       meet_class_invariants, allow_subtypes)
            else:
                value = proxy_for_type(cast(type, Any), statespace, smt_name,
                                       meet_class_invariants, allow_subtypes)
        debug('created proxy for', param.name, 'as type:', type(value))
        args.arguments[param.name] = value
    return args

_UNABLE_TO_REPR = '<unable to repr>'
def message_sort_key(m: AnalysisMessage) -> tuple:
    return (m.state, _UNABLE_TO_REPR not in m.message, -len(m.message))

class MessageCollector:
    def __init__(self):
        self.by_pos = {}

    def extend(self, messages: Iterable[AnalysisMessage]) -> None:
        for message in messages:
            self.append(message)

    def append(self, message: AnalysisMessage) -> None:
        key = (message.filename, message.line, message.column)
        if key in self.by_pos:
            self.by_pos[key] = max(
                self.by_pos[key], message, key=message_sort_key)
        else:
            self.by_pos[key] = message

    def get(self) -> List[AnalysisMessage]:
        return [m for (k, m) in sorted(self.by_pos.items())]


@dataclass
class AnalysisOptions:
    per_condition_timeout: float = 1.5
    per_path_timeout: float = 0.75
    report_all: bool = False

    # Transient members (not user-configurable):
    deadline: float = float('NaN')
    stats: Optional[collections.Counter] = None

    def incr(self, key: str):
        if self.stats is not None:
            self.stats[key] += 1


_DEFAULT_OPTIONS = AnalysisOptions()


def analyzable_members(module: types.ModuleType) -> Iterator[Tuple[str, Union[Type, Callable]]]:
    module_name = module.__name__
    for name, member in inspect.getmembers(module):
        if not (inspect.isclass(member) or inspect.isfunction(member)):
            continue
        if member.__module__ != module_name:
            continue
        yield (name, member)


def analyze_any(entity: object, options: AnalysisOptions) -> List[AnalysisMessage]:
    if inspect.isclass(entity):
        return analyze_class(cast(Type, entity), options)
    elif inspect.isfunction(entity):
        self_class: Optional[type] = None
        fn = cast(Callable, entity)
        if fn.__name__ != fn.__qualname__:
            self_thing = walk_qualname(sys.modules[fn.__module__],
                                       fn.__qualname__.split('.')[-2])
            assert isinstance(self_thing, type)
            self_class = self_thing
        return analyze_function(fn, options, self_type=self_class)
    elif inspect.ismodule(entity):
        return analyze_module(cast(types.ModuleType, entity), options)
    else:
        raise CrosshairInternal(
            'Entity type not analyzable: ' + str(type(entity)))


def analyze_module(module: types.ModuleType, options: AnalysisOptions) -> List[AnalysisMessage]:
    debug('Analyzing module ', module)
    messages = MessageCollector()
    for (name, member) in analyzable_members(module):
        messages.extend(analyze_any(member, options))
    message_list = messages.get()
    debug('Module', module.__name__, 'has', len(message_list), 'messages')
    return message_list


def message_class_clamper(cls: type):
    '''
    We clamp messages for a clesses method to appear on the class itself.
    So, even if the method is defined on a superclass, or defined dynamically (via
    decorator etc), we report it on the class definition instead.
    '''
    cls_file = inspect.getsourcefile(cls)
    (lines, cls_start_line) = inspect.getsourcelines(cls)

    def clamp(message: AnalysisMessage):
        if not samefile(message.filename, cls_file):
            return replace(message, filename=cls_file, line=cls_start_line)
        else:
            return message
    return clamp


def analyze_class(cls: type, options: AnalysisOptions = _DEFAULT_OPTIONS) -> List[AnalysisMessage]:
    debug('Analyzing class ', cls.__name__)
    messages = MessageCollector()
    class_conditions = get_class_conditions(cls)
    for method, conditions in class_conditions.methods.items():
        if conditions.has_any():
            cur_messages = analyze_function(getattr(cls, method),
                                            options=options,
                                            self_type=cls)
            clamper = message_class_clamper(cls)
            messages.extend(map(clamper, cur_messages))

    return messages.get()


def analyze_function(fn: Callable,
                     options: AnalysisOptions = _DEFAULT_OPTIONS,
                     self_type: Optional[type] = None) -> List[AnalysisMessage]:
    debug('Analyzing ', fn.__name__)
    all_messages = MessageCollector()

    if self_type is not None:
        class_conditions = get_class_conditions(self_type)
        conditions = class_conditions.methods[fn.__name__]
    else:
        conditions = get_fn_conditions(fn, self_type=self_type)
        if conditions is None:
            debug('Skipping ', str(fn),
                  ': Unable to determine the function signature.')
            return []

    for syntax_message in conditions.syntax_messages():
        all_messages.append(AnalysisMessage(MessageType.SYNTAX_ERR,
                                            syntax_message.message,
                                            syntax_message.filename,
                                            syntax_message.line_num, 0, ''))
    conditions = conditions.compilable()
    for post_condition in conditions.post:
        messages = analyze_single_condition(fn, options, replace(
            conditions, post=[post_condition]))
        all_messages.extend(messages)
    return all_messages.get()


def analyze_single_condition(fn: Callable,
                             options: AnalysisOptions,
                             conditions: Conditions) -> Sequence[AnalysisMessage]:
    debug('Analyzing postcondition: "', conditions.post[0].expr_source, '"')
    debug('assuming preconditions: ', ','.join(
        [p.expr_source for p in conditions.pre]))
    options.deadline = time.time() + options.per_condition_timeout

    analysis = analyze_calltree(fn, options, conditions)

    (condition,) = conditions.post
    addl_ctx = (' ' + condition.addl_context if condition.addl_context else '') + '.'
    if analysis.verification_status is VerificationStatus.UNKNOWN:
        message = 'Not confirmed' + addl_ctx
        analysis.messages = [AnalysisMessage(MessageType.CANNOT_CONFIRM, message,
                                             condition.filename, condition.line, 0, '')]
    elif analysis.verification_status is VerificationStatus.CONFIRMED:
        message = 'Confirmed over all paths' + addl_ctx
        analysis.messages = [AnalysisMessage(MessageType.CONFIRMED, message,
                                             condition.filename, condition.line, 0, '')]

    return analysis.messages


class ShortCircuitingContext:
    engaged = False
    intercepted = False

    def __init__(self, space_getter: Callable[[], StateSpace]):
        self.space_getter = space_getter

    def __enter__(self):
        assert not self.engaged
        self.engaged = True

    def __exit__(self, exc_type, exc_value, tb):
        assert self.engaged
        self.engaged = False

    def make_interceptor(self, original: Callable) -> Callable:
        subconditions = get_fn_conditions(original)
        if subconditions is None:
            return original
        sig = subconditions.sig

        def wrapper(*a: object, **kw: Dict[str, object]) -> object:
            #debug('short circuit wrapper ', original)
            if (not self.engaged) or self.space_getter().running_framework_code:
                return original(*a, **kw)
            # We *heavily* bias towards concrete execution, because it's often the case
            # that a single short-circuit will render the path useless. TODO: consider
            # decaying short-crcuit probability over time.
            use_short_circuit = self.space_getter().fork_with_confirm_or_else(0.95)
            if not use_short_circuit:
                debug('short circuit: Choosing not to intercept', original)
                return original(*a, **kw)
            try:
                self.engaged = False
                debug('short circuit: Intercepted a call to ', original)
                self.intercepted = True
                return_type = sig.return_annotation

                # Deduce type vars if necessary
                if len(typing_inspect.get_parameters(sig.return_annotation)) > 0 or typing_inspect.is_typevar(sig.return_annotation):
                    typevar_bindings: typing.ChainMap[object, type] = collections.ChainMap(
                    )
                    bound = sig.bind(*a, **kw)
                    bound.apply_defaults()
                    for param in sig.parameters.values():
                        argval = bound.arguments[param.name]
                        value_type = python_type(argval)
                        #debug('unify', value_type, param.annotation)
                        if not dynamic_typing.unify(value_type, param.annotation, typevar_bindings):
                            debug(
                                'aborting intercept due to signature unification failure')
                            return original(*a, **kw)
                        #debug('unify bindings', typevar_bindings)
                    return_type = dynamic_typing.realize(
                        sig.return_annotation, typevar_bindings)
                    debug('short circuit: Deduced return type was ', return_type)

                # adjust arguments that may have been mutated
                assert subconditions is not None
                bound = sig.bind(*a, **kw)
                mutable_args = subconditions.mutable_args
                for argname, arg in bound.arguments.items():
                    if mutable_args is None or argname in mutable_args:
                        forget_contents(arg, self.space_getter())

                if return_type is type(None):
                    return None
                # note that the enforcement wrapper ensures postconditions for us, so we
                # can just return a free variable here.
                return proxy_for_type(return_type, self.space_getter(), 'proxyreturn' + self.space_getter().uniq())
            finally:
                self.engaged = True
        functools.update_wrapper(wrapper, original)
        return wrapper

@dataclass
class CallTreeAnalysis:
    messages: Sequence[AnalysisMessage]
    verification_status: VerificationStatus
    num_confirmed_paths: int = 0


def analyze_calltree(fn: Callable,
                     options: AnalysisOptions,
                     conditions: Conditions) -> CallTreeAnalysis:
    debug('Begin analyze calltree ', fn.__name__)

    all_messages = MessageCollector()
    search_root = SinglePathNode(True)
    space_exhausted = False
    failing_precondition: Optional[ConditionExpr] = conditions.pre[0] if conditions.pre else None
    failing_precondition_reason: str = ''
    num_confirmed_paths = 0

    cur_space: List[StateSpace] = [cast(StateSpace, None)]
    short_circuit = ShortCircuitingContext(lambda: cur_space[0])
    _ = get_subclass_map()  # ensure loaded
    top_analysis: Optional[CallAnalysis] = None
    enforced_conditions = EnforcedConditions(
        fn_globals(fn), builtin_patches(),
        interceptor=short_circuit.make_interceptor)
    def in_symbolic_mode():
        return (cur_space[0] is not None and
                not cur_space[0].running_framework_code)
    patched = Patched(in_symbolic_mode)
    with enforced_conditions, patched, enforced_conditions.disabled_enforcement():
        for i in itertools.count(1):
            start = time.time()
            if start > options.deadline:
                debug('Exceeded condition timeout, stopping')
                break
            options.incr('num_paths')
            debug('Iteration ', i)
            space = TrackingStateSpace(execution_deadline=start + options.per_path_timeout,
                                       model_check_timeout=options.per_path_timeout / 2,
                                       search_root=search_root)
            cur_space[0] = space
            try:
                # The real work happens here!:
                call_analysis = attempt_call(
                    conditions, space, fn, short_circuit, enforced_conditions)
                if failing_precondition is not None:
                    cur_precondition = call_analysis.failing_precondition
                    if cur_precondition is None:
                        if call_analysis.verification_status is not None:
                            # We escaped the all the pre conditions on this try:
                            failing_precondition = None
                    elif (cur_precondition.line == failing_precondition.line and
                          call_analysis.failing_precondition_reason):
                        failing_precondition_reason = call_analysis.failing_precondition_reason
                    elif cur_precondition.line > failing_precondition.line:
                        failing_precondition = cur_precondition
                        failing_precondition_reason = call_analysis.failing_precondition_reason

            except UnexploredPath:
                call_analysis = CallAnalysis(VerificationStatus.UNKNOWN)
            except IgnoreAttempt:
                call_analysis = CallAnalysis()
            status = call_analysis.verification_status
            if status == VerificationStatus.CONFIRMED:
                num_confirmed_paths += 1
            top_analysis, space_exhausted = space.bubble_status(call_analysis)
            overall_status = top_analysis.verification_status if top_analysis else None
            debug('Iter complete. Worst status found so far:',
                  overall_status.name if overall_status else 'None')
            if space_exhausted or top_analysis == VerificationStatus.REFUTED:
                break
    top_analysis = search_root.child.get_result()
    if top_analysis.messages:
        #log = space.execution_log()
        all_messages.extend(
            replace(m,
                    #execution_log=log,
                    test_fn=fn.__qualname__,
                    condition_src=conditions.post[0].expr_source)
            for m in top_analysis.messages)
    if top_analysis.verification_status is None:
        top_analysis.verification_status = VerificationStatus.UNKNOWN
    if failing_precondition:
        assert num_confirmed_paths == 0
        addl_ctx = ' ' + failing_precondition.addl_context if failing_precondition.addl_context else ''
        message = f'Unable to meet precondition{addl_ctx}'
        if failing_precondition_reason:
            message += f' (possibly because {failing_precondition_reason}?)'
        all_messages.extend([AnalysisMessage(MessageType.PRE_UNSAT, message + '.',
                                             failing_precondition.filename, failing_precondition.line, 0, '')])
        top_analysis = CallAnalysis(VerificationStatus.REFUTED)

    assert top_analysis.verification_status is not None
    debug(('Exhausted' if space_exhausted else 'Aborted'),
          ' calltree search with', top_analysis.verification_status.name,
          'and', len(all_messages.get()), 'messages.',
          'Number of iterations: ', i)
    return CallTreeAnalysis(messages=all_messages.get(),
                            verification_status=top_analysis.verification_status,
                            num_confirmed_paths=num_confirmed_paths)


def get_input_description(statespace: StateSpace,
                          fn_name: str,
                          bound_args: inspect.BoundArguments,
                          return_val: object = _MISSING,
                          addl_context: str = '') -> str:
    debug('get_input_description: return_val: ', type(return_val))
    call_desc = ''
    if return_val is not _MISSING:
        try:
            repr_str = repr(return_val)
        except Exception as e:
            if isinstance(e, IgnoreAttempt):
                raise
            debug(f'Exception attempting to repr function output: {e}')
            repr_str = _UNABLE_TO_REPR
        if repr_str != 'None':
            call_desc = call_desc + ' (which returns ' + repr_str + ')'
    messages: List[str] = []
    for argname, argval in list(bound_args.arguments.items()):
        try:
            repr_str = repr(argval)
        except Exception as e:
            if isinstance(e, IgnoreAttempt):
                raise
            debug(f'Exception attempting to repr input "{argname}": {repr(e)}')
            repr_str = _UNABLE_TO_REPR
        messages.append(argname + ' = ' + repr_str)
    call_desc = fn_name + '(' + ', '.join(messages) + ')' + call_desc

    if addl_context:
        return addl_context + ' when calling ' + call_desc # ' and '.join(messages)
    elif messages:
        return 'when calling ' + call_desc # ' and '.join(messages)
    else:
        return 'for any input'


class UnEqual:
    pass


_UNEQUAL = UnEqual()


def deep_eq(old_val: object, new_val: object, visiting: Set[Tuple[int, int]]) -> bool:
    # TODO: test just about all of this
    if old_val is new_val:
        return True
    if type(old_val) != type(new_val):
        return False
    visit_key = (id(old_val), id(new_val))
    if visit_key in visiting:
        return True
    visiting.add(visit_key)
    try:
        if isinstance(old_val, CrossHairValue):
            return old_val == new_val
        elif hasattr(old_val, '__dict__') and hasattr(new_val, '__dict__'):
            return deep_eq(old_val.__dict__, new_val.__dict__, visiting)
        elif isinstance(old_val, dict):
            assert isinstance(new_val, dict)
            for key in set(itertools.chain(old_val.keys(), *new_val.keys())):
                if (key in old_val) ^ (key in new_val):
                    return False
                if not deep_eq(old_val.get(key, _UNEQUAL), new_val.get(key, _UNEQUAL), visiting):
                    return False
            return True
        elif isinstance(old_val, Iterable):
            assert isinstance(new_val, Iterable)
            if isinstance(old_val, Sized):
                if len(old_val) != len(new_val):
                    return False
            return all(deep_eq(o, n, visiting) for (o, n) in
                       itertools.zip_longest(old_val, new_val, fillvalue=_UNEQUAL))
        elif type(old_val) is object:
            # deepclone'd object instances are close enough to equal for our purposes
            return True
        else:
            # hopefully this is just ints, bools, etc
            return old_val == new_val
    finally:
        visiting.remove(visit_key)


def attempt_call(conditions: Conditions,
                 space: StateSpace,
                 fn: Callable,
                 short_circuit: ShortCircuitingContext,
                 enforced_conditions: EnforcedConditions) -> CallAnalysis:
    bound_args = gen_args(conditions.sig, space)

    code_obj = fn.__code__
    fn_filename, fn_start_lineno = (
        code_obj.co_filename, code_obj.co_firstlineno)
    try:
        (lines, _) = inspect.getsourcelines(fn)
    except OSError:
        lines = []
    fn_end_lineno = fn_start_lineno + len(lines)

    def locate_msg(detail: str, suggested_filename: str, suggested_lineno: int) -> Tuple[str, str, int, int]:
        if ((os.path.abspath(suggested_filename) == os.path.abspath(fn_filename)) and
            (fn_start_lineno <= suggested_lineno <= fn_end_lineno)):
            return (detail, suggested_filename, suggested_lineno, 0)
        else:
            try:
                exprline = linecache.getlines(suggested_filename)[
                    suggested_lineno - 1].strip()
            except IndexError:
                exprline = '<unknown>'
            detail = f'"{exprline}" yields {detail}'
            return (detail, fn_filename, fn_start_lineno, 0)

    with space.framework():
        original_args = copy.deepcopy(bound_args)
    space.checkpoint()

    lcls: Mapping[str, object] = bound_args.arguments
    # In preconditions, __old__ exists but is just bound to the same args.
    # This lets people write class invariants using `__old__` to, for example,
    # demonstrate immutability.
    lcls = {'__old__': AttributeHolder(lcls), **lcls}
    expected_exceptions = conditions.raises
    for precondition in conditions.pre:
        with ExceptionFilter(expected_exceptions) as efilter:
            with enforced_conditions.enabled_enforcement(), short_circuit:
                precondition_ok = precondition.evaluate(lcls)
            if not precondition_ok:
                debug('Failed to meet precondition', precondition.expr_source)
                return CallAnalysis(failing_precondition=precondition)
        if efilter.ignore:
            debug('Ignored exception in precondition.', efilter.analysis)
            return efilter.analysis
        elif efilter.user_exc is not None:
            (user_exc, tb) = efilter.user_exc
            debug('Exception attempting to meet precondition',
                  precondition.expr_source, ':',
                  user_exc,
                  tb.format())
            return CallAnalysis(failing_precondition=precondition,
                                failing_precondition_reason=
                                f'it raised "{repr(user_exc)} at {tb.format()[-1]}"')

    with ExceptionFilter(expected_exceptions) as efilter:
        a, kw = bound_args.args, bound_args.kwargs
        with enforced_conditions.enabled_enforcement(), short_circuit:
            assert not space.running_framework_code
            __return__ = fn(*a, **kw)
        lcls = {**bound_args.arguments,
                '__return__': __return__,
                '_': __return__,
                '__old__': AttributeHolder(original_args.arguments),
                fn.__name__: fn}

    if efilter.ignore:
        debug('Ignored exception in function.', efilter.analysis)
        return efilter.analysis
    elif efilter.user_exc is not None:
        (e, tb) = efilter.user_exc
        detail = name_of_type(type(e)) + ': ' + str(e)
        frame_filename, frame_lineno = frame_summary_for_fn(tb, fn)
        debug('exception while evaluating function body:', detail, frame_filename, 'line', frame_lineno)
        detail += ' ' + get_input_description(space, fn.__name__, original_args, _MISSING)
        return CallAnalysis(VerificationStatus.REFUTED,
                            [AnalysisMessage(MessageType.EXEC_ERR,
                                             *locate_msg(detail, frame_filename, frame_lineno),
                                             ''.join(tb.format()))])

    for argname, argval in bound_args.arguments.items():
        if (conditions.mutable_args is not None and
            argname not in conditions.mutable_args):
            old_val, new_val = original_args.arguments[argname], argval
            if not deep_eq(old_val, new_val, set()):
                detail = 'Argument "{}" is not marked as mutable, but changed from {} to {}'.format(
                    argname, old_val, new_val)
                debug('Mutablity problem:', detail)
                return CallAnalysis(VerificationStatus.REFUTED,
                                    [AnalysisMessage(MessageType.POST_ERR, detail,
                                                     fn_filename, fn_start_lineno, 0, '')])

    (post_condition,) = conditions.post
    with ExceptionFilter(expected_exceptions) as efilter:
        # TODO: re-enable post-condition short circuiting. This will require refactoring how
        # enforced conditions and short curcuiting interact, so that post-conditions are
        # selectively run when, and only when, performing a short circuit.
        #with enforced_conditions.enabled_enforcement(), short_circuit:
        isok = bool(post_condition.evaluate(lcls))
    if efilter.ignore:
        debug('Ignored exception in postcondition.', efilter.analysis)
        return efilter.analysis
    elif efilter.user_exc is not None:
        (e, tb) = efilter.user_exc
        detail = repr(e) + ' ' + get_input_description(space, fn.__name__,
                                                       original_args, __return__, post_condition.addl_context)
        debug('exception while calling postcondition:', detail)
        failures = [AnalysisMessage(MessageType.POST_ERR,
                                    *locate_msg(detail, post_condition.filename, post_condition.line),
                                    ''.join(tb.format()))]
        return CallAnalysis(VerificationStatus.REFUTED, failures)
    if isok:
        debug('Postcondition confirmed.')
        return CallAnalysis(VerificationStatus.CONFIRMED)
    else:
        detail = 'false ' + \
                 get_input_description(
                     space, fn.__name__, original_args, __return__, post_condition.addl_context)
        debug(detail)
        failures = [AnalysisMessage(MessageType.POST_FAIL,
                                    *locate_msg(detail, post_condition.filename, post_condition.line), '')]
        return CallAnalysis(VerificationStatus.REFUTED, failures)
