from __future__ import annotations

import contextlib
import hashlib
import itertools
import operator
import os
import re
import shutil
import sys
from copy import deepcopy
from functools import cached_property, reduce
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence, cast

import tomlkit
from pbs_installer import PythonVersion

from pdm._types import NotSet, NotSetType, RepositoryConfig
from pdm.compat import CompatibleSequence
from pdm.exceptions import NoPythonVersion, PdmUsageError, ProjectError
from pdm.models.backends import DEFAULT_BACKEND, BuildBackend, get_backend_by_spec
from pdm.models.caches import PackageCache
from pdm.models.markers import EnvSpec
from pdm.models.python import PythonInfo
from pdm.models.repositories import BaseRepository, LockedRepository
from pdm.models.requirements import Requirement, parse_line, parse_requirement, strip_extras
from pdm.models.specifiers import PySpecSet
from pdm.project.config import Config, ensure_boolean
from pdm.project.lockfile import FLAG_INHERIT_METADATA, Lockfile, load_lockfile
from pdm.project.project_file import PyProject
from pdm.utils import (
    cd,
    deprecation_warning,
    expand_env_vars_in_auth,
    find_project_root,
    find_python_in_path,
    get_all_installable_python_versions,
    get_class_init_params,
    is_conda_base,
    is_conda_base_python,
    is_path_relative_to,
    normalize_name,
)

if TYPE_CHECKING:
    from findpython import Finder

    from pdm.core import Core
    from pdm.environments import BaseEnvironment
    from pdm.installers.base import BaseSynchronizer
    from pdm.models.caches import CandidateInfoCache, HashCache, WheelCache
    from pdm.models.candidates import Candidate
    from pdm.resolver.base import Resolver
    from pdm.resolver.providers import BaseProvider
    from pdm.resolver.reporters import RichLockReporter


PYENV_ROOT = os.path.expanduser(os.getenv("PYENV_ROOT", "~/.pyenv"))


