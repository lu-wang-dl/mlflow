# This module is copied from legacy databricks CLI python library
# module `databricks_cli.configure.provider`, see
# https://github.com/databricks/databricks-cli/blob/0.18.0/databricks_cli/configure/provider.py
# because the latest Databricks Runtime does not contain legacy databricks CLI
# but MLflow still depends on it.

import os
import sys
from abc import ABCMeta, abstractmethod
from configparser import ConfigParser
from os.path import expanduser, join

_home = expanduser("~")
CONFIG_FILE_ENV_VAR = "DATABRICKS_CONFIG_FILE"
HOST = "host"
USERNAME = "username"
PASSWORD = "password"  # NOQA
TOKEN = "token"
REFRESH_TOKEN = "refresh_token"
INSECURE = "insecure"
JOBS_API_VERSION = "jobs-api-version"
DEFAULT_SECTION = "DEFAULT"

# User-provided override for the DatabricksConfigProvider
_config_provider = None


class InvalidConfigurationError(RuntimeError):
    @staticmethod
    def for_profile(profile):
        if profile is None:
            return InvalidConfigurationError(
                "You haven't configured the CLI yet! "
                f"Please configure by entering `{sys.argv[0]} configure`"
            )
        return InvalidConfigurationError(
            f"You haven't configured the CLI yet for the profile {profile}! "
            "Please configure by entering "
            f"`{sys.argv[0]} configure --profile {profile}`"
        )


def _get_path():
    return os.environ.get(CONFIG_FILE_ENV_VAR, join(_home, ".databrickscfg"))


def _fetch_from_fs():
    raw_config = ConfigParser()
    raw_config.read(_get_path())
    return raw_config


def _create_section_if_absent(raw_config, profile):
    if not raw_config.has_section(profile) and profile != DEFAULT_SECTION:
        raw_config.add_section(profile)


def _get_option_if_exists(raw_config, profile, option):
    if profile == DEFAULT_SECTION:
        # We must handle the DEFAULT_SECTION differently since it is not in the _sections property
        # of raw config.
        return raw_config.get(profile, option) if raw_config.has_option(profile, option) else None
    # Check if option is defined in the profile.
    elif option not in raw_config._sections.get(profile, {}).keys():
        return None
    return raw_config.get(profile, option)


def _set_option(raw_config, profile, option, value):
    if value:
        raw_config.set(profile, option, value)
    else:
        raw_config.remove_option(profile, option)


