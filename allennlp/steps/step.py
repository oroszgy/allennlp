import copy
import inspect
import itertools
import json
import logging
import random
import re
import weakref
from os import PathLike
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import (
    Optional,
    Any,
    Set,
    List,
    Dict,
    Type,
    Callable,
    Union,
    cast,
    TypeVar,
    Generic,
    Iterable,
    Tuple,
    MutableSet,
    get_origin,
    get_args,
    MutableMapping,
)

from allennlp.common import Registrable, Params
from allennlp.common.checks import ConfigurationError
from allennlp.common.from_params import (
    infer_params,
    pop_and_construct_arg,
)
from allennlp.common.util import hash_object
from allennlp.steps.format import Format, DillFormat

logger = logging.getLogger(__name__)

_version_re = re.compile("""^[a-zA-Z0-9]+$""")

T = TypeVar("T")


class StepCache(Registrable):
    def __contains__(self, step: object) -> bool:
        """This is a generic implementation of __contains__. If you are writing your own
        `StepCache`, you might want to write a faster one yourself."""
        if not isinstance(step, Step):
            return False
        try:
            self.__getitem__(step)
            return True
        except KeyError:
            return False

    def __getitem__(self, step: "Step") -> Any:
        raise NotImplementedError()

    def __setitem__(self, step: "Step", value: Any) -> None:
        raise NotImplementedError()

    def path_for_step(self, step: "Step") -> Optional[Path]:
        return None


@StepCache.register("memory")
class MemoryStepCache(StepCache):
    def __init__(self):
        self.cache: Dict[str, Any] = {}

    def __getitem__(self, step: "Step") -> Any:
        return self.cache[step.unique_id()]

    def __setitem__(self, step: "Step", value: Any) -> None:
        if step.cache_results:
            self.cache[step.unique_id()] = value
        else:
            logger.warning("Tried to cache step %s despite being marked as uncacheable.", step.name)

    def __contains__(self, step: object):
        if isinstance(step, Step):
            return step.unique_id() in self.cache
        else:
            return False

    def __len__(self) -> int:
        return len(self.cache)


default_step_cache = MemoryStepCache()


@StepCache.register("directory")
class DirectoryStepCache(StepCache):
    def __init__(self, dir: Union[str, PathLike]):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)

        # We keep an in-memory cache as well so we don't have to de-serialize stuff
        # we happen to have in memory already.
        self.cache: MutableMapping[str, Any] = weakref.WeakValueDictionary()

    def __contains__(self, step: object) -> bool:
        if isinstance(step, Step):
            if step.unique_id() in self.cache:
                return True
            metadata_file = self.path_for_step(step) / "metadata.json"
            return metadata_file.exists()
        else:
            return False

    def __getitem__(self, step: "Step") -> Any:
        try:
            return self.cache[step.unique_id()]
        except KeyError:
            if step not in self:
                raise KeyError(step)
            result = step.format.read(self.path_for_step(step))
            self.cache[step.unique_id()] = result
            return result

    def __setitem__(self, step: "Step", value: Any) -> None:
        location = self.path_for_step(step)
        location.mkdir(parents=True, exist_ok=True)

        metadata_location = location / "metadata.json"
        if metadata_location.exists():
            raise ValueError(f"{metadata_location} already exists! Will not overwrite.")
        temp_metadata_location = metadata_location.with_suffix(".temp")

        try:
            step.format.write(value, location)
            metadata = {
                "step": step.unique_id(),
                "checksum": step.format.checksum(location),
            }
            with temp_metadata_location.open("wt") as f:
                json.dump(metadata, f)
            self.cache[step.unique_id()] = value
            temp_metadata_location.rename(metadata_location)
        except:  # noqa: E722
            temp_metadata_location.unlink(missing_ok=True)
            raise

    def __len__(self) -> int:
        return sum(1 for _ in self.dir.glob("*/metadata.json"))

    def path_for_step(self, step: "Step") -> Path:
        return self.dir / step.unique_id()


