# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2017 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Configuration files residing on disk."""

import types
import os.path
import textwrap
import traceback
import configparser
import contextlib

import yaml
from PyQt5.QtCore import QSettings

import qutebrowser
from qutebrowser.config import configexc, config
from qutebrowser.utils import standarddir, utils, qtutils


# The StateConfig instance
state = None


class StateConfig(configparser.ConfigParser):

    """The "state" file saving various application state."""

    def __init__(self):
        super().__init__()
        self._filename = os.path.join(standarddir.data(), 'state')
        self.read(self._filename, encoding='utf-8')
        for sect in ['general', 'geometry']:
            try:
                self.add_section(sect)
            except configparser.DuplicateSectionError:
                pass

        deleted_keys = ['fooled', 'backend-warning-shown']
        for key in deleted_keys:
            self['general'].pop(key, None)

    def init_save_manager(self, save_manager):
        """Make sure the config gets saved properly.

        We do this outside of __init__ because the config gets created before
        the save_manager exists.
        """
        save_manager.add_saveable('state-config', self._save)

    def _save(self):
        """Save the state file to the configured location."""
        with open(self._filename, 'w', encoding='utf-8') as f:
            self.write(f)


class YamlConfig:

    """A config stored on disk as YAML file.

    Class attributes:
        VERSION: The current version number of the config file.
    """

    VERSION = 1

    def __init__(self):
        self._filename = os.path.join(standarddir.config(auto=True),
                                      'autoconfig.yml')
        self.values = {}

    def init_save_manager(self, save_manager):
        """Make sure the config gets saved properly.

        We do this outside of __init__ because the config gets created before
        the save_manager exists.
        """
        save_manager.add_saveable('yaml-config', self._save)

    def _save(self):
        """Save the changed settings to the YAML file."""
        data = {'config_version': self.VERSION, 'global': self.values}
        with qtutils.savefile_open(self._filename) as f:
            f.write(textwrap.dedent("""
                # DO NOT edit this file by hand, qutebrowser will overwrite it.
                # Instead, create a config.py - see :help for details.

            """.lstrip('\n')))
            utils.yaml_dump(data, f)

    def load(self):
        """Load self.values from the configured YAML file."""
        try:
            with open(self._filename, 'r', encoding='utf-8') as f:
                yaml_data = utils.yaml_load(f)
        except FileNotFoundError:
            return
        except OSError as e:
            desc = configexc.ConfigErrorDesc("While reading", e)
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])
        except yaml.YAMLError as e:
            desc = configexc.ConfigErrorDesc("While parsing", e)
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])

        try:
            global_obj = yaml_data['global']
        except KeyError:
            desc = configexc.ConfigErrorDesc(
                "While loading data",
                "Toplevel object does not contain 'global' key")
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])
        except TypeError:
            desc = configexc.ConfigErrorDesc("While loading data",
                                             "Toplevel object is not a dict")
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])

        if not isinstance(global_obj, dict):
            desc = configexc.ConfigErrorDesc(
                "While loading data",
                "'global' object is not a dict")
            raise configexc.ConfigFileErrors('autoconfig.yml', [desc])

        self.values = global_obj


class ConfigAPI:

    """Object which gets passed to config.py as "config" object.

    This is a small wrapper over the Config object, but with more
    straightforward method names (get/set call get_obj/set_obj) and a more
    shallow API.

    Attributes:
        _config: The main Config object to use.
        _keyconfig: The KeyConfig object.
        load_autoconfig: Whether autoconfig.yml should be loaded.
        errors: Errors which occurred while setting options.
    """

    def __init__(self, conf, keyconfig):
        self._config = conf
        self._keyconfig = keyconfig
        self.load_autoconfig = True
        self.errors = []

    @contextlib.contextmanager
    def _handle_error(self, action, name):
        try:
            yield
        except configexc.Error as e:
            text = "While {} '{}'".format(action, name)
            self.errors.append(configexc.ConfigErrorDesc(text, e))

    def finalize(self):
        """Do work which needs to be done after reading config.py."""
        self._config.update_mutables()

    def get(self, name):
        with self._handle_error('getting', name):
            return self._config.get_obj(name)

    def set(self, name, value):
        with self._handle_error('setting', name):
            self._config.set_obj(name, value)

    def bind(self, key, command, *, mode='normal', force=False):
        with self._handle_error('binding', key):
            self._keyconfig.bind(key, command, mode=mode, force=force)

    def unbind(self, key, *, mode='normal'):
        with self._handle_error('unbinding', key):
            self._keyconfig.unbind(key, mode=mode)


def read_config_py(filename=None):
    """Read a config.py file."""
    api = ConfigAPI(config.instance, config.key_instance)

    if filename is None:
        filename = os.path.join(standarddir.config(), 'config.py')
        if not os.path.exists(filename):
            return api

    container = config.ConfigContainer(config.instance, configapi=api)
    basename = os.path.basename(filename)

    module = types.ModuleType('config')
    module.config = api
    module.c = container
    module.__file__ = filename

    try:
        with open(filename, mode='rb') as f:
            source = f.read()
    except OSError as e:
        text = "Error while reading {}".format(basename)
        desc = configexc.ConfigErrorDesc(text, e)
        raise configexc.ConfigFileErrors(basename, [desc])

    try:
        code = compile(source, filename, 'exec')
    except (ValueError, TypeError) as e:
        # source contains NUL bytes
        desc = configexc.ConfigErrorDesc("Error while compiling", e)
        raise configexc.ConfigFileErrors(basename, [desc])
    except SyntaxError as e:
        desc = configexc.ConfigErrorDesc("Syntax Error", e,
                                         traceback=traceback.format_exc())
        raise configexc.ConfigFileErrors(basename, [desc])

    try:
        exec(code, module.__dict__)
    except Exception as e:
        api.errors.append(configexc.ConfigErrorDesc(
            "Unhandled exception",
            exception=e, traceback=traceback.format_exc()))

    api.finalize()
    return api


def init():
    """Initialize config storage not related to the main config."""
    global state
    state = StateConfig()
    state['general']['version'] = qutebrowser.__version__

    # Set the QSettings path to something like
    # ~/.config/qutebrowser/qsettings/qutebrowser/qutebrowser.conf so it
    # doesn't overwrite our config.
    #
    # This fixes one of the corruption issues here:
    # https://github.com/qutebrowser/qutebrowser/issues/515

    path = os.path.join(standarddir.config(auto=True), 'qsettings')
    for fmt in [QSettings.NativeFormat, QSettings.IniFormat]:
        QSettings.setPath(fmt, QSettings.UserScope, path)