def _overwrite_config(raw_config):
    config_path = _get_path()
    # Create config file with owner only rw permissions
    if not os.path.exists(config_path):
        file_descriptor = os.open(config_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(file_descriptor)

    # Change file permissions to owner only rw if that's not the case
    if not os.stat(config_path).st_mode == 0o100600:
        os.chmod(config_path, 0o600)

    with open(config_path, "w") as cfg:
        raw_config.write(cfg)


def update_and_persist_config(profile, databricks_config):
    """
    Takes a DatabricksConfig and adds the in memory contents to the persisted version of the
    config. This will overwrite any other config that was persisted to the file system under the
    same profile.

    Args:
        databricks_config: DatabricksConfig
    """
    profile = profile if profile else DEFAULT_SECTION
    raw_config = _fetch_from_fs()
    _create_section_if_absent(raw_config, profile)
    _set_option(raw_config, profile, HOST, databricks_config.host)
    _set_option(raw_config, profile, USERNAME, databricks_config.username)
    _set_option(raw_config, profile, PASSWORD, databricks_config.password)
    _set_option(raw_config, profile, TOKEN, databricks_config.token)
    _set_option(raw_config, profile, REFRESH_TOKEN, databricks_config.refresh_token)
    _set_option(raw_config, profile, INSECURE, databricks_config.insecure)
    _set_option(raw_config, profile, JOBS_API_VERSION, databricks_config.jobs_api_version)
    _overwrite_config(raw_config)


def get_config():
    """
    Returns a DatabricksConfig containing the hostname and authentication used to talk to
    the Databricks API. By default, we leverage the DefaultConfigProvider to get
    this config, but this behavior may be overridden by calling 'set_config_provider'

    If no DatabricksConfig can be found, an InvalidConfigurationError will be raised.
    """
    global _config_provider
    if _config_provider:
        config = _config_provider.get_config()
        if config:
            return config
        raise InvalidConfigurationError(
            "Custom provider returned no DatabricksConfig: %s" % _config_provider
        )

    config = DefaultConfigProvider().get_config()
    if config:
        return config
    raise InvalidConfigurationError.for_profile(None)


def get_config_for_profile(profile):
    """
    [Deprecated] Reads from the filesystem and gets a DatabricksConfig for the
    specified profile. If it does not exist, then return a DatabricksConfig with fields set
    to None.

    Internal callers should prefer get_config() to use user-specified overrides, and
    to return appropriate error messages as opposited to invalid configurations.

    If you want to read from a specific profile, please instead use
    'ProfileConfigProvider(profile).get_config()'.

    This method is maintained for backwards-compatibility. It may be removed in future versions.

    Returns:
        DatabricksConfig
    """
    profile = profile if profile else DEFAULT_SECTION
    config = EnvironmentVariableConfigProvider().get_config()
    if config and config.is_valid:
        return config

    config = ProfileConfigProvider(profile).get_config()
    if config:
        return config
    return DatabricksConfig.empty()


def set_config_provider(provider):
    """
    Sets a DatabricksConfigProvider that will be used for all future calls to get_config(),
    used by the Databricks CLI code to discover the user's credentials.
    """
    global _config_provider
    if provider and not isinstance(provider, DatabricksConfigProvider):
        raise Exception("Must be instance of DatabricksConfigProvider: %s" % _config_provider)
    _config_provider = provider


def get_config_provider():
    """
    Returns the current DatabricksConfigProvider.
    If None, the DefaultConfigProvider will be used.
    """
    global _config_provider
    return _config_provider


class DatabricksConfigProvider:
    """
    Responsible for providing hostname and authentication information to make
    API requests against the Databricks REST API.
    This method should generally return None if it cannot provide credentials, in order
    to facilitate chanining of providers.
    """

    __metaclass__ = ABCMeta

    @abstractmethod
    def get_config(self):
        pass


class DefaultConfigProvider(DatabricksConfigProvider):
    """Look for credentials in a chain of default locations."""

    def __init__(self):
        self._providers = (
            SparkTaskContextConfigProvider(),
            EnvironmentVariableConfigProvider(),
            ProfileConfigProvider(),
        )

    def get_config(self):
        for provider in self._providers:
            config = provider.get_config()
            if config is not None and config.is_valid:
                return config
        return None


class SparkTaskContextConfigProvider(DatabricksConfigProvider):
    """Loads credentials from Spark TaskContext if running in a Spark Executor."""

    @staticmethod
    def _get_spark_task_context_or_none():
        try:
            from pyspark import TaskContext  # pylint: disable=import-error

            return TaskContext.get()
        except ImportError:
            return None

    @staticmethod
    def set_insecure(x):
        from pyspark import SparkContext  # pylint: disable=import-error

        new_val = "True" if x else None
        SparkContext._active_spark_context.setLocalProperty("spark.databricks.ignoreTls", new_val)

    def get_config(self):
        context = self._get_spark_task_context_or_none()
        if context is not None:
            host = context.getLocalProperty("spark.databricks.api.url")
            token = context.getLocalProperty("spark.databricks.token")
            insecure = context.getLocalProperty("spark.databricks.ignoreTls")
            config = DatabricksConfig.from_token(
                host=host, token=token, refresh_token=None, insecure=insecure, jobs_api_version=None
            )
            if config.is_valid:
                return config
        return None


class EnvironmentVariableConfigProvider(DatabricksConfigProvider):
    """Loads from system environment variables."""

    def get_config(self):
        host = os.environ.get("DATABRICKS_HOST")
        username = os.environ.get("DATABRICKS_USERNAME")
        password = os.environ.get("DATABRICKS_PASSWORD")
        token = os.environ.get("DATABRICKS_TOKEN")
        refresh_token = os.environ.get("DATABRICKS_REFRESH_TOKEN")
        insecure = os.environ.get("DATABRICKS_INSECURE")
        jobs_api_version = os.environ.get("DATABRICKS_JOBS_API_VERSION")
        config = DatabricksConfig(
            host, username, password, token, refresh_token, insecure, jobs_api_version
        )
        if config.is_valid:
            return config
        return None


class ProfileConfigProvider(DatabricksConfigProvider):
    """Loads from the databrickscfg file."""

    def __init__(self, profile=DEFAULT_SECTION):
        self.profile = profile

    def get_config(self):
        raw_config = _fetch_from_fs()
        host = _get_option_if_exists(raw_config, self.profile, HOST)
        username = _get_option_if_exists(raw_config, self.profile, USERNAME)
        password = _get_option_if_exists(raw_config, self.profile, PASSWORD)
        token = _get_option_if_exists(raw_config, self.profile, TOKEN)
        refresh_token = _get_option_if_exists(raw_config, self.profile, REFRESH_TOKEN)
        insecure = _get_option_if_exists(raw_config, self.profile, INSECURE)
        jobs_api_version = _get_option_if_exists(raw_config, self.profile, JOBS_API_VERSION)
        config = DatabricksConfig(
            host, username, password, token, refresh_token, insecure, jobs_api_version
        )
        if config.is_valid:
            return config
        return None


class DatabricksConfig:
    def __init__(
        self,
        host,
        username,
        password,
        token,
        refresh_token=None,
        insecure=None,
        jobs_api_version=None,
    ):  # noqa
        self.host = host
        self.username = username
        self.password = password
        self.token = token
        self.refresh_token = refresh_token
        self.insecure = insecure
        self.jobs_api_version = jobs_api_version

    @classmethod
    def from_token(cls, host, token, refresh_token=None, insecure=None, jobs_api_version=None):
        return DatabricksConfig(
            host=host,
            username=None,
            password=None,
            token=token,
            refresh_token=refresh_token,
            insecure=insecure,
            jobs_api_version=jobs_api_version,
        )

    @classmethod
    def from_password(cls, host, username, password, insecure=None, jobs_api_version=None):
        return DatabricksConfig(
            host=host,
            username=username,
            password=password,
            token=None,
            refresh_token=None,
            insecure=insecure,
            jobs_api_version=jobs_api_version,
        )

    @classmethod
    def empty(cls):
        return DatabricksConfig(
            host=None,
            username=None,
            password=None,
            token=None,
            refresh_token=None,
            insecure=None,
            jobs_api_version=None,
        )

    @property
    def is_valid_with_token(self):
        return self.host is not None and self.token is not None

    @property
    def is_valid_with_password(self):
        return self.host is not None and self.username is not None and self.password is not None

    @property
    def is_valid(self):
        return self.is_valid_with_token or self.is_valid_with_password