class Project:
    """Core project class.

    Args:
        core: The core instance.
        root_path: The root path of the project.
        is_global: Whether the project is global.
        global_config: The path to the global config file.
    """

    PYPROJECT_FILENAME = "pyproject.toml"
    DEPENDENCIES_RE = re.compile(r"(?:(.+?)-)?dependencies")

    def __init__(
        self,
        core: Core,
        root_path: str | Path | None,
        is_global: bool = False,
        global_config: str | Path | None = None,
    ) -> None:
        import platformdirs

        self._lockfile: Lockfile | None = None
        self._environment: BaseEnvironment | None = None
        self._python: PythonInfo | None = None
        self._cache_dir: Path | None = None
        self.core = core

        if global_config is None:
            global_config = platformdirs.user_config_path("pdm") / "config.toml"
        self.global_config = Config(Path(global_config), is_global=True)
        global_project = Path(self.global_config["global_project.path"]).expanduser()

        if root_path is None:
            root_path = find_project_root() if not is_global else global_project
        if (
            not is_global
            and root_path is None
            and self.global_config["global_project.fallback"]
            and not is_conda_base()
        ):
            root_path = global_project
            is_global = True
            if self.global_config["global_project.fallback_verbose"]:
                self.core.ui.info("Project is not found, fallback to the global project")

        self.root: Path = Path(root_path or "").absolute()
        self.is_global = is_global
        self.enable_write_lockfile = os.getenv("PDM_FROZEN_LOCKFILE", os.getenv("PDM_NO_LOCK", "0")).lower() not in (
            "1",
            "true",
        )
        self.init_global_project()

    def __repr__(self) -> str:
        return f"<Project '{self.root.as_posix()}'>"

    @cached_property
    def cache_dir(self) -> Path:
        return Path(self.config.get("cache_dir", "")).expanduser()

    @cached_property
    def pyproject(self) -> PyProject:
        return PyProject(self.root / self.PYPROJECT_FILENAME, ui=self.core.ui)

    @property
    def lockfile(self) -> Lockfile:
        if self._lockfile is None:
            enable_pylock = self.config["lock.format"] == "pylock"
            if (path := self.root / "pylock.toml").exists() and enable_pylock:
                self.set_lockfile(path)
            elif (path := self.root / "pdm.lock").exists():
                if enable_pylock:  # pragma: no cover
                    self.core.ui.warn(
                        "`lock.format` is set to pylock but pylock.toml is not found, using pdm.lock instead. "
                        "You can generate pylock with `pdm export -f pylock -o pylock.toml`."
                    )
                self.set_lockfile(path)
            else:
                file_path = "pylock.toml" if enable_pylock else "pdm.lock"
                self.set_lockfile(self.root / file_path)
            assert self._lockfile is not None
        return self._lockfile

    def set_lockfile(self, path: str | Path) -> None:
        self._lockfile = load_lockfile(self, path)
        if self.config.get("use_uv"):
            self._lockfile.default_strategies.discard(FLAG_INHERIT_METADATA)
        if not self.config["strategy.inherit_metadata"]:
            self._lockfile.default_strategies.discard(FLAG_INHERIT_METADATA)

    @cached_property
    def config(self) -> Mapping[str, Any]:
        """A read-only dict configuration"""
        import collections

        return collections.ChainMap(self.project_config, self.global_config)

    @property
    def scripts(self) -> dict[str, str | dict[str, str]]:
        return self.pyproject.settings.get("scripts", {})

    @cached_property
    def project_config(self) -> Config:
        """Read-and-writable configuration dict for project settings"""
        config = Config(self.root / "pdm.toml")
        # TODO: for backward compatibility, remove this in the future
        if self.root.joinpath(".pdm.toml").exists():
            legacy_config = Config(self.root / ".pdm.toml").self_data
            config.update((k, v) for k, v in legacy_config.items() if k != "python.path")
        return config

    @property
    def name(self) -> str:
        return self.pyproject.metadata.get("name")

    @property
    def python(self) -> PythonInfo:
        if not self._python:
            python = self.resolve_interpreter()
            if python.major < 3:
                raise PdmUsageError(
                    "Python 2.7 has reached EOL and PDM no longer supports it. "
                    "Please upgrade your Python to 3.6 or later.",
                )
            if self.is_global and is_conda_base_python(python.path):  # pragma: no cover
                raise PdmUsageError("Can't use global project in conda base environment since it is managed by conda")
            self._python = python
        return self._python

    @python.setter
    def python(self, value: PythonInfo) -> None:
        self._python = value
        self._saved_python = value.path.as_posix()

    @property
    def _saved_python(self) -> str | None:
        if os.getenv("PDM_PYTHON"):
            return os.getenv("PDM_PYTHON")
        with contextlib.suppress(FileNotFoundError):
            return self.root.joinpath(".pdm-python").read_text("utf-8").strip()
        with contextlib.suppress(FileNotFoundError):
            # TODO: remove this in the future
            with self.root.joinpath(".pdm.toml").open("rb") as fp:
                data = tomlkit.load(fp)
                if data.get("python", {}).get("path"):
                    return data["python"]["path"]
        return None

    @_saved_python.setter
    def _saved_python(self, value: str | None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        python_file = self.root.joinpath(".pdm-python")
        if value is None:
            with contextlib.suppress(FileNotFoundError):
                python_file.unlink()
            return
        python_file.write_text(value, "utf-8")

    def resolve_interpreter(self) -> PythonInfo:
        """Get the Python interpreter path."""
        from pdm.cli.commands.venv.utils import iter_venvs
        from pdm.models.venv import get_venv_python

        def match_version(python: PythonInfo) -> bool:
            return python.valid and self.python_requires.contains(python.version, True)

        def note(message: str) -> None:
            if not self.is_global:
                self.core.ui.info(message)

        def is_active_venv(python: PythonInfo) -> bool:
            if not (venv := os.getenv("VIRTUAL_ENV", os.getenv("CONDA_PREFIX"))):
                return False
            return is_path_relative_to(python.executable, venv)

        config = self.config
        saved_path = self._saved_python
        if saved_path and not ensure_boolean(os.getenv("PDM_IGNORE_SAVED_PYTHON")):
            python = PythonInfo.from_path(saved_path)
            if match_version(python):
                return python
            elif not python.valid:
                note("The saved Python interpreter does not exist or broken. Trying to find another one.")
            else:
                note(
                    "The saved Python interpreter doesn't match the project's requirement. Trying to find another one."
                )
            self._saved_python = None  # Clear the saved path if it doesn't match

        if config.get("python.use_venv") and not self.is_global:
            # Resolve virtual environments from env-vars
            ignore_active_venv = ensure_boolean(os.getenv("PDM_IGNORE_ACTIVE_VENV"))
            venv_in_env = os.getenv("VIRTUAL_ENV", os.getenv("CONDA_PREFIX"))
            # We don't auto reuse conda's base env since it may cause breakage when removing packages.
            if not ignore_active_venv and venv_in_env and not is_conda_base():
                python = PythonInfo.from_path(get_venv_python(Path(venv_in_env)))
                if match_version(python):
                    note(
                        f"Inside an active virtualenv [success]{venv_in_env}[/], reusing it.\n"
                        "Set env var [success]PDM_IGNORE_ACTIVE_VENV[/] to ignore it."
                    )
                    return python
            # otherwise, get a venv associated with the project
            for _, venv in iter_venvs(self):
                python = PythonInfo.from_path(venv.interpreter)
                if match_version(python) and not (ignore_active_venv and is_active_venv(python)):
                    note(f"Virtualenv [success]{venv.root}[/] is reused.")
                    self.python = python
                    return python

            if not self.root.joinpath("__pypackages__").exists():
                self.core.ui.warn(
                    f"Project requires a python version of {self.python_requires}, "
                    f"The virtualenv is being created for you as it cannot be matched to the right version."
                )
                note("python.use_venv is on, creating a virtualenv for this project...")
                venv_path = self._create_virtualenv()
                self.python = PythonInfo.from_path(get_venv_python(venv_path))
                return self.python

        if self.root.joinpath("__pypackages__").exists() or not config["python.use_venv"] or self.is_global:
            for py_version in self.iter_interpreters(
                filter_func=match_version, respect_version_file=config["python.use_python_version"]
            ):
                note("[success]__pypackages__[/] is detected, using the PEP 582 mode")
                self.python = py_version
                return py_version

        raise NoPythonVersion(f"No Python that satisfies {self.python_requires} is found on the system.")

    def get_environment(self) -> BaseEnvironment:
        from pdm.environments import PythonEnvironment, PythonLocalEnvironment

        """Get the environment selected by this project"""

        if self.is_global:
            env = PythonEnvironment(self)
            # Rewrite global project's python requires to be
            # compatible with the exact version
            env.python_requires = PySpecSet(f"=={self.python.version}")
            return env

        return (
            PythonEnvironment(self)
            if self.config["python.use_venv"] and self.python.get_venv() is not None
            else PythonLocalEnvironment(self)
        )

    def _create_virtualenv(self, python: str | None = None) -> Path:
        from pdm.cli.commands.venv.backends import BACKENDS

        backend: str = self.config["venv.backend"]
        if backend == "virtualenv" and self.config["use_uv"]:
            backend = "uv"
        venv_backend = BACKENDS[backend](self, python)
        path = venv_backend.create(
            force=True,
            in_project=self.config["venv.in_project"],
            prompt=self.config["venv.prompt"],
            with_pip=self.config["venv.with_pip"],
        )
        self.core.ui.echo(f"Virtualenv is created successfully at [success]{path}[/]", err=True)
        return path

    @property
    def environment(self) -> BaseEnvironment:
        if not self._environment:
            self._environment = self.get_environment()
        return self._environment

    @environment.setter
    def environment(self, value: BaseEnvironment | None) -> None:
        self._environment = value

    @property
    def python_requires(self) -> PySpecSet:
        return PySpecSet(self.pyproject.metadata.get("requires-python", ""))

    def get_dependencies(
        self, group: str | None = None, all_dependencies: dict[str, list[Requirement]] | None = None
    ) -> Sequence[Requirement]:
        group = normalize_name(group or "default")
        if all_dependencies is None:
            all_dependencies = self._resolve_dependencies([group])
        if group not in all_dependencies:
            raise ProjectError(f"Dependency group {group} does not exist")
        return CompatibleSequence(all_dependencies[group])

    def iter_groups(self) -> Iterable[str]:
        groups = {"default"}
        if self.pyproject.metadata.get("optional-dependencies"):
            groups.update(self.pyproject.metadata["optional-dependencies"].keys())
        groups.update(self.pyproject._data.get("dependency-groups", {}).keys())
        groups.update(self.pyproject.settings.get("dev-dependencies", {}).keys())
        return {normalize_name(g) for g in groups}

    def _resolve_dependencies(
        self, requested_groups: list[str] | None = None, include_referred: bool = True
    ) -> dict[str, list[Requirement]]:
        """Resolve dependencies for the given groups, and return a list of requirements for each group.

        The .groups attribute will be set to all that refers this requirement directly or indirectly.
        If `include_referred` is True, all self-references and `include-group` will be expanded to
        corresponding requirements. Otherwise, each group only contains explicitly defined requirements.
        """

        def _get_dependencies(group: str) -> tuple[list[Requirement], set[str]]:
            in_metadata = group in metadata_dependencies
            collected_deps: list[str] = []
            referred: set[str] = set()
            deps = metadata_dependencies.get(group, []) if in_metadata else dev_dependencies[group]
            for item in deps:
                if isinstance(item, str):
                    try:
                        name, extras = strip_extras(item)
                    except AssertionError:
                        pass
                    else:
                        if normalize_name(name) == project_name:
                            if extras:
                                allowed = (
                                    set(metadata_dependencies)
                                    if in_metadata
                                    else {*metadata_dependencies, *dev_dependencies}
                                )
                                extras = tuple(normalize_name(extra) for extra in extras)
                                not_allowed = set(extras) - allowed
                                if not_allowed:
                                    raise ProjectError(
                                        f"Optional dependency group '{group}' cannot "
                                        f"include non-existing extras: [{','.join(not_allowed)}]"
                                    )
                                referred.update(extras)
                            continue
                    collected_deps.append(item)
                elif not in_metadata and isinstance(item, dict):
                    if tuple(item.keys()) != ("include-group",):
                        raise ProjectError(f"Invalid dependency group item: {item}")
                    include_group = normalize_name(item["include-group"])
                    if include_group not in dev_dependencies:
                        raise ProjectError(f"Missing group '{include_group}' in `include-group`")
                    referred.add(include_group)
                else:
                    raise ProjectError(f"Invalid dependency in group {group}: {item}")
            result: list[Requirement] = []
            with cd(self.root):
                for line in collected_deps:
                    if line.startswith("-e ") and in_metadata:
                        self.core.ui.warn(
                            f"Skipping editable dependency [b]{line}[/] in the"
                            r" [success]\[project][/] table. Please move it to the "
                            r"[success]\[tool.pdm.dev-dependencies][/] table"
                        )
                        continue
                    req = parse_line(line)
                    req.groups = [group]
                    # make editable packages behind normal ones to override correctly.
                    result.append(req)
            return result, referred

        if requested_groups is None:
            requested_groups = list(self.iter_groups())
        requested_groups = [normalize_name(g) for g in requested_groups]
        referred_groups: dict[str, set[str]] = {}
        metadata_dependencies = {
            normalize_name(k): v for k, v in self.pyproject.metadata.get("optional-dependencies", {}).items()
        }
        metadata_dependencies["default"] = self.pyproject.metadata.get("dependencies", [])
        dev_dependencies = self.pyproject.dev_dependencies
        group_deps: dict[str, list[Requirement]] = {}
        project_name = normalize_name(self.name) if self.name else None
        for group in requested_groups:
            deps, referred = _get_dependencies(group)
            group_deps[group] = deps
            if referred:
                referred_groups[group] = referred
        extra_deps: dict[str, list[Requirement]] = {}
        while referred_groups:
            updated = False
            ref_iter = list(referred_groups.items())
            for group, referred in ref_iter:
                for ref in list(referred):
                    if ref not in requested_groups:
                        deps, r = _get_dependencies(ref)
                        group_deps[ref] = deps
                        if r:
                            referred_groups[ref] = r
                            # append to the ref_iter to process later
                            ref_iter.append((ref, r))
                        requested_groups.append(ref)
                    if ref in referred_groups:  # not resolved yet
                        continue
                    extra_deps.setdefault(group, []).extend(group_deps[ref])
                    for req in itertools.chain(group_deps[ref], extra_deps.get(ref, [])):
                        if group not in req.groups:
                            req.groups.append(group)
                    referred.remove(ref)
                    updated = True
                if not referred:
                    referred_groups.pop(group)
            if not updated:
                raise ProjectError(f"Cyclic dependency group include detected: {set(referred_groups)}")
        if include_referred:
            for group, deps in extra_deps.items():
                group_deps[group].extend(deps)
        return group_deps

    @property
    def all_dependencies(self) -> dict[str, Sequence[Requirement]]:
        return {k: CompatibleSequence(v) for k, v in self._resolve_dependencies(include_referred=False).items()}

    @property
    def default_source(self) -> RepositoryConfig:
        """Get the default source from the pypi setting"""
        config = RepositoryConfig(
            config_prefix="pypi",
            name="pypi",
            url=self.config["pypi.url"],
            verify_ssl=self.config["pypi.verify_ssl"],
            username=self.config.get("pypi.username"),
            password=self.config.get("pypi.password"),
            ca_certs=self.config.get("pypi.ca_certs"),
            client_cert=self.config.get("pypi.client_cert"),
            client_key=self.config.get("pypi.client_key"),
        )
        return config

    @property
    def sources(self) -> list[RepositoryConfig]:
        return self.get_sources(include_stored=not self.config.get("pypi.ignore_stored_index", False))

    def get_sources(self, expand_env: bool = True, include_stored: bool = False) -> list[RepositoryConfig]:
        result: dict[str, RepositoryConfig] = {}
        for source in self.pyproject.settings.get("source", []):
            result[source["name"]] = RepositoryConfig(**source, config_prefix="pypi")

        def merge_sources(other_sources: Iterable[RepositoryConfig]) -> None:
            for source in other_sources:
                name = source.name
                if name in result:
                    result[name].passive_update(source)
                elif include_stored:
                    result[name] = source

        merge_sources(self.project_config.iter_sources())
        merge_sources(self.global_config.iter_sources())
        if "pypi" in result:
            result["pypi"].passive_update(self.default_source)
        elif include_stored:
            # put pypi source at the beginning
            result = {"pypi": self.default_source, **result}

        sources: list[RepositoryConfig] = []
        for source in result.values():
            if not source.url:
                continue
            if expand_env:
                source.url = DEFAULT_BACKEND(self.root).expand_line(expand_env_vars_in_auth(source.url))
            sources.append(source)
        return sources

    def get_repository(
        self,
        cls: type[BaseRepository] | None = None,
        ignore_compatibility: bool | NotSetType = NotSet,
        env_spec: EnvSpec | None = None,
    ) -> BaseRepository:
        """Get the repository object"""
        if cls is None:
            cls = self.core.repository_class
        sources = self.sources or []
        params = get_class_init_params(cls)
        if "env_spec" in params:
            return cls(sources, self.environment, env_spec=env_spec)
        else:
            return cls(sources, self.environment, ignore_compatibility=ignore_compatibility)

    def get_locked_repository(self, env_spec: EnvSpec | None = None) -> LockedRepository:
        try:
            lockfile = self.lockfile._data.unwrap()
        except ProjectError:
            lockfile = {}

        return LockedRepository(lockfile, self.sources, self.environment, env_spec=env_spec)

    def split_extras_groups(self, all_groups: list[str]) -> tuple[list[str], list[str]]:
        """Split the groups into extras and non-extras."""
        extras: list[str] = []
        groups: list[str] = []
        optional_groups = {normalize_name(group) for group in self.pyproject.metadata.get("optional-dependencies", [])}
        for group in all_groups:
            if group in optional_groups:
                extras.append(group)
            else:
                groups.append(group)
        return extras, groups

    @property
    def locked_repository(self) -> LockedRepository:
        deprecation_warning("Project.locked_repository is deprecated, use Project.get_locked_repository() instead", 2)
        return self.get_locked_repository()

    def get_provider(
        self,
        strategy: str = "all",
        tracked_names: Iterable[str] | None = None,
        for_install: bool = False,
        ignore_compatibility: bool | NotSetType = NotSet,
        direct_minimal_versions: bool = False,
        env_spec: EnvSpec | None = None,
        locked_repository: LockedRepository | None = None,
    ) -> BaseProvider:
        """Build a provider class for resolver.

        :param strategy: the resolve strategy
        :param tracked_names: the names of packages that needs to update
        :param for_install: if the provider is for install
        :param ignore_compatibility: if the provider should ignore the compatibility when evaluating candidates
        :param direct_minimal_versions: if the provider should prefer minimal versions instead of latest
        :returns: The provider object
        """

        import inspect

        from pdm.resolver.providers import get_provider

        if env_spec is None:
            env_spec = (
                self.environment.allow_all_spec if ignore_compatibility in (True, NotSet) else self.environment.spec
            )
        repo_params = inspect.signature(self.get_repository).parameters
        if "env_spec" in repo_params:
            repository = self.get_repository(env_spec=env_spec)
        else:  # pragma: no cover
            repository = self.get_repository(ignore_compatibility=ignore_compatibility)
        if locked_repository is None:
            try:
                locked_repository = self.get_locked_repository(env_spec)
            except Exception:  # pragma: no cover
                if strategy != "all":
                    self.core.ui.warn("Unable to reuse the lock file as it is not compatible with PDM")

        provider_class = get_provider(strategy)
        params: dict[str, Any] = {}
        if strategy != "all":
            params["tracked_names"] = [strip_extras(name)[0] for name in tracked_names or ()]
        if "locked_repository" in inspect.signature(provider_class).parameters:
            params["locked_repository"] = locked_repository
        else:
            locked_candidates: dict[str, list[Candidate]] = (
                {} if locked_repository is None else locked_repository.all_candidates
            )
            params["locked_candidates"] = locked_candidates
        return provider_class(repository=repository, direct_minimal_versions=direct_minimal_versions, **params)

    def get_reporter(
        self, requirements: list[Requirement], tracked_names: Iterable[str] | None = None
    ) -> RichLockReporter:  # pragma: no cover
        """Return the reporter object to construct a resolver.

        :param requirements: requirements to resolve
        :param tracked_names: the names of packages that needs to update
        :param spinner: optional spinner object
        :returns: a reporter
        """
        from pdm.resolver.reporters import RichLockReporter

        return RichLockReporter(requirements, self.core.ui)

    def write_lockfile(
        self, toml_data: Any = None, show_message: bool = True, write: bool = True, **_kwds: Any
    ) -> None:
        """Write the lock file to disk."""
        if _kwds:  # pragma: no cover
            deprecation_warning("Extra arguments have been moved to `format_lockfile` function", stacklevel=2)
        if toml_data is not None:  # pragma: no cover
            deprecation_warning(
                "Passing toml_data to write_lockfile is deprecated, please use `format_lockfile` instead", stacklevel=2
            )
            self.lockfile.set_data(toml_data)
        self.lockfile.update_hash(self.pyproject.content_hash("sha256"))
        if write and self.enable_write_lockfile:
            self.lockfile.write(show_message)

    def make_self_candidate(self, editable: bool = True) -> Candidate:
        from unearth import Link

        from pdm.models.candidates import Candidate

        req = parse_requirement(self.root.as_uri(), editable)
        assert self.name
        req.name = self.name
        can = Candidate(req, name=self.name, link=Link.from_path(self.root))
        can.prepare(self.environment).metadata
        return can

    def is_lockfile_hash_match(self) -> bool:
        algo, hash_value = self.lockfile.hash
        if not hash_value:
            return False
        content_hash = self.pyproject.content_hash(algo)
        return content_hash == hash_value

    def use_pyproject_dependencies(
        self, group: str, dev: bool = False
    ) -> tuple[list[str], Callable[[list[str]], None]]:
        """Get the dependencies array and setter in the pyproject.toml
        Return a tuple of two elements, the first is the dependencies array,
        and the second value is a callable to set the dependencies array back.
        """
        from pdm.formats.base import make_array

        def update_dev_dependencies(deps: list[str]) -> None:
            from tomlkit.container import OutOfOrderTableProxy

            dependency_groups: list[str | dict[str, str]] = tomlkit.array().multiline(True)
            dev_dependencies: list[str] = tomlkit.array().multiline(True)
            for dep in deps:
                if isinstance(dep, str) and dep.startswith("-e"):
                    dev_dependencies.append(dep)
                else:
                    dependency_groups.append(dep)
            if dependency_groups:
                self.pyproject.dependency_groups[group] = dependency_groups
            else:
                self.pyproject.dependency_groups.pop(group, None)
            if dev_dependencies:
                settings.setdefault("dev-dependencies", {})[group] = dev_dependencies
            else:
                settings.setdefault("dev-dependencies", {}).pop(group, None)
            if isinstance(self.pyproject._data["tool"], OutOfOrderTableProxy):
                # In case of a separate table, we have to remove and re-add it to make the write correct.
                # This may change the order of tables in the TOML file, but it's the best we can do.
                # see bug pdm-project/pdm#2056 for details
                del self.pyproject._data["tool"]["pdm"]
                self.pyproject._data["tool"]["pdm"] = settings

        metadata, settings = self.pyproject.metadata, self.pyproject.settings
        if group == "default":
            return metadata.get("dependencies", tomlkit.array()), lambda x: metadata.__setitem__("dependencies", x)
        dev_dependencies = deepcopy(self.pyproject._data.get("dependency-groups", {}))
        for dev_group, items in self.pyproject.settings.get("dev-dependencies", {}).items():
            dev_dependencies.setdefault(dev_group, []).extend(items)
        deps_setter = [
            (
                metadata.get("optional-dependencies", {}),
                lambda x: metadata.setdefault("optional-dependencies", {}).__setitem__(group, x)
                if x
                else metadata.setdefault("optional-dependencies", {}).pop(group, None),
            ),
            (dev_dependencies, update_dev_dependencies),
        ]
        normalized_group = normalize_name(group)
        for deps, setter in deps_setter:
            normalized_groups = {normalize_name(g) for g in deps}
            if group in deps:
                return make_array(deps[group], True), setter
            if normalized_group in normalized_groups:
                raise PdmUsageError(f"Group {group} already exists in another non-normalized form")
        # If not found, return an empty list and a setter to add the group
        return tomlkit.array().multiline(True), deps_setter[int(dev)][1]

    def add_dependencies(
        self,
        requirements: Iterable[str | Requirement],
        to_group: str = "default",
        dev: bool = False,
        show_message: bool = True,
        write: bool = True,
    ) -> list[Requirement]:
        """Add requirements to the given group, and return the requirements of that group."""
        if isinstance(requirements, Mapping):  # pragma: no cover
            deprecation_warning(
                "Passing a requirements map to add_dependencies is deprecated, please pass an iterable", stacklevel=2
            )
            requirements = requirements.values()
        deps, setter = self.use_pyproject_dependencies(to_group, dev)
        updated_indices: set[int] = set()

        with cd(self.root):
            parsed_deps = [(parse_line(dep) if isinstance(dep, str) else None) for dep in deps]

            for req in requirements:
                if isinstance(req, str):
                    req = parse_line(req)
                matched_index = next(
                    (
                        i
                        for i, r in enumerate(deps)
                        if isinstance(r, str) and req.matches(r) and i not in updated_indices
                    ),
                    None,
                )
                dep = req.as_line()
                if matched_index is None:
                    updated_indices.add(len(deps))
                    deps.append(dep)
                    parsed_deps.append(req)
                else:
                    deps[matched_index] = dep
                    parsed_deps[matched_index] = req
                    updated_indices.add(matched_index)
        setter(deps)
        if write:
            self.pyproject.write(show_message)
        for r in parsed_deps:
            if r is not None:
                r.groups = [to_group]
        return [r for r in parsed_deps if r is not None]

    def init_global_project(self) -> None:
        if not self.is_global or not self.pyproject.empty():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.pyproject.set_data({"project": {"dependencies": ["pip", "setuptools", "wheel"]}})
        self.pyproject.write()

    @property
    def backend(self) -> BuildBackend:
        return get_backend_by_spec(self.pyproject.build_system)(self.root)

    def cache(self, name: str) -> Path:
        path = self.cache_dir / name
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # The path could be not accessible
            pass
        return path

    def make_wheel_cache(self) -> WheelCache:
        from pdm.models.caches import get_wheel_cache

        return get_wheel_cache(self.cache("wheels"))

    @property
    def package_cache(self) -> PackageCache:
        return PackageCache(self.cache("packages"))

    def make_candidate_info_cache(self) -> CandidateInfoCache:
        from pdm.models.caches import CandidateInfoCache, EmptyCandidateInfoCache

        python_hash = hashlib.sha1(str(self.environment.python_requires).encode()).hexdigest()
        file_name = f"package_meta_{python_hash}.json"
        return (
            CandidateInfoCache(self.cache("metadata") / file_name)
            if self.core.state.enable_cache
            else EmptyCandidateInfoCache(self.cache("metadata") / file_name)
        )

    def make_hash_cache(self) -> HashCache:
        from pdm.models.caches import EmptyHashCache, HashCache

        return HashCache(self.cache("hashes")) if self.core.state.enable_cache else EmptyHashCache(self.cache("hashes"))

    def iter_interpreters(
        self,
        python_spec: str | None = None,
        search_venv: bool | None = None,
        filter_func: Callable[[PythonInfo], bool] | None = None,
        respect_version_file: bool = True,
    ) -> Iterable[PythonInfo]:
        """Iterate over all interpreters that matches the given specifier.
        And optionally install the interpreter if not found.
        """
        from packaging.version import InvalidVersion

        from pdm.cli.commands.python import InstallCommand

        def read_version_from_version_file(python_version_file: Path) -> str | None:
            content = python_version_file.read_text().strip()
            content_lines = [cl for cl in content.splitlines() if not cl.lstrip().startswith("#")]

            return content_lines[0] if len(content_lines) == 1 else None

        version_file = self.root.joinpath(".python-version")
        found = False
        if respect_version_file and not python_spec and (os.getenv("PDM_PYTHON_VERSION") or version_file.exists()):
            requested = os.getenv("PDM_PYTHON_VERSION") or read_version_from_version_file(version_file)
            if requested is not None and requested not in self.python_requires:
                self.core.ui.warn(".python-version is found but the version is not in requires-python, ignored.")
            elif requested is not None:
                python_spec = requested
        for interpreter in self.find_interpreters(python_spec, search_venv):
            if filter_func is None or filter_func(interpreter):
                found = True
                yield interpreter
        if found or self.is_global:
            return

        if not python_spec:  # handle both empty string and None
            # Get the best match meeting the requires-python
            best_match = self.get_best_matching_cpython_version()
            if best_match is None:
                return
            python_spec = str(best_match)
        else:
            try:
                if python_spec not in self.python_requires:
                    return
            except InvalidVersion:
                return
        try:
            # otherwise if no interpreter is found, try to install it
            installed = InstallCommand.install_python(self, python_spec)
        except Exception as e:
            self.core.ui.error(f"Failed to install Python {python_spec}: {e}")
            return
        else:
            if filter_func is None or filter_func(installed):
                yield installed

    def find_interpreters(
        self, python_spec: str | None = None, search_venv: bool | None = None
    ) -> Iterable[PythonInfo]:
        """Return an iterable of interpreter paths that matches the given specifier,
        which can be:
            1. a version specifier like 3.7
            2. an absolute path
            3. a short name like python3
            4. None that returns all possible interpreters
        """
        config = self.config
        python: str | Path | None = None
        finder_arg: str | None = None

        if not python_spec:
            if config.get("python.use_pyenv", True) and os.path.exists(PYENV_ROOT):
                pyenv_shim = os.path.join(PYENV_ROOT, "shims", "python3")
                if os.name == "nt":
                    pyenv_shim += ".bat"
                if os.path.exists(pyenv_shim):
                    yield PythonInfo.from_path(pyenv_shim)
                elif os.path.exists(pyenv_shim.replace("python3", "python")):
                    yield PythonInfo.from_path(pyenv_shim.replace("python3", "python"))
            python = shutil.which("python") or shutil.which("python3")
            if python:
                yield PythonInfo.from_path(python)
        else:
            if not all(c.isdigit() for c in python_spec.split(".")):
                path = Path(python_spec)
                if path.exists():
                    python = find_python_in_path(python_spec)
                    if python:
                        yield PythonInfo.from_path(python)
                        return
                if len(path.parts) == 1:  # only check for spec with only one part
                    python = shutil.which(python_spec)
                    if python:
                        yield PythonInfo.from_path(python)
                        return
            finder_arg = python_spec
        if search_venv is None:
            search_venv = cast(bool, config["python.use_venv"])
        finder = self._get_python_finder(search_venv)
        for entry in finder.find_all(finder_arg, allow_prereleases=True):
            yield PythonInfo(entry)
        if not python_spec:
            # Lastly, return the host Python as well
            this_python = getattr(sys, "_base_executable", sys.executable)
            yield PythonInfo.from_path(this_python)

    def _get_python_finder(self, search_venv: bool = True) -> Finder:
        from findpython import Finder

        from pdm.cli.commands.venv.utils import VenvProvider

        providers: list[str] = self.config["python.providers"]
        venv_pos = -1
        if not providers:
            venv_pos = 0
        elif "venv" in providers:
            venv_pos = providers.index("venv")
            providers.remove("venv")
        old_rye_root = os.getenv("RYE_PY_ROOT")
        os.environ["RYE_PY_ROOT"] = os.path.expanduser(self.config["python.install_root"])
        try:
            finder = Finder(resolve_symlinks=True, selected_providers=providers or None)
        finally:
            if old_rye_root:  # pragma: no cover
                os.environ["RYE_PY_ROOT"] = old_rye_root
            else:
                del os.environ["RYE_PY_ROOT"]
        if search_venv and venv_pos >= 0:
            finder.add_provider(VenvProvider(self), venv_pos)
        return finder

    @property
    def is_distribution(self) -> bool:
        if not self.name:
            return False
        settings = self.pyproject.settings
        if "package-type" in settings:
            return settings["package-type"] == "library"
        elif "distribution" in settings:
            return cast(bool, settings["distribution"])
        else:
            return True

    def get_setting(self, key: str) -> Any:
        """
        Get a setting from its dotted key (without the `tool.pdm` prefix).

        Returns `None` if the key does not exists.
        """
        try:
            return reduce(operator.getitem, key.split("."), self.pyproject.settings)
        except KeyError:
            return None

    def env_or_setting(self, var: str, key: str) -> Any:
        """
        Get a value from environment variable and fallback on a given setting.

        Returns `None` if both the environment variable and the key does not exists.
        """
        return os.getenv(var.upper()) or self.get_setting(key)

    def get_best_matching_cpython_version(
        self, use_minimum: bool | None = False, freethreaded: bool = False
    ) -> PythonVersion | None:
        """
        Returns the best matching CPython version that fits requires-python, this platform and arch.
        If no best match could be found, return None.

        Default for best match strategy is "highest" possible interpreter version. If "minimum" shall be used,
        set `use_minimum` to True.
        """

        def get_version(version: PythonVersion) -> str:
            return f"{version.major}.{version.minor}.{version.micro}"

        all_matches = get_all_installable_python_versions(build_dir=False)
        filtered_matches = [
            v
            for v in all_matches
            if v.freethreaded == freethreaded
            and get_version(v) in self.python_requires
            and v.implementation.lower() == "cpython"
        ]
        if filtered_matches:
            if use_minimum:
                return min(filtered_matches, key=lambda v: (v.major, v.minor, v.micro))
            return max(filtered_matches, key=lambda v: (v.major, v.minor, v.micro))

        return None

    @property
    def lock_targets(self) -> list[EnvSpec]:
        return [self.environment.allow_all_spec]

    def get_resolver(self, allow_uv: bool = True) -> type[Resolver]:
        """Get the resolver class to use for the project."""
        from pdm.resolver.resolvelib import RLResolver
        from pdm.resolver.uv import UvResolver

        if allow_uv and self.config.get("use_uv"):
            return UvResolver
        else:
            return RLResolver

    def get_synchronizer(self, quiet: bool = False, allow_uv: bool = True) -> type[BaseSynchronizer]:
        """Get the synchronizer class to use for the project."""
        from pdm.installers import BaseSynchronizer, Synchronizer, UvSynchronizer
        from pdm.installers.uv import QuietUvSynchronizer

        if allow_uv and self.config.get("use_uv"):
            return QuietUvSynchronizer if quiet else UvSynchronizer
        if quiet:
            return BaseSynchronizer
        return getattr(self.core, "synchronizer_class", Synchronizer)