class Step(Registrable, Generic[T]):
    DETERMINISTIC: bool = False
    CACHEABLE: Optional[bool] = None
    VERSION: Optional[str] = None
    FORMAT: Format = DillFormat("gz")

    def __init__(
        self,
        step_name: Optional[str] = None,
        cache_results: Optional[bool] = None,
        step_format: Optional[Format] = None,
        produce_results: bool = False,
        **kwargs,
    ):
        """
        `Step.__init__()` takes all the arguments we want to run the step with. They get passed
        to `Step.run()` (almost) as they are. If the arguments are other instances of `Step`, those
        will be replaced with the step's results before calling `run()`. Further, there are two special
        parameters:
        * `step_name` contains an optional human-readable name for the step. This name is used for
          error messages and the like, and has no consequence on the actual computation.
        * `cache_results` specifies whether the results of this step should be cached. If this is
          `False`, the step is recomputed every time it is needed. If this is not set at all,
          we cache if the step is marked as `DETERMINISTIC`, and we don't cache otherwise.
        """
        if self.VERSION is not None:
            assert _version_re.match(
                self.VERSION
            ), f"Invalid characters in version '{self.VERSION}'"
        self.name = step_name
        self.kwargs = kwargs

        self.unique_id_cache: Optional[str] = None
        if self.name is None:
            self.name = self.unique_id()

        self.produce_results = produce_results

        self.format = step_format
        if self.format is None:
            self.format = self.FORMAT

        if cache_results is True:
            if not self.CACHEABLE:
                raise ConfigurationError(
                    f"Step {self.name} is configured to use the cache, but it's not a cacheable step."
                )
            if not self.DETERMINISTIC:
                logger.warning(
                    f"Step {self.name} is going to be cached despite not being deterministic."
                )
            self.cache_results = True
        elif cache_results is False:
            self.cache_results = False
        elif cache_results is None:
            c = (self.DETERMINISTIC, self.CACHEABLE)
            if c == (False, None):
                self.cache_results = False
            elif c == (True, None):
                self.cache_results = True
            elif c == (False, False):
                self.cache_results = False
            elif c == (True, False):
                self.cache_results = False
            elif c == (False, True):
                logger.warning(
                    f"Step {self.name} is set to be cacheable despite not being deterministic."
                )
                self.cache_results = True
            elif c == (True, True):
                self.cache_results = True
            else:
                assert False, "Step.DETERMINISTIC or step.CACHEABLE are set to an invalid value."
        else:
            raise ConfigurationError(
                f"Step {step_name}'s cache_results parameter is set to an invalid value."
            )

        self.temp_dir_for_run: Optional[
            PathLike
        ] = None  # This is set only while the run() method runs.

    @classmethod
    def from_params(
        cls: Type["Step"],
        params: Params,
        constructor_to_call: Callable[..., "Step"] = None,
        constructor_to_inspect: Union[Callable[..., "Step"], Callable[["Step"], None]] = None,
        existing_steps: Optional[Dict[str, "Step"]] = None,
        **extras,
    ) -> "Step":
        # Why do we need a custom from_params? Step classes have a run() method that takes all the
        # parameters necessary to perform the step. The __init__() method of the step takes those
        # same parameters, but each of them could be wrapped in another Step instead of being
        # supplied directly. from_params() doesn't know anything about these shenanigans, so
        # we have to supply the necessary logic here.

        # TODO: Maybe we figure out later if we need this and what to do about it?
        if constructor_to_call is not None:
            raise ConfigurationError(
                f"{cls.__name__}.from_params cannot be called with a constructor_to_call."
            )
        if constructor_to_inspect is not None:
            raise ConfigurationError(
                f"{cls.__name__}.from_params cannot be called with a constructor_to_inspect."
            )

        if existing_steps is None:
            existing_steps = {}

        if isinstance(params, str):
            params = Params({"type": params})

        if not isinstance(params, Params):
            raise ConfigurationError(
                "from_params was passed a `params` object that was not a `Params`. This probably "
                "indicates malformed parameters in a configuration file, where something that "
                "should have been a dictionary was actually a list, or something else. "
                f"This happened when constructing an object of type {cls}."
            )

        as_registrable = cast(Type[Registrable], cls)
        choice = params.pop_choice("type", choices=as_registrable.list_available())
        subclass, constructor_name = as_registrable.resolve_class_name(choice)
        kwargs: Dict[str, Any] = {}

        parameters = infer_params(subclass, subclass.run)
        del parameters["self"]
        init_parameters = infer_params(subclass)
        del init_parameters["self"]
        del init_parameters["kwargs"]
        parameter_overlap = parameters.keys() & init_parameters.keys()
        assert len(parameter_overlap) <= 0, (
            f"If this assert fails it means that you wrote a Step with a run() method that takes one of the "
            f"reserved parameters ({', '.join(init_parameters.keys())})"
        )
        parameters.update(init_parameters)

        accepts_kwargs = False
        for param_name, param in parameters.items():
            if param.kind == param.VAR_KEYWORD:
                # When a class takes **kwargs we store the fact that the method allows extra keys; if
                # we get extra parameters, instead of crashing, we'll just pass them as-is to the
                # constructor, and hope that you know what you're doing.
                accepts_kwargs = True
                continue

            annotation = Union[Step[param.annotation], param.annotation]

            explicitly_set = param_name in params
            constructed_arg = pop_and_construct_arg(
                subclass.__name__, param_name, annotation, param.default, params, **extras
            )

            def annotation_could_be_str(a) -> bool:
                if a == str:
                    return True
                if a == Any:
                    return True
                if get_origin(a) == Union:
                    return any(annotation_could_be_str(o) for o in get_args(a))
                return False

            if isinstance(constructed_arg, str) and not annotation_could_be_str(param.annotation):
                # We found a string, but we did not want a string.
                if constructed_arg in existing_steps:  # the string matches an existing task
                    constructed_arg = existing_steps[constructed_arg]
                else:
                    raise _RefStep.MissingStepError(constructed_arg)

            if isinstance(constructed_arg, Step):
                if isinstance(constructed_arg, _RefStep):
                    try:
                        constructed_arg = existing_steps[constructed_arg.ref()]
                    except KeyError:
                        raise _RefStep.MissingStepError(constructed_arg.ref())

                return_type = inspect.signature(constructed_arg.run).return_annotation
                if return_type == inspect.Signature.empty:
                    logger.warning(
                        "Step %s has no return type annotation. Those are really helpful when "
                        "debugging, so we recommend them highly.",
                        subclass.__name__,
                    )
                elif not issubclass(return_type, param.annotation):
                    raise ConfigurationError(
                        f"Step {constructed_arg.name} returns {return_type}, but "
                        f"{subclass.__name__} expects {param.annotation}."
                    )

            # If the param wasn't explicitly set in `params` and we just ended up constructing
            # the default value for the parameter, we can just omit it.
            # Leaving it in can cause issues with **kwargs in some corner cases, where you might end up
            # with multiple values for a single parameter (e.g., the default value gives you lazy=False
            # for a dataset reader inside **kwargs, but a particular dataset reader actually hard-codes
            # lazy=True - the superclass sees both lazy=True and lazy=False in its constructor).
            if explicitly_set or constructed_arg is not param.default:
                kwargs[param_name] = constructed_arg

        if accepts_kwargs:
            kwargs.update(params)
        else:
            params.assert_empty(subclass.__name__)

        return subclass(**kwargs)

    def run(self, **kwargs) -> T:
        raise NotImplementedError()

    def _run_with_temp_dir(self, cache: StepCache, **kwargs) -> T:
        if self.temp_dir_for_run is not None:
            raise ValueError("You can only run a Step's run() method once at a time.")
        step_dir = cache.path_for_step(self)
        if step_dir is None:
            temp_dir = TemporaryDirectory(prefix=self.unique_id() + "-", suffix=".temp")
            self.temp_dir_for_run = Path(temp_dir.name)
            try:
                return self.run(**kwargs)
            finally:
                self.temp_dir_for_run = None
                temp_dir.cleanup()
        else:
            self.temp_dir_for_run = step_dir / "run"
            try:
                self.temp_dir_for_run.mkdir(exist_ok=True, parents=True)
                return self.run(**kwargs)
            finally:
                # No cleanup, as we want to keep the directory for restarts or serialization.
                self.temp_dir_for_run = None

    def temp_dir(self) -> PathLike:
        """Returns a temporary directory that a step can use while its `run()` method runs."""
        return self.temp_dir_for_run

    @classmethod
    def _replace_steps_with_results(cls, o: Any, cache: StepCache):
        if isinstance(o, Step):
            return o.result(cache)
        elif isinstance(o, List):
            return [cls._replace_steps_with_results(i, cache) for i in o]
        elif isinstance(o, Set):
            return {cls._replace_steps_with_results(i, cache) for i in o}
        elif isinstance(o, Dict):
            return {key: cls._replace_steps_with_results(value, cache) for key, value in o.items()}
        else:
            return o

    def result(self, cache: Optional[StepCache] = None) -> T:
        if cache is None:
            cache = default_step_cache
        if self in cache:
            return cache[self]

        kwargs = self._replace_steps_with_results(self.kwargs, cache)
        result = self._run_with_temp_dir(cache, **kwargs)
        if self.cache_results:
            # If we have an iterator as a result, we have to copy it into a list first,
            # otherwise we can't cache it.
            if hasattr(result, "__next__"):
                result = list(result)
            cache[self] = result
        return result

    def ensure_result(self, cache: Optional[StepCache] = None) -> None:
        if not self.cache_results:
            raise ValueError(
                "It does not make sense to call ensure_result() on a step that's not cacheable."
            )

        if cache is None:
            cache = default_step_cache
        if self in cache:
            return

        kwargs = self._replace_steps_with_results(self.kwargs, cache)
        result = self._run_with_temp_dir(cache, **kwargs)
        # If we have an iterator as a result, we have to copy it into a list first,
        # otherwise we can't cache it.
        if hasattr(result, "__next__"):
            result = list(result)
        cache[self] = result

    def dry_run(self, cached_steps: MutableSet["Step"]) -> Iterable[Tuple[str, bool]]:
        if self in cached_steps:
            yield self.name, True
            return

        def find_steps_from_inputs(o: Any):
            if isinstance(o, Step):
                yield from o.dry_run(cached_steps)
            elif isinstance(o, List):
                yield from itertools.chain.from_iterable(find_steps_from_inputs(i) for i in o)
            elif isinstance(o, Set):
                yield from itertools.chain.from_iterable(find_steps_from_inputs(i) for i in o)
            elif isinstance(o, Dict):
                yield from itertools.chain.from_iterable(
                    find_steps_from_inputs(i) for i in o.values()
                )

        yield from find_steps_from_inputs(self.kwargs)
        yield self.name, False
        cached_steps.add(self)

    def unique_id(self) -> str:
        if self.unique_id_cache is None:
            self.unique_id_cache = self.__class__.__name__
            if self.VERSION is not None:
                self.unique_id_cache += "-"
                self.unique_id_cache += self.VERSION

            self.unique_id_cache += "-"
            if self.DETERMINISTIC:

                def replace_steps_with_hashes(o: Any):
                    if isinstance(o, Step):
                        return o.unique_id()
                    elif isinstance(o, List):
                        return [replace_steps_with_hashes(i) for i in o]
                    elif isinstance(o, Set):
                        return {replace_steps_with_hashes(i) for i in o}
                    elif isinstance(o, Dict):
                        return {key: replace_steps_with_hashes(value) for key, value in o.items()}
                    else:
                        return o

                self.unique_id_cache += hash_object(replace_steps_with_hashes(self.kwargs))[:32]
            else:
                self.unique_id_cache += hash_object(random.getrandbits((58 ** 32).bit_length()))[
                    :32
                ]

        return self.unique_id_cache

    def __hash__(self):
        return hash(self.unique_id())

    def __eq__(self, other):
        if isinstance(self, Step):
            return self.unique_id() == other.unique_id()
        else:
            return False

    def dependencies(self) -> Set["Step"]:
        def dependencies_internal(o: Any) -> Iterable[Step]:
            if isinstance(o, Step):
                yield o
            elif isinstance(o, str):
                return  # Confusingly, str is an Iterable of itself, resulting in infinite recursion.
            elif isinstance(o, Iterable):
                yield from itertools.chain(*(dependencies_internal(i) for i in o))
            elif isinstance(o, Dict):
                yield from dependencies_internal(o.values())
            else:
                return

        return set(dependencies_internal(self.kwargs.values()))

    def recursive_dependencies(self) -> Set["Step"]:
        seen = set()
        steps = list(self.dependencies())
        while len(steps) > 0:
            step = steps.pop()
            if step in seen:
                continue
            seen.add(step)
            steps.extend(step.dependencies())
        return seen


@Step.register("ref")
class _RefStep(Step[T]):
    def run(self, ref: str) -> T:
        raise ConfigurationError(
            f"Step {self.name} is still a RefStep (referring to {ref}). RefSteps cannot be executed. "
            "They are only useful while parsing an experiment."
        )

    def ref(self) -> str:
        return self.kwargs["ref"]

    class MissingStepError(Exception):
        def __init__(self, ref: str):
            self.ref = ref


def step_graph_from_params(params: Dict[str, Params]) -> Dict[str, Step]:
    # This algorithm for resolving step dependencies is O(n^2). Since we're
    # anticipating the number of steps to be in the dozens at most, we choose
    # simplicity over cleverness.
    unparsed_steps: Dict[str, Params] = params
    next_unparsed_steps: Dict[str, Params] = {}
    parsed_steps: Dict[str, Step] = {}
    steps_parsed = 0
    while len(unparsed_steps) > 0 or len(next_unparsed_steps) > 0:
        if len(unparsed_steps) <= 0:
            if steps_parsed <= 0:
                raise ConfigurationError(
                    f"Cannot parse steps {','.join(next_unparsed_steps.keys())}. Do you have a "
                    f"circle in your steps, or are you referring to a step that doesn't exist?"
                )
            unparsed_steps = next_unparsed_steps
            next_unparsed_steps = {}
            steps_parsed = 0
        step_name, step_params = unparsed_steps.popitem()
        if step_name in parsed_steps:
            raise ConfigurationError(f"Duplicate step name {step_name}")
        step_params_backup = copy.deepcopy(step_params)
        try:
            parsed_steps[step_name] = Step.from_params(
                step_params, existing_steps=parsed_steps, extras={"step_name": step_name}
            )
            steps_parsed += 1
        except _RefStep.MissingStepError:
            next_unparsed_steps[step_name] = step_params_backup

    # Sanity-check the graph
    for step in parsed_steps.values():
        if step.cache_results:
            nondeterministic_dependencies = [
                s for s in step.recursive_dependencies() if not s.DETERMINISTIC
            ]
            if len(nondeterministic_dependencies) > 0:
                nd_step = nondeterministic_dependencies[0]
                logger.warning(
                    f"Task {step.name} is set to cache results, but depends on non-deterministic "
                    f"step {nd_step.name}. This will produce confusing results."
                )
                # We show this warning only once.
                break

    return parsed_steps